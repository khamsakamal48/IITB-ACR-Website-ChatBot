"""Sync/reconcile tests against a fake WordPress (real scrape_site extraction code).

  KB_TEST_SOURCE=~/acr-chatbot/knowledge \
  uv run --python 3.12 --with-requirements requirements.txt --with pytest --with httpx \
      python -I -m pytest -q test_sync.py
"""
import importlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = Path(os.path.expanduser(os.environ.get("KB_TEST_SOURCE", "~/acr-chatbot/knowledge")))
AUTH = {"Authorization": "Bearer test-token"}
pytestmark = pytest.mark.skipif(not (SRC / "faq").exists(), reason=f"no bundle at {SRC}")

YFF_URL = "https://acr.iitbombay.org/young-faculty-fellowship/"
NEW_URL = "https://acr.iitbombay.org/brand-new-scholarship/"
EMPTY_URL = "https://acr.iitbombay.org/empty-stub/"


def page_html(title, body):
    return (f"<html><body><header><nav>MENU Home About</nav></header><main><h1>{title}</h1>"
            f"{body}<p>Donate Now</p></main><footer class='site-footer'>FOOTER</footer></body></html>").encode()


class Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content
        self.headers = {"X-WP-TotalPages": "1"}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeWP:
    """Two types: page (YFF updated + a new page + an empty stub) and news (nothing changed)."""
    def __init__(self):
        self.pages = [
            {"id": 323900, "slug": "young-faculty-fellowship", "link": YFF_URL, "type": "page",
             "title": {"rendered": "Young Faculty Fellowship"}, "date": "2026-09-04T10:00:00",
             "modified": "2026-11-18T09:30:00"},
            {"id": 999001, "slug": "brand-new-scholarship", "link": NEW_URL, "type": "page",
             "title": {"rendered": "Brand New Scholarship"}, "date": "2026-11-17T10:00:00",
             "modified": "2026-11-17T10:00:00"},
            {"id": 999002, "slug": "empty-stub", "link": EMPTY_URL, "type": "page",
             "title": {"rendered": "Empty Stub"}, "date": "2026-11-16T10:00:00",
             "modified": "2026-11-16T10:00:00"},
        ]
        self.html = {
            YFF_URL: page_html("Young Faculty Fellowship",
                               "<p>The Young Faculty Fellowship now also supports zorbquantum research "
                               "groups across all departments of IIT Bombay, from 2027 onwards.</p>"),
            NEW_URL: page_html("Brand New Scholarship",
                               "<p>A new merit scholarship for first-generation learners, funded by the "
                               "Class of 2001, covering tuition and hostel fees for four years.</p>"),
            EMPTY_URL: page_html("Empty Stub", "<p>Hi</p>"),
        }
        self.live_page_ids = None   # reconcile override

    def api_all(self, base, params=None):
        params = params or {}
        if base == "pages":
            items = [dict(p) for p in self.pages]
            if self.live_page_ids is not None:
                return [{"id": i} for i in self.live_page_ids]
            since = params.get("modified_after")
            return [p for p in items if not since or p["modified"] > since]
        return []

    def get(self, url, timeout=60, **kw):
        if url.endswith("/types"):
            return Resp(data={"page": {"rest_base": "pages"}, "news": {"rest_base": "news"}})
        if url in self.html:
            return Resp(content=self.html[url])
        return Resp(status=404)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    kb = tmp_path / "knowledge"
    kb.mkdir()
    shutil.copy(SRC / "index.md", kb / "index.md")
    for sub in ("faq", "concepts", "sources/page", "sources/causes"):
        shutil.copytree(SRC / sub, kb / sub)
    monkeypatch.setenv("KB_DIR", str(kb))
    monkeypatch.setenv("KB_INDEX_DB", str(tmp_path / "idx.sqlite"))
    monkeypatch.setenv("KB_TOKEN", "test-token")
    monkeypatch.setenv("KB_GIT", "1")
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent / "site-scrape"))
    import knowledge, sync, app  # noqa: E401
    importlib.reload(knowledge); importlib.reload(sync); importlib.reload(app)
    import scrape_site
    wp = FakeWP()
    monkeypatch.setattr(scrape_site, "load_taxonomies", lambda: {})
    monkeypatch.setattr(scrape_site, "api_all", wp.api_all)
    monkeypatch.setattr(scrape_site.session, "get", wp.get)
    from fastapi.testclient import TestClient
    with TestClient(app.app) as c:
        c.kb, c.wp, c.state = kb, wp, tmp_path / "sync-state.json"
        yield c


