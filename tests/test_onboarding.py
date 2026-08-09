from __future__ import annotations

import json
import sqlite3

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
    assert searches == {"historique": 1, "Ingénierie IA": 1}
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
    conn.execute("UPDATE search SET active = 0 WHERE name = 'Data'")
    conn.commit()

    assert sync_profile_searches(conn) is True
    assert conn.execute("SELECT active FROM search WHERE name = 'Data'").fetchone()[0] == 1
    assert conn.execute("SELECT active FROM search WHERE name = 'historique'").fetchone()[0] == 1
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


def test_renaming_a_category_keeps_existing_triage(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"label": "Data", "keywords": ["AI Engineer"], "exclude": []}],
    )
    match_id, search_id = conn.execute(
        "SELECT m.id, m.search_id FROM match m JOIN search s ON s.id = m.search_id "
        "WHERE s.name = 'Data'"
    ).fetchone()
    conn.execute("UPDATE match SET state = 'later' WHERE id = ?", (match_id,))
    conn.commit()
    intent_id = conn.execute(
        "SELECT id FROM career_intent WHERE account_id = ?", (account_id,)
    ).fetchone()[0]

    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"id": intent_id, "label": "Data Science", "keywords": ["AI Engineer"], "exclude": []}],
    )

    rows = conn.execute(
        "SELECT m.id, m.state, m.search_id FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.active = 1 WHERE m.search_id = ?",
        (search_id,),
    ).fetchall()
    assert [(row["id"], row["state"], row["search_id"]) for row in rows] == [
        (match_id, "later", search_id)
    ]
    assert conn.execute("SELECT name FROM search WHERE id = ?", (search_id,)).fetchone()[0] == (
        "Data Science"
    )
    conn.close()


def test_more_than_four_categories_are_rejected(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    with pytest.raises(OnboardingError, match="4 catégories maximum"):
        complete_profile(
            conn,
            account_id,
            workspace_id,
            [cv_id],
            [
                {"label": f"Piste {index}", "keywords": ["AI"], "exclude": []}
                for index in range(5)
            ],
        )
    assert conn.execute("SELECT count(*) FROM career_intent").fetchone()[0] == 0
    conn.close()


def test_removed_category_search_is_deactivated_but_others_are_kept(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [
            {"label": "Data", "keywords": ["AI Engineer"], "exclude": []},
            {"label": "Ops", "keywords": ["SRE"], "exclude": []},
        ],
    )
    kept_id = conn.execute(
        "SELECT id FROM career_intent WHERE label = 'Data'"
    ).fetchone()[0]

    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"id": kept_id, "label": "Data", "keywords": ["AI Engineer"], "exclude": []}],
    )

    searches = {
        row["name"]: row["active"]
        for row in conn.execute("SELECT name, active FROM search")
    }
    active = {name: state for name, state in searches.items() if state == 1}
    assert active == {"historique": 1, "Data": 1}
    assert [name for name, state in searches.items() if state == 0] == ["Ops (archivée 3)"]
    conn.close()


def test_swapping_two_category_names_is_accepted(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [
            {"label": "Data", "keywords": ["AI Engineer"], "exclude": []},
            {"label": "Ops", "keywords": ["SRE"], "exclude": []},
        ],
    )
    intents = {
        row["label"]: row["id"]
        for row in conn.execute("SELECT id, label FROM career_intent")
    }

    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [
            {"id": intents["Data"], "label": "Ops", "keywords": ["AI Engineer"], "exclude": []},
            {"id": intents["Ops"], "label": "Data", "keywords": ["SRE"], "exclude": []},
        ],
    )

    rows = {
        row["name"]: json.loads(row["include_json"])
        for row in conn.execute("SELECT name, include_json FROM search WHERE active = 1")
    }
    assert rows["Ops"] == ["AI Engineer"]
    assert rows["Data"] == ["SRE"]
    conn.close()


def test_category_name_taken_by_a_foreign_search_is_rejected(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"label": "Data", "keywords": ["AI"], "exclude": []}],
    )
    intent_id = conn.execute("SELECT id FROM career_intent").fetchone()[0]

    with pytest.raises(OnboardingError, match="existe déjà"):
        complete_profile(
            conn,
            account_id,
            workspace_id,
            [cv_id],
            [{"id": intent_id, "label": "historique", "keywords": ["AI"], "exclude": []}],
        )
    assert conn.execute("SELECT name FROM search WHERE id = (SELECT search_id FROM career_intent)")\
        .fetchone()[0] == "Data"
    conn.close()


def test_unnamed_category_is_rejected_instead_of_dropped(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    with pytest.raises(OnboardingError, match="nom"):
        complete_profile(
            conn,
            account_id,
            workspace_id,
            [cv_id],
            [
                {"label": "Data", "keywords": ["AI"], "exclude": []},
                {"label": "  ", "keywords": ["ML"], "exclude": []},
            ],
        )
    assert conn.execute("SELECT count(*) FROM career_intent").fetchone()[0] == 0
    conn.close()


def test_matching_failure_after_save_keeps_the_profile(tmp_path, monkeypatch) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)

    def boom(_conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("jobwatch.onboarding.run_matching", boom)
    intents = complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"label": "Data", "keywords": ["AI Engineer"], "exclude": []}],
    )

    assert [intent.label for intent in intents] == ["Data"]
    assert conn.execute(
        "SELECT completed_at IS NOT NULL FROM candidate_profile WHERE account_id = ?",
        (account_id,),
    ).fetchone()[0] == 1
    conn.close()


