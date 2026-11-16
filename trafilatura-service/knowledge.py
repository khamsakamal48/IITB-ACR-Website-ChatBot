"""ACR knowledge service — serves the OKF Markdown bundle to the n8n chat agent.

No chunks, no embeddings: the agent reads WHOLE pages. Search is lexical (SQLite FTS5
/ BM25) and only tells the agent which pages to open.

Env:
  KB_DIR          bundle root (default /data/knowledge)
  KB_INDEX_DB     FTS index file, kept OUTSIDE the bundle (default /data/kb-index.sqlite)
  KB_TOKEN        required Bearer token for every /kb route
  KB_ALLOW_DRAFTS "1" = also serve concept pages not yet approved (testing only)
  KB_GIT          "1" = commit every write to a local git repo inside KB_DIR

Read routes (chat agent):   GET  /kb/context  /kb/page  /kb/search  /kb/list
Write routes (sync jobs):   PUT  /kb/source   PUT /kb/page   DELETE /kb/page
                            PUT  /kb/faq      POST /kb/reindex   GET /kb/stale
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

import okf

KB_DIR = Path(os.environ.get("KB_DIR", "/data/knowledge"))
KB_INDEX_DB = Path(os.environ.get("KB_INDEX_DB", "/data/kb-index.sqlite"))
ALLOW_DRAFTS = os.environ.get("KB_ALLOW_DRAFTS") == "1"
GIT = os.environ.get("KB_GIT") == "1"

PAGE_CHARS = 30_000            # default page window; long PDFs are read in windows
TIER_BOOST = {"concepts": 1.6, "faq": 1.4, "sources": 1.0}   # bm25 is negative: bigger = better
REVIEW_NOTES = re.compile(r"\n## Review notes\b.*?(?=\n## |\Z)", re.S)

router = APIRouter(prefix="/kb", tags=["knowledge"])
_lock = threading.Lock()


# ---------------------------------------------------------------- auth
def require_token(authorization: Optional[str] = Header(None)) -> None:
    token = os.environ.get("KB_TOKEN")
    if not token:
        raise HTTPException(503, "KB_TOKEN not configured on the server")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(401, "invalid or missing bearer token")


# ---------------------------------------------------------------- paths & policy
def resolve(rel: str) -> Path:
    """Bundle-relative .md path -> absolute path; refuses anything outside KB_DIR."""
    rel = rel.strip().lstrip("/")
    if not rel.endswith(".md"):
        rel += ".md"
    p = (KB_DIR / rel).resolve()
    root = KB_DIR.resolve()
    if root not in p.parents or not rel.split("/")[0] in ("concepts", "faq", "sources", "index.md"):
        raise HTTPException(400, f"path outside the knowledge bundle: {rel}")
    return p


def rel_of(p: Path) -> str:
    return p.resolve().relative_to(KB_DIR.resolve()).as_posix()


def servable(rel: str, meta: dict) -> bool:
    """Concept pages reach the bot only once the team approved them (verified: true).
    A stale concept stays servable: it is the last approved version until recompiled."""
    if rel.startswith("concepts/"):
        return ALLOW_DRAFTS or meta.get("verified") is True
    return True


def for_bot(body: str) -> str:
    return REVIEW_NOTES.sub("", body).strip()


# ---------------------------------------------------------------- FTS index
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
  title, description, tags, body,
  path UNINDEXED, tier UNINDEXED, type UNINDEXED, url UNINDEXED,
  modified UNINDEXED, servable UNINDEXED,
  tokenize = 'porter unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS concept_sources(concept TEXT, url TEXT);
CREATE INDEX IF NOT EXISTS cs_url ON concept_sources(url);
"""


