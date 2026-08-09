from __future__ import annotations

import pytest

from jobwatch.db import connect, init_db
from jobwatch.onboarding import OnboardingError, complete_profile, sync_profile_searches
from jobwatch.onboarding_ui import render_onboarding
from jobwatch.serve import render_page


def _profile_db(tmp_path):
    conn = connect(":memory:")
    init_db(conn)
    workspace_id = conn.execute(
        "INSERT INTO workspace (slug, name) VALUES ('ami', 'ami')"
    ).lastrowid
    account_id = conn.execute(
        "INSERT INTO account (email) VALUES ('ami@example.com')"
    ).lastrowid
    conn.execute(
        "INSERT INTO membership (account_id, workspace_id, role) VALUES (?, ?, 'owner')",
        (account_id, workspace_id),
    )
    cv_path = tmp_path / "cv.pdf"
    cv_path.write_bytes(b"%PDF-1.4")
    cv_id = conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES ('cv', 'CV', ?)",
        (str(cv_path),),
    ).lastrowid
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 'test')")
    source_id = conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"]
    company_id = conn.execute(
        "INSERT INTO company (name) VALUES ('Acme')"
    ).lastrowid
    conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url, collected_at) "
        "VALUES (?, ?, 'AI Engineer', 'https://example.com/ai', datetime('now'))",
        (source_id, company_id),
    )
    old_search_id = conn.execute(
        "INSERT INTO search (name, include_json, exclude_json, locations_json, active) "
        "VALUES ('historique', '[\"PO\"]', '[]', '[]', 1)"
    ).lastrowid
    offer_id = conn.execute("SELECT id FROM offer WHERE url = 'https://example.com/ai'").fetchone()["id"]
    conn.execute(
        "INSERT INTO match (search_id, offer_id, state) VALUES (?, ?, 'new')",
        (old_search_id, offer_id),
    )
    conn.commit()
    return conn, int(account_id), int(workspace_id), int(cv_id)


def test_complete_profile_persists_active_searches_and_matches(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"label": "Ingénierie IA", "keywords": ["AI Engineer"], "exclude": []}],
    )

    searches = {
        row["name"]: row["active"]
        for row in conn.execute("SELECT name, active FROM search ORDER BY id")
    }
    assert searches == {"historique": 0, "Ingénierie IA": 1}
    assert conn.execute(
        "SELECT count(*) FROM match m JOIN search s ON s.id = m.search_id "
        "WHERE s.name = 'Ingénierie IA'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT document_library_id FROM candidate_profile_document WHERE account_id = ?",
        (account_id,),
    ).fetchone()[0] == cv_id
    conn.close()


def test_profile_search_sync_restores_category_after_config_sync(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"label": "Data", "keywords": ["AI Engineer"], "exclude": []}],
    )
    conn.execute("UPDATE search SET active = 1 WHERE name = 'historique'")
    conn.commit()

    assert sync_profile_searches(conn) is True
    assert conn.execute("SELECT active FROM search WHERE name = 'historique'").fetchone()[0] == 0
    assert conn.execute("SELECT active FROM search WHERE name = 'Data'").fetchone()[0] == 1
    conn.close()


def test_unified_page_uses_category_and_hides_historical_track_tabs(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"label": "Ingénierie IA", "keywords": ["AI Engineer"], "exclude": []}],
    )
    page = render_page(conn, "all")
    assert "via Ingénierie IA" in page
    assert "Ingénieur IA" not in page
    assert "Chef de projet / PO" not in page
    assert 'class="track-tabs"' not in page
    conn.close()


def test_duplicate_category_names_are_rejected(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    with pytest.raises(OnboardingError, match="noms différents"):
        complete_profile(
            conn,
            account_id,
            workspace_id,
            [cv_id],
            [
                {"label": "Data", "keywords": ["AI"], "exclude": []},
                {"label": "data", "keywords": ["ML"], "exclude": []},
            ],
        )
    conn.close()


def test_onboarding_can_return_to_mode_choice() -> None:
    page = render_onboarding("csrf")
    assert 'id="back-to-choice-upload"' in page
    assert 'id="back-to-choice-intents"' in page
    edit_page = render_onboarding(
        "csrf", initial_intents=[{"label": "Data", "keywords": ["ML"], "exclude": []}]
    )
    assert 'id="back-to-choice-upload"' in edit_page and 'type="button" hidden' in edit_page
