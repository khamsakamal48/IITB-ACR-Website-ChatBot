#!/usr/bin/env python3
"""Scrape every public page (and linked PDF) of acr.iitbombay.org into RAG-ready Markdown.

    pip install -r requirements.txt
    python scrape_site.py                       # full run -> ./output
    python scrape_site.py --types csr,news --limit 5
    python scrape_site.py --selftest

URL list comes from the WordPress REST API (no sitemap on this site). Bodies come from
the LIVE page HTML, because the theme renders most types from ACF fields and
`content.rendered` is empty. Extraction = prune site chrome (same xpaths as
trafilatura-service/app.py) then convert the WHOLE body with markdownify. Trafilatura was
tried first but keeps only one "main" subtree and silently drops modals ("Read More"
project text) and testimonial blocks. Template text that survives pruning is removed by a
corpus-wide repeated-line pass instead.
"""
import argparse
import csv
import hashlib
import html
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from lxml import html as lxml_html
from markdownify import markdownify
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "trafilatura-service"))
from app import BOILERPLATE_XPATH  # noqa: E402  (tuned chrome xpaths, shared with the service)

BASE = "https://acr.iitbombay.org"
API = f"{BASE}/wp-json/wp/v2"
# Icon-font glyphs (material-symbols) render as words: "keyboard_arrow_up", "reply_all"...
PRUNE_XPATH = BOILERPLATE_XPATH + [
    '//*[contains(@class,"material-symbols")]', "//script", "//style", "//noscript",
    "//svg", "//form", "//iframe", '//*[@aria-hidden="true"]']

SKIP_TYPES = {"attachment", "nav_menu_item", "wp_block", "wp_template", "wp_template_part",
              "wp_global_styles", "wp_navigation", "wp_font_family", "wp_font_face",
              "e-floating-buttons", "elementor_library", "common_section"}
# Name-only pages -> one roster file per type. Value = listing page used as citation url.
ROSTER_TYPES = {
    "annual_contributors": ("Annual Contributors", BASE + "/"),
    "dean_circle": ("Dean's Circle", BASE + "/deans-circle/"),
    "leadership_circle": ("Leadership Circle", BASE + "/leadership-circle/"),
    "giving_circle": ("Giving Circle", BASE + "/giving-circle/"),
    "chairman_circle": ("Chairman's Circle", BASE + "/chairmans-circle/"),
    "director_circle": ("Director's Circle", BASE + "/directors-circle/"),
}
JUNK_SLUG = re.compile(r"^(sample-page|testing-title.*|hello-world|test|search.*|.*thank-you.*|.*-error)$")
BLOCKLIST = {"/resources", "/icic-merchant-response"}  # mirrors acr_website_chatbot.kb_blocklist
MIN_CHARS = 100          # body shorter than this after cleaning = empty page
BOILER_MIN_DOCS = 30     # a line repeated verbatim in >= this many pages is template text
BOILER_FRAC = 0.02       # ...or in >= 2% of pages, whichever is larger
LISTING_SHARE = 0.5      # page whose text is mostly link cards to other scraped pages...
LISTING_MIN_CARDS = 10   # ...and has many of them = listing (small hubs like /decennial-reunion stay)
NEAR_DUP = 0.9           # PDFs sharing >= 90% of lines = re-uploaded copy of the same report

session = requests.Session()
session.headers["User-Agent"] = "ACR-KB-Scraper/1.0 (+IIT Bombay ACR chatbot)"
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504))))


# ---------- fetching ----------

def cached_get(url, cache: Path, refetch=False) -> bytes | None:
    f = cache / hashlib.sha1(url.encode()).hexdigest()
    if f.exists() and not refetch:
        return f.read_bytes()
    try:
        r = session.get(url, timeout=60)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    f.write_bytes(r.content)
    return r.content


def api_all(path, params=None):
    out, page = [], 1
    while True:
        r = session.get(f"{API}/{path}", params={**(params or {}), "per_page": 100, "page": page}, timeout=60)
        if r.status_code == 400:  # past last page
            break
        r.raise_for_status()
        out += r.json()
        if page >= int(r.headers.get("X-WP-TotalPages", 1)):
            break
        page += 1
    return out


