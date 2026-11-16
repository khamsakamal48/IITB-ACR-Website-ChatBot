# ACR Website Chatbot — v2 (Markdown knowledge, no vector DB)

Public chatbot for **IIT Bombay Alumni & Corporate Relations** — https://acr.iitbombay.org.
It answers visitors from **whole Markdown pages**, not vector-search chunks, so it does
not return half an answer, and it gives the same answer every time.

> **Status (16 Nov 2026)**
>
> | Phase | What | State |
> |---|---|---|
> | 0 | Repo + tooling housekeeping | ✅ done |
> | 1 | Knowledge bundle: website + team FAQ → OKF Markdown | ✅ done |
> | 2 | 52 compiled topic pages | ✅ drafted · ⏳ **team review** |
> | 3 | Knowledge service (container) | ✅ built & tested · ⏳ **deploy (this README)** |
> | 4 | n8n chat workflow (Gemini Flash, guardrails, memory, logging) | ⏳ waits for a Gemini API credential |
> | 5 | Sync workflows write files instead of embeddings | ⏳ |
> | 6 | Evaluation + soft launch, then retire v1 | ⏳ |
>
> **Prod still runs the v1 vector-RAG workflows** until Phase 6. Their docs, SQL and
> checklist are in [`legacy-v1/`](legacy-v1/). Deploying the Phase 3 container does
> **not** affect them: `POST /extract`, which v1 Workflow A uses, is unchanged.

---

## How it works

```
             ┌──── same for every chat turn (cached by Gemini, ~32K tokens) ────┐
             │ rules + tone │ all 211 FAQ answers │ index of approved topics   │
             └──────────────────────────────────────────────────────────────────┘
WordPress widget ─► n8n chat (Phase 4) ─► guardrails ─► AI Agent · Gemini Flash
   (BravesChat)                                   │   ├─ Postgres chat memory
                                                  │   ├─ tool: search  ─┐
                                                  │   ├─ tool: read    ─┼─► acr-knowledge service
                                                  │   └─ tool: browse  ─┘   (Markdown files on disk)
                                                  └─► link/email allow-list ─► log ─► reply
```

**Knowledge in three layers.** Higher layers win when they disagree:

| Layer | Folder | Written by | Count |
|---|---|---|---|
| **FAQ** | `faq/` | The team, in the FAQ sheet | 211 answers, 27 categories |
| **Topic pages** | `concepts/` | AI from the website + FAQ, then **approved by the team** | 52 |
| **Website pages** | `sources/<type>/` | The site, one file per WordPress item | 1,962 |

