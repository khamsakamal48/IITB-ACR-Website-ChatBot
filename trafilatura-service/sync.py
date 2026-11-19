"""Website -> knowledge bundle sync (replaces v1 Workflows A and A2's embedding pipeline).

Reuses site-scrape/scrape_site.py's extraction verbatim (WP REST item list, live-HTML
fetch, chrome pruning, markdownify, cleaning, roster + PDF handling), so synced pages
are identical in shape to the bulk-scraped ones. No chunking, no embeddings, no LLM.

  POST /kb/sync       pages modified since the last run -> replace sources/*.md
  POST /kb/reconcile  pages no longer published on WordPress -> delete sources/*.md
  GET  /kb/sync/state last run summaries + watermark

n8n schedules both (v2 Workflows A and A2). State lives next to the bundle:
/data/sync-state.json (watermark + last results).
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import knowledge as kb
import okf

router = APIRouter(prefix="/kb", tags=["sync"])
BOILERPLATE = Path(__file__).with_name("boilerplate.txt")  # template lines from the bulk scrape
_running = threading.Lock()


def _ss():
    """Import lazily: scrape_site pulls in requests/markdownify/pymupdf only when syncing."""
    import scrape_site
    return scrape_site


def _state_path() -> Path:
    return kb.KB_INDEX_DB.parent / "sync-state.json"


def _state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(s: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _initial_watermark() -> str:
    """First run: newest `modified` date already in the bundle (the bulk scrape's cut-off)."""
    newest = ""
    for p in (kb.KB_DIR / "sources").glob("*/*.md"):
        if p.name == "index.md":
            continue
        meta, _ = okf.split_frontmatter(p.read_text(encoding="utf-8"))
        newest = max(newest, str(meta.get("modified") or ""))
    return f"{newest or '2000-01-01'}T00:00:00"


def _known_urls() -> set[str]:
    with kb.db() as con:
        return {r[0] for r in con.execute("SELECT url FROM docs WHERE url != ''")}


class SyncIn(BaseModel):
    since: Optional[str] = Field(None, description="ISO datetime override, e.g. 2026-11-01T00:00:00")
    types: list[str] = Field(default_factory=list, description="limit to these WP types (testing)")
    dry_run: bool = False
    max_items: int = Field(400, ge=1, le=5000, description="safety cap per run")


@router.post("/sync", dependencies=[Depends(kb.require_token)])
def sync(inp: SyncIn):
    if not _running.acquire(blocking=False):
        raise HTTPException(409, "a sync or reconcile is already running")
    try:
        return _sync(inp)
    finally:
        _running.release()


def _sync(inp: SyncIn) -> dict:
    ss = _ss()
    state = _state()
    since = inp.since or state.get("watermark") or _initial_watermark()
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    terms = ss.load_taxonomies()
    all_types = ss.session.get(f"{ss.API}/types", timeout=60).json()
    fields = "id,slug,link,title,date,modified,type," + ",".join(terms)

    changed = []
    for name, t in all_types.items():
        if name in ss.SKIP_TYPES or (inp.types and name not in inp.types):
            continue
        try:
            items = ss.api_all(t["rest_base"], {"_fields": fields, "modified_after": since})
        except Exception as e:  # one broken type must not stop the rest
            changed.append({"type": name, "error": str(e)[:200]})
            continue
        for it in items:
            path = urlparse(it["link"]).path.rstrip("/")
            if ss.JUNK_SLUG.match(it["slug"]) or path in ss.BLOCKLIST:
                continue
            it["title"] = ss.html.unescape(it["title"]["rendered"]).strip()
            it["tags"] = {tax: [terms[tax][i] for i in it[tax] if i in terms[tax]]
                          for tax in terms if it.get(tax)}
            it["tags"] = {k: v for k, v in it["tags"].items() if v}
            changed.append(it)
    errors = [c for c in changed if "error" in c]
    items = [c for c in changed if "error" not in c]
    if len(items) > inp.max_items:
        raise HTTPException(422, f"{len(items)} changed items > max_items={inp.max_items}; "
                                 "raise max_items or run the bulk scrape instead")

    bad = set(BOILERPLATE.read_text(encoding="utf-8").splitlines()) - {""} if BOILERPLATE.exists() else set()
    known = _known_urls()
    report = {"since": since, "seen": len(items), "written": [], "unchanged": 0, "skipped": [],
              "new_pdfs": [], "rosters": [], "stale_concepts": [], "errors": errors, "dry_run": inp.dry_run}
    newest = since
    touched, created = set(), False
    roster_types = {it["type"] for it in items if it["type"] in ss.ROSTER_TYPES}

    with kb.write_lock():
        for it in items:
            newest = max(newest, it["modified"])
            if it["type"] in ss.ROSTER_TYPES:
                continue
            try:
                created |= _sync_item(ss, it, bad, known, inp.dry_run, report, touched)
            except Exception as e:      # one bad page must not abort the run
                report["errors"].append({"url": it["link"], "error": str(getattr(e, "detail", e))[:200]})

        for t in sorted(roster_types):
            if inp.dry_run:
                report["rosters"].append({"type": t, "dry_run": True})
                continue
            members = []
            for it in ss.api_all(all_types[t]["rest_base"], {"_fields": fields}):
                it["title"] = ss.html.unescape(it["title"]["rendered"]).strip()
                it["tags"] = {tax: [terms[tax][i] for i in it[tax] if i in terms[tax]]
                              for tax in terms if it.get(tax)}
                members.append(it)
            if members:
                meta, body = ss.roster_md(t, members, terms)
                r = kb.write_source("rosters", f"{t}.md", body, meta)
                report["rosters"].append({"type": t, "members": len(members), "changed": r["changed"]})
                touched.add("rosters")

        if not inp.dry_run:
            for folder in touched:
                okf.write_folder_index(kb.KB_DIR / "sources" / folder)
            if created:
                okf.write_root_index(kb.KB_DIR)
            if report["written"] or report["new_pdfs"] or touched:
                kb.git_commit(f"sync: {len(report['written'])} pages, {len(report['new_pdfs'])} pdfs "
                              f"(modified after {since})")

    report["stale_concepts"] = sorted(set(report["stale_concepts"]))
    report["watermark"] = newest
    if not inp.dry_run and not inp.since and not inp.types:
        state.update(watermark=newest, last_sync={**{k: v for k, v in report.items()
                                                     if k not in ("written",)},
                                                  "written": len(report["written"]), "at": started})
        _save_state(state)
    return report


def _sync_item(ss, it, bad, known, dry_run, report, touched) -> bool:
    """Fetch + extract + write one changed WP item. Returns True if a file was created."""
    created = False
    raw = _fetch(ss, it["link"])
    if raw is None:
        report["errors"].append({"url": it["link"], "error": "fetch failed"})
        return created
    md = ss.strip_lines(ss.clean(ss.html_to_md(raw), it["title"], it["link"]), bad)
    n = ss.body_len(md)
    if n < ss.MIN_CHARS:
        report["skipped"].append({"url": it["link"], "reason": f"empty ({n} chars)"})
        return created
    share, cards = ss.link_share(md, known)
    if share >= ss.LISTING_SHARE and cards >= ss.LISTING_MIN_CARDS:
        report["skipped"].append({"url": it["link"], "reason": "listing page"})
        return created
    meta = {"url": it["link"], "title": it["title"], "post_id": it["id"],
            "published": it["date"][:10], "modified": it["modified"][:10], **it["tags"]}
    slug = ss.file_name(it["link"], it["type"])
    if dry_run:
        report["written"].append({"path": f"sources/{it['type']}/{slug}", "dry_run": True})
        return created
    r = kb.write_source(it["type"], slug, md, meta)
    if r["changed"]:
        report["written"].append({"path": r["path"], "created": r["created"]})
        report["stale_concepts"] += r["stale_concepts"]
        touched.add(it["type"])
        created |= r["created"]
    else:
        report["unchanged"] += 1
    for pdf in sorted(ss.pdf_links(raw, it["link"]) - known):
        done = _add_pdf(ss, pdf, it["link"])
        if done:
            report["new_pdfs"].append(done)
            known.add(pdf)
            touched.add("pdf")
            created = True

    return created


def _fetch(ss, url: str) -> Optional[str]:
    try:
        r = ss.session.get(url, timeout=60)
    except Exception:
        return None
    return r.content.decode("utf-8", "replace") if r.status_code == 200 else None


def _add_pdf(ss, url: str, page_url: str) -> Optional[dict]:
    try:
        r = ss.session.get(url, timeout=120)
        if r.status_code != 200:
            return None
        title = Path(urlparse(url).path).stem.replace("-", " ").replace("_", " ")
        md = ss.clean(ss.pdf_to_md(r.content), title, url)
    except Exception:          # corrupt/encrypted PDF: skip, don't fail the sync
        return None
    if ss.body_len(md) < ss.MIN_CHARS:
        return None
    slug = Path(urlparse(url).path).stem[:150] + ".md"
    res = kb.write_source("pdf", slug, md, {"url": url, "title": title, "linked_from": [page_url]})
    return {"path": res["path"], "from": page_url}


class ReconcileIn(BaseModel):
    dry_run: bool = False
    min_ratio: float = Field(0.5, ge=0.0, le=1.0,
                             description="skip a type if WP returns < this share of the bundle's pages")


@router.post("/reconcile", dependencies=[Depends(kb.require_token)])
def reconcile(inp: ReconcileIn):
    """Delete source pages whose WordPress item is no longer published. PDFs and rosters
    are left alone (rosters are rebuilt by sync; PDFs have no WP id)."""
    if not _running.acquire(blocking=False):
        raise HTTPException(409, "a sync or reconcile is already running")
    try:
        ss = _ss()
        all_types = ss.session.get(f"{ss.API}/types", timeout=60).json()
        bundle: dict[str, dict[int, Path]] = defaultdict(dict)
        for p in (kb.KB_DIR / "sources").glob("*/*.md"):
            if p.name == "index.md" or p.parent.name in ("pdf", "rosters"):
                continue
            meta, _ = okf.split_frontmatter(p.read_text(encoding="utf-8"))
            if meta.get("post_id"):
                bundle[p.parent.name][int(meta["post_id"])] = p

        report = {"deleted": [], "skipped_types": [], "checked": {}, "stale_concepts": [], "dry_run": inp.dry_run}
        doomed: list[Path] = []
        for folder, pages in sorted(bundle.items()):
            t = all_types.get(folder)
            if not t:
                report["skipped_types"].append({"type": folder, "reason": "not a WordPress type any more"})
                continue
            try:
                live = {it["id"] for it in ss.api_all(t["rest_base"], {"_fields": "id"})}
            except Exception as e:
                report["skipped_types"].append({"type": folder, "reason": f"API error: {str(e)[:120]}"})
                continue
            gone = [pid for pid in pages if pid not in live]
            report["checked"][folder] = {"bundle": len(pages), "live": len(live), "gone": len(gone)}
            if len(live) < inp.min_ratio * len(pages):   # API outage / auth change: don't mass-delete
                report["skipped_types"].append({"type": folder, "reason": f"only {len(live)} live vs "
                                                f"{len(pages)} in bundle; refusing to delete"})
                continue
            doomed += [pages[pid] for pid in gone]

        if inp.dry_run:
            report["deleted"] = [kb.rel_of(p) for p in doomed]
            return report
        with kb.write_lock():
            folders = set()
            for p in doomed:
                meta, _ = okf.split_frontmatter(p.read_text(encoding="utf-8"))
                rel = kb.rel_of(p)
                p.unlink()
                with kb._lock, kb.db() as con:
                    con.execute("DELETE FROM docs WHERE path = ?", (rel,))
                    report["stale_concepts"] += kb.mark_concepts_stale(con, meta.get("resource"))
                report["deleted"].append(rel)
                folders.add(p.parent)
            for f in folders:
                okf.write_folder_index(f)
            if doomed:
                okf.write_root_index(kb.KB_DIR)
                kb.git_commit(f"reconcile: removed {len(doomed)} unpublished pages")
        report["stale_concepts"] = sorted(set(report["stale_concepts"]))
        state = _state()
        state["last_reconcile"] = {**report, "deleted": len(report["deleted"]),
                                   "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        _save_state(state)
        return report
    finally:
        _running.release()


@router.get("/sync/state", dependencies=[Depends(kb.require_token)])
def sync_state():
    s = _state()
    s.setdefault("watermark", None)
    return s