def load_taxonomies():
    """{rest_base: {term_id: name}} for every taxonomy."""
    taxes = session.get(f"{API}/taxonomies", timeout=60).json()
    terms = {}
    for t in taxes.values():
        try:
            terms[t["rest_base"]] = {x["id"]: html.unescape(x["name"])
                                     for x in api_all(t["rest_base"], {"_fields": "id,name"})}
        except requests.HTTPError:
            pass
    return terms


def load_items(types, terms):
    all_types = session.get(f"{API}/types", timeout=60).json()
    fields = "id,slug,link,title,date,modified,type," + ",".join(terms)
    items = []
    for name, t in all_types.items():
        if name in SKIP_TYPES or (types and name not in types):
            continue
        for it in api_all(t["rest_base"], {"_fields": fields}):
            path = urlparse(it["link"]).path.rstrip("/")
            if JUNK_SLUG.match(it["slug"]) or path in BLOCKLIST:
                continue
            it["title"] = html.unescape(it["title"]["rendered"]).strip()
            it["tags"] = {tax: [terms[tax][i] for i in it[tax] if i in terms[tax]]
                          for tax in terms if it.get(tax)}
            it["tags"] = {k: v for k, v in it["tags"].items() if v}
            items.append(it)
    return items


# ---------- cleaning ----------

def html_to_md(raw: str) -> str:
    tree = lxml_html.fromstring(raw)
    for x in PRUNE_XPATH:
        for el in tree.xpath(x):
            if el.getparent() is not None:
                el.drop_tree()
    body = tree.find("body")
    return markdownify(lxml_html.tostring(body if body is not None else tree, encoding="unicode"),
                       heading_style="ATX", strip=["img"])


IMG_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
EMPTY_LINK = re.compile(r"\[\s*\]\([^)]*\)")
REL_LINK = re.compile(r"\]\((/[^)\s]*)\)")
HEADING = re.compile(r"^(#{1,6})\s+")
PDF_PLACEHOLDER = re.compile(r"^\*{0,2}==> picture .* omitted <==\*{0,2}$|^-{3,}$|^\*{3,}$")
# Related-content carousels and sitewide CTA blocks: other pages' teasers leaking into
# every story would make retrieval return the wrong page.
DROP_SECTION = re.compile(r"(you may also like|engage with us|give back to make a difference|"
                          r"download (annual report|project catalogue)|checkout .*annual report|"
                          r"annual activity report 20\d\d-\d\d)$", re.I)
LINK_TEXT = re.compile(r"\[([^\]]*)\]", re.S)
DOUBLED = re.compile(r"^(.+?) \1$")          # theme renders button labels twice (hover)
ANCHOR_LINK = re.compile(r"\[([^\]]*)\]\(#[^)]*\)")  # in-page/modal anchors: keep text only


def _link_text(m):
    t = re.sub(r"(^|\s)#{1,6}\s", r"\1", " ".join(m.group(1).split()))  # "[#### Card title ...]"
    return "[" + DOUBLED.sub(r"\1", t) + "]"


def collapse_name_headings(lines):
    """Runs of >=3 same-level headings whose body is <=1 short line (donor/batch name lists)
    become one bullet list, so the chunker doesn't emit one tiny chunk per person."""
    secs, cur = [], [None, []]
    for l in lines:
        if HEADING.match(l):
            secs.append(cur)
            cur = [l, []]
        else:
            cur[1].append(l)
    secs.append(cur)

    def is_name(sec):
        h, b = sec
        b = [x for x in b if x.strip()]
        return (h is not None and HEADING.match(h).group(1) in ("###", "####", "#####")
                and len(h) < 90 and len(b) <= 1 and all(len(x) < 90 for x in b))

    out, i = [], 0
    while i < len(secs):
        j = i
        lvl = secs[i][0] and HEADING.match(secs[i][0]).group(1)
        while j < len(secs) and is_name(secs[j]) and HEADING.match(secs[j][0]).group(1) == lvl:
            j += 1
        if j - i >= 3:
            for h, b in secs[i:j]:
                d = next((x.strip() for x in b if x.strip()), "")
                out.append("- " + HEADING.sub("", h).strip() + (f" — {d}" if d else ""))
            out.append("")
            i = j
        else:
            h, b = secs[i]
            out += ([h] if h is not None else []) + b
            i += 1
    return out



