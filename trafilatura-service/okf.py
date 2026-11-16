"""OKF v0.2 helpers shared by the knowledge service and tools/build_okf.py.

One place decides how a scraped page, a FAQ category, a folder listing and the root
index are written, so the offline builder and the live sync produce identical files.
"""
from __future__ import annotations

import collections
import datetime as dt
import os
import re
import tempfile
from pathlib import Path

import yaml

OKF_VERSION = "0.2"

# source folder -> (human label, OKF type). Unknown folders fall back to their name.
TYPES = {
    "page": ("Website pages", "page"),
    "awards": ("Alumni awards (one page per awardee)", "award"),
    "news": ("News", "news"),
    "post": ("Posts / profiles", "post"),
    "allstory": ("Stories", "story"),
    "events_list": ("Events (past and upcoming)", "event"),
    "newsletter": ("Newsletters", "newsletter"),
    "csr_newsletter": ("CSR newsletters", "newsletter"),
    "batch_legacy": ("Batch legacy (class gifts)", "batch-legacy"),
    "legacy_project": ("Legacy projects", "legacy-project"),
    "campaign": ("Campaigns", "campaign"),
    "causes": ("Causes", "cause"),
    "csr": ("CSR partners", "csr"),
    "casestudy": ("Case studies", "case-study"),
    "media_gallery": ("Media gallery", "media"),
    "rosters": ("Donor recognition rosters (giving circles)", "roster"),
    "pdf": ("PDF documents (annual reports, brochures)", "document"),
}
TAG_KEYS = ("award_category", "news_year", "event_category", "storycategory")
GROUPED = {"awards", "news", "events_list", "allstory"}      # index grouped by tag
LINK_ONLY = re.compile(r"^\s*(\[[^\]]*\]\([^)]*\)\s*)+$")


def today() -> str:
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- text helpers
def split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return yaml.safe_load(text[4:end]) or {}, text[end + 5:]
    return {}, text


def dump(meta: dict, body: str) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{fm}---\n\n{body.strip()}\n"


def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def describe(body: str, n: int = 200) -> str:
    """First real prose paragraph: skip headings, link-only lines, list items, dates."""
    for para in re.split(r"\n\s*\n", body):
        p = para.strip()
        if (not p or p.startswith(("#", "|", "- ", "* ", "!", ">", "```"))
                or LINK_ONLY.match(p) or len(p) < 60):
            continue
        p = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", p)
        p = re.sub(r"[*_`]+", "", p)
        return clip(p, n)
    return ""


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "misc"


def as_date(v) -> str:
    return str(v)[:10] if v else ""


