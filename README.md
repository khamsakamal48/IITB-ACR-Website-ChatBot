# ACR Website Chatbot — v2 (Markdown knowledge, no vector DB)

Public chatbot for **IIT Bombay Alumni & Corporate Relations** — https://acr.iitbombay.org.
It answers visitors from **whole Markdown pages**, not vector-search chunks, so it does
not return half an answer, and it gives the same answer every time.

> **Status (19 Nov 2026)**
>
> | Phase | What | State |
> |---|---|---|
> | 0 | Repo + tooling housekeeping | ✅ done |
> | 1 | Knowledge bundle: website + team FAQ → OKF Markdown | ✅ done |
> | 2 | 52 compiled topic pages | ✅ drafted · ⏳ **team review** |
> | 3 | Knowledge service (container) | ✅ deployed |
> | 4 | n8n chat workflow (gpt-6-luna via OpenRouter, guardrails, local memory + log) | ✅ built & tested, **not public yet** |
> | 5 | Website sync writes files instead of embeddings (A, A2) | ✅ built & tested · ⏳ deploy (step 8) |
> | 5b | FAQ refresh | manual: [FAQ changes](#faq-changes) |
> | 5c | Job D: redraft stale topic pages | ⏳ after the team's topic-page review |
> | 6 | Evaluation + soft launch, then retire v1 | ⏳ |
>
> **Prod still runs the v1 vector-RAG workflows** until Phase 6. Their docs, SQL and
> checklist are in [`legacy-v1/`](legacy-v1/). The v2 container keeps `POST /extract`,
> which v1 Workflow A uses, unchanged.

---

## How it works

```
             ┌──── same for every chat turn (~32K tokens) ──────────────────────┐
             │ rules + tone │ all 211 FAQ answers │ index of approved topics   │
             └──────────────────────────────────────────────────────────────────┘
WordPress widget ─► n8n chat ─► rate limit ─► guardrails ─► AI Agent · gpt-6-luna
   (BravesChat)        (Phase 4)   (local DB)   (Flash-Lite)   │  ├─ chat memory (local Postgres)
                                                               │  ├─ tool: search ─┐
                                                               │  ├─ tool: read   ─┼─► acr-knowledge service
                                                               │  └─ tool: browse ─┘   (Markdown files on disk)
                                                               └─► /kb/check-answer ─► reply ─► chat_logs
WordPress ─(every 6 h: changed pages; nightly: unpublished pages)─► /kb/sync, /kb/reconcile ─► files
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

# on the server: data/ must belong to YOU, not root
ls -lnd /opt/acr-chatbot/data /opt/acr-chatbot/data/knowledge   # want: 1000 1000 (your id -u / id -g)
sudo chown -R "$USER:$USER" /opt/acr-chatbot/data                # fix if it shows 0 0
```

> Create `data/` yourself **before** the first `compose up`. If the folder doesn't exist,
> rootful podman creates it as root. The service, which runs as your user, then can't
> write its search index and exits with `Permission denied: '/data/kb-index.lock'`.
> The fix is always the `chown` above, followed by `sudo podman restart acr-trafilatura`.

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

### 7. Local chatbot database (chat memory + chat log)

The v2 chat keeps its memory and log in a **separate `acr_chatbot` database inside the
server's existing `n8n-postgres` container**. It sits on the same podman network as n8n, so
queries take milliseconds instead of the ~2 s per message that round trips to Supabase cost.
n8n's own `n8n` database is not touched. The v1 bot keeps using Supabase until it's retired.

```bash
cd /opt/acr-chatbot && git pull
PW=$(openssl rand -hex 24)
sudo podman exec -i n8n-postgres psql -v ON_ERROR_STOP=1 -U n8n -d postgres -v pw="$PW" \
     < sql/v2-local-postgres.sql        # last lines: acr_chatbot | 3 | 1  (db, tables, trigger)
echo "$PW"                              # copy for the n8n credential below, then clear your screen
```

- Safe to re-run, but **re-running resets the `acr_chatbot` password** to the new `$PW`.
  Update the n8n credential if you do.
- If `-U n8n` is refused, the container's superuser has another name: check
  `sudo podman exec n8n-postgres printenv POSTGRES_USER`.

Then, in n8n, go to **Credentials → New → Postgres**:

| Field | Value |
|---|---|
| Name | `ACR Chatbot (local Postgres)` |
| Host | `n8n-postgres` |
| Database | `acr_chatbot` |
| User | `acr_chatbot` |
| Password | the `$PW` printed above |
| Port | `5432` |
| SSL | **Disable** (container-to-container traffic on the private podman network) |

**Backups:** Supabase backed up the v1 data. This database is on your server, so include it
in the server's backups, e.g. a nightly dump kept for 14 days. Add this line to **root's**
crontab (`sudo crontab -e`), since podman here runs as root:

```bash
15 2 * * * mkdir -p /opt/acr-chatbot/backups && podman exec n8n-postgres pg_dump -U n8n -Fc acr_chatbot > /opt/acr-chatbot/backups/acr_chatbot-$(date +\%F).dump && find /opt/acr-chatbot/backups -name 'acr_chatbot-*.dump' -mtime +14 -delete
```

`backups/` is git-ignored (it holds chat data). Restore with
`podman exec -i n8n-postgres pg_restore -U n8n -d acr_chatbot --clean < <file>`.

### 8. Website sync (Phase 5)

Two n8n workflows keep the knowledge in step with WordPress. They call the knowledge
service, which reuses `site-scrape/scrape_site.py`'s extraction, so synced pages look
exactly like the bulk-scraped ones:

| n8n workflow | Schedule | Calls | Emails Kamal when |
|---|---|---|---|
| **ACR v2 — A: Sync website → knowledge** | every 6 h, at :45 | `POST /kb/sync` (pages modified since last run) | the call fails or any page errors |
| **ACR v2 — A2: Remove unpublished pages** | nightly 02:30 | `POST /kb/reconcile` | the call fails or it refuses a mass delete |

Deploy (the container gains the sync code; data is kept):

```bash
cd /opt/acr-chatbot && git pull
sudo podman compose build && sudo podman rm -f acr-trafilatura && sudo podman compose up -d
sleep 20 && source .env
# preview what the first sync would change (writes nothing)
curl -s -X POST localhost:8080/kb/sync -H "Authorization: Bearer $KB_TOKEN" \
     -H 'Content-Type: application/json' -d '{"dry_run": true}' | python3 -m json.tool | head -40
# first real run: everything changed since the bulk scrape (~1-2 min)
curl -s -X POST localhost:8080/kb/sync -H "Authorization: Bearer $KB_TOKEN" \
     -H 'Content-Type: application/json' -d '{}' | python3 -m json.tool | head -40
```

Then activate both workflows in n8n. Status at any time: `GET /kb/sync/state`.

- **Scanned (image-only) PDFs are skipped** as "no text", like the bulk scrape: the
  container has no OCR. (On a laptop with Tesseract installed, the PDF library OCRs them
  automatically and a run can take minutes.)
- **Reconcile never mass-deletes**: if WordPress returns fewer than half the pages the
  bundle holds for a type (outage, permission change), that type is skipped and you get an email.

### Operating it

All commands run from `/opt/acr-chatbot`.

| Task | How |
|---|---|
| **Logs** | `sudo podman logs -f acr-trafilatura` |
| **Update code** | `git pull && sudo podman compose build && sudo podman rm -f acr-trafilatura && sudo podman compose up -d` (everything in `data/` is kept). podman-compose's `up -d --build` builds the new image but **keeps the old container running**, so remove it explicitly. Check with `podman inspect acr-trafilatura --format '{{.Image}}'`. |
| **Replace the whole bundle** | **Bootstrap / disaster recovery only.** Once the sync (step 8) is live, rsyncing a rebuilt bundle with `--delete` **reverts every page the sync refreshed since your scrape**. If you must: run a fresh scrape first, rsync, `curl -X POST -H "Authorization: Bearer $KB_TOKEN" localhost:8080/kb/reindex`, then `POST /kb/sync` with `{"since": "<scrape date>T00:00:00"}`. |
| **History / rollback** | Every write through the API is a commit: `git -C data/knowledge log --oneline`. Roll back with `git -C data/knowledge revert <sha>`, then `/kb/reindex`. This history is local to the server and never pushed. |
| **Rotate the token** | Edit `KB_TOKEN` in `.env`, run `sudo podman compose up -d`, then update the n8n credential |
| **Rebuild the search index** | `POST /kb/reindex`, or delete `data/kb-index.sqlite` and restart |
| **Backup** | `data/knowledge/` is the only state; the index can always be rebuilt |

Website changes reach the bot automatically through the sync (step 8). FAQ changes are
manual: see [FAQ changes](#faq-changes).

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

The team edits the FAQ spreadsheet; nobody edits `faq/*.md` by hand. To publish a new
version, push **only the FAQ** to the server (this replaces `faq/` and nothing else):

```bash
# laptop: spreadsheet -> JSON
uv run --with pyyaml --with openpyxl python -I tools/faq_to_json.py "Chatbot FAQ ACR.xlsx" > faq.json
scp faq.json <user>@<server>:/opt/acr-chatbot/
# server
cd /opt/acr-chatbot && source .env
curl -s -X PUT localhost:8080/kb/faq -H "Authorization: Bearer $KB_TOKEN" \
     -H 'Content-Type: application/json' --data @faq.json; echo    # {"categories":27,"answers":211}
rm faq.json
```

The bot uses the new answers from the next message. Every update is in the local history
(`git -C data/knowledge log`). Don't use `tools/build_okf.py` + rsync for FAQ updates: it
rebuilds the website pages from your last scrape too, undoing the sync's updates.

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

- **Phase 4: chat workflow. Done, not public yet.** n8n workflow `T4BkIX8xsaZzu6hd`
  (`workflows/C-v2-chat-okf.json`, generated by `tools/make_chat_v2_workflow.py`): webhook
  (BravesChat-compatible, `X-N8N-Auth`) → input checks → per-session rate limit → Guardrails
  (jailbreak + NSFW on `google/gemini-3.5-flash-lite` via OpenRouter) → AI Agent on
  `openai/gpt-6-luna` via OpenRouter with tools search / read / browse + "Escalate to ACR
  Team" → `/kb/check-answer` (link/email allow-list, grounded phone numbers) → reply →
  `chat_logs`. Memory and log in the local `acr_chatbot` database. ~4 s for FAQ answers,
  ~8 s with page lookups.
- **Phase 5: sync. Done.** `/kb/sync` (every 6 h) and `/kb/reconcile` (nightly) via n8n
  workflows A and A2; FAQ updates are manual (`tools/faq_to_json.py`). Still to come: job D,
  which redrafts stale topic pages for approval, once the team has approved the topic pages.
- **Phase 6: evaluation and launch.** Golden set = the 211 FAQ questions plus their
  alternative phrasings, scored by an LLM judge, with the same answer required on 3
  repeat runs; plus long-tail and red-team sets. Soft-launch on one page, switch
  BravesChat, then retire v1 (pgvector tables, C2, embedding credentials).