def test_deleted_category_frees_its_name_for_a_later_rename(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [
            {"label": "Data", "keywords": ["AI Engineer"], "exclude": []},
            {"label": "Ops", "keywords": ["SRE"], "exclude": []},
        ],
    )
    data_id = conn.execute("SELECT id FROM career_intent WHERE label = 'Data'").fetchone()[0]

    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"id": data_id, "label": "Data", "keywords": ["AI Engineer"], "exclude": []}],
    )
    data_id = conn.execute("SELECT id FROM career_intent WHERE label = 'Data'").fetchone()[0]

    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"id": data_id, "label": "Ops", "keywords": ["AI Engineer"], "exclude": []}],
    )

    active = {
        row["name"]: row["id"]
        for row in conn.execute("SELECT id, name FROM search WHERE active = 1")
    }
    assert set(active) == {"historique", "Ops"}
    assert active["Ops"] == conn.execute(
        "SELECT search_id FROM career_intent WHERE account_id = ?", (account_id,)
    ).fetchone()[0]
    conn.close()


def test_category_deleted_and_its_name_reused_in_the_same_save(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [
            {"label": "Data", "keywords": ["AI Engineer"], "exclude": []},
            {"label": "Ops", "keywords": ["SRE"], "exclude": []},
        ],
    )
    intents = {
        row["label"]: (row["id"], row["search_id"])
        for row in conn.execute("SELECT id, label, search_id FROM career_intent")
    }

    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"id": intents["Data"][0], "label": "Ops", "keywords": ["AI Engineer"], "exclude": []}],
    )

    kept = conn.execute("SELECT search_id FROM career_intent").fetchone()[0]
    assert kept == intents["Data"][1]
    assert conn.execute("SELECT name FROM search WHERE id = ?", (kept,)).fetchone()[0] == "Ops"
    assert conn.execute(
        "SELECT active FROM search WHERE id = ?", (intents["Ops"][1],)
    ).fetchone()[0] == 0
    conn.close()


def test_other_account_categories_stay_active(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    other_id = conn.execute(
        "INSERT INTO account (email) VALUES ('autre@example.com')"
    ).lastrowid
    conn.execute(
        "INSERT INTO membership (account_id, workspace_id, role) VALUES (?, ?, 'owner')",
        (other_id, workspace_id),
    )
    conn.commit()
    complete_profile(
        conn,
        int(other_id),
        workspace_id,
        [cv_id],
        [{"label": "Produit", "keywords": ["Product Owner"], "exclude": []}],
    )

    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"label": "Data", "keywords": ["AI Engineer"], "exclude": []}],
    )

    assert conn.execute("SELECT active FROM search WHERE name = 'Produit'").fetchone()[0] == 1
    assert conn.execute("SELECT active FROM search WHERE name = 'Data'").fetchone()[0] == 1
    conn.close()


def test_manual_category_with_code_fence_is_saved(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    label = "Data ```json``` avancé"
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [cv_id],
        [{"label": label, "keywords": ["AI Engineer"], "exclude": []}],
    )

    assert conn.execute("SELECT label FROM career_intent").fetchone()[0] == label
    assert conn.execute("SELECT active FROM search WHERE name = ?", (label,)).fetchone()[0] == 1
    conn.close()


def test_category_name_owned_by_another_account_is_rejected(tmp_path) -> None:
    conn, account_id, workspace_id, cv_id = _profile_db(tmp_path)
    other_id = int(
        conn.execute("INSERT INTO account (email) VALUES ('autre@example.com')").lastrowid
    )
    conn.execute(
        "INSERT INTO membership (account_id, workspace_id, role) VALUES (?, ?, 'owner')",
        (other_id, workspace_id),
    )
    conn.commit()
    complete_profile(
        conn,
        other_id,
        workspace_id,
        [cv_id],
        [{"label": "Data", "keywords": ["Data Engineer"], "exclude": []}],
    )
    other_search_id = conn.execute(
        "SELECT search_id FROM career_intent WHERE account_id = ?", (other_id,)
    ).fetchone()[0]

    with pytest.raises(OnboardingError, match="existe déjà"):
        complete_profile(
            conn,
            account_id,
            workspace_id,
            [cv_id],
            [{"label": "Data", "keywords": ["AI Engineer"], "exclude": []}],
        )

    row = conn.execute(
        "SELECT name, include_json, active FROM search WHERE id = ?", (other_search_id,)
    ).fetchone()
    assert row["name"] == "Data"
    assert json.loads(row["include_json"]) == ["Data Engineer"]
    assert row["active"] == 1
    assert conn.execute(
        "SELECT count(*) FROM career_intent WHERE account_id = ?", (account_id,)
    ).fetchone()[0] == 0
    conn.close()