def write_atomic(path: Path, text: str) -> None:
    """Readers never see a half-written page (the chat may read while sync writes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- source pages
def source_doc(meta: dict, body: str, folder: str) -> dict:
    """Scrape/extract metadata + Markdown body -> OKF frontmatter for sources/<folder>/."""
    _, okf_type = TYPES.get(folder, (folder, folder))
    title = str(meta.get("title") or "").strip() or "Untitled"
    tags = meta.get("tags") or sorted({str(t) for k in TAG_KEYS for t in (meta.get(k) or [])})
    ts = as_date(meta.get("modified") or meta.get("published"))
    desc = describe(body, 120 if folder == "awards" else 200)
    if folder == "awards":                                # "B. Tech., 1984, Electrical Engg."
        m = re.search(r"^# .+\n+([^\n#]{4,90})\n", body, re.M)
        if m and re.search(r"(19|20)\d{2}", m.group(1)):
            desc = clip(m.group(1).strip("() ") + " — " + desc, 160)
    url = meta.get("url") or meta.get("resource")
    okf = {
        "type": okf_type,
        "title": title,
        "description": desc,
        "resource": url,
        "tags": list(tags) or None,
        "timestamp": ts or None,
        "sources": [url] if url else None,
        "generated": False,           # verbatim site content, not LLM-written
        "verified": True,             # published by ACR on the official site
        "status": "current",
        "post_id": meta.get("post_id"),
        "published": as_date(meta.get("published")) or None,
        "modified": as_date(meta.get("modified")) or None,
        "linked_from": meta.get("linked_from"),
    }
    return {k: v for k, v in okf.items() if v not in (None, "", [])}


def write_folder_index(folder_dir: Path) -> int:
    """(Re)write sources/<folder>/index.md from the frontmatter of the pages in it."""
    folder = folder_dir.name
    label, _ = TYPES.get(folder, (folder, folder))
    docs = []
    for p in sorted(folder_dir.glob("*.md")):
        if p.name == "index.md":
            continue
        meta, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        docs.append({**meta, "file": p.name, "title": str(meta.get("title", p.stem))})

    def row(d: dict) -> str:
        date = "" if folder == "awards" else (d.get("published") or d.get("timestamp") or "")
        bits = [f"- [{d['title']}]({d['file']})"]
        if date:
            bits.append(f"({date})")
        if d.get("description"):
            bits.append(f"— {clip(str(d['description']), 150 if folder == 'awards' else 110)}")
        return " ".join(bits)

    def surname(d: dict):
        name = re.sub(r"\([^)]*\)", "", d["title"].lower())
        return re.sub(r"^(mr|ms|mrs|dr|prof|late|shri|smt)\.?\s+", "", name).split()[-1:]

    lines = [f"# {label}", "",
             f"{len(docs)} pages. Open a page by its link; newest first unless grouped.", ""]
    if folder in GROUPED:
        groups: dict[str, list[dict]] = collections.defaultdict(list)
        for d in docs:
            for t in d.get("tags") or ["Other"]:
                groups[str(t)].append(d)
        for g in sorted(groups, reverse=(folder == "news")):
            lines += ["", f"## {g}", ""]
            key = surname if folder == "awards" else (
                lambda d: (str(d.get("published") or ""), d["title"]))
            for d in sorted(groups[g], key=key, reverse=folder != "awards"):
                lines.append(row(d))
    else:
        for d in sorted(docs, key=lambda d: str(d.get("timestamp") or ""), reverse=True):
            lines.append(row(d))

    meta = {"type": "index", "title": f"Index — {label}",
            "description": f"Listing of all {len(docs)} {label.lower()} pages.",
            "generated": True, "timestamp": today()}
    write_atomic(folder_dir / "index.md", dump(meta, "\n".join(lines)))
    return len(docs)


# ---------------------------------------------------------------- FAQ
FAQ_FIELDS = ("category", "subcategory", "question", "alternative", "answer", "page")


def write_faq(rows: list[dict], faq_dir: Path, sheet_name: str) -> dict[str, int]:
    """rows: dicts with FAQ_FIELDS (blank category/subcategory = same as previous row).
    Regenerates faq/ completely (stale categories removed). Returns {category: count}."""
    cats: dict[str, list[dict]] = collections.defaultdict(list)
    cat = sub = ""
    for r in rows:
        cell = lambda k: re.sub(r"[ \t]+", " ", str(r.get(k) or "")).strip()
        cat, sub = cell("category") or cat, cell("subcategory") or sub
        q, a = cell("question"), cell("answer")
        if q and a:
            cats[cat or "General"].append(
                {"q": q, "alt": cell("alternative"), "a": a, "sub": sub, "page": cell("page")})
    if not cats:
        raise ValueError("no FAQ rows with both question and answer")

    faq_dir.mkdir(parents=True, exist_ok=True)
    wanted = {f"{slugify(c)}.md" for c in cats} | {"index.md"}
    for old in faq_dir.glob("*.md"):
        if old.name not in wanted:
            old.unlink()

    for cat, items in cats.items():
        body = [f"# FAQ — {cat}", "",
                "Team-approved answers. These take priority over website pages.", ""]
        for it in items:
            body += [f"## {it['q']}", ""]
            if it["alt"]:
                body += [f"*Also asked as:* {it['alt']}", ""]
            body += [it["a"], ""]
            bits = [f"Topic: {cat} › {it['sub']}" if it["sub"] else f"Topic: {cat}"]
            if it["page"]:
                bits.append(f"Related page: {it['page']}")
            body += ["*" + " · ".join(bits) + "*", ""]
        meta = {
            "type": "faq", "title": f"FAQ — {cat}",
            "description": clip(f"{len(items)} team-approved answers about {cat}: "
                                + "; ".join(i["q"] for i in items[:4]), 220),
            "tags": sorted({i["sub"] for i in items if i["sub"]}),
            "timestamp": today(), "sources": [sheet_name],
            "generated": False, "verified": True, "status": "current",
        }
        write_atomic(faq_dir / f"{slugify(cat)}.md", dump(meta, "\n".join(body)))

    idx = ["# FAQ categories", ""] + [
        f"- [{c}]({slugify(c)}.md) — {len(v)} answers"
        for c, v in sorted(cats.items(), key=lambda kv: -len(kv[1]))]
    write_atomic(faq_dir / "index.md", dump(
        {"type": "index", "title": "Index — FAQ", "generated": True, "timestamp": today(),
         "description": f"{sum(map(len, cats.values()))} team-approved Q&As in {len(cats)} categories."},
        "\n".join(idx)))
    return {c: len(v) for c, v in cats.items()}


# ---------------------------------------------------------------- root index
def write_root_index(root: Path) -> None:
    faq_n = cat_n = 0
    for p in (root / "faq").glob("*.md"):
        if p.name != "index.md":
            cat_n += 1
            faq_n += p.read_text(encoding="utf-8").count("\n## ")
    lines = ["# ACR knowledge base", "",
             "Authority order: **FAQ** (team-approved) > **concepts** (compiled, verified) > "
             "**sources** (website pages; newest `modified` wins on conflict).", "",
             "## FAQ", "", f"- [All FAQ categories](faq/index.md) — "
             f"{faq_n} answers in {cat_n} categories", "", "## Concepts", ""]
    concepts = sorted(p for p in (root / "concepts").glob("*.md") if p.name != "index.md") \
        if (root / "concepts").exists() else []
    for c in concepts:
        m, _ = split_frontmatter(c.read_text(encoding="utf-8"))
        lines.append(f"- [{m.get('title', c.stem)}](concepts/{c.name}) — {m.get('description', '')}")
    if not concepts:
        lines.append("- (not compiled yet)")
    lines += ["", "## Website sources", ""]
    folders = [d for d in (root / "sources").iterdir() if d.is_dir()] \
        if (root / "sources").exists() else []
    counts = {d.name: sum(1 for p in d.glob("*.md") if p.name != "index.md") for d in folders}
    for f in sorted(counts, key=lambda f: (-counts[f], f)):
        label, _ = TYPES.get(f, (f, f))
        lines.append(f"- [{label}](sources/{f}/index.md) — {counts[f]} pages")
    meta = {"type": "index", "title": "ACR knowledge base",
            "description": "Root map of the IIT Bombay ACR chatbot knowledge bundle (OKF "
                           f"v{OKF_VERSION}).", "generated": True, "timestamp": today()}
    write_atomic(root / "index.md", dump(meta, "\n".join(lines)))
