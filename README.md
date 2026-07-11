# ACR Website Chatbot — Self-Updating Knowledge Base (Production RAG)

AI chatbot for **IIT Bombay Alumni & Development (ACR)** — https://acr.iitbombay.org.
Answers questions about site content and **returns the live page link** in every
reply (e.g. "how do I donate to scholarships?" → answer + clickable donation URL).
The knowledge base (KB) auto-syncs with the site (edits/adds/deletes on a schedule)
plus team-authored **FAQs**. Retrieval is a **production hybrid RAG** — semantic +
keyword, recency-aware, with contextual chunks — not naive vector search.

> **Status: build stage.** Nothing is installed yet on local or production. This
> README is the step-by-step **deployment runbook** — follow it top to bottom.

## Architecture

```
INGEST (Workflow A, every 6h)
  WordPress REST API ─► n8n "Fetch & Extract"
     (content.rendered)      │  POST html ─► Trafilatura service ─► Markdown
                             │  header-aware sectionize (carry section_heading)
                             ▼
                       Contextualize (LLM: 1-sentence context per chunk)
                             ▼
                       embed (text-embedding-3-small) ─► Supabase documents
                             (content, metadata{url,title,modified,section_heading,context}, embedding, fts)

RETRIEVE (Workflow C — chat)
  User (chat widget) ─► Chat Trigger ─► AI Agent ─► streamed answer + live links
                                           ├─ Chat Model sub-node (gpt-4o-mini)
                                           ├─ Postgres Chat Memory (per-session)
                                           └─ KB tool ─► C2 sub-workflow:
                                                embed query ─► hybrid_search() RPC
                                                (vector + BM25 → RRF → recency decay) ─► top-5
```

- **Source of truth = WordPress REST API** (`/wp-json/wp/v2/...`): stable `id`,
  live `link`, `modified`, `status` → reliable change-detection, no WP plugin.
  Content spans **22 custom post types**.
- **Extraction = self-hosted Trafilatura service** (`trafilatura-service/`,
  FastAPI in Docker). WP `content.rendered` HTML → clean Markdown with headings +
  links. Replaces Jina Reader — **no rate limits**.
- **Chunking = header-aware.** Split Markdown on `#`/`##`/`###`, one chunk per
  section, `section_heading` + `chunk_index` carried in metadata.
- **Contextual retrieval** (Anthropic technique): a cheap LLM writes a 1-sentence
  context per chunk, prepended before embedding → big hit-rate gain on isolated chunks.
- **Store = Supabase (pgvector).** `documents(id, content, metadata jsonb,
  embedding, fts)`. `fts` = generated `tsvector` for keyword/BM25. Structured
  fields live in `metadata`; citations from `metadata.url`/`title`; recency from
  `metadata.modified`.
- **Hybrid retrieval = `hybrid_search()` SQL RPC:** semantic (pgvector cosine) +
  keyword (BM25 `ts_rank`) fused with **Reciprocal Rank Fusion**, then multiplied
  by a **recency time-decay** (`exp(-λ·age_days)`) so a newer page beats an older
  one on ties. No external reranker.
- **Chat = AI Agent + memory.** Chat Trigger (public embeddable widget) → AI Agent.
  KB is a **tool** (C2 sub-workflow). **Postgres Chat Memory** in
  `n8n_chat_histories`, per-visitor session.
