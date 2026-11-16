# Production RAG Architecture Plan — ACR Chatbot (and reusable pipeline)

Status: **BUILT (awaiting deploy + OpenAI cred).** Phases 0-4 implemented in the repo; deployment steps in `README.md`. Supersedes the naive/semantic-only retrieval in Workflow C.
Stack: n8n + Supabase (pgvector) + OpenAI `text-embedding-3-small` (unchanged for A/B/C consistency).

## Decisions (locked)

- **Retrieval depth:** full production stack incl. contextual retrieval.
- **Reranker:** none. Postgres-only hybrid — vector + BM25, fused with RRF, then recency time-decay. Add a cross-encoder reranker later only if eval demands it.
- **Knowledge graph:** deferred. Build the portable hybrid-retrieval module first; KG is a documented future add-on, not v1.

## Why this over naive RAG

Naive = embed chunks, cosine top-k. Fails on: exact terms/names, chunk-out-of-context meaning, stale content winning over fresh, and no source citation. The fix is **four cheap layers**, not a graph:

1. **Rich chunk metadata** → enables URL citation + recency.
2. **Contextual retrieval** (Anthropic technique) → prepend 1-sentence LLM-generated context to each chunk before embedding. ~35-50% fewer retrieval misses.
3. **Hybrid search** (semantic + keyword) fused with RRF → catches exact terms pure-vector drops.
4. **Recency time-decay** in scoring → newer page wins ties.

Everything below is a **portable retrieval module** — build once, swap the ingest source per future pipeline project.

---

## Target architecture

```
INGEST (Workflow A):
  WP REST → clean MD (Trafilatura: output_format=markdown, include_links=True)
    → header-aware chunk (split on ##/###, carry section title)
    → contextual prefix (cheap LLM: 1 sentence situating chunk in its doc)
    → embed (text-embedding-3-small)
    → upsert to Supabase: content, context, embedding, tsvector, metadata

RETRIEVE (Workflow C tool):
  query → hybrid_search RPC:
      vector cosine (top-N)  +  BM25 ts_rank (top-N)
    → RRF fuse
    → × recency decay  exp(-λ * age_days)
    → top-k
  → AI Agent answers WITH cited source URL (system msg enforces)
```

---

## Schema (Supabase) — Phase 0

Extend the chunks table:

| column | type | purpose |
|---|---|---|
| `content` | text | chunk text |
| `context` | text | LLM-generated contextual prefix |
| `embedding` | vector(1536) | embed of `context + "\n" + content` |
| `fts` | tsvector GENERATED | `to_tsvector('english', content)` for BM25 |
| `url` | text | **source link for citation** |
| `title` | text | page title |
| `section_heading` | text | nearest `##` heading |
| `last_modified` | timestamptz | recency signal (from WP `modified`) |
| `post_type` | text | filter/scoping |
| `chunk_index` | int | ordering within page |

Indexes: `ivfflat`/`hnsw` on `embedding`, `GIN` on `fts`, btree on `last_modified`, `url`.

`hybrid_search(query_text, query_embedding, match_count, rrf_k, lambda)` SQL function:
- CTE `sem` = top-N by `1 - (embedding <=> query_embedding)`
- CTE `kw` = top-N by `ts_rank(fts, websearch_to_tsquery(query_text))`
- RRF: `sum(1.0 / (rrf_k + rank))` across both lists
- multiply by `exp(-lambda * extract(epoch from now()-last_modified)/86400)`
- return top `match_count` with content, url, title, section_heading, last_modified.

(Supabase has a documented `hybrid_search` starting template — extend it with the recency multiplier.)

---

## Phases

