# ACR Chatbot — Self-Updating Knowledge Base (n8n + Supabase)

**Site:** https://acr.iitbombay.org — IIT Bombay Alumni & Development
**n8n instance:** `n8n-mcp-prod` → https://n8n.iitbacr.space
**Stack:** WordPress REST API (source of truth) → Jina Reader (extract) → OpenAI (embed/chat) → Supabase pgvector (KB)

---

## Goal

AI chatbot that answers questions about site content **and returns the live
page link** in every answer (e.g. "how do I donate to scholarships" → answer +
clickable donation URL). KB must stay in sync automatically: reflect page
**edits, additions, deletions** on a schedule, plus host team-authored **FAQs**
that live nowhere on the site.

## Why REST API, not blind scraping / Crawl4AI

Raw scraping (Crawl4AI) gives markdown but **no change signal and no stable
key** — can't do reliable edit/delete sync without full rebuilds. WordPress
REST API gives every item's `id` (stable key), live `link` (for citation),
`modified` (change detection) and `status`. We use REST for change-detection
and scrape only for clean text. Confirmed: `/wp-json/wp/v2/pages` is **public,
no auth, no plugin needed**.

## Content universe — 22 custom post types (verified via `/wp-json/wp/v2/types`)

Content is NOT just pages/posts. Ingest these `rest_base` routes:

```
pages, posts, campaign, causes, awards, alumni_story, news, csr,
allstory, casestudy, events_list, annual_report, annual_contributors,
legacy_project, batch_legacy, jobs, dean_desk, dean_circle,
leadership_circle, giving_circle, chairman_circle, director_circle
```

Maybe (confirm relevance): `newsletter`, `csr_newsletter`, `media_gallery`,
`common_section`.
**Skip** WP internals: attachment, nav_menu_item, wp_block, wp_template*,
wp_global_styles, wp_navigation, wp_font_*, elementor_library.
**Filter out:** `Sample Page` (id 2) and any `status != publish`.

This list lives in **one editable node** in Workflow A — adjust there.

---

## Supabase schema (run once in SQL editor)

```sql
create extension if not exists vector;

create table documents (
  id          bigserial primary key,
  post_id     bigint,                 -- WP item ID; null for FAQs
  source_type text,                   -- rest_base ('pages','causes',...) or 'faq'
  url         text,                   -- live link for citation
  title       text,
  content     text,                   -- chunk text
  chunk_index int,
  modified    timestamptz,
  embedding   vector(1536)
);
create index on documents using ivfflat (embedding vector_cosine_ops);
create index on documents (post_id, source_type);

create table sync_state ( key text primary key, value timestamptz );
insert into sync_state values ('last_sync','1970-01-01') on conflict do nothing;

create or replace function match_documents(
  query_embedding vector(1536), match_count int, filter jsonb default '{}'
) returns table (id bigint, content text, url text, title text,
                 source_type text, similarity float)
language sql stable as $$
  select d.id, d.content, d.url, d.title, d.source_type,
         1 - (d.embedding <=> query_embedding) as similarity
  from documents d
  order by d.embedding <=> query_embedding
  limit match_count;
$$;
```

**Lifecycle by `post_id`+`source_type`:** new = insert, edited =
delete-then-insert chunks, deleted = delete rows. Every chunk carries `url` →
citations come for free.

---

## Workflows (built in n8n-mcp-prod)

### A — KB Sync (Schedule, every 6h) — CORE ✅ BUILT
**n8n workflow ID `XLnhEqr9EQV7QPZs`** (inactive). 9 nodes: Every 6h →
Get Watermark → Fetch/Extract/Chunk (Code, loops all 22 types + Jina) →
Delete Changed → Restore Items → OpenAI Embeddings → Build Rows →
Insert Chunks → Update Watermark. Validated: 0 errors.
Needs before run: Supabase schema applied + Postgres & OpenAI creds attached.

1. Schedule Trigger.
2. Postgres (Supabase): read `last_sync` watermark.
3. Code node: emit the content-type list (array above) → loop.
4. Per type: HTTP `GET /wp-json/wp/v2/{rest_base}?status=publish&per_page=100&modified_after={last_sync}&page=N`, paginate.
5. Filter: publish only, drop Sample Page.
6. HTTP `GET https://r.jina.ai/{link}` → clean markdown. Prepend title + url.
7. Code: chunk (~800 tokens, ~100 overlap).
8. OpenAI Embeddings `text-embedding-3-small`.
9. Postgres: `DELETE where post_id=:id and source_type=:type`, then INSERT chunks.
10. Postgres: set `last_sync = max(modified seen)`.

### A2 — Deletion reconcile (Schedule, nightly) ✅ BUILT — ID `V9ntIMrBhp7FzHfa`
1. For each type: fetch all published IDs (`&_fields=id`), paginate → live set.
2. Postgres: `SELECT DISTINCT post_id,source_type FROM documents WHERE source_type<>'faq'`.
3. Delete orphans (in DB, not in live set) — covers deletes + unpublish.

### B — FAQ ingestion (Google Sheet) ✅ BUILT — ID `lkwnBD6mt0QToQho`
Full-refresh (delete all faq → re-insert) so removed sheet rows drop
automatically — no separate FAQ reconcile needed. Sheet cols: `question`,
`answer`, `url` (optional). Set spreadsheet ID + Google Sheets cred on the
Read node.
Sheet cols: `faq_id, question, answer, url?`. Per row: embed `question+answer`,
upsert `source_type='faq'`, `post_id=null`, key on `faq_id`. Reconcile removed
rows like A2.

### C — Chatbot RAG (Webhook) ✅ BUILT — ID `Tv9Oyxzal6jojZA5`
POST `/webhook/acr-chatbot` body `{ "question": "..." }` → returns
`{ answer, sources[] }`. Flow: Webhook → Embed Question → Match Documents
(`match_documents`) → Build Context → OpenAI Chat (`gpt-4o-mini`) → Respond.
Embed question → `match_documents(embedding,5)` → build context w/ urls →
OpenAI `gpt-4o-mini`. System prompt: answer only from context; **always cite
the live page link** as markdown; if unknown, say so + offer human handoff.
Return `{answer, sources[]}`.

---

## Content extraction
**Jina Reader** — `GET https://r.jina.ai/{page-url}` → clean readability
markdown, uniform across Gutenberg + Elementor, zero infra. Fallback: n8n HTML
Extract node on `.entry-content`/`main`/`.elementor` if privacy requires
in-house fetches.

## Credentials needed in n8n (wire in n8n UI before running)
- **Postgres** — Supabase connection (host, db, user, password, port 5432/6543).
- **OpenAI** — API key.
- **Google Sheets** OAuth (Workflow B only).

## Build order & verification
1. Supabase: run schema SQL; confirm tables + `match_documents`.
2. Wire Postgres + OpenAI creds in n8n.
3. Workflow A: run manually on a few types; check `documents` rows have correct
   `url`/`post_id`. Edit a page, re-run → chunks replaced, not duplicated.
4. Workflow A2: trash a test item, run → rows gone; restore + run A → returns.
5. Workflow B: add FAQ row → `source_type='faq'` row appears.
6. Workflow C: POST "how do I donate for scholarships" → answer includes live
   donation link; unknown question declined gracefully.
7. Watermark: run A twice, no changes → 2nd run processes ~0 items.
8. Schedules: A every 6h, A2 nightly, B on sheet edit.

## Out of scope (later)
Guardrails / prompt-injection defense, rate limiting, WP widget embed,
conversation memory, analytics.