- **Format:** [Open Knowledge Format (OKF) v0.2](https://www.marktechpost.com/2026/06/16/google-cloud-introduces-open-knowledge-format-okf-a-vendor-neutral-markdown-spec-for-giving-ai-agents-curated-context/).
  Each file is plain Markdown with a small YAML header (`type`, `title`, `description`,
  `sources`, `verified`, `status`, …), and pages link to each other with normal Markdown
  links. This is Karpathy's "LLM wiki" pattern in Google's file format.
- **Updates replace files.** When a page changes on WordPress, its file is overwritten.
  There's no chunking and no re-embedding. Topic pages built from that page are flagged
  `stale` so they can be rewritten and re-approved.
- **Search is lexical** (SQLite FTS5 / BM25). It only tells the agent which pages to
  open, and the agent always reads the whole page.
- **Unapproved topic pages are never served.** A topic page reaches the bot only once
  its header says `verified: true`.

## Repo layout

```
.
├── README.md                     ← this file (v2 overview + deploy runbook)
├── docker-compose.yml            ← the knowledge service container
├── trafilatura-service/          ← knowledge service (FastAPI)
│   ├── app.py                       POST /extract (HTML → Markdown, used by sync)
│   ├── knowledge.py                 /kb/* API for the chat agent and sync jobs
│   ├── okf.py                       OKF file formats (shared with tools/build_okf.py)
│   ├── API.md                       endpoint reference
│   ├── test_knowledge.py            tests (pytest)
│   └── Dockerfile, requirements.txt
├── site-scrape/scrape_site.py    ← full-site scraper: WordPress REST + PDFs → Markdown
├── tools/
│   ├── build_okf.py              ← scrape output + FAQ sheet → knowledge bundle
│   └── make_review_doc.py        ← topic pages → one Word file for team review
├── workflows/                    ← n8n workflow JSONs (currently v1; rewired in Phases 4–5)
├── legacy-v1/                    ← v1 vector-RAG runbook, SQL, plans (prod until cutover)
└── BravesChat/                   ← WordPress chat-widget plugin (separate upstream repo, git-ignored)
```

**Never in git** (`.gitignore`): the knowledge bundle (`knowledge/`), the scrape output, the
FAQ spreadsheet, the review `.docx`, `.env` and `data/`. They're live content and
contain published bank details, so they're copied to the server, not committed.

---

# Deploy runbook — knowledge service (Phase 3)

Run this on the n8n prod server (`n8n.iitbacr.space`, rootful podman). Everything lives
in one folder: the repo is cloned into **`/opt/acr-chatbot`**, and the knowledge data sits
in **`/opt/acr-chatbot/data`** (git-ignored; the compose default, so `KB_HOST_DIR` doesn't
need setting).

```
/opt/acr-chatbot/                ← git clone of this repo
├── docker-compose.yml
├── .env                         ← secrets (step 3, never committed)
├── trafilatura-service/ …
└── data/                        ← all state (git-ignored)
    ├── knowledge/               ← the bundle (index.md, faq/, concepts/, sources/)
    ├── kb-index.sqlite          ← search index (built automatically on start)
    └── kb-index.lock
```

> **The v1 extractor container is replaced, not kept alongside.** v1 already runs a
> container called `acr-trafilatura`, and the new one has the same name, network and
> `/extract` behaviour. The old one holds no data (v1 kept everything in Supabase), so
> removing it loses nothing. Step 4 removes it just before starting the new one, which
> means `/extract` is down for about a minute. v1 Workflow A calls it every 6 hours, so
> do the swap between runs.

### What you need

- SSH to the server, with `sudo podman` and `podman compose` (or `docker compose`).
- The n8n network name. The compose file expects `n8n-production_n8n-net`; confirm it
  (and see where the v1 container came from):
  ```bash
  sudo podman ps -a --format '{{.Names}}  {{.Networks}}  {{.Status}}' | grep -iE 'n8n|trafilatura'
  ```
  If the n8n network is named differently, change `name:` at the bottom of `docker-compose.yml`.
- The knowledge bundle. The current build lives on Kamal's laptop at
  `~/acr-chatbot/knowledge/`, with a copy in the project's Google Drive folder under
  `knowledge/`. To build it from scratch instead, see [Rebuild the bundle](#rebuild-the-bundle-from-the-live-site).

### 1. Get the code into `/opt/acr-chatbot`

```bash
sudo mkdir -p /opt/acr-chatbot && sudo chown "$USER:$USER" /opt/acr-chatbot
# private repo: use an account or deploy key with read access
git clone https://github.com/khamsakamal48/IITB-ACR-Website-ChatBot.git /opt/acr-chatbot
# later updates
cd /opt/acr-chatbot && git pull
```

### 2. Copy the knowledge bundle into `data/`

```bash
# on the server
mkdir -p /opt/acr-chatbot/data

# from the laptop (Drive copy works too)
rsync -a --delete ~/acr-chatbot/knowledge/ <user>@<server>:/opt/acr-chatbot/data/knowledge/
```

### 3. Create `.env` next to `docker-compose.yml`

```bash
cd /opt/acr-chatbot
cat > .env <<EOF
KB_TOKEN=$(openssl rand -hex 32)
KB_UID=$(id -u)
KB_GID=$(id -g)
EOF
chmod 600 .env
cat .env      # copy KB_TOKEN for step 6
```

- `KB_TOKEN` is the shared secret. n8n sends it on every `/kb` call. Compose refuses to
  start without it.
- `KB_UID`/`KB_GID` must belong to the owner of `/opt/acr-chatbot/data` (you, after
  step 1). The container runs as that user, so files it writes stay editable by you and
  can be rsynced over.
- Optional: `KB_ALLOW_DRAFTS=1` also serves **unapproved** topic pages. Use it only on a
  test instance.

### 4. Replace the v1 container and start the new one

```bash
cd /opt/acr-chatbot
# where was v1 started from? (for your notes; its folder can be archived afterwards)
sudo podman inspect acr-trafilatura \
  --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' 2>/dev/null

sudo podman compose build                       # build first, so the gap is short
sudo podman rm -f acr-trafilatura               # remove the v1 container (stateless)
sudo podman compose up -d                       # start the new one
sudo podman ps --filter name=acr-trafilatura --format '{{.Names}} {{.Status}}'
```

The first start builds the search index over about 2,000 pages, which takes around 10
seconds. The healthcheck allows 20 seconds. Optionally, reclaim the old image's space
afterwards with `sudo podman image prune`.

Once it's verified (step 5), the old v1 checkout folder printed above is no longer used by
anything. Archive or delete it at your convenience. **Don't** touch the Supabase tables or
the v1 n8n workflows; prod still answers from them until Phase 6.

### 5. Verify

```bash
cd /opt/acr-chatbot && source .env
curl -s localhost:8080/health                                   # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/kb/context          # 401 (token required)
curl -s -H "Authorization: Bearer $KB_TOKEN" localhost:8080/kb/context | head -c 300; echo
curl -s -H "Authorization: Bearer $KB_TOKEN" \
     "localhost:8080/kb/search?q=Can%20I%20donate%20by%20cheque" | python3 -m json.tool | head -20
curl -s -X POST localhost:8080/extract -H 'Content-Type: application/json' \
     -d '{"url":"https://acr.iitbombay.org/"}' | head -c 300; echo   # v1 Workflow A path
```

Check that n8n can reach the service by container name:
```bash
sudo podman exec <n8n-container> wget -qO- \
  --header "Authorization: Bearer $KB_TOKEN" \
  "http://acr-trafilatura:8000/kb/search?q=80G" | head -c 300; echo
```

Expected: search returns `faq/donations.md` near the top. Port 8080 answers on
`localhost` only, because the host port is bound to `127.0.0.1` on purpose; `/kb` has write
routes and must not be reachable from the internet. Finally, run v1 Workflow A once by hand
in n8n (or wait for its next run) and confirm it still succeeds.

### 6. Add the n8n credential (once)

In n8n, go to **Credentials → New → Header Auth**:

| Field | Value |
|---|---|
| Name | `ACR Knowledge Service` |
| Header name | `Authorization` |
| Header value | `Bearer <KB_TOKEN from .env>` |

The Phase 4 chat workflow and the Phase 5 sync jobs use this credential with base URL
`http://acr-trafilatura:8000`. See [`trafilatura-service/API.md`](trafilatura-service/API.md).

### Operating it

All commands run from `/opt/acr-chatbot`.

| Task | How |
|---|---|
| **Logs** | `sudo podman logs -f acr-trafilatura` |
| **Update code** | `git pull && sudo podman compose up -d --build` (everything in `data/` is kept) |
| **Replace the whole bundle** | rsync the new `knowledge/` into `data/knowledge/` (as in step 2), then `curl -X POST -H "Authorization: Bearer $KB_TOKEN" localhost:8080/kb/reindex` |
| **History / rollback** | Every write through the API is a commit: `git -C data/knowledge log --oneline`. Roll back with `git -C data/knowledge revert <sha>`, then `/kb/reindex`. This history is local to the server and never pushed. |
| **Rotate the token** | Edit `KB_TOKEN` in `.env`, run `sudo podman compose up -d`, then update the n8n credential |
| **Rebuild the search index** | `POST /kb/reindex`, or delete `data/kb-index.sqlite` and restart |
| **Backup** | `data/knowledge/` is the only state; the index can always be rebuilt |

Until the Phase 5 sync is live, content updates are manual: rebuild the bundle (below),
rsync it in and reindex.

---

## Content workflow

### Rebuild the bundle from the live site

A full run takes about 30 minutes of WordPress API calls. Run it on a local disk, **not**
inside the Google Drive folder, because Drive is very slow for thousands of small files.

```bash
# 1. scrape (writes one Markdown file per page + manifest.csv)
uv run --python 3.12 --with-requirements site-scrape/requirements.txt \
    python -I site-scrape/scrape_site.py --out ~/acr-chatbot/scrape
# 2. build the OKF bundle (keeps existing concepts/; regenerates faq/ and sources/)
uv run --with pyyaml --with openpyxl python -I tools/build_okf.py \
    --scrape ~/acr-chatbot/scrape --faq "Chatbot FAQ ACR.xlsx" --out ~/acr-chatbot/knowledge
```

`build_okf.py` takes about 3 seconds. Check `python -I site-scrape/scrape_site.py --help` for
type filters and other options.

### Team review of topic pages (Phase 2)

1. Generate the review file:
   ```bash
   uv run --with pyyaml --with python-docx python -I tools/make_review_doc.py \
       --concepts ~/acr-chatbot/knowledge/concepts --out "ACR Chatbot - Topic Pages for Review.docx"
   ```
2. The team marks it up in Word or Google Docs (suggested edits plus the Approve box per page).
3. Apply the corrections to `concepts/<slug>.md` and set `verified: true` and
   `status: current` on approved pages. Then push them to the server with rsync +
   `/kb/reindex`, or with `PUT /kb/page`.

The current draft review file is `ACR Chatbot - Topic Pages for Review.docx` in the project
Drive folder. Each page's **Review notes** list the conflicts found between the FAQ and the
website (e.g. the recurring-gift minimum, the alumni count, Diamond Jubilee reunions).

### FAQ changes

The team edits the **FAQ sheet**; nobody edits `faq/*.md` by hand. Until Workflow B is
rewired (Phase 5), rerun `tools/build_okf.py` and rsync. Once it is rewired, Workflow B
posts the sheet to `PUT /kb/faq`.

---

## Development

```bash
# service tests (run against a throwaway copy of a real bundle)
cd trafilatura-service
KB_TEST_SOURCE=~/acr-chatbot/knowledge \
uv run --python 3.12 --with-requirements requirements.txt --with pytest --with httpx \
    python -I -m pytest -q test_knowledge.py

# run the service locally
docker build -t acr-trafilatura:dev trafilatura-service
docker run --rm -p 127.0.0.1:18080:8000 --user "$(id -u):$(id -g)" \
    -e KB_TOKEN=dev -e KB_ALLOW_DRAFTS=1 -v ~/acr-chatbot-data:/data acr-trafilatura:dev
#   ~/acr-chatbot-data/knowledge must contain a bundle (copy one in first)
```

n8n workflow edits follow the pull → edit → push loop in [`workflows/README.md`](workflows/README.md).

## Roadmap (Phases 4–6)

- **Phase 4: chat workflow.** Chat Trigger (BravesChat webhook) → input guardrails (n8n
  Guardrails node: jailbreak, PII, off-topic) → AI Agent on **Gemini Flash** (native Google
  Gemini Chat Model node) with tools `search`, `read`, `browse` and the existing
  "Escalate to ACR Team" mail tool → output link/email allow-list → `chat_logs` table.
  Memory: Postgres Chat Memory (schema `acr_website_chatbot`). Edge: rate limits,
  allowed origins, message-length cap. **Blocked on a dedicated Gemini API credential in
  prod n8n.**
- **Phase 5: sync.** Workflow A → `/extract` → `PUT /kb/source`; A2 → `DELETE /kb/page`;
  B → `PUT /kb/faq`; a new daily job rewrites `stale` topic pages (marked unverified, emailed to
  the team for approval).
- **Phase 6: evaluation and launch.** Golden set = the 211 FAQ questions plus their
  alternative phrasings, scored by an LLM judge, with the same answer required on 3
  repeat runs; plus long-tail and red-team sets. Soft-launch on one page, switch
  BravesChat, then retire v1 (pgvector tables, C2, embedding credentials).