### Phase 0 — Schema migration ✅ DONE — `phase0-hybrid.sql`
- Adds `context` col, generated `fts` (tsvector), GIN/btree indexes, `hybrid_search()` RPC.
- `match_documents()` left intact for backward compat.
- Note: `context` stored in `metadata.context` (n8n Vector Store node can't write the dedicated column); the SQL `context` column is unused/optional.

### Phase 1 — Ingest upgrade + URL citation ✅ DONE — Workflow A
- `Fetch & Extract`: header-aware `sectionize()` (split on `#`..`######`), one chunk per section, carries `section_heading` + `chunk_index` + page `outline`.
- Metadata already had `url`/`title`/`modified`; recency now populated per chunk.

### Phase 2 — Contextual retrieval ✅ DONE — Workflow A
- New `Contextualize (OpenAI)` HTTP node (gpt-4o-mini, 1 sentence/chunk) + `Merge Context` code node. Prepends context into embedded content; stores raw prefix in `metadata.context`. `fts` regenerates from enriched content.

### Phase 3 — Hybrid retrieval + recency ✅ DONE — Workflows C + C2
- Replaced C's vector-only retrieve-as-tool with a **Tool Workflow** node → `C2-retrieve-hybrid.json` (embed query → `hybrid_search()` RPC → formatted top-5 + `urls`).
- C system message updated: prefer most-recent `Updated` date on conflict.
- Extraction also swapped Jina → self-hosted **Trafilatura** service (`trafilatura-service/` + `docker-compose.yml`), fed WP `content.rendered`.
- Tune `lambda`/`rrf_k`/`full_count` in C2's `hybrid_search(...)` call.

### Phase 4 — Eval ✅ DONE (harness built) — `EVAL-retrieval.json` + `eval-dataset.csv`
- Uses **n8n native Evaluations**: Evaluation Trigger (Google Sheet dataset) → Execute C2 → Code scores `hit@5` + `MRR` → Evaluation *Set Metrics* → Evaluations tab.
- Tests C2 retrieval directly (isolates the tuning knob). Starter dataset = 24 Q/A rows; fill real `expected_url`s.
- Reranker decision deferred to post-eval: add a cross-encoder only if `hit@5` plateaus low.
- Pending: fill dataset URLs, attach Google Sheets cred, run + tune. Optional later: 2nd eval driving full C + LLM-as-judge answer correctness.

### Future (deferred) — Caching (prompt + query)
Three distinct layers; n8n's chat memory is NOT a cache (it's per-session history).

- **Prompt caching (provider-side).** OpenAI = automatic, free, prompts ≥1024 tokens, ~50% off cached input, ~5-10 min TTL. Already benefits Workflow C's agent (stable system message + tool schema re-sent each turn → auto-cached). Contextualize (A) only benefits if the shared page prefix >1024 tokens — currently small (outline only); to exploit, feed whole-page text as prefix + chunk last (costs more up-front, worth it only for long pages / heavy backfill). Embeddings API is NOT prompt-cached. Anthropic caching needs explicit `cache_control` — n8n's LangChain Chat Model node doesn't expose it, so only reachable in our raw HTTP nodes, not the Agent path.
- **Semantic query cache (NOT built-in; must build).** Pre-retrieval check: embed query → similarity search vs a `query_cache(embedding, query, response, created_at)` table → if top match ≥ ~0.95 and not stale, return cached answer and skip C2+agent; else run normal path and write result to cache.
- **Conflict with recency requirement.** A cached answer goes stale when the source page updates → can serve outdated donation/payment info. If ever built: short TTL + invalidate cache on every Workflow A/A2 KB change; cache ONLY stable FAQ-type intents; NEVER cache anything with amounts/dates.
- **Verdict:** defer. Low traffic + strong recency need = risk > reward. Prompt cache on OpenAI is already free/automatic; nothing to do. Revisit query cache only if LLM cost/latency becomes a real problem.

### Future (deferred) — Knowledge graph
- Add only if a pipeline project needs multi-hop/relational queries.
- Path: extract entities/relations at ingest → store edges (pg table or Neo4j) → GraphRAG-style traversal feeding the same agent. Metadata schema above already forward-compatible.

---

## Reusability for the pipeline
Phases 0 + 3 = the reusable core: schema + `hybrid_search` RPC + agent tool. Each future RAG project reuses them verbatim and only swaps the Phase 1 ingest source (WP REST → whatever). Contextual prefixing (Phase 2) is source-agnostic too.
