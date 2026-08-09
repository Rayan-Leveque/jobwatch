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
