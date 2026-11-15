#!/usr/bin/env python3
"""Build the ACR chatbot knowledge bundle (OKF v0.2 layout) from local inputs.

Inputs  (never committed to git — knowledge-base data):
  --scrape  site-scrape/output/        one Markdown file per WP item (+ manifest.csv)
  --faq     "Chatbot FAQ ACR.xlsx"     team-authored FAQ sheet
Output:
  --out     knowledge/
    index.md                       root map (small; goes into the chat prompt)
    faq/<category>.md              one OKF file per FAQ category  (type: faq, verified)
    faq/index.md
    sources/<type>/<slug>.md       scraped pages, frontmatter normalised to OKF
    sources/<type>/index.md        per-type listing (agent browses these)
    concepts/                      compiled topic pages (Phase 2) — never touched here

Re-runnable: regenerates faq/ and sources/ from scratch, leaves concepts/ alone.

  uv run --with pyyaml --with openpyxl python -I tools/build_okf.py \
      --scrape site-scrape/output --faq "Chatbot FAQ ACR.xlsx" --out knowledge
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import re
import shutil
from pathlib import Path

import openpyxl
import yaml

OKF_VERSION = "0.2"
TODAY = dt.date.today().isoformat()

# scrape folder -> (human label, OKF type). Unknown folders fall back to their name.
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
# frontmatter keys that become OKF tags
TAG_KEYS = ("award_category", "news_year", "event_category", "storycategory")
LINK_ONLY = re.compile(r"^\s*(\[[^\]]*\]\([^)]*\)\s*)+$")


# ---------------------------------------------------------------- helpers
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
        p = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", p)        # drop link targets
        p = re.sub(r"[*_`]+", "", p)
        return clip(p, n)
    return ""


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "misc"


def as_date(v) -> str:
    return str(v)[:10] if v else ""


# ---------------------------------------------------------------- sources
def build_sources(scrape: Path, out: Path) -> dict[str, list[dict]]:
    listing: dict[str, list[dict]] = collections.defaultdict(list)
    for src in sorted(scrape.glob("*/*.md")):
        folder = src.parent.name
        label, okf_type = TYPES.get(folder, (folder, folder))
        meta, body = split_frontmatter(src.read_text(encoding="utf-8"))
        title = str(meta.get("title") or src.stem)
        tags = sorted({str(t) for k in TAG_KEYS for t in (meta.get(k) or [])})
        ts = as_date(meta.get("modified") or meta.get("published"))
        desc = describe(body, 120 if folder == "awards" else 200)
        if folder == "awards":                            # "B. Tech., 1984, Electrical Engg."
            m = re.search(r"^# .+\n+([^\n#]{4,90})\n", body, re.M)
            if m and re.search(r"(19|20)\d{2}", m.group(1)):
                desc = clip(m.group(1).strip("() ") + " — " + desc, 160)

        okf = {
            "type": okf_type,
            "title": title,
            "description": desc,
            "resource": meta.get("url"),
            "tags": tags or None,
            "timestamp": ts or None,
            # OKF v0.2 provenance / trust
            "sources": [meta["url"]] if meta.get("url") else None,
            "generated": False,          # verbatim site content, not LLM-written
            "verified": True,            # published by ACR on the official site
            "status": "current",
            # original scrape fields kept for sync (post_id = WP change detection)
            "post_id": meta.get("post_id"),
            "published": as_date(meta.get("published")) or None,
            "modified": as_date(meta.get("modified")) or None,
            "linked_from": meta.get("linked_from"),
        }
        okf = {k: v for k, v in okf.items() if v not in (None, "", [])}
        dest = out / "sources" / folder / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(dump(okf, body), encoding="utf-8")
        listing[folder].append({**okf, "file": src.name, "chars": len(body)})
    return listing


def write_source_indexes(listing: dict[str, list[dict]], out: Path) -> None:
    for folder, docs in listing.items():
        label, _ = TYPES.get(folder, (folder, folder))
        lines = [f"# {label}", "",
                 f"{len(docs)} pages. Open a page by its link; newest first unless grouped.", ""]

        def row(d: dict) -> str:
            date = "" if folder == "awards" else (d.get("published") or d.get("timestamp") or "")
            bits = [f"- [{d['title']}]({d['file']})"]
            if date:
                bits.append(f"({date})")
            if d.get("description"):
                bits.append(f"— {clip(d['description'], 150 if folder == 'awards' else 110)}")
            return " ".join(bits)

        def surname(d: dict) -> str:
            name = re.sub(r"\([^)]*\)", "", d["title"].lower())
            return re.sub(r"^(mr|ms|mrs|dr|prof|late|shri|smt)\.?\s+", "", name).split()[-1:]

        group_key = {"awards": "tags", "news": "tags", "events_list": "tags",
                     "allstory": "tags"}.get(folder)
        if group_key:
            groups: dict[str, list[dict]] = collections.defaultdict(list)
            for d in docs:
                for t in d.get("tags") or ["Other"]:
                    groups[t].append(d)
            for g in sorted(groups, reverse=(folder == "news")):
                lines += ["", f"## {g}", ""]
                key = surname if folder == "awards" else (
                    lambda d: (d.get("published") or "", d["title"]))
                for d in sorted(groups[g], key=key, reverse=folder != "awards"):
                    lines.append(row(d))
        else:
            for d in sorted(docs, key=lambda d: d.get("timestamp") or "", reverse=True):
                lines.append(row(d))

        meta = {"type": "index", "title": f"Index — {label}",
                "description": f"Listing of all {len(docs)} {label.lower()} pages.",
                "generated": True, "timestamp": TODAY}
        (out / "sources" / folder / "index.md").write_text(dump(meta, "\n".join(lines)),
                                                           encoding="utf-8")


# ---------------------------------------------------------------- FAQ
def build_faq(xlsx: Path, out: Path) -> dict[str, list[dict]]:
    ws = openpyxl.load_workbook(xlsx, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    head = [str(h or "").strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if n in h:
                    return i
        raise SystemExit(f"FAQ sheet: no column like {names}; header = {head}")

    ci, si, qi = col("category"), col("subcategory"), col("user question")
    ai, ans, pg = col("alternative"), col("answer"), col("relevant", "page")

    cats: dict[str, list[dict]] = collections.defaultdict(list)
    cat = sub = ""
    for r in rows[1:]:
        cell = lambda i: re.sub(r"[ \t]+", " ", str(r[i] or "")).strip()
        cat, sub = cell(ci) or cat, cell(si) or sub       # forward-fill merged cells
        q, a = cell(qi), cell(ans)
        if not q or not a:
            continue
        cats[cat].append({"q": q, "alt": cell(ai), "a": a, "sub": sub, "page": cell(pg)})

    fdir = out / "faq"
    fdir.mkdir(parents=True, exist_ok=True)
    for cat, items in cats.items():
        body = [f"# FAQ — {cat}", "",
                "Team-approved answers. These take priority over website pages.", ""]
        for it in items:
            body += [f"## {it['q']}", ""]
            if it["alt"]:
                body += [f"*Also asked as:* {it['alt']}", ""]
            body += [it["a"], ""]
            meta_bits = [f"Topic: {cat} › {it['sub']}" if it["sub"] else f"Topic: {cat}"]
            if it["page"]:
                meta_bits.append(f"Related page: {it['page']}")
            body += ["*" + " · ".join(meta_bits) + "*", ""]
        meta = {
            "type": "faq",
            "title": f"FAQ — {cat}",
            "description": clip(f"{len(items)} team-approved answers about {cat}: "
                                + "; ".join(i["q"] for i in items[:4]), 220),
            "tags": sorted({i["sub"] for i in items if i["sub"]}),
            "timestamp": TODAY,
            "sources": [xlsx.name],
            "generated": False,
            "verified": True,
            "status": "current",
        }
        (fdir / f"{slugify(cat)}.md").write_text(dump(meta, "\n".join(body)), encoding="utf-8")

    idx = ["# FAQ categories", ""] + [
        f"- [{c}]({slugify(c)}.md) — {len(v)} answers" for c, v in
        sorted(cats.items(), key=lambda kv: -len(kv[1]))]
    (fdir / "index.md").write_text(dump(
        {"type": "index", "title": "Index — FAQ", "generated": True, "timestamp": TODAY,
         "description": f"{sum(map(len, cats.values()))} team-approved Q&As in {len(cats)} categories."},
        "\n".join(idx)), encoding="utf-8")
    return cats


# ---------------------------------------------------------------- root index
def write_root_index(out: Path, listing, cats) -> None:
    concepts = sorted((out / "concepts").glob("*.md")) if (out / "concepts").exists() else []
    lines = ["# ACR knowledge base", "",
             "Authority order: **FAQ** (team-approved) > **concepts** (compiled, verified) > "
             "**sources** (website pages; newest `modified` wins on conflict).", "",
             "## FAQ", "", f"- [All FAQ categories](faq/index.md) — "
             f"{sum(map(len, cats.values()))} answers in {len(cats)} categories", ""]
    lines += ["## Concepts", ""]
    if concepts:
        for c in concepts:
            if c.name == "index.md":
                continue
            m, _ = split_frontmatter(c.read_text(encoding="utf-8"))
            lines.append(f"- [{m.get('title', c.stem)}](concepts/{c.name}) — {m.get('description', '')}")
    else:
        lines.append("- (not compiled yet)")
    lines += ["", "## Website sources", ""]
    for folder in sorted(listing, key=lambda f: -len(listing[f])):
        label, _ = TYPES.get(folder, (folder, folder))
        lines.append(f"- [{label}](sources/{folder}/index.md) — {len(listing[folder])} pages")
    meta = {"type": "index", "title": "ACR knowledge base",
            "description": "Root map of the IIT Bombay ACR chatbot knowledge bundle (OKF "
                           f"v{OKF_VERSION}).", "generated": True, "timestamp": TODAY}
    (out / "index.md").write_text(dump(meta, "\n".join(lines)), encoding="utf-8")


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scrape", type=Path, required=True)
    ap.add_argument("--faq", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    for sub in ("sources", "faq"):                        # regenerate; keep concepts/
        shutil.rmtree(a.out / sub, ignore_errors=True)
    a.out.mkdir(parents=True, exist_ok=True)

    listing = build_sources(a.scrape, a.out)
    write_source_indexes(listing, a.out)
    cats = build_faq(a.faq, a.out)
    write_root_index(a.out, listing, cats)

    # report
    if (a.scrape / "manifest.csv").exists():
        rows = list(csv.DictReader(open(a.scrape / "manifest.csv", encoding="utf-8")))
        skipped = collections.Counter(r["type"] for r in rows if r["status"] == "empty")
        print("scrape items skipped as empty:", dict(skipped.most_common()))
    n_src = sum(map(len, listing.values()))
    no_desc = sum(1 for d in (x for v in listing.values() for x in v) if not d.get("description"))
    print(f"sources: {n_src} files in {len(listing)} types ({no_desc} without description)")
    print(f"faq: {sum(map(len, cats.values()))} Q&As in {len(cats)} categories")
    print(f"wrote {a.out.resolve()}")


if __name__ == "__main__":
    main()
