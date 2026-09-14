"""Parcours CLI de collecte planifiée, avec de vrais serveurs HTTP locaux."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.collectors.wttj import WttjCollector
from jobwatch.db import connect


def test_run_schedules_each_site_and_retries_without_losing_the_window(tmp_path, monkeypatch):
    calls = []
    failing = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            calls.append((url.path, parse_qs(url.query)))
            partial = url.path == '/sr/Quiet' and parse_qs(url.query).get('offset') == ['0']
            if url.path in failing and not partial:
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            if url.path.startswith('/sr/'):
                company = url.path.rsplit('/', 1)[1]
                body = {"content": [{
                    "id": "1", "name": "AI Engineer", "company": {"name": company},
                    "location": {"city": "Paris"},
                }]}
                if url.path in failing:
                    body['content'][0].update(id='2', name='AI Research Engineer')
                    body['totalFound'] = 2  # La deuxième page échoue, pas la première.
            elif url.path == '/ft':
                body = {"resultats": []}
            else:
                self.wfile.write(b'<ul></ul>')  # Une recherche vide reste un succès.
                return
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            data = self.rfile.read(int(self.headers['Content-Length']))
            calls.append((self.path, json.loads(data) if self.path == '/wttj' else {}))
            self.send_response(200)
            self.end_headers()
            # Une réponse JSON invalide ne doit pas repousser la collecte.
            body = {} if self.path in failing else (
                {"access_token": "test"} if self.path == '/token' else {"hits": []}
            )
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setattr('jobwatch.collectors.smartrecruiters.BASE_URL', base + '/sr/{slug}')
    monkeypatch.setattr('jobwatch.collectors.linkedin.SEARCH_URL', base + '/linkedin')
    monkeypatch.setattr('jobwatch.collectors.france_travail.TOKEN_URL', base + '/token')
    monkeypatch.setattr('jobwatch.collectors.france_travail.SEARCH_URL', base + '/ft')
    monkeypatch.setattr(WttjCollector, 'search_url', property(lambda self: base + '/wttj'))
    db = tmp_path / 'jobwatch.db'
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({
        'db': str(db), 'searches': [{'name': 'IA IDF', 'include': ['AI']}],
        'sources': {
            'smartrecruiters': {
                'companies': ['Daily', 'Quiet'], 'interval_days': 1,
                'company_intervals': {'Quiet': 4},
            },
            'linkedin': {
                'interval_days': 4, 'hours': 48,
                'queries': [{'keywords': 'AI', 'location': 'Île-de-France'}],
            },
            'wttj': {
                'interval_days': 4, 'hours': 48, 'queries': ['AI'],
                'algolia': {'app_id': 'APP', 'api_key': 'public', 'index': 'jobs'},
            },
            'france_travail': {'client_id': 'test', 'client_secret': 'test', 'keywords': 'AI'},
        },
    }))
    runner = CliRunner()
    args = ['run', '--config', str(config)]
    conn = None
    try:
        first = runner.invoke(cli, args)
        assert first.exit_code == 0, first.output
        assert '2 nouvelles offres collectées, 2 nouveaux matchs' in first.output
        conn = connect(db)
        marks = dict(conn.execute('SELECT name, last_success_at FROM source'))
        assert set(marks) == {
            'france_travail', 'smartrecruiters:daily', 'smartrecruiters:quiet', 'linkedin', 'wttj',
        }
        assert all(marks.values())
        window = next(data['f_TPR'][0] for path, data in calls if path == '/linkedin')
        assert int(window[1:]) >= 5 * 86400  # Quatre jours + un jour de chevauchement.

        calls.clear()
        second = runner.invoke(cli, args)
        assert second.exit_code == 0, second.output
        assert '5 source(s) différée(s)' in second.output
        assert calls == []

        # Un jour passe. Deux employeurs du même connecteur gardent leurs calendriers.
        conn.execute("UPDATE source SET last_success_at = datetime('now', '-25 hours')")
        conn.commit()
        daily = runner.invoke(cli, args)
        assert daily.exit_code == 0, daily.output
        assert {path for path, _ in calls} == {'/sr/Daily', '/token', '/ft'}
        assert conn.execute('SELECT count(*) FROM offer').fetchone()[0] == 2

        # Quatre jours passent. Les sources saines avancent malgré les deux pannes.
        conn.execute("UPDATE source SET last_success_at = datetime('now', '-97 hours')")
        conn.commit()
        before = dict(conn.execute('SELECT name, last_success_at FROM source'))
        failing.update({'/sr/Quiet', '/wttj'})
        calls.clear()
        failure = runner.invoke(cli, args)
        assert failure.exit_code == 1, failure.output
        assert 'collecte incomplète' in failure.output
        after = dict(conn.execute('SELECT name, last_success_at FROM source'))
        for name in ('smartrecruiters:quiet', 'wttj'):
            assert after[name] == before[name]
        for name in ('smartrecruiters:daily', 'linkedin', 'france_travail'):
            assert after[name] != before[name]
        assert conn.execute("SELECT count(*) FROM offer WHERE title = 'AI Research Engineer'").fetchone()[0] == 1

        # La panne dure. La fenêtre doit couvrir tout le retard, pas seulement quatre jours.
        conn.execute("UPDATE source SET last_success_at = datetime('now', '-169 hours') "
                     "WHERE name IN ('smartrecruiters:quiet', 'wttj')")
        conn.commit()
        failing.clear()
        calls.clear()
        retry = runner.invoke(cli, args)
        assert retry.exit_code == 0, retry.output
        assert {path for path, _ in calls} == {'/sr/Quiet', '/wttj'}
        body = next(data for path, data in calls if path == '/wttj')
        since = int(body['numericFilters'].split('>')[1])
        assert time.time() - since >= 193 * 3600
        assert conn.execute('SELECT count(*) FROM match').fetchone()[0] == 3

        # Ni un échec OAuth ni un refus LinkedIn ne font avancer leur dernier succès.
        conn.execute("UPDATE source SET last_success_at = datetime('now', '-97 hours') "
                     "WHERE name IN ('france_travail', 'linkedin')")
        conn.commit()
        before = dict(conn.execute('SELECT name, last_success_at FROM source'))
        failing.update({'/token', '/linkedin'})
        calls.clear()
        failure = runner.invoke(cli, args)
        assert failure.exit_code == 1, failure.output
        assert {path for path, _ in calls} == {'/token', '/linkedin'}
        assert dict(conn.execute('SELECT name, last_success_at FROM source')) == before
        failing.clear()
        calls.clear()
        assert runner.invoke(cli, args).exit_code == 0
        assert {path for path, _ in calls} == {'/token', '/ft', '/linkedin'}
    finally:
        if conn is not None:
            conn.close()
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize('value', [0, -1, 2, True, 1.5, '4', None])
@pytest.mark.parametrize('source', ['linkedin', 'wttj', 'france_travail', 'smartrecruiters'])
def test_run_rejects_invalid_intervals_before_network(tmp_path, value, source):
    configs = {
        'linkedin': {'queries': [{'keywords': 'AI', 'location': 'Paris'}]},
        'wttj': {'queries': ['AI'], 'algolia': {'app_id': 'APP', 'api_key': 'key', 'index': 'jobs'}},
        'france_travail': {'client_id': 'test', 'client_secret': 'test', 'keywords': 'AI'},
        'smartrecruiters': {'companies': ['Acme']},
    }
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({
        'db': str(tmp_path / 'jobwatch.db'),
        'searches': [{'name': 'AI', 'include': ['AI']}],
        'sources': {source: {**configs[source], 'interval_days': value}},
    }))
    result = CliRunner().invoke(cli, ['run', '--config', str(config)])
    assert result.exit_code == 1, result.output
    assert f'sources.{source}.interval_days' in result.output
    assert not (tmp_path / 'jobwatch.db').exists()