@contextmanager
def db():
    KB_INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(KB_INDEX_DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    try:
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def _index_one(con: sqlite3.Connection, p: Path) -> None:
    rel = rel_of(p)
    con.execute("DELETE FROM docs WHERE path = ?", (rel,))
    if rel.startswith("concepts/"):
        con.execute("DELETE FROM concept_sources WHERE concept = ?", (rel,))
    if p.name == "index.md" or not p.exists():
        return
    meta, body = okf.split_frontmatter(p.read_text(encoding="utf-8"))
    tier = rel.split("/")[0]
    con.execute(
        "INSERT INTO docs(title, description, tags, body, path, tier, type, url, modified, servable)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(meta.get("title", "")), str(meta.get("description", "")),
         " ".join(map(str, meta.get("tags") or [])), for_bot(body), rel, tier,
         str(meta.get("type", "")), meta.get("resource") or "",
         str(meta.get("modified") or meta.get("timestamp") or ""),
         int(servable(rel, meta))))
    if tier == "concepts":
        con.executemany("INSERT INTO concept_sources VALUES (?,?)",
                        [(rel, s) for s in meta.get("sources") or [] if str(s).startswith("http")])


def reindex_all() -> int:
    with _lock, db() as con:
        con.execute("DELETE FROM docs")
        con.execute("DELETE FROM concept_sources")
        n = 0
        for p in sorted(KB_DIR.glob("**/*.md")):
            if p.name != "index.md":
                _index_one(con, p)
                n += 1
    return n


STOPWORDS = set("""a an and are as at be been being but by can could did do does for from had has
have how i if in into is it its me my of on or our please so tell that the their them there these
they this to was we were what when where which who whom whose why will with would you your about
any anyone give get got know need want received receive won win""".split())


def query_terms(q: str) -> list[str]:
    """Visitors type sentences; keep only the content words."""
    words = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 1]
    terms = [t for t in words if t not in STOPWORDS] or words
    return list(dict.fromkeys(terms))[:12]


def query_passes(q: str) -> list[str]:
    """Pass 1: pages matching all terms or all-but-one, ranked together by BM25 (one
    stray word in a visitor's sentence must not hide the right page; full matches
    still score higher). Pass 2: any term, to fill up."""
    terms = [f'"{t}"' for t in query_terms(q)]
    if not terms:
        return []
    if len(terms) >= 3:
        passes = [" OR ".join("(" + " AND ".join(terms[:i] + terms[i + 1:]) + ")"
                              for i in range(len(terms)))]
    else:
        passes = [" AND ".join(terms)]
    if len(terms) >= 2:
        passes.append(" OR ".join(terms))
    return passes


