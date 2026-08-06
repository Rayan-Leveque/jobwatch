"""Tableau de bord web local en lecture seule, servi par `jw serve`.

La page est regénérée à chaque requête GET / depuis l'état actuel de la base.
Le rendu est pur (`render_page`) et le serveur HTTP n'utilise que la
bibliothèque standard.
"""

from __future__ import annotations

import html
import re
import sqlite3
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import click

from jobwatch.db import connect

STATUS_LABELS = {
    "applied": "Candidature envoyée",
    "follow_up": "Relance",
    "interview": "Entretien",
    "rejected": "Refus",
    "offer": "Offre reçue",
}
STATUS_UNKNOWN = "Statut inconnu"
CONTRACT_LABELS = {
    "permanent": "CDI",
    "fixed_term": "CDD",
    "internship": "Stage",
    "other": "Autre",
}

_MONTHS = {
    1: "janv.", 2: "févr.", 3: "mars", 4: "avr.", 5: "mai", 6: "juin",
    7: "juil.", 8: "août", 9: "sept.", 10: "oct.", 11: "nov.", 12: "déc.",
}
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


class ServeError(Exception):
    """Échec attendu de l'amorçage du serveur. La CLI affiche un message clair et sort."""


def _short_date(value: str) -> str:
    """2026-08-05 12:00 -> « 5 août » ; garde tel quel si non ISO."""
    m = _DATE_RE.match(value)
    if m:
        return f"{int(m.group(3))} {_MONTHS.get(int(m.group(2)), m.group(2))}"
    return value


def _matches(conn: sqlite3.Connection, state: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, m.state AS state, s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = ? AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        "ORDER BY o.collected_at DESC, m.id DESC",
        (state,),
    ).fetchall()


def _applications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.id AS id, c.name AS company, o.title AS title, "
        "       o.location AS location, o.contract AS contract, "
        "       o.platform AS platform, o.url AS url, a.note AS note, "
        "       s.name AS search_name, "
        "       a.created_at AS created_at, "
        "       (SELECT e.type FROM event e WHERE e.application_id = a.id "
        "        ORDER BY e.at DESC, e.id DESC LIMIT 1) AS status, "
        "       (SELECT e.at FROM event e WHERE e.application_id = a.id "
        "        ORDER BY e.at DESC, e.id DESC LIMIT 1) AS status_at "
        "FROM application a "
        "JOIN offer o ON o.id = a.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "LEFT JOIN match m ON m.id = a.match_id "
        "LEFT JOIN search s ON s.id = m.search_id "
        "ORDER BY a.created_at DESC, a.id DESC"
    ).fetchall()


def _link(url: object) -> str:
    """Renvoie le lien de l'offre si son schéma est http/https, sinon rien."""
    value = str(url or "")
    if not value:
        return ""
    try:
        scheme = urlsplit(value).scheme.lower()
    except ValueError:
        return ""
    if scheme not in ("http", "https"):
        return ""
    escaped = html.escape(value)
    return f'<a href="{escaped}" target="_blank" rel="noopener noreferrer">offre ↗</a>'


def _meta(row: sqlite3.Row, date_label: str, date: object, search_name: object = None) -> str:
    """Ligne de métadonnées : plateforme, lieu, contrat, recherche, date, lien."""
    parts: list[str] = []
    if row["platform"]:
        parts.append(f'<span class="platform">{html.escape(str(row["platform"]))}</span>')
    text: list[str] = []
    if row["location"]:
        text.append(str(row["location"]))
    if row["contract"]:
        contract = str(row["contract"])
        text.append(CONTRACT_LABELS.get(contract, contract))
    if search_name:
        text.append(f"via {search_name}")
    if date:
        text.append(f"{date_label} {_short_date(str(date))}")
    if text:
        parts.append(html.escape(" · ".join(text)))
    link = _link(row["url"])
    if link:
        parts.append(link)
    return " · ".join(parts)