def test_sync_replaces_changed_page_adds_new_skips_empty_and_marks_stale(client):
    r = client.post("/kb/sync", headers=AUTH, json={})
    assert r.status_code == 200, r.text
    rep = r.json()
    paths = {w["path"] for w in rep["written"]}
    assert paths == {"sources/page/young-faculty-fellowship.md", "sources/page/brand-new-scholarship.md"}
    assert [s["url"] for s in rep["skipped"]] == [EMPTY_URL]
    yff = (client.kb / "sources/page/young-faculty-fellowship.md").read_text()
    assert "zorbquantum" in yff and "MENU" not in yff and "FOOTER" not in yff
    assert "Donate Now" not in yff                      # template line from boilerplate.txt
    assert "post_id: 323900" in yff and "modified: '2026-11-18'" in yff
    assert "concepts/young-faculty-fellowship.md" in rep["stale_concepts"]
    hits = client.get("/kb/search", params={"q": "zorbquantum"}, headers=AUTH).json()["results"]
    assert hits and hits[0]["path"] == "sources/page/young-faculty-fellowship.md"
    assert "Brand New Scholarship" in client.get("/kb/list", params={"folder": "sources/page"},
                                                 headers=AUTH).json()["content"]
    assert json.loads(client.state.read_text())["watermark"] == "2026-11-18T09:30:00"
    import subprocess
    log = subprocess.run(["git", "-C", str(client.kb), "log", "--oneline"], capture_output=True, text=True).stdout
    assert log.count("\n") == 1 and "sync: 2 pages" in log           # one commit per sync batch


def test_second_sync_only_sees_newer_items_and_unchanged_content_is_a_noop(client):
    client.post("/kb/sync", headers=AUTH, json={})
    assert client.post("/kb/sync", headers=AUTH, json={}).json()["seen"] == 0   # watermark moved on
    rep = client.post("/kb/sync", headers=AUTH, json={"since": "2026-11-01T00:00:00"}).json()
    assert rep["written"] == [] and rep["unchanged"] == 2 and rep["stale_concepts"] == []


def test_dry_run_writes_nothing(client):
    before = (client.kb / "sources/page/young-faculty-fellowship.md").read_text()
    rep = client.post("/kb/sync", headers=AUTH, json={"dry_run": True}).json()
    assert rep["dry_run"] and len(rep["written"]) == 2
    assert (client.kb / "sources/page/young-faculty-fellowship.md").read_text() == before
    assert not client.state.exists()


def test_reconcile_deletes_unpublished_and_refuses_mass_delete(client):
    pages = {int(l.split(":")[1]) for p in (client.kb / "sources/page").glob("*.md") if p.name != "index.md"
             for l in p.read_text().splitlines() if l.startswith("post_id:")}
    victim = 323900                                      # YFF disappears from WordPress
    client.wp.live_page_ids = sorted(pages - {victim})
    rep = client.post("/kb/reconcile", headers=AUTH, json={"dry_run": True}).json()
    assert rep["deleted"] == ["sources/page/young-faculty-fellowship.md"]
    assert (client.kb / "sources/page/young-faculty-fellowship.md").exists()
    rep = client.post("/kb/reconcile", headers=AUTH, json={}).json()
    assert rep["deleted"] == ["sources/page/young-faculty-fellowship.md"]
    assert not (client.kb / "sources/page/young-faculty-fellowship.md").exists()
    assert "concepts/young-faculty-fellowship.md" in rep["stale_concepts"]
    client.wp.live_page_ids = [victim]                   # WP suddenly returns ~nothing
    rep = client.post("/kb/reconcile", headers=AUTH, json={}).json()
    assert rep["deleted"] == []
    assert any(s["type"] == "page" and "refusing" in s["reason"] for s in rep["skipped_types"])
    # a folder whose type vanished from WordPress is skipped too, never mass-deleted
    assert any(s["type"] == "causes" for s in rep["skipped_types"]) and (client.kb / "sources/causes").exists()


def test_sync_endpoints_need_token(client):
    assert client.post("/kb/sync", json={}).status_code == 401
    assert client.post("/kb/reconcile", json={}).status_code == 401
    assert client.get("/kb/sync/state").status_code == 401
