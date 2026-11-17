# ACR knowledge service — API

Base URL from n8n: `http://acr-trafilatura:8000` (shared podman network).
Every `/kb` route needs `Authorization: Bearer <KB_TOKEN>` (n8n: Header Auth credential).
Pages are always returned **whole** (long PDFs in 30k-char windows) — never chunks.

## Chat agent (read)

| Route | Use | Returns |
|---|---|---|
| `GET /kb/context` | Once per chat turn → system-prompt prefix. Root index (approved topics only) + every FAQ answer. Byte-identical until the bundle changes, so Gemini caches it. | `{version, chars, text}` |
| `GET /kb/search?q=…&tier=&type=&limit=8` | Agent tool: which pages to open. Lexical BM25 (no embeddings); sentence-style questions OK. `tier` = `concepts`/`faq`/`sources`. | `{results:[{path,title,description,url,modified,type,tier,snippet}]}` |
| `GET /kb/page?path=…&offset=0` | Agent tool: read one whole page. Follow `next_offset` for long documents. Review notes are stripped. | `{path,title,url,modified,content,next_offset,…}` |
| `GET /kb/list?folder=sources/awards` | Agent tool: browse a folder listing (`concepts`, `faq`, `sources/<type>`). | `{folder, content}` |

| `POST /kb/check-answer` `{answer, steps, session_id, question, started_at, model}` | After the agent, before replying. Links/emails must be on the ACR allow-list; a phone number must appear in the prompt knowledge or this turn's tool results. `steps` = the agent's `intermediateSteps` as-is. | The complete `chat_logs` record: `{output, outcome, sanitized, tool_calls, pages_read, latency_ms, model, kb_version, …}` |

Unapproved topic pages (`verified: false`) are invisible to all read routes unless the
container runs with `KB_ALLOW_DRAFTS=1` (testing only). A `stale` page stays visible —
it is the last approved version until it is recompiled and re-approved.

## Sync jobs (write)

| Route | Caller | Effect |
|---|---|---|
| `PUT /kb/source` `{folder, slug, url, title, markdown, post_id, published, modified, tags}` | Workflow A (page changed on WordPress) | Writes `sources/<folder>/<slug>.md`, refreshes listing + search, marks topic pages built from `url` as `stale`. Returns `{path, created, stale_concepts}`. |
| `DELETE /kb/page?path=…` | Workflow A2 (page unpublished) | Removes page, refreshes listing + search, marks dependent topics stale. |
| `PUT /kb/faq` `{rows:[{category, subcategory, question, alternative, answer, page}]}` | Workflow B (FAQ sheet) | Full refresh of `faq/` (removed categories deleted). |
| `PUT /kb/page` `{path, content}` | Recompile job / applying team edits | Writes a whole OKF file (frontmatter required). |
| `GET /kb/stale` | Recompile job | Topic pages waiting for recompilation. |
| `POST /kb/reindex` | Manual | Rebuilds the search index and root index. |

Every write is committed to a local git repo inside the bundle (`KB_GIT=1`) for history
and rollback — it is never pushed anywhere.

## Unchanged

`POST /extract` `{url}` or `{html}` → `{markdown, title, date, url}` (no auth, internal only)
and `GET /health`.

## Tests

```
KB_TEST_SOURCE=~/acr-chatbot/knowledge \
uv run --python 3.12 --with-requirements requirements.txt --with pytest --with httpx \
    python -I -m pytest -q test_knowledge.py
```