def clean(md: str, title: str, base_url: str) -> str:
    """Normalize one page body. H1 is the title, every body heading is pushed below it."""
    md = re.sub(r"(?<=[.!?\"”’)])n(n ?)*n(?=\s|$)", "", md)  # literal "nn nn" from escaped \n\n
    md = re.sub(r"^\[[a-z_]+ [a-z_]+=[^\]]*\]$", "", md, flags=re.M)  # unrendered WP shortcodes
    md = LINK_TEXT.sub(_link_text, md)
    md = ANCHOR_LINK.sub(r"\1", md)
    out, last = [], None
    for line in md.splitlines():
        line = line.rstrip()
        if IMG_LINE.match(line) or PDF_PLACEHOLDER.match(line.strip()):
            continue
        if not line.strip():
            out.append("")
            continue
        line = EMPTY_LINK.sub("", line)
        line = re.sub(r"^(\s*)[-*] [•●▪]\s*", r"\1- ", line)  # "- • item" from PDFs
        line = re.sub(r"^(\s*)\* ", r"\1- ", line)
        line = REL_LINK.sub(lambda m: f"]({urljoin(base_url, m.group(1))})", line)
        if HEADING.match(line):
            hashes = HEADING.match(line).group(1)
            line = "#" * min(len(hashes) + 1, 6) + " " + line[len(hashes):].strip()
        if line.lstrip("# ").strip().lower() == title.lower() or re.fullmatch(r"#+", line.strip()):
            continue  # title repeated in body (we prepend our own H1) / empty heading
        if line.strip() == last:
            continue  # "Load More\n\nLoad More"
        last = line.strip()
        out.append(line)
    out = drop_sections(out, lambda h, body: not DROP_SECTION.match(h))
    out = collapse_name_headings(out)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return f"# {title}\n\n{body}\n" if body else f"# {title}\n"


def _boilerplate_key(line):
    s = line.strip()
    # headings ("Success Mantra" on every award bio) and table rows are structure/data;
    # contact lines must survive somewhere. Never strip these corpus-wide.
    if not s or s.startswith(("#", "|")) or s == "-" or "mailto:" in s or "tel:" in s:
        return None
    return s


def boilerplate_lines(docs, min_docs, frac):
    counts = Counter()
    for d in docs:
        counts.update({k for k in map(_boilerplate_key, d.splitlines()) if k})
    cut = max(min_docs, frac * len(docs))
    return {l for l, n in counts.items() if n >= cut}


def drop_sections(lines, keep_heading):
    """Remove each heading for which keep_heading(text, body) is False, with its body
    (everything until the next heading of the same or higher level)."""
    out, skip = [], None
    for i, l in enumerate(lines):
        m = HEADING.match(l)
        if m and (skip is None or len(m.group(1)) <= skip):
            skip = None
            lvl, body = len(m.group(1)), []
            for x in lines[i + 1:]:
                hm = HEADING.match(x)
                if hm and len(hm.group(1)) <= lvl:
                    break
                body.append(x)
            if not keep_heading(HEADING.sub("", l).strip(), body):
                skip = lvl
                continue
        if skip is None:
            out.append(l)
    return out


def strip_lines(md, bad):
    kept = [l for l in md.splitlines() if l.strip() not in bad]
    l0 = kept[0] if kept else ""
    # headings whose whole section became empty (sitewide CTA blocks) go too; H1 always stays
    kept = drop_sections(kept, lambda h, body: any(x.strip() for x in body) or l0 == f"# {h}")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"


