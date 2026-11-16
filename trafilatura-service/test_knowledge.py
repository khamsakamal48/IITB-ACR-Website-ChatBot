"""Tests for the /kb routes, run against a throwaway copy of a real bundle.

  KB_TEST_SOURCE=~/acr-chatbot/knowledge \
  uv run --python 3.12 --with-requirements requirements.txt --with pytest --with httpx \
      python -I -m pytest -q test_knowledge.py
"""
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = Path(os.path.expanduser(os.environ.get("KB_TEST_SOURCE", "~/acr-chatbot/knowledge")))
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

pytestmark = pytest.mark.skipif(not (SRC / "faq").exists(), reason=f"no bundle at {SRC}")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    kb = tmp_path / "knowledge"
    kb.mkdir()
    shutil.copy(SRC / "index.md", kb / "index.md")
    for sub in ("faq", "concepts", "sources/page", "sources/pdf", "sources/causes"):
        shutil.copytree(SRC / sub, kb / sub)
    monkeypatch.setenv("KB_DIR", str(kb))
    monkeypatch.setenv("KB_INDEX_DB", str(tmp_path / "idx.sqlite"))
    monkeypatch.setenv("KB_TOKEN", TOKEN)
    monkeypatch.delenv("KB_ALLOW_DRAFTS", raising=False)
    monkeypatch.setenv("KB_GIT", "1")
    sys.path.insert(0, str(HERE))
    import knowledge, app  # noqa: E401
    importlib.reload(knowledge)
    importlib.reload(app)
    from fastapi.testclient import TestClient
    with TestClient(app.app) as c:           # runs lifespan -> builds index
        c.kb = kb
        c.mod = knowledge
        yield c


def test_auth_required(client):
    assert client.get("/kb/context").status_code == 401
    assert client.get("/kb/context", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/health").status_code == 200          # extractor health stays open


def test_context_has_all_faq_and_hides_unapproved_concepts(client):
    r = client.get("/kb/context", headers=AUTH).json()
    assert r["text"].count("\n## ") >= 211                   # every FAQ answer is in the prompt
    assert "](concepts/" not in r["text"]                    # all concepts are still drafts
    assert "(none approved yet)" in r["text"]
    assert client.get("/kb/context", headers=AUTH).json()["version"] == r["version"]   # cache-stable


def test_draft_concept_hidden_until_verified_then_served_without_review_notes(client):
    path = "concepts/ways-of-giving.md"
    assert client.get("/kb/page", params={"path": path}, headers=AUTH).status_code == 404
    # team approves the page -> PUT with verified: true
    text = (client.kb / path).read_text().replace("verified: false", "verified: true")
    assert client.put("/kb/page", json={"path": path, "content": text}, headers=AUTH).status_code == 200
    page = client.get("/kb/page", params={"path": path}, headers=AUTH).json()
    assert page["verified"] is True and "Review notes" not in page["content"]
    assert "](concepts/ways-of-giving.md)" in client.get("/kb/context", headers=AUTH).json()["text"]
    hits = client.get("/kb/search", params={"q": "ways of giving cheque"}, headers=AUTH).json()["results"]
    assert hits[0]["path"] == path                            # approved concept ranks first


def test_path_traversal_refused(client):
    for bad in ("../../etc/passwd", "/etc/passwd", "sources/../../secret"):
        assert client.get("/kb/page", params={"path": bad}, headers=AUTH).status_code in (400, 404)


def test_search_finds_faq_and_sources(client):
    hits = client.get("/kb/search", params={"q": "80G tax exemption"}, headers=AUTH).json()["results"]
    assert hits and any(h["path"] == "faq/donations.md" for h in hits[:3])
    assert all(not h["path"].startswith("concepts/") for h in hits)     # drafts excluded
    only_pages = client.get("/kb/search", params={"q": "young faculty fellowship", "tier": "sources"},
                            headers=AUTH).json()["results"]
    assert only_pages and all(h["tier"] == "sources" for h in only_pages)


def test_long_pdf_is_read_in_windows(client):
    big = max((client.kb / "sources/pdf").glob("*.md"), key=lambda p: p.stat().st_size)
    rel = f"sources/pdf/{big.name}"
    first = client.get("/kb/page", params={"path": rel}, headers=AUTH).json()
    assert first["next_offset"] and len(first["content"]) <= 30_000
    second = client.get("/kb/page", params={"path": rel, "offset": first["next_offset"]}, headers=AUTH).json()
    assert second["offset"] == first["next_offset"] and second["content"][:50] != first["content"][:50]


def test_put_source_replaces_page_and_marks_concepts_stale(client):
    url = "https://acr.iitbombay.org/young-faculty-fellowship/"
    users = [p for p in (client.kb / "concepts").glob("*.md") if url in p.read_text()]
    assert users, "fixture expects a concept citing the YFF page"
    r = client.put("/kb/source", headers=AUTH, json={
        "folder": "page", "slug": "young-faculty-fellowship", "url": url,
        "title": "Young Faculty Fellowship", "post_id": 323900, "modified": "2026-11-20",
        "markdown": "The Young Faculty Fellowship now supports zyxwquantum researchers."})
    assert r.status_code == 200 and r.json()["created"] is False
    assert set(r.json()["stale_concepts"]) == {f"concepts/{p.name}" for p in users}
    assert "status: stale" in users[0].read_text()
    hits = client.get("/kb/search", params={"q": "zyxwquantum"}, headers=AUTH).json()["results"]
    assert hits[0]["path"] == "sources/page/young-faculty-fellowship.md"
    assert client.get("/kb/stale", headers=AUTH).json()["stale"]
    log = subprocess.run(["git", "-C", str(client.kb), "log", "--oneline"], capture_output=True, text=True)
    assert "update sources/page/young-faculty-fellowship.md" in log.stdout


def test_new_source_appears_in_listing_and_delete_removes_it(client):
    r = client.put("/kb/source", headers=AUTH, json={
        "folder": "causes", "slug": "new-cause", "url": "https://acr.iitbombay.org/causes/new-cause/",
        "title": "Brand New Cause", "markdown": "# Brand New Cause\n\n" + "Funding qwertyplex labs. " * 5})
    assert r.json()["created"] is True
    listing = client.get("/kb/list", params={"folder": "sources/causes"}, headers=AUTH).json()["content"]
    assert "Brand New Cause" in listing
    assert client.delete("/kb/page", params={"path": "sources/causes/new-cause.md"}, headers=AUTH).status_code == 200
    assert not client.get("/kb/search", params={"q": "qwertyplex"}, headers=AUTH).json()["results"]
    assert "Brand New Cause" not in client.get("/kb/list", params={"folder": "sources/causes"},
                                               headers=AUTH).json()["content"]


def test_faq_refresh_replaces_categories(client):
    rows = [{"category": "Donations", "subcategory": "Process", "question": "How do I donate?",
             "alternative": "give money", "answer": "Use our portal at https://example.org", "page": "Ways"},
            {"category": "", "subcategory": "", "question": "Second q?", "alternative": "",
             "answer": "Second a.", "page": ""}]
    r = client.put("/kb/faq", json={"rows": rows}, headers=AUTH)
    assert r.json() == {"categories": 1, "answers": 2}
    names = sorted(p.name for p in (client.kb / "faq").glob("*.md"))
    assert names == ["donations.md", "index.md"]               # removed categories are deleted
    ctx = client.get("/kb/context", headers=AUTH).json()["text"]
    assert "Second q?" in ctx and "ASMP" not in ctx
    assert client.put("/kb/faq", json={"rows": [{"category": "x"}]}, headers=AUTH).status_code == 422