- **Pluggable models:** embeddings/LLM are swappable (see [Swapping models](#swapping-models)).
  Defaults: `text-embedding-3-small` (1536-dim) + `gpt-4o-mini`.

## Repo layout

```
.
├── README.md                  ← this file (deploy runbook)
├── RAG-Architecture-Plan.md   ← production RAG design + phase status
├── KB-Chatbot-Plan.md         ← original KB/sync design doc
├── schema.sql                 ← base Supabase DDL (run FIRST)
├── phase0-hybrid.sql          ← hybrid retrieval migration (run SECOND)
├── docker-compose.yml         ← Trafilatura service
├── trafilatura-service/       ← FastAPI HTML→Markdown microservice
│   ├── app.py  ├── Dockerfile  └── requirements.txt
├── eval-dataset.csv           ← starter eval questions (paste into Google Sheet)
└── workflows/                 ← n8n workflows as code
    ├── A-sync-wp-to-supabase.json    (ingest)
    ├── A2-deletion-reconcile.json    (orphan delete)
    ├── B-faq-ingestion.json          (FAQ refresh)
    ├── C-chatbot-rag-webhook.json    (chat agent)
    ├── C2-retrieve-hybrid.json       (hybrid retrieval tool sub-workflow)
    └── EVAL-retrieval.json           (hit@5 / MRR eval harness)
```

## Workflows

| # | Name | Trigger | Job |
|---|------|---------|-----|
| A | KB Sync | every 6h | Fetch changed items (22 types) → Trafilatura → sectionize → contextualize → embed → upsert. Watermarked (`modified_after`). |
| A2 | Deletion Reconcile | nightly | Live published IDs vs KB → delete orphans. |
| B | FAQ Ingestion | daily | Full-refresh FAQs from a Google Sheet. |
| C | Chatbot RAG (Agent + Memory) | chat widget | Chat Trigger → AI Agent + Postgres memory; KB tool = C2. Streams answers, cites live links, prefers most-recent on conflict. |
| C2 | Hybrid Retrieve (tool) | called by C | Embed query → `hybrid_search()` → formatted top-5 (+`urls` for eval). |
| EVAL | Retrieval eval | manual | Runs a Google-Sheet question set through C2 → logs `hit@5`/`MRR` to n8n Evaluations tab. |

---

# Deployment runbook

Two targets: **Local** (test on your machine) and **Production** (`n8n.iitbacr.space`).
Steps are identical except where a 🖥️ **LOCAL** / ☁️ **PROD** note calls out a difference.

## Prerequisites

- **Docker + Docker Compose** (for Trafilatura; and for local n8n).
- **Supabase project** (cloud, or local `supabase` stack). Note its **DB
  connection** (host/port/db/user/password) and **project URL + service_role key**.
- **OpenAI API key** (embeddings + LLM + contextual prefix). ⚠️ Currently the
  **missing piece** — every AI step needs it.
- **Google account** (Sheets) for FAQ (B) and the eval dataset.
- n8n:
  - 🖥️ **LOCAL** — run n8n in Docker (Step 3).
  - ☁️ **PROD** — existing instance at `https://n8n.iitbacr.space`.

## Step 1 — Supabase schema

In the Supabase SQL editor, run **in order**:

1. `schema.sql` — extension, `documents` table, `sync_state`, `match_documents()`.
2. `phase0-hybrid.sql` — adds `context` col, generated `fts` col, GIN/btree
   indexes, and the `hybrid_search()` RPC.

Verify: `select proname from pg_proc where proname in ('match_documents','hybrid_search');`
returns both.

## Step 2 — Trafilatura extraction service

From the repo root:

```bash
docker compose up -d --build
curl -s localhost:8080/health                       # {"status":"ok"}
curl -s -X POST localhost:8080/extract \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://acr.iitbombay.org/"}' | head
```

Note the URL Workflow A will call it by:
- 🖥️ **LOCAL** (n8n also in Docker, same network) → `http://trafilatura:8000/extract`
- 🖥️ **LOCAL** (n8n on host, service in Docker) → `http://localhost:8080/extract`
- ☁️ **PROD** → host the service reachable from n8n and use
  `http://<host-ip>:8080/extract` (open the port or put behind a tunnel/HTTPS).
  Do **not** expose it publicly without at least IP allow-listing.

## Step 3 — n8n

- 🖥️ **LOCAL** — minimal run:
  ```bash
  docker run -it --rm -p 5678:5678 -v n8n_data:/home/node/.n8n docker.n8n.io/n8nio/n8n
  ```
  Open `http://localhost:5678`. (To share a Docker network with Trafilatura, run
  both from one compose file or `docker network connect`.)
- ☁️ **PROD** — already running; just import workflows + creds below.

## Step 4 — Credentials (create in n8n UI, once)

| Credential | Used by |
|---|---|
| **Postgres** (Supabase DB) | A/A2/B watermark & SQL, **C Postgres Chat Memory**, **C2 Hybrid Search** |
| **Supabase API** (URL + service_role) | *Insert to Supabase* vector nodes in A, B |
| **OpenAI** (`openAiApi`) | A *Contextualize*, A/B *Embeddings*, C *Chat Model*, **C2 *Embed Query*** |
| **Google Sheets** (OAuth) | B *Read FAQ Sheet*, EVAL *Evaluation Trigger* |

## Step 5 — Import workflows + fill placeholders

Import the JSONs from `workflows/`. Then wire the IDs that only exist after import:

1. **Set the Trafilatura URL** — Workflow A → *Fetch & Extract* node → edit
   `TRAFILATURA_URL` (top of the code) to the value from Step 2.
2. **Import C2 first**, copy its **workflow ID**, then paste it into:
   - Workflow C → *ACR Knowledge Base (Hybrid)* node → `REPLACE_WITH_C2_WORKFLOW_ID`.
   - EVAL → *Run Hybrid Retrieve (C2)* node → `REPLACE_WITH_C2_WORKFLOW_ID`.
3. **FAQ sheet** — Workflow B → *Read FAQ Sheet* → set spreadsheet ID
   (cols `question`, `answer`, optional `url`).
4. **Eval sheet** — see Step 8.

## Step 6 — Seed the knowledge base

Run **Workflow A** manually once (full backfill). This is the only heavy run;
Trafilatura is self-hosted so no throttling. Confirm rows land:

```sql
select count(*), count(metadata->>'modified') as with_date,
       count(context) as with_ctx from documents;
```

Then **activate** A (schedule), A2, and B.

## Step 7 — Embed the chat widget

Add to the WordPress site (C uses n8n's Chat Trigger):

```html
<link href="https://cdn.jsdelivr.net/npm/@n8n/chat/dist/style.css" rel="stylesheet"/>
<script type="module">
import { createChat } from 'https://cdn.jsdelivr.net/npm/@n8n/chat/dist/chat.bundle.es.js';
createChat({ webhookUrl: 'https://n8n.iitbacr.space/webhook/4b32064b-25a7-499f-9d32-108760371c8f/chat' });
</script>
```
- ☁️ **PROD** webhook URL as above. 🖥️ **LOCAL**: use your local Chat Trigger URL
  (`http://localhost:5678/webhook/<id>/chat`) — copy it from C's Chat Trigger node.

## Step 8 — Evaluate & tune retrieval

1. New **Google Sheet**, paste `eval-dataset.csv` (cols `question`,
   `expected_url`, `notes`). Fill the `REPLACE-*` `expected_url`s with the real
   live pages.
2. EVAL → *Evaluation Trigger* → pick the sheet (fills `documentId`/`sheetName`).
3. Run EVAL. Read **`hit_at_5`** and **`mrr`** in the workflow's **Evaluations tab**.
4. Tune in C2's *Hybrid Search* node — the `hybrid_search(...)` args:
   `lambda` (recency strength), `rrf_k`, `full_count`. Re-run, compare. If `hit@5`
   stays low after tuning, add a cross-encoder reranker.

---

## Swapping models

Each model is a node you change in the UI — no code edits:

- **Embeddings:** A *Embeddings*, B *Embeddings*, and **C2 *Embed Query*** (an HTTP
  call to OpenAI — change model there for C2). All three MUST use the **same**
  embedding model as ingest, or similarity breaks.
- **LLM (chat):** C *Chat Model* — swap for Anthropic/Google/Groq/Ollama.
- **Contextual prefix LLM:** A *Contextualize* (HTTP node) — any cheap chat model.

**Match the vector dimension to `schema.sql`.** `documents.embedding` is
`vector(1536)`. A different-dim model (e.g. `text-embedding-3-large` = 3072)
means altering that column and re-running A + B.

## Verify / smoke test

- **Ingest:** run A → `documents` fills; `metadata->>'url'`, `->>'modified'`,
  `->>'section_heading'`, `context` all set. Edit a page, re-run → chunks
  replaced, not duplicated. Run A again with no changes → ~0 items (watermark).
- **Deletes:** trash a test item → run A2 → its rows gone.
- **FAQ:** add a sheet row → run B → `metadata->>'source_type'='faq'` row appears.
- **Retrieval:** call C2 directly with a question → returns top-5 with URLs; a
  query about a recently-edited page ranks the fresh chunk first.
- **Chat:** open the widget → ask "how do I donate for scholarships" → streamed
  answer + live link. Follow-up ("for a specific department?") uses prior turn
  (memory). Reopen widget → history reloads. One row per session in
  `n8n_chat_histories`.
- **Eval:** run EVAL → `hit@5`/`MRR` logged in the Evaluations tab.

## Known limits / next steps

- **OpenAI credential** must be added before A (contextualize/embed), C (chat),
  and C2 (embed) can run. Keep embeddings = `text-embedding-3-small` to match the KB.
- **Trafilatura fetches static HTML** (we send `content.rendered`, so no fetch —
  but shortcode/page-builder CPTs may render thin). Spot-check a `campaign`/
  Elementor page after first ingest; if thin, switch that path to the URL-fetch
  variant of the service.
- **Contextual prefix cost:** one cheap LLM call per chunk at ingest. Full
  backfill is the only bulk cost; incremental runs touch only changed pages.
- Not yet done: guardrails / prompt-injection defense, rate limiting, dropping the
  widget `<script>` into the live WP theme, analytics, LLM-as-judge answer eval,
  optional knowledge-graph layer (deferred — see `RAG-Architecture-Plan.md`).
- Consider `git init` here for real version history.

See `RAG-Architecture-Plan.md` for the production-RAG rationale and phase status,
and `KB-Chatbot-Plan.md` for the original KB/sync design.