def _match_card(row: sqlite3.Row) -> str:
    cls = "new" if row["state"] == "new" else "seen"
    company = html.escape(str(row["company"] or "Société inconnue"))
    title = html.escape(str(row["title"] or ""))
    meta = _meta(row, "collecté le", row["collected_at"], row["search_name"])
    return (
        f'<article class="row row-{cls}"><div class="body">'
        f'<div class="card-topline"><div class="company">{company}</div></div>'
        f'<div class="role">{title}</div>'
        f'<div class="meta">{meta}</div></div></article>'
    )


def _application_card(row: sqlite3.Row) -> str:
    status = str(row["status"] or "")
    cls = status if status in STATUS_LABELS else "unknown"
    label = STATUS_LABELS.get(status, STATUS_UNKNOWN)
    company = html.escape(str(row["company"] or "Société inconnue"))
    title = html.escape(str(row["title"] or ""))
    pill = f'<span class="pill {cls}">{html.escape(label)}</span>'
    meta = _meta(row, "candidature le", row["created_at"], row["search_name"])
    note = f'<p class="note">{html.escape(str(row["note"]))}</p>' if row["note"] else ""
    return (
        f'<article class="row row-applied"><div class="body">'
        f'<div class="card-topline"><div class="company">{company}</div>{pill}</div>'
        f'<div class="role">{title}</div>'
        f'<div class="meta">{meta}</div>{note}</div></article>'
    )


def _card(row: sqlite3.Row, key: str) -> str:
    if key == "applied":
        return _application_card(row)
    return _match_card(row)


def _section(key: str, label: str, subtitle: str, rows, empty_text: str, open_default: bool) -> str:
    if rows:
        cards = "\n".join(_card(row, key) for row in rows)
    else:
        cards = f'<p class="empty-note">{empty_text}</p>'
    open_attr = " open" if open_default else ""
    default = "1" if open_default else "0"
    return (
        f'<details class="section section-{key}"{open_attr} data-section="{key}" '
        f'data-default="{default}">'
        f'<summary><span class="summary-copy"><span class="section-dot"></span>'
        f'<span><span class="section-title">{html.escape(label)}</span>'
        f'<span class="section-subtitle">{html.escape(subtitle)}</span></span></span>'
        f'<span class="summary-tail"><span class="count">{len(rows)}</span>'
        f'<span class="chevron" aria-hidden="true"></span></span></summary>'
        f'<div class="card-list">{cards}</div></details>'
    )


