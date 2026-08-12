from __future__ import annotations

import sqlite3

import pytest

from jobwatch.db import connect, init_db
from jobwatch.profile import (
    MAX_PROFILE_FIELD_LENGTH,
    ProfileDetails,
    ProfileError,
    draft_profile_context,
    profile_details,
    save_profile_details,
)
from jobwatch.profile_ui import render_profile


@pytest.fixture()
def profile_db() -> tuple[sqlite3.Connection, int, int]:
    conn = connect(":memory:")
    init_db(conn)
    workspace_id = int(
        conn.execute(
            "INSERT INTO workspace (slug, name) VALUES ('alice', 'Alice')"
        ).lastrowid
    )
    account_id = int(
        conn.execute(
            "INSERT INTO account (email) VALUES ('alice@example.com')"
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO membership (account_id, workspace_id, role) VALUES (?, ?, 'owner')",
        (account_id, workspace_id),
    )
    conn.execute(
        "INSERT INTO candidate_profile (account_id, workspace_id, completed_at) "
        "VALUES (?, ?, datetime('now'))",
        (account_id, workspace_id),
    )
    conn.commit()
    yield conn, account_id, workspace_id
    conn.close()


def test_optional_profile_round_trip_and_draft_context(profile_db) -> None:
    conn, account_id, workspace_id = profile_db
    details = save_profile_details(
        conn,
        account_id,
        workspace_id,
        {
            "motivations": "  Construire des produits IA utiles. ",
            "targets": "AI Engineer dans la mobilité",
            "highlights": "Déploiement réel d'un assistant RAG.",
            "preferred_tone": "Direct et chaleureux",
            "constraints_text": "Ne pas annoncer de disponibilité immédiate.",
            "reusable_details": "Bénévolat dans une association locale.",
        },
    )

    assert details == profile_details(conn, account_id)
    assert details.motivations == "Construire des produits IA utiles."
    context = draft_profile_context(conn)
    assert context is not None
    assert "# PROFIL PERSONNEL" not in context
    assert "## Motivations\n\nConstruire des produits IA utiles." in context
    assert "Déploiement réel d'un assistant RAG." in context


def test_empty_profile_is_valid_and_produces_no_draft_context(profile_db) -> None:
    conn, account_id, workspace_id = profile_db
    details = save_profile_details(conn, account_id, workspace_id, {})
    assert details.has_personalization is False
    assert draft_profile_context(conn) is None


def test_profile_write_is_scoped_to_authenticated_workspace(profile_db) -> None:
    conn, account_id, _workspace_id = profile_db
    other_workspace = int(
        conn.execute(
            "INSERT INTO workspace (slug, name) VALUES ('bob', 'Bob')"
        ).lastrowid
    )
    with pytest.raises(ProfileError, match="terminez"):
        save_profile_details(
            conn, account_id, other_workspace, {"motivations": "Ne doit pas passer"}
        )
    assert profile_details(conn, account_id).motivations == ""


def test_ambiguous_instance_never_supplies_another_profile_to_draft(profile_db) -> None:
    conn, account_id, workspace_id = profile_db
    save_profile_details(
        conn, account_id, workspace_id, {"motivations": "Contexte d'Alice"}
    )
    other_account = int(
        conn.execute(
            "INSERT INTO account (email) VALUES ('intrus@example.com')"
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO candidate_profile "
        "(account_id, workspace_id, motivations, completed_at) "
        "VALUES (?, ?, 'Contexte intrus', datetime('now'))",
        (other_account, workspace_id),
    )
    conn.commit()
    assert draft_profile_context(conn) is None


def test_profile_rejects_oversized_values(profile_db) -> None:
    conn, account_id, workspace_id = profile_db
    with pytest.raises(ProfileError, match="dépasse"):
        save_profile_details(
            conn,
            account_id,
            workspace_id,
            {"highlights": "x" * (MAX_PROFILE_FIELD_LENGTH + 1)},
        )


def test_profile_page_guides_empty_user_and_escapes_identity() -> None:
    page = render_profile(
        ProfileDetails(),
        "csrf",
        email="alice+test@example.com",
        workspace_slug="alice<unsafe>",
        welcome=True,
    )
    assert "Tous les champs sont facultatifs" in page
    assert "Passer pour l’instant" in page
    assert "alice&lt;unsafe&gt;" in page
    assert 'name="highlights"' in page
