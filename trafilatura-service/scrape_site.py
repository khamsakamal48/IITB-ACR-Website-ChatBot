"""Scrape all pages of acr.iitbombay.org -> Trafilatura service -> markdown files.

URLs are discovered by crawling: BFS from the homepage, following in-domain
<a href> links (sitemap ignored). Each page is POSTed to the Trafilatura
service; result saved as one .md file with the source URL in YAML front matter.

Usage: python scrape_site.py [--site https://acr.iitbombay.org] [--service http://localhost:8080] [--out ./pages]
"""
import argparse
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

HREF = re.compile(r'href=["\']([^"\'#]+)', re.I)
# skip non-page assets
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|svg|webp|ico|css|js|pdf|zip|mp4|mp3|woff2?|ttf)$", re.I)


def crawl_urls(site, session, max_pages):
    """BFS in-domain crawl from homepage, collecting HTML page URLs."""
    host = urllib.parse.urlparse(site).netloc
    start = site.rstrip("/") + "/"
    seen, pages, queue = {start}, [], [start]
    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"  ! crawl {url}: {e}", file=sys.stderr)
            continue
        if "html" not in r.headers.get("Content-Type", ""):
            continue
        pages.append(url)
        for href in HREF.findall(r.text):
            nxt = urllib.parse.urljoin(url, href)
            nxt, _ = urllib.parse.urldefrag(nxt)
            p = urllib.parse.urlparse(nxt)
            if p.netloc != host or p.scheme not in ("http", "https"):
                continue
            if SKIP_EXT.search(p.path) or nxt in seen:
                continue
            seen.add(nxt)
            queue.append(nxt)
    return sorted(set(pages))


def slug(url):
    p = urllib.parse.urlparse(url)
    name = (p.path.strip("/") or "index").replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return f"{name}.md"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="https://acr.iitbombay.org")
    ap.add_argument("--service", default="http://localhost:8080")
    ap.add_argument("--out", default="./pages")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--max-pages", type=int, default=2000)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = "ACR-KB-Scraper/1.0"

    urls = crawl_urls(args.site, s, args.max_pages)
    if not urls:
        print("No URLs found crawling the site.", file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(urls)} URLs")

    ok = 0
    for i, url in enumerate(urls, 1):
        try:
            r = s.post(f"{args.service}/extract", json={"url": url}, timeout=120)
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            print(f"[{i}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
            continue
        md = d.get("markdown") or ""
        if not md.strip():
            print(f"[{i}/{len(urls)}] empty  {url}", file=sys.stderr)
            continue
        # URL (and title/date) stored in front matter
        title = (d.get("title") or "").replace('"', "'")
        fm = f'---\nurl: {url}\ntitle: "{title}"\ndate: {d.get("date") or ""}\n---\n\n'
        (out / slug(url)).write_text(fm + md, encoding="utf-8")
        ok += 1
        print(f"[{i}/{len(urls)}] ok    {url}")
        time.sleep(args.delay)

    print(f"Done. {ok}/{len(urls)} saved to {out.resolve()}")


if __name__ == "__main__":
    main()