def render_page(conn: sqlite3.Connection) -> str:
    """Rend la page HTML complète depuis l'état actuel de la base."""
    new = _matches(conn, "new")
    seen = _matches(conn, "seen")
    applied = _applications(conn)
    body = "\n".join(
        (
            _section(
                "new", "Nouveaux matchs", "À découvrir", new,
                "Aucun nouveau match pour l'instant.", True,
            ),
            _section(
                "seen", "Vus", "Déjà parcourus", seen,
                "Aucun match parcouru pour l'instant.", False,
            ),
            _section(
                "applied", "Candidatures", "Dernier statut connu", applied,
                "Aucune candidature pour l'instant.", False,
            ),
        )
    )
    total = len(new) + len(seen) + len(applied)
    stamp = datetime.now(UTC).astimezone().strftime("%d/%m/%Y %H:%M")
    return _page_template(
        body=body, total=total,
        new_count=len(new), seen_count=len(seen), applied_count=len(applied),
        stamp=stamp,
    )


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    """Fabrique une classe de gestionnaire HTTP branchée sur render_page.

    Chaque requête ouvre sa propre connexion : ThreadingHTTPServer sert chaque
    requête dans un thread dédié, et la page relit ainsi l'état le plus récent.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "jobwatch"

        def do_GET(self) -> None:
            if urlsplit(self.path).path != "/":
                self._send_text(404, "404 Not Found\n")
                return
            try:
                conn = connect(db_path)
                try:
                    page = render_page(conn)
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if status == 200:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, status: int, text: str) -> None:
            self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def url_of(host: str, port: int) -> str:
    if ":" in host:
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def serve_http(db_path: Path, host: str, port: int) -> None:
    """Crée le serveur HTTP et le sert jusqu'à Ctrl-C."""
    try:
        server = ThreadingHTTPServer((host, port), make_handler(db_path))
    except (OSError, OverflowError) as exc:
        raise ServeError(f"impossible d'écouter sur {host}:{port} : {exc}") from exc
    bound_port = int(server.server_address[1])
    click.echo(f"tableau de bord jobwatch : {url_of(host, bound_port)} (Ctrl-C pour arrêter)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("arrêt du serveur")
    finally:
        server.server_close()


_CSS = """\
:root {
  color-scheme:dark;
  --bg:#090b10; --bg-deep:#06070a; --surface:#11141c; --surface-2:#171b25;
  --surface-hover:#1c2130; --fg:#f3f5f8; --muted:#9198a8; --muted-2:#6e7585;
  --line:rgba(255,255,255,.085); --line-strong:rgba(255,255,255,.16);
  --accent:#b9f46f; --accent-ink:#17210d; --accent-soft:rgba(185,244,111,.12);
  --violet:#ab91ff; --violet-soft:rgba(171,145,255,.12);
  --amber:#ffbe63; --amber-soft:rgba(255,190,99,.12);
  --blue:#72b7ff; --blue-soft:rgba(114,183,255,.13);
  --danger:#ff879b; --danger-soft:rgba(255,135,155,.12);
  --shadow:0 22px 60px rgba(0,0,0,.30);
  --card-shadow:0 8px 28px rgba(0,0,0,.16);
  --radius-xl:24px; --radius-lg:18px; --radius-md:14px;
}
html[data-theme="light"] {
  color-scheme:light;
  --bg:#f3f1eb; --bg-deep:#ebe7de; --surface:#fffefa; --surface-2:#f8f6ef;
  --surface-hover:#f4f1e8; --fg:#191b1f; --muted:#686d76; --muted-2:#858990;
  --line:rgba(29,31,35,.095); --line-strong:rgba(29,31,35,.17);
  --accent:#42752d; --accent-ink:#fff; --accent-soft:rgba(66,117,45,.10);
  --violet:#7052c8; --violet-soft:rgba(112,82,200,.09);
  --amber:#9a5d08; --amber-soft:rgba(154,93,8,.09);
  --blue:#1269ad; --blue-soft:rgba(18,105,173,.09);
  --danger:#b63c54; --danger-soft:rgba(182,60,84,.10);
  --shadow:0 22px 55px rgba(52,46,34,.12);
  --card-shadow:0 7px 24px rgba(52,46,34,.07);
}
* { box-sizing:border-box }
html { min-height:100%; background:var(--bg); scroll-behavior:smooth }
body { min-height:100vh; margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
  -webkit-font-smoothing:antialiased; -webkit-tap-highlight-color:transparent }
button, input { font:inherit }
button, summary, a { -webkit-tap-highlight-color:transparent }
.ambient { position:fixed; inset:0; overflow:hidden; pointer-events:none; z-index:0 }
.ambient::before { content:""; position:absolute; width:480px; height:480px;
  top:-250px; left:50%; transform:translateX(-50%); border-radius:50%;
  background:radial-gradient(circle, rgba(171,145,255,.22), transparent 67%);
  filter:blur(12px) }
.ambient::after { content:""; position:absolute; width:320px; height:320px;
  top:260px; right:-240px; border-radius:50%;
  background:radial-gradient(circle, rgba(185,244,111,.11), transparent 70%) }
html[data-theme="light"] .ambient::before { background:radial-gradient(circle, rgba(112,82,200,.13), transparent 67%) }
html[data-theme="light"] .ambient::after { background:radial-gradient(circle, rgba(66,117,45,.10), transparent 70%) }
.shell { position:relative; z-index:1; width:min(100%, 760px); margin:0 auto;
  padding:max(18px, env(safe-area-inset-top)) calc(16px + env(safe-area-inset-right))
  calc(40px + env(safe-area-inset-bottom)) calc(16px + env(safe-area-inset-left)) }
.topbar { min-height:48px; display:flex; align-items:center; justify-content:space-between;
  gap:16px; margin-bottom:30px }
.identity { display:flex; align-items:center; gap:11px; min-width:0 }
.monogram { width:38px; height:38px; display:grid; place-items:center; border-radius:12px;
  color:var(--accent-ink); background:var(--accent); font-size:.9rem; font-weight:850;
  letter-spacing:-.04em; box-shadow:0 0 0 5px var(--accent-soft) }
.identity-copy { display:flex; flex-direction:column; line-height:1.18 }
.identity-name { font-weight:750; letter-spacing:-.015em }
.identity-sub { margin-top:3px; color:var(--muted); font-size:.73rem; letter-spacing:.02em }
.theme-toggle { position:relative; width:48px; height:48px; flex:0 0 48px; border:1px solid var(--line);
  border-radius:15px; color:var(--fg); background:color-mix(in srgb, var(--surface) 86%, transparent);
  box-shadow:var(--card-shadow); cursor:pointer; transition:transform .2s ease, background .2s ease,
  border-color .2s ease }
.theme-toggle:active { transform:scale(.94) }
.theme-toggle svg { position:absolute; inset:0; margin:auto; width:20px; height:20px;
  transition:opacity .2s ease, transform .35s cubic-bezier(.2,.8,.2,1) }
.icon-sun { opacity:0; transform:rotate(-70deg) scale(.6) }
.icon-moon { opacity:1; transform:rotate(0) scale(1) }
html[data-theme="light"] .icon-sun { opacity:1; transform:rotate(0) scale(1) }
html[data-theme="light"] .icon-moon { opacity:0; transform:rotate(70deg) scale(.6) }
.theme-toggle:focus-visible, #q:focus-visible, .clear-search:focus-visible,
summary:focus-visible, a:focus-visible { outline:3px solid var(--violet); outline-offset:3px }
.hero { margin-bottom:22px }
.eyebrow { margin:0 0 10px; color:var(--accent); font-size:.69rem; font-weight:800;
  letter-spacing:.16em; text-transform:uppercase }
h1 { max-width:560px; margin:0; font-size:clamp(2.25rem, 10vw, 4.6rem); line-height:.98;
  font-weight:810; letter-spacing:-.065em }
h1 span { color:var(--muted-2); font-weight:620 }
.hero-meta { display:flex; align-items:center; flex-wrap:wrap; gap:8px; margin:18px 0 0;
  color:var(--muted); font-size:.78rem }
.live-dot { width:7px; height:7px; border-radius:50%; background:var(--accent);
  box-shadow:0 0 0 5px var(--accent-soft) }
.stats { display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin:24px 0 22px }
.stat { min-width:0; padding:14px 13px 13px; border:1px solid var(--line); border-radius:var(--radius-md);
  background:linear-gradient(145deg, var(--surface-2), var(--surface)); box-shadow:var(--card-shadow) }
.stat-value { display:block; font-size:1.55rem; line-height:1; font-weight:790; letter-spacing:-.05em }
.stat-label { display:block; margin-top:8px; color:var(--muted); font-size:.67rem; line-height:1.25;
  letter-spacing:.045em; text-transform:uppercase }
.stat-new .stat-value { color:var(--accent) }
.stat-seen .stat-value { color:var(--blue) }
.stat-applied .stat-value { color:var(--violet) }
.search-dock { position:sticky; top:calc(env(safe-area-inset-top) + 8px); z-index:20;
  margin:0 -6px 26px; padding:6px; border:1px solid transparent; border-radius:20px;
  transition:background .2s ease, border-color .2s ease, box-shadow .2s ease }
.search-dock.stuck { border-color:var(--line); background:color-mix(in srgb, var(--bg) 84%, transparent);
  box-shadow:var(--shadow); -webkit-backdrop-filter:blur(18px); backdrop-filter:blur(18px) }
.search-box { position:relative; display:flex; align-items:center; min-height:54px;
  border:1px solid var(--line-strong); border-radius:16px; background:var(--surface);
  box-shadow:var(--card-shadow); overflow:hidden; transition:border-color .2s ease, box-shadow .2s ease }
.search-box:focus-within { border-color:color-mix(in srgb, var(--violet) 70%, transparent);
  box-shadow:0 0 0 4px var(--violet-soft), var(--card-shadow) }
.search-icon { width:21px; height:21px; flex:none; margin-left:16px; color:var(--muted) }
#q { width:100%; height:52px; min-width:0; padding:0 8px 0 11px; border:0; outline:0;
  color:var(--fg); background:transparent; font-size:16px; -webkit-appearance:none; appearance:none }
#q::placeholder { color:var(--muted-2); opacity:1 }
#q::-webkit-search-cancel-button { display:none }
.clear-search { display:none; width:44px; height:44px; flex:0 0 44px; margin-right:4px;
  border:0; border-radius:12px; color:var(--muted); background:transparent; cursor:pointer }
.clear-search.visible { display:grid; place-items:center }
.clear-search svg { width:18px; height:18px }
.search-status { min-height:18px; margin:7px 11px 0; color:var(--muted); font-size:.72rem }
.section { margin:0 0 14px; border:1px solid var(--line); border-radius:var(--radius-xl);
  background:color-mix(in srgb, var(--surface) 82%, transparent); box-shadow:var(--card-shadow);
  overflow:hidden }
.section > summary { min-height:72px; display:flex; align-items:center; justify-content:space-between;
  gap:14px; padding:12px 16px; list-style:none; cursor:pointer; user-select:none }
.section > summary::-webkit-details-marker { display:none }
.summary-copy { min-width:0; display:flex; align-items:center; gap:12px }
.section-dot { width:10px; height:10px; flex:0 0 10px; border-radius:50%;
  background:var(--muted-2); box-shadow:0 0 0 5px rgba(145,152,168,.09) }
.section-new .section-dot { background:var(--accent); box-shadow:0 0 0 5px var(--accent-soft) }
.section-seen .section-dot { background:var(--blue); box-shadow:0 0 0 5px var(--blue-soft) }
.section-applied .section-dot { background:var(--violet); box-shadow:0 0 0 5px var(--violet-soft) }
.section-title, .section-subtitle { display:block }
.section-title { overflow:hidden; color:var(--fg); font-size:.94rem; font-weight:740;
  letter-spacing:-.015em; text-overflow:ellipsis; white-space:nowrap }
.section-subtitle { margin-top:2px; color:var(--muted); font-size:.7rem }
.summary-tail { display:flex; align-items:center; gap:11px; flex:none }
.count { min-width:30px; height:30px; padding:0 8px; display:inline-grid; place-items:center;
  border:1px solid var(--line); border-radius:10px; color:var(--muted); background:var(--surface-2);
  font-size:.71rem; font-weight:750; font-variant-numeric:tabular-nums }
.chevron { width:9px; height:9px; border-right:2px solid var(--muted); border-bottom:2px solid var(--muted);
  transform:rotate(45deg) translate(-2px,-2px); transition:transform .25s ease }
.section:not([open]) .chevron { transform:rotate(-45deg) translate(-1px,-1px) }
.card-list { display:grid; gap:8px; padding:0 8px 8px }
.row { position:relative; min-height:88px; padding:15px 14px 14px 17px; border:1px solid var(--line);
  border-radius:var(--radius-lg); background:var(--surface-2); overflow:hidden;
  box-shadow:0 1px 0 rgba(255,255,255,.025) inset; transition:transform .18s ease,
  border-color .18s ease, background .18s ease }
.row::before { content:""; position:absolute; inset:13px auto 13px 0; width:3px; border-radius:0 4px 4px 0;
  background:var(--muted-2) }
.row.row-new::before { background:var(--accent) }
.row.row-seen::before { background:var(--blue) }
.row.row-applied::before { background:var(--violet) }
.row .body { min-width:0 }
.card-topline { display:flex; align-items:flex-start; justify-content:space-between; gap:10px }
.company { min-width:0; overflow-wrap:anywhere; color:var(--fg); font-size:.74rem; line-height:1.35;
  font-weight:810; letter-spacing:.065em; text-transform:uppercase }
.role { max-width:620px; margin-top:5px; overflow-wrap:anywhere; color:var(--fg); font-size:.98rem;
  line-height:1.34; font-weight:590; letter-spacing:-.018em }
.pill { flex:none; min-height:22px; display:inline-flex; align-items:center; padding:2px 8px;
  border:1px solid var(--line); border-radius:999px; font-size:.6rem; line-height:1;
  font-weight:800; letter-spacing:.07em; text-transform:uppercase }
.pill.applied { color:var(--blue); border-color:color-mix(in srgb, var(--blue) 38%, transparent);
  background:var(--blue-soft) }
.pill.follow_up { color:var(--amber); border-color:color-mix(in srgb, var(--amber) 38%, transparent);
  background:var(--amber-soft) }
.pill.interview { color:var(--violet); border-color:color-mix(in srgb, var(--violet) 35%, transparent);
  background:var(--violet-soft) }
.pill.offer { color:var(--accent); border-color:color-mix(in srgb, var(--accent) 38%, transparent);
  background:var(--accent-soft) }
.pill.rejected { color:var(--danger); border-color:color-mix(in srgb, var(--danger) 38%, transparent);
  background:var(--danger-soft) }
.pill.unknown { color:var(--muted); background:var(--surface) }
.meta { display:flex; align-items:center; flex-wrap:wrap; gap:2px 6px; margin-top:11px;
  color:var(--muted); font-size:.72rem; line-height:1.4; overflow-wrap:anywhere }
.platform { min-height:24px; display:inline-flex; align-items:center; padding:2px 8px; border-radius:999px;
  color:var(--blue); background:var(--blue-soft); font-size:.64rem; font-weight:760; letter-spacing:.015em }
.meta a { min-height:44px; display:inline-flex; align-items:center; padding:0 10px; border:1px solid var(--line);
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:.69rem; font-weight:680;
  text-decoration:none; transition:border-color .15s ease, background .15s ease }
.note { margin:12px 0 0; padding:11px 12px; border:1px dashed var(--line-strong);
  border-radius:12px; color:var(--muted); background:color-mix(in srgb, var(--surface) 55%, transparent);
  font-size:.75rem; overflow-wrap:anywhere }
.empty-note { margin:0; padding:14px 13px; border:1px dashed var(--line-strong);
  border-radius:13px; color:var(--muted); background:var(--surface-2);
  font-size:.75rem; overflow-wrap:anywhere }
.no-results { margin:0 0 14px; padding:26px 18px; border:1px dashed var(--line-strong);
  border-radius:var(--radius-lg); color:var(--muted); text-align:center }
.no-results strong { display:block; margin-bottom:3px; color:var(--fg) }
.footer { margin-top:24px; color:var(--muted-2); font-size:.68rem; text-align:center }
a { color:var(--blue) }
@media (hover:hover) {
  .theme-toggle:hover, .clear-search:hover { background:var(--surface-hover) }
  .row:hover { transform:translateY(-1px); border-color:var(--line-strong); background:var(--surface-hover) }
  .meta a:hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
@media (min-width:620px) {
  .shell { padding-left:24px; padding-right:24px }
  .topbar { margin-bottom:42px }
  .stats { gap:12px }
  .stat { padding:18px }
  .section > summary { padding-left:20px; padding-right:20px }
  .card-list { padding:0 10px 10px; gap:10px }
  .row { padding:18px 18px 17px 21px }
}
@media (max-width:370px) {
  .stat { padding:12px 9px }
  .stat-value { font-size:1.35rem }
  .stat-label { font-size:.59rem }
  .section-subtitle { display:none }
}
@media (prefers-reduced-motion:reduce) {
  *, *::before, *::after { scroll-behavior:auto !important; animation:none !important; transition:none !important }
}
"""

_JS = """\
(function () {
  const root = document.documentElement;
  const themeToggle = document.getElementById('theme-toggle');
  const themeColor = document.getElementById('theme-color');
  const syncThemeUI = () => {
    const isLight = root.dataset.theme === 'light';
    themeToggle.setAttribute('aria-label', isLight ? 'Passer au thème sombre' : 'Passer au thème clair');
    themeToggle.setAttribute('aria-pressed', isLight ? 'true' : 'false');
    themeColor.setAttribute('content', isLight ? '#f3f1eb' : '#090b10');
  };
  syncThemeUI();
  themeToggle.addEventListener('click', () => {
    root.dataset.theme = root.dataset.theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('jw-theme', root.dataset.theme); } catch (_) {}
    syncThemeUI();
  });

  const q = document.getElementById('q');
  const clearSearch = document.getElementById('clear-search');
  const searchStatus = document.getElementById('search-status');
  const noResults = document.getElementById('no-results');
  const searchDock = document.getElementById('search-dock');
  const details = [...document.querySelectorAll('.section')];
  const rows = [...document.querySelectorAll('.row')];
  const readSession = (key, fallback) => {
    try { return sessionStorage.getItem(key) ?? fallback; } catch (_) { return fallback; }
  };
  const writeSession = (key, value) => {
    try { sessionStorage.setItem(key, value); } catch (_) {}
  };
  const normalize = value => value.toLocaleLowerCase('fr').normalize('NFD')
    .replace(/[\\u0300-\\u036f]/g, '');
  let saved = {};
  try { saved = JSON.parse(readSession('jw-open', '{}')) || {}; } catch (_) {}
  q.value = readSession('jw-q', '');
  details.forEach((d, i) => {
    const key = d.dataset.section;
    const stored = saved[key] !== undefined ? saved[key] : saved[i];
    d.dataset.open = stored !== undefined ? String(stored)
      : d.dataset.default === '1' ? '1' : '0';
    d.open = d.dataset.open === '1';
    d.addEventListener('toggle', () => {
      if (q.value.trim()) return;
      d.dataset.open = d.open ? '1' : '0';
      saved[key] = d.dataset.open;
      writeSession('jw-open', JSON.stringify(saved));
    });
  });
  const apply = () => {
    const rawNeedle = q.value.trim();
    const needle = normalize(rawNeedle);
    let shownTotal = 0;
    rows.forEach(r => {
      const visible = !needle || normalize(r.textContent).includes(needle);
      r.hidden = !visible;
      if (visible) shownTotal += 1;
    });
    details.forEach(d => {
      const sectionRows = [...d.querySelectorAll('.row')];
      const shown = sectionRows.filter(r => !r.hidden).length;
      d.open = needle ? shown > 0 : d.dataset.open === '1';
      const c = d.querySelector('.count');
      if (c) c.textContent = needle ? `${shown}/${sectionRows.length}` : `${sectionRows.length}`;
    });
    clearSearch.classList.toggle('visible', Boolean(rawNeedle));
    noResults.hidden = !needle || shownTotal > 0;
    searchStatus.textContent = needle
      ? `${shownTotal} sur ${rows.length} offre${shownTotal === 1 ? '' : 's'}`
      : `${rows.length} offres`;
  };
  q.addEventListener('input', () => {
    writeSession('jw-q', q.value);
    apply();
  });
  q.addEventListener('keydown', e => {
    if (e.key === 'Escape' && q.value) {
      e.preventDefault(); q.value = ''; writeSession('jw-q', ''); apply();
    }
  });
  clearSearch.addEventListener('click', () => {
    q.value = ''; writeSession('jw-q', ''); apply(); q.focus();
  });
  const observeDock = () => searchDock.classList.toggle('stuck', searchDock.getBoundingClientRect().top
    <= parseFloat(getComputedStyle(searchDock).top) + 1);
  document.addEventListener('scroll', observeDock, {passive:true});
  observeDock();
  apply();
})();
"""


def _page_template(*, body, total, new_count, seen_count, applied_count, stamp) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#090b10" id="theme-color">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>jobwatch · tableau de bord</title>
<script>
(function () {{
  try {{
    const saved = localStorage.getItem('jw-theme');
    document.documentElement.dataset.theme = saved === 'light' ? 'light' : 'dark';
  }} catch (_) {{
    document.documentElement.dataset.theme = 'dark';
  }}
}})();
</script>
<style>
{_CSS}</style></head><body>
<div class="ambient" aria-hidden="true"></div>
<div class="shell">
  <header>
    <div class="topbar">
      <div class="identity">
        <div class="monogram" aria-hidden="true">JW</div>
        <div class="identity-copy"><span class="identity-name">jobwatch</span>
          <span class="identity-sub">Suivi de vos offres</span></div>
      </div>
      <button class="theme-toggle" id="theme-toggle" type="button" aria-label="Passer au thème clair">
        <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <circle cx="12" cy="12" r="3.7"/><path d="M12 2v2.1M12 19.9V22M4.93 4.93l1.49 1.49M17.58 17.58l1.49 1.49M2 12h2.1M19.9 12H22M4.93 19.07l1.49-1.49M17.58 6.42l1.49-1.49"/>
        </svg>
        <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <path d="M20.2 15.1A8.4 8.4 0 0 1 8.9 3.8 8.5 8.5 0 1 0 20.2 15.1Z"/>
        </svg>
      </button>
    </div>
    <div class="hero">
      <p class="eyebrow">Tableau de bord</p>
      <h1>Vos offres,<br><span>sous contrôle.</span></h1>
      <p class="hero-meta"><span class="live-dot" aria-hidden="true"></span>
        Mis à jour le {stamp}</p>
    </div>
    <div class="stats" aria-label="Vue d'ensemble">
      <div class="stat stat-new"><span class="stat-value">{new_count}</span><span class="stat-label">Nouveaux matchs</span></div>
      <div class="stat stat-seen"><span class="stat-value">{seen_count}</span><span class="stat-label">Vus</span></div>
      <div class="stat stat-applied"><span class="stat-value">{applied_count}</span><span class="stat-label">Candidatures</span></div>
    </div>
  </header>
  <div class="search-dock" id="search-dock">
    <div class="search-box">
      <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
        <circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>
      </svg>
      <input id="q" type="search" aria-label="Filtrer les offres"
        placeholder="Entreprise, poste, lieu, recherche…" autocomplete="off" enterkeyhint="search">
      <button class="clear-search" id="clear-search" type="button" aria-label="Effacer la recherche">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <circle cx="12" cy="12" r="8"/><path d="m9 9 6 6M15 9l-6 6"/>
        </svg>
      </button>
    </div>
    <div class="search-status" id="search-status" aria-live="polite">{total} offres</div>
  </div>
  <main>
{body}
    <div class="no-results" id="no-results" hidden><strong>Aucune offre trouvée</strong>
      Essayez un autre mot-clé.</div>
  </main>
  <footer class="footer">Lecture seule · données locales · base SQLite jobwatch</footer>
</div>
<script>
{_JS}</script></body></html>
"""
