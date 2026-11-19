"""ACR knowledge service.

POST /extract  { "url": "..." } OR { "html": "<...>" } -> { markdown, title, date, url }
               HTML/URL -> clean Markdown for the sync (Workflow A). No auth (internal net).
/kb/*          the OKF Markdown knowledge bundle for the chat agent — see knowledge.py.
"""
from contextlib import asynccontextmanager
from typing import Optional

import trafilatura
from trafilatura.metadata import extract_metadata
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import knowledge
import sync


@asynccontextmanager
async def lifespan(_app):
    knowledge.startup()          # build the search index on first boot
    yield


app = FastAPI(title="ACR Knowledge Service", version="2.0", lifespan=lifespan)
app.include_router(knowledge.router)
app.include_router(sync.router)

# Strip site chrome (nav/header/footer) before extraction. Content-less pages
# (thank-you stubs, redirects) otherwise fall back to the nav menu — ~700 chars
# of boilerplate that defeats any length-based "is this page empty" check.
# After pruning, menu-only pages collapse to <150 chars while real pages keep
# their body, so Workflow A's Sectionize length floor can drop the junk.
#
# Match chrome classes exactly enough to miss content: this theme names real body
# blocks `page-header` / `section-header` / `detail-page-header-left-content`, so a
# bare contains(@class,"header") deletes every legacy_project intro and project
# section. Same trap for "menu" (site nav is `mobile-menu`/`menuOverlay`).
# "footer" is scoped below <body>: Elementor pages carry body class
# `page-template-elementor_header_footer`, so an unscoped match deletes the whole page.
BOILERPLATE_XPATH = [
    "//header", "//footer", "//nav",
    '//*[contains(@class,"mobile-menu")]', '//*[contains(@class,"menuOverlay")]',
    '//*[contains(@class,"header-box")]', '//*[contains(@class,"header-list")]',
    '//*[contains(@class,"header-search")]',
    '//body//*[contains(@class,"footer")]',
    '//*[contains(@id,"menu")]', '//*[contains(@id,"footer")]',
]


class ExtractIn(BaseModel):
    url: Optional[str] = None
    html: Optional[str] = None
    include_links: bool = True
    include_tables: bool = True
    favor_recall: bool = True  # keep more content; WP pages have real body text
    prune_chrome: bool = True  # drop nav/header/footer so length reflects real content


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/extract")
def extract(inp: ExtractIn):
    if not inp.url and not inp.html:
        raise HTTPException(status_code=400, detail="provide 'url' or 'html'")

    doc = inp.html
    if inp.url and not doc:
        doc = trafilatura.fetch_url(inp.url)
    if not doc:
        raise HTTPException(status_code=502, detail="failed to fetch url")

    md = trafilatura.extract(
        doc,
        output_format="markdown",   # headings (#/##), links, lists preserved
        include_links=inp.include_links,
        include_tables=inp.include_tables,
        favor_recall=inp.favor_recall,
        include_comments=False,
        prune_xpath=BOILERPLATE_XPATH if inp.prune_chrome else None,
    ) or ""

    title = None
    date = None
    try:
        meta = extract_metadata(doc)
        if meta:
            title = meta.title
            date = meta.date
    except Exception:
        pass

    return {"markdown": md, "title": title, "date": date, "url": inp.url}
