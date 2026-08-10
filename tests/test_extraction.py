from __future__ import annotations

import json

from jobwatch import extraction


def test_extract_prefers_job_posting_jsonld_and_reads_structured_fields() -> None:
    description = "Mission principale : construire des systèmes RAG en Python. " * 12
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "AI Engineer",
        "hiringOrganization": {"name": "Acme"},
        "description": description,
        "experienceRequirements": "Deux ans d'expérience",
        "skills": ["Python", "RAG"],
        "jobLocationType": "TELECOMMUTE",
    }
    page = (
        '<html><body><nav>Navigation très longue</nav><script type="application/ld+json">'
        f"{json.dumps(posting)}</script><footer>Pied de page</footer></body></html>"
    )

    result = extraction.extract(page)

    assert result.method == "jsonld"
    assert "Mission principale" in result.markdown
    assert result.fields == {
        "experience": "Deux ans d'expérience",
        "remote": "Télétravail (jobLocationType : TELECOMMUTE)",
        "stack": "Python, RAG",
    }


def test_extract_keeps_raw_page_when_readable_text_loses_salary(monkeypatch) -> None:
    useful = "Développer et maintenir la plateforme de données. " * 12
    page = f"<html><body><main>{useful}</main><aside>Salaire : 55 000 EUR</aside></body></html>"
    monkeypatch.setattr(extraction, "_readable_markdown", lambda html: useful)

    result = extraction.extract(page)

    assert result.method == "raw"
    assert "55 000 EUR" in result.markdown
    assert "salary" in result.lost_markers


def test_extract_skips_readable_extraction_when_jsonld_wins(monkeypatch) -> None:
    description = "Mission principale : construire des systèmes RAG en Python. " * 12
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "AI Engineer",
        "hiringOrganization": {"name": "Acme"},
        "description": description,
    }
    page = (
        '<html><body><script type="application/ld+json">'
        f"{json.dumps(posting)}</script></body></html>"
    )
    calls = []
    monkeypatch.setattr(
        extraction, "_readable_markdown", lambda html: calls.append(html) or ""
    )

    result = extraction.extract(page)

    assert result.method == "jsonld"
    assert calls == []


def test_extract_runs_readable_extraction_once_on_the_raw_fallback(monkeypatch) -> None:
    page = "<html><body><main>Trop court</main></body></html>"
    calls = []
    monkeypatch.setattr(
        extraction, "_readable_markdown", lambda html: calls.append(html) or "Trop court"
    )

    result = extraction.extract(page)

    assert result.method == "raw"
    assert len(calls) == 1


def test_extract_salvages_figures_left_outside_the_body(monkeypatch) -> None:
    """Le cas LinkedIn : le salaire vit hors du corps, mais le mot « salaire » y est.

    Le garde-fou ne voit alors aucune perte de marqueur, et sans repêchage le
    chiffre disparaîtrait silencieusement du texte envoyé au modèle.
    """
    body = "Nous recherchons un ingénieur IA. Le salaire est discuté en entretien. " * 8
    page = (
        f"<html><body><main>{body}</main>"
        "<aside><h3>Base pay range</h3><p>€38,000.00/yr - €65,000.00/yr</p></aside>"
        "</body></html>"
    )
    monkeypatch.setattr(extraction, "_readable_markdown", lambda html: body)

    result = extraction.extract(page)

    assert result.method == "readable"  # on garde le gain de l'extraction...
    assert "€38,000.00/yr - €65,000.00/yr" in result.markdown  # ...sans perdre le chiffre
    assert result.salvaged_lines == 1
    assert result.raw_chars == len(extraction.markdownify(page).strip())


def test_extract_salvage_stays_bounded(monkeypatch) -> None:
    """Le repêchage ne doit jamais réintroduire la page entière."""
    # Le corps parle déjà de salaire : le garde-fou ne voit aucune perte et
    # laisse donc le repêchage travailler, qui doit rester borné.
    body = "Poste d'ingénieur data, salaire selon profil. " * 12
    noise = "".join(f"<li>Offre voisine à {n} 000 EUR</li>" for n in range(40, 80))
    page = f"<html><body><main>{body}</main><ul>{noise}</ul></body></html>"
    monkeypatch.setattr(extraction, "_readable_markdown", lambda html: body)

    result = extraction.extract(page)

    assert result.salvaged_lines == extraction.MAX_SALVAGED_LINES
    salvaged = result.markdown.split(extraction.SALVAGE_HEADING)[1].splitlines()
    assert len(salvaged) == extraction.MAX_SALVAGED_LINES