# ---------------------------------------------------------------- git history
def git_commit(message: str) -> None:
    if not GIT:
        return
    g = ["git", "-C", str(KB_DIR), "-c", "user.name=acr-knowledge", "-c", "user.email=kb@localhost"]
    if not (KB_DIR / ".git").exists():
        subprocess.run(g + ["init", "-q"], check=False)
    subprocess.run(g + ["add", "-A"], check=False)
    subprocess.run(g + ["commit", "-q", "-m", message], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextmanager
def write_lock():
    """Serialise writers across uvicorn workers (sync jobs may overlap)."""
    KB_INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    with open(KB_INDEX_DB.with_suffix(".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def mark_concepts_stale(con: sqlite3.Connection, url: Optional[str]) -> list[str]:
    """A source page changed/vanished -> every concept built from it needs recompiling."""
    if not url:
        return []
    variants = {url, url.rstrip("/"), url.rstrip("/") + "/"}
    rows = con.execute(f"SELECT DISTINCT concept FROM concept_sources WHERE url IN "
                       f"({','.join('?' * len(variants))})", tuple(variants)).fetchall()
    marked = []
    for (rel,) in rows:
        p = KB_DIR / rel
        if not p.exists():
            continue
        meta, body = okf.split_frontmatter(p.read_text(encoding="utf-8"))
        if meta.get("status") != "stale":
            meta["status"] = "stale"
            okf.write_atomic(p, okf.dump(meta, body))
            _index_one(con, p)
        marked.append(rel)
    return marked


# ---------------------------------------------------------------- read routes
_ctx_cache: dict = {}


@router.get("/context", dependencies=[Depends(require_token)])
def context():
    """Static prompt prefix: root index (approved concepts only) + every FAQ answer.
    Identical across calls until the bundle changes -> Gemini caches it."""
    files = [KB_DIR / "index.md"] + sorted((KB_DIR / "faq").glob("*.md"))
    sig = tuple((str(f), f.stat().st_mtime_ns) for f in files if f.exists())
    concepts_sig = tuple(sorted((p.name, p.stat().st_mtime_ns) for p in (KB_DIR / "concepts").glob("*.md"))) \
        if (KB_DIR / "concepts").exists() else ()
    key = (sig, concepts_sig, ALLOW_DRAFTS)
    if _ctx_cache.get("key") == key:
        return _ctx_cache["value"]

    parts = []
    _, root = okf.split_frontmatter((KB_DIR / "index.md").read_text(encoding="utf-8"))
    keep = []
    for line in root.splitlines():
        m = re.search(r"\]\((concepts/[^)]+\.md)\)", line)
        if m:
            cm, _ = okf.split_frontmatter((KB_DIR / m.group(1)).read_text(encoding="utf-8")) \
                if (KB_DIR / m.group(1)).exists() else ({}, "")
            if not servable(m.group(1), cm):
                continue
        keep.append(line)
    if not any("](concepts/" in ln for ln in keep):
        keep = [ln if ln.strip() != "## Concepts" else "## Concepts\n\n- (none approved yet)" for ln in keep]
    parts.append("\n".join(keep).strip())
    for f in sorted((KB_DIR / "faq").glob("*.md")):
        if f.name == "index.md":
            continue
        _, body = okf.split_frontmatter(f.read_text(encoding="utf-8"))
        parts.append(f"<!-- file: faq/{f.name} -->\n{body.strip()}")
    text = "\n\n---\n\n".join(parts)
    value = {"version": hashlib.sha256(text.encode()).hexdigest()[:12],
             "chars": len(text), "text": text}
    _ctx_cache.update(key=key, value=value)
    return value


@router.get("/page", dependencies=[Depends(require_token)])
def page(path: str = Query(..., description="e.g. concepts/ways-of-giving.md"),
         offset: int = Query(0, ge=0), max_chars: int = Query(PAGE_CHARS, ge=1000, le=120_000)):
    p = resolve(path)
    if not p.exists():
        raise HTTPException(404, f"no such page: {path}")
    rel = rel_of(p)
    meta, body = okf.split_frontmatter(p.read_text(encoding="utf-8"))
    if not servable(rel, meta):
        raise HTTPException(404, f"page not approved yet: {rel}")
    body = for_bot(body)
    window = body[offset:offset + max_chars]
    end = offset + len(window)
    if end < len(body):                       # cut at a heading/paragraph, not mid-sentence
        cut = max(window.rfind("\n#"), window.rfind("\n\n"))
        if cut > max_chars // 2:
            window, end = window[:cut], offset + cut
    return {
        "path": rel, "title": meta.get("title"), "type": meta.get("type"),
        "url": meta.get("resource"), "modified": meta.get("modified") or meta.get("timestamp"),
        "verified": meta.get("verified"), "status": meta.get("status"),
        "content": window, "offset": offset, "total_chars": len(body),
        "next_offset": end if end < len(body) else None,
    }


@router.get("/search", dependencies=[Depends(require_token)])
def search(q: str = Query(..., min_length=2), type: Optional[str] = None,
           tier: Optional[str] = Query(None, pattern="^(concepts|faq|sources)$"),
           limit: int = Query(8, ge=1, le=25)):
    """Which whole pages to open. Strict match first, looser passes fill up."""
    where = ["docs MATCH ?", "servable = 1"]
    args: list = []
    if type:
        where.append("type = ?"); args.append(type)
    if tier:
        where.append("tier = ?"); args.append(tier)
    sql = ("SELECT path, title, description, url, modified, type, tier,"
           " bm25(docs, 10.0, 5.0, 3.0, 1.0) AS score,"
           " snippet(docs, 3, '**', '**', ' … ', 18) AS snip"
           f" FROM docs WHERE {' AND '.join(where)} ORDER BY score LIMIT ?")
    seen, hits = set(), []
    with db() as con:
        for pass_no, fq in enumerate(query_passes(q)):
            if len(hits) >= limit:
                break
            for r in con.execute(sql, [fq, *args, limit * 3]).fetchall():
                if r[0] in seen:
                    continue
                seen.add(r[0])
                hits.append({"path": r[0], "title": r[1], "description": r[2], "url": r[3] or None,
                             "modified": r[4] or None, "type": r[5], "tier": r[6],
                             "snippet": r[8], "_score": r[7] * TIER_BOOST.get(r[6], 1.0),
                             "_pass": pass_no})
    hits.sort(key=lambda h: (h["_pass"], h["_score"]))
    for h in hits:
        h.pop("_score"); h.pop("_pass")
    return {"query": q, "results": hits[:limit]}


@router.get("/list", dependencies=[Depends(require_token)])
def listing(folder: str = Query(..., description="e.g. sources/awards, faq, concepts")):
    folder = folder.strip("/")
    if folder == "concepts":
        rows = []
        for p in sorted((KB_DIR / "concepts").glob("*.md")):
            m, _ = okf.split_frontmatter(p.read_text(encoding="utf-8"))
            if servable(rel_of(p), m):
                rows.append(f"- [{m.get('title', p.stem)}]({p.name}) — {m.get('description', '')}")
        return {"folder": folder, "content": "# Topic pages\n\n" + ("\n".join(rows) or "- (none approved yet)")}
    p = resolve(f"{folder}/index.md")
    if not p.exists():
        raise HTTPException(404, f"no listing for {folder}")
    _, body = okf.split_frontmatter(p.read_text(encoding="utf-8"))
    return {"folder": folder, "content": body.strip()}


# ---------------------------------------------------------------- write routes
class SourceIn(BaseModel):
    folder: str = Field(..., pattern=r"^[a-z0-9_]+$", description="WP type, e.g. page, news, awards")
    slug: str = Field(..., pattern=r"^[A-Za-z0-9._-]+$")
    markdown: str = Field(..., min_length=1)
    url: str
    title: Optional[str] = None
    post_id: Optional[int] = None
    published: Optional[str] = None
    modified: Optional[str] = None
    tags: list[str] = []


@router.put("/source", dependencies=[Depends(require_token)])
def put_source(inp: SourceIn):
    """Sync job (Workflow A): one changed WP item -> sources/<folder>/<slug>.md.
    Replaces the file, refreshes the folder listing and search index, and flags every
    concept built from this URL as stale. No chunking, no embedding."""
    body = inp.markdown.strip()
    if not body.startswith("# "):
        body = f"# {inp.title or inp.slug}\n\n{body}"
    meta = okf.source_doc({"url": inp.url, "title": inp.title, "post_id": inp.post_id,
                           "published": inp.published, "modified": inp.modified,
                           "tags": inp.tags}, body, inp.folder)
    p = resolve(f"sources/{inp.folder}/{inp.slug}")
    with write_lock():
        existed = p.exists()
        okf.write_atomic(p, okf.dump(meta, body))
        okf.write_folder_index(p.parent)
        with _lock, db() as con:
            _index_one(con, p)
            stale = mark_concepts_stale(con, inp.url)
        if not existed:
            okf.write_root_index(KB_DIR)
        git_commit(f"{'update' if existed else 'add'} {rel_of(p)}")
    return {"path": rel_of(p), "created": not existed, "stale_concepts": stale}


class PageIn(BaseModel):
    path: str
    content: str = Field(..., min_length=10, description="full file incl. OKF frontmatter")


@router.put("/page", dependencies=[Depends(require_token)])
def put_page(inp: PageIn):
    """Write a whole OKF file (concept recompiles, team-approved edits)."""
    meta, _ = okf.split_frontmatter(inp.content)
    if not meta.get("type"):
        raise HTTPException(422, "content must start with OKF frontmatter containing 'type'")
    p = resolve(inp.path)
    with write_lock():
        existed = p.exists()
        okf.write_atomic(p, inp.content if inp.content.endswith("\n") else inp.content + "\n")
        with _lock, db() as con:
            _index_one(con, p)
        if p.parent.parent.name == "sources":
            okf.write_folder_index(p.parent)
        okf.write_root_index(KB_DIR)
        git_commit(f"{'update' if existed else 'add'} {rel_of(p)}")
    return {"path": rel_of(p), "created": not existed}


@router.delete("/page", dependencies=[Depends(require_token)])
def delete_page(path: str):
    """Sync job (Workflow A2): page unpublished on WordPress."""
    p = resolve(path)
    if not p.exists():
        raise HTTPException(404, f"no such page: {path}")
    meta, _ = okf.split_frontmatter(p.read_text(encoding="utf-8"))
    rel = rel_of(p)
    with write_lock():
        p.unlink()
        with _lock, db() as con:
            con.execute("DELETE FROM docs WHERE path = ?", (rel,))
            con.execute("DELETE FROM concept_sources WHERE concept = ?", (rel,))
            stale = mark_concepts_stale(con, meta.get("resource"))
        if p.parent.parent.name == "sources":
            okf.write_folder_index(p.parent)
        okf.write_root_index(KB_DIR)
        git_commit(f"delete {rel}")
    return {"deleted": rel, "stale_concepts": stale}


class FaqIn(BaseModel):
    rows: list[dict] = Field(..., min_length=1, description=f"keys: {', '.join(okf.FAQ_FIELDS)}")
    sheet_name: str = "Chatbot FAQ ACR"


@router.put("/faq", dependencies=[Depends(require_token)])
def put_faq(inp: FaqIn):
    """Sync job (Workflow B): the whole FAQ sheet -> faq/ (full refresh)."""
    with write_lock():
        try:
            cats = okf.write_faq(inp.rows, KB_DIR / "faq", inp.sheet_name)
        except ValueError as e:
            raise HTTPException(422, str(e))
        okf.write_root_index(KB_DIR)
        with _lock, db() as con:
            con.execute("DELETE FROM docs WHERE tier = 'faq'")
            for p in (KB_DIR / "faq").glob("*.md"):
                _index_one(con, p)
        git_commit("refresh FAQ from sheet")
    return {"categories": len(cats), "answers": sum(cats.values())}


@router.post("/reindex", dependencies=[Depends(require_token)])
def reindex():
    with write_lock():
        okf.write_root_index(KB_DIR)
        n = reindex_all()
    return {"indexed": n}


@router.get("/stale", dependencies=[Depends(require_token)])
def stale():
    """Concepts whose sources changed since they were compiled (input to recompile job)."""
    out = []
    for p in sorted((KB_DIR / "concepts").glob("*.md")):
        m, _ = okf.split_frontmatter(p.read_text(encoding="utf-8"))
        if m.get("status") == "stale":
            out.append({"path": rel_of(p), "title": m.get("title"), "sources": m.get("sources")})
    return {"stale": out}


def startup() -> None:
    """Build the search index on boot if it is missing or empty."""
    if not KB_DIR.exists():
        return
    data_dir = KB_INDEX_DB.parent
    for d in (data_dir, KB_DIR):
        if not os.access(d, os.W_OK):
            st = d.stat()
            raise RuntimeError(
                f"{d} is owned by uid {st.st_uid}:{st.st_gid} but the service runs as "
                f"uid {os.getuid()}:{os.getgid()} and cannot write to it. On the host run: "
                f"sudo chown -R <KB_UID>:<KB_GID> <data dir>  (README step 2), then restart.")
    try:
        with db() as con:
            n = con.execute("SELECT count(*) FROM docs").fetchone()[0]
    except sqlite3.DatabaseError:
        KB_INDEX_DB.unlink(missing_ok=True)
        n = 0
    if n == 0:
        with write_lock():
            reindex_all()
