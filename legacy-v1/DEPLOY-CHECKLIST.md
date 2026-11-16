# Deploy Checklist — ACR Chatbot (Production RAG)

Tick top to bottom. Detail for each step lives in `README.md`. 🖥️ = local, ☁️ = prod.

## 0. Prereqs
- [ ] Docker + Docker Compose installed
- [ ] Supabase project ready — DB conn (host/port/db/user/pass) + project URL + service_role key noted
- [ ] **OpenAI API key** in hand (blocks A, C, C2 until added)
- [ ] Google account for Sheets (FAQ + eval)
- [ ] 🖥️ plan for local n8n (Docker) / ☁️ access to `n8n.iitbacr.space`

## 1. Supabase schema (run in order)
- [ ] Run `schema.sql`
- [ ] Run `phase0-hybrid.sql`
- [ ] Verify: `select proname from pg_proc where proname in ('match_documents','hybrid_search');` → both returned
- [ ] Verify `fts` col + indexes: `\d+ documents` shows `fts` (tsvector) + `documents_fts_idx`

## 2. Trafilatura service
- [ ] `docker compose up -d --build`
- [ ] `curl -s localhost:8080/health` → `{"status":"ok"}`
- [ ] Test extract: `curl -s -X POST localhost:8080/extract -H 'Content-Type: application/json' -d '{"url":"https://acr.iitbombay.org/"}'` returns markdown
- [ ] Decide the URL n8n will use: 🖥️ `http://trafilatura:8000` (same net) / `http://localhost:8080` (host) — ☁️ `http://<host-ip>:8080` (port opened / tunnel, IP allow-listed)

## 3. n8n
- [ ] 🖥️ start local n8n (`docker run ... n8nio/n8n`), open `http://localhost:5678`
- [ ] ☁️ confirm prod instance reachable

## 4. Credentials (n8n UI)
- [ ] **Postgres** (Supabase DB) → A/A2/B SQL, C Postgres Chat Memory, C2 Hybrid Search
- [ ] **Supabase API** (URL + service_role) → A & B Insert-to-Supabase vector nodes
- [ ] **OpenAI** (`openAiApi`) → A Contextualize, A/B Embeddings, C Chat Model, C2 Embed Query
- [ ] **Google Sheets** (OAuth) → B Read FAQ Sheet, EVAL Evaluation Trigger

## 5. Import workflows + fill placeholders
- [ ] Import A, A2, B, C, **C2**, EVAL
- [ ] A → *Fetch & Extract* → set `TRAFILATURA_URL` (from step 2)
- [ ] Copy **C2 workflow ID** → paste into C *ACR Knowledge Base (Hybrid)* (`REPLACE_WITH_C2_WORKFLOW_ID`)
- [ ] Copy **C2 workflow ID** → paste into EVAL *Run Hybrid Retrieve* (`REPLACE_WITH_C2_WORKFLOW_ID`)
- [ ] B → *Read FAQ Sheet* → set FAQ spreadsheet ID (cols `question`, `answer`, opt `url`)
- [ ] Attach creds on each node (Postgres/Supabase/OpenAI/Sheets per step 4)

## 6. Verify node config (version-sensitive — do NOT skip)
- [ ] C *ACR Knowledge Base (Hybrid)* — toolWorkflow input maps `query` via `$fromAI`
- [ ] C2 *Embed Query* + *Hybrid Search* nodes run (test C2 alone with a sample `query`)
- [ ] EVAL *Set Metrics* — `hit_at_5` + `mrr` present, type = number
- [ ] EVAL *Evaluation Trigger* — sheet picked from dropdown after Sheets cred attached

## 7. Seed KB
- [ ] Run **A** manually (full backfill)
- [ ] Verify: `select count(*), count(metadata->>'modified') with_date, count(context) with_ctx from documents;` all > 0
- [ ] Spot-check an Elementor/`campaign` page chunk isn't thin (else switch that path to URL-fetch)
- [ ] Activate A (schedule), A2, B

## 8. Chat widget
- [ ] Get C's Chat Trigger URL (☁️ prod webhook / 🖥️ localhost webhook)
- [ ] Embed `<script>` snippet on WP site (☁️) or test page (🖥️)
- [ ] Send a test question → streamed answer + live link

## 9. Eval + tune
- [ ] New Google Sheet ← paste `eval-dataset.csv`; fill real `expected_url`s (replace `REPLACE-*`)
- [ ] EVAL *Evaluation Trigger* → select that sheet
- [ ] Run EVAL → read `hit_at_5` / `mrr` in Evaluations tab
- [ ] Tune C2 `hybrid_search(...)` args (`lambda`, `rrf_k`, `full_count`) → re-run → compare
- [ ] If `hit@5` plateaus low → consider a cross-encoder reranker

## 10. Smoke test (end-to-end)
- [ ] Edit a WP page → re-run A → chunks replaced not duplicated
- [ ] Run A again, no changes → ~0 items (watermark works)
- [ ] Trash a test item → run A2 → its rows gone
- [ ] Add FAQ row → run B → `source_type='faq'` row appears
- [ ] Chat follow-up question uses prior turn (memory); reopen widget → history reloads
- [ ] Ask about a freshly-edited page → newest chunk wins (recency decay)

---
**Blockers before ANY AI step runs:** OpenAI credential (step 4) + all placeholders filled (step 5).
Full detail: `README.md`. Design + phase status: `RAG-Architecture-Plan.md`.