CARD_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]\((https?://[^)\s]+)\)")  # allows "[…]" inside


def link_share(md, urls):
    """Fraction of the page that is link text pointing at other scraped pages (news/story
    index pages). Those cards duplicate the articles and would outrank them in retrieval."""
    body = md.split("\n", 1)[1] if "\n" in md else ""
    cards = [t for t, u in CARD_LINK.findall(body) if u in urls]
    return sum(map(len, cards)) / max(len(body), 1), len(cards)


def line_overlap(a, b):
    a, b = set(a.splitlines()) - {""}, set(b.splitlines()) - {""}
    return len(a & b) / max(min(len(a), len(b)), 1)


def body_len(md):
    return len(md.split("\n", 1)[1].strip()) if "\n" in md else 0


def pdf_links(raw: str, page_url: str):
    found = set()
    for h in re.findall(r'href=["\']([^"\']+\.pdf)(?:[?#][^"\']*)?["\']', raw, re.I):
        u = urljoin(page_url, html.unescape(h))
        if urlparse(u).netloc == urlparse(BASE).netloc:
            found.add(u)
    return found


def pdf_to_md(data: bytes) -> str:
    import pymupdf
    import pymupdf4llm
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = [p["text"] for p in pymupdf4llm.to_markdown(doc, page_chunks=True, show_progress=False)]
    # running headers/footers: lines on >= 30% of pages (min 3)
    bad = boilerplate_lines(pages, 3, 0.3) if len(pages) >= 3 else set()
    return "\n\n".join(strip_lines(p, bad) for p in pages)


# ---------- output ----------

def write_md(path: Path, meta: dict, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump({k: v for k, v in meta.items() if v not in (None, [], {})},
                        sort_keys=False, allow_unicode=True, width=1000)
    path.write_text(f"---\n{fm}---\n\n{body}", encoding="utf-8")


def file_name(url, post_type):
    p = urlparse(url).path.strip("/").removeprefix(post_type + "/")  # folder already = type
    return (p.replace("/", "__") or "home")[:150] + ".md"


def roster_md(t, members, terms):
    """One roster file per name-only type (giving circles). Returns (meta, markdown).
    Shared with the knowledge service's incremental sync."""
    label, url = ROSTER_TYPES[t]
    year_tax = next((k for k in terms if k.startswith(t) and "year" in k), None)
    groups = defaultdict(list)
    for m in members:
        for y in (m["tags"].get(year_tax) or ["Members"]):
            groups[y].append(m["title"])
    lines = [f"# {label} — IIT Bombay donors\n",
             f"Members of the IIT Bombay {label} ({len(members)} names), as listed on acr.iitbombay.org.\n"]
    for g in sorted(groups, reverse=True):
        lines.append(f"## {label} {g}\n" if g != "Members" else f"## {label} Members\n")
        lines += [f"- {n}" for n in sorted(set(groups[g]))] + [""]
    meta = {"url": url, "title": label, "source_type": t, "source": "roster",
            "modified": max(m["modified"] for m in members)[:10]}
    return meta, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", help="comma list of post types (default: all)")
    ap.add_argument("--limit", type=int, help="max items per type (testing)")
    ap.add_argument("--out", default=str(HERE / "output"))
    # ponytail: cache lives outside Google Drive so 300MB of raw HTML doesn't sync
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "acr-site-scrape"))
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--refetch", action="store_true", help="ignore cached HTML/PDF")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    out, cache = Path(a.out), Path(a.cache)
    cache.mkdir(parents=True, exist_ok=True)
    types = set(a.types.split(",")) if a.types else None

    print("Loading taxonomies + item list from REST API...")
    terms = load_taxonomies()
    items = load_items(types, terms)
    if a.limit:
        per = defaultdict(list)
        for i in items:
            per[i["type"]].append(i)
        items = [i for v in per.values() for i in v[:a.limit]]
    print(f"{len(items)} items")

    manifest = []
    rosters = defaultdict(list)
    pages = [i for i in items if i["type"] not in ROSTER_TYPES]
    for i in items:
        if i["type"] in ROSTER_TYPES:
            rosters[i["type"]].append(i)

    # 1. fetch + extract HTML pages
    def work(it):
        raw = cached_get(it["link"], cache, a.refetch)
        if raw is None:
            return it, None, set()
        raw = raw.decode("utf-8", "replace")
        return it, clean(html_to_md(raw), it["title"], it["link"]), pdf_links(raw, it["link"])

    docs, pdf_from = [], defaultdict(set)
    with ThreadPoolExecutor(a.workers) as ex:
        for n, (it, md, pdfs) in enumerate(ex.map(work, pages), 1):
            if n % 100 == 0:
                print(f"  fetched {n}/{len(pages)}")
            if md is None:
                manifest.append([it["link"], "", it["type"], "error", 0, it["modified"]])
                continue
            for p in pdfs:
                pdf_from[p].add(it["link"])
            docs.append((it, md))

    # 2. corpus-wide template lines (CTAs, footer blurbs the xpaths missed)
    bad = boilerplate_lines([md for _, md in docs], BOILER_MIN_DOCS, BOILER_FRAC)
    out.mkdir(parents=True, exist_ok=True)
    (out / "_boilerplate_removed.txt").write_text("\n".join(sorted(bad)) + "\n", encoding="utf-8")

    # PDFs linked from template blocks (e.g. "Checkout Annual Report" on every page) are not
    # page-specific: keep them out of per-page pdf_links.
    sitewide = {p for p, srcs in pdf_from.items() if len(srcs) >= max(BOILER_MIN_DOCS, BOILER_FRAC * len(docs))}

    # 3. dedup (newest wins) + write
    seen, page_urls = {}, {it["link"] for it in items}
    for it, md in sorted(docs, key=lambda x: x[0]["modified"], reverse=True):
        md = strip_lines(md, bad)
        n = body_len(md)
        if n < MIN_CHARS:
            manifest.append([it["link"], "", it["type"], "empty", n, it["modified"]])
            continue
        share, cards = link_share(md, page_urls)
        if share >= LISTING_SHARE and cards >= LISTING_MIN_CARDS:
            manifest.append([it["link"], "", it["type"], "listing", n, it["modified"]])
            continue
        h = hashlib.sha1(md.split("\n", 1)[1].encode()).hexdigest()
        if h in seen:
            manifest.append([it["link"], seen[h], it["type"], "dup", n, it["modified"]])
            continue
        f = out / it["type"] / file_name(it["link"], it["type"])
        seen[h] = str(f.relative_to(out))
        write_md(f, {
            "url": it["link"], "title": it["title"], "source_type": it["type"], "source": "html",
            "post_id": it["id"], "published": it["date"][:10], "modified": it["modified"][:10],
            **it["tags"],
            "pdf_links": sorted(p for p, srcs in pdf_from.items() if it["link"] in srcs and p not in sitewide),
        }, md)
        manifest.append([it["link"], seen[h], it["type"], "ok", n, it["modified"]])

    # 4. rosters
    for t, members in rosters.items():
        meta, body = roster_md(t, members, terms)
        f = out / "rosters" / f"{t}.md"
        write_md(f, meta, body)
        manifest.append([meta["url"], str(f.relative_to(out)), t, "ok", len(members), ""])

    # 5. linked PDFs
    if not a.no_pdf:
        print(f"{len(pdf_from)} linked PDFs")
        def pdf_work(u):
            data = cached_get(u, cache, a.refetch)
            if data is None:
                return u, None
            try:
                return u, pdf_to_md(data)
            except Exception as e:  # corrupt/encrypted PDF: log, don't kill the run
                print(f"  pdf fail {u}: {e}")
                return u, None
        kept = {}  # url -> (md, file, linked_from)
        with ThreadPoolExecutor(a.workers) as ex:
            # reverse path order = newest /uploads/YYYY/MM/ first, so the latest upload wins
            for u, md in ex.map(pdf_work, sorted(pdf_from, reverse=True)):
                if md is None:
                    manifest.append([u, "", "pdf", "error", 0, ""])
                    continue
                title = Path(urlparse(u).path).stem.replace("-", " ").replace("_", " ")
                md = clean(md, title, u)
                n = body_len(md)
                if n < MIN_CHARS:  # scanned image PDF, no text layer
                    manifest.append([u, "", "pdf", "empty", n, ""])
                    continue
                src = {"(sitewide)"} if u in sitewide else set(pdf_from[u])
                twin = next((k for k, v in kept.items() if line_overlap(md, v[0]) >= NEAR_DUP), None)
                if twin:
                    kept[twin][2].update(src)
                    manifest.append([u, kept[twin][1], "pdf", "dup", n, ""])
                    continue
                f = out / "pdf" / (Path(urlparse(u).path).stem[:150] + ".md")
                kept[u] = (md, str(f.relative_to(out)), src, title)
                manifest.append([u, kept[u][1], "pdf", "ok", n, ""])
        for u, (md, f, src, title) in kept.items():
            write_md(out / f, {"url": u, "title": title, "source_type": "pdf", "source": "pdf",
                               "linked_from": ["(sitewide)"] if "(sitewide)" in src else sorted(src)}, md)

    with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["url", "file", "type", "status", "chars", "modified"])
        w.writerows(manifest)
    print(Counter(r[3] for r in manifest))


