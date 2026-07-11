"""ACR Trafilatura extractor — HTML/URL -> clean Markdown for the KB sync (Workflow A).
Replaces the Jina Reader call. Self-hosted, no rate limits.
POST /extract  { "url": "..." }  OR  { "html": "<...>" }  -> { markdown, title, date, url }
"""
from typing import Optional

import trafilatura
from trafilatura.metadata import extract_metadata
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="ACR Trafilatura Extractor", version="1.0")


class ExtractIn(BaseModel):
    url: Optional[str] = None
    html: Optional[str] = None
    include_links: bool = True
    include_tables: bool = True
    favor_recall: bool = True  # keep more content; WP pages have real body text


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