def selftest():
    md = clean("# Hello\n\n![x](a.png)\nSee [docs](/about-us/) and [](/x)\n\n\n\n## Sub\ntext\n"
               "[Donate Now\n\nDonate Now](https://p.org/x) [Read More](#modal-1)\n\nLoad More\n\nLoad More\n"
               "* 20\n\n  Pledged\n##\n### Ann\nCivil\n### Bob\n\n### Cy\nMech\n## End\nlong text",
               "Hello", BASE + "/news/a/")
    assert md.startswith("# Hello\n\n"), md
    assert "![x]" not in md and "[](" not in md
    assert f"[docs]({BASE}/about-us/)" in md
    assert "### Sub" in md and "\n\n\n" not in md
    assert "[Donate Now](https://p.org/x) Read More" in md, md
    assert md.count("Load More") == 1 and "- 20" in md
    assert "- Ann — Civil\n- Bob\n- Cy — Mech" in md and "### End" in md, md
    assert not re.search(r"^#+$", md, re.M)
    assert file_name(BASE + "/csr/abc/", "csr") == "abc.md"
    assert file_name(BASE + "/", "page") == "home.md"
    docs = ["# A\nDonate Now\nreal a", "# B\nDonate Now\nreal b", "# C\nDonate Now\nreal c"]
    bad = boilerplate_lines(docs, 2, 0.5)
    assert bad == {"Donate Now"}, bad
    assert strip_lines(docs[0], bad) == "# A\nreal a\n"
    assert strip_lines("# T\n## CTA\nDonate Now\n## Keep\n### Sub\nx\n", {"Donate Now"}) == "# T\n## Keep\n### Sub\nx\n"
    md = clean("intro\n## You May Also Like\n#### Other story\nteaser\n## Real\nbody", "T", BASE + "/")
    assert "teaser" not in md and "Other story" not in md and "### Real" in md, md
    assert body_len("# T\n\nabc\n") == 3
    card = "[May 2023 Big news teaser […] Know More](https://x.org/n/1)\n"
    assert link_share("# T\n" + card * 3 + "short intro", {"https://x.org/n/1"})[0] > LISTING_SHARE
    share, cards = link_share("# T\n" + card + "x" * 400, {"https://x.org/n/1"})
    assert share < LISTING_SHARE and cards == 1
    assert "[Golden Reunion text](https://x.org/r)" in clean("[#### Golden Reunion text](https://x.org/r)", "T", BASE)
    assert line_overlap("a\nb\nc", "a\nb\nc\nd") == 1.0 and line_overlap("a\nb", "c\nd") == 0
    assert "searchandfilter" not in clean('[searchandfilter slug="all-news"]\nbody', "T", BASE)
    assert clean("Cornell (1981).nn nn", "T", BASE) == "# T\n\nCornell (1981).\n"
    assert pdf_links('<a href="/wp-content/u/x.pdf">x</a><a href="https://o.com/y.pdf">',
                     BASE + "/p/") == {BASE + "/wp-content/u/x.pdf"}
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
