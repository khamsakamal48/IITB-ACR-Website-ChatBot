-- ACR chatbot v2 — local database inside the server's n8n-postgres container.
-- Separate database + login, so n8n's own `n8n` database is never touched.
-- Safe to re-run. Run as the container's superuser (README: "Local chatbot database"):
--
--   PW=$(openssl rand -hex 24)
--   sudo podman exec -i n8n-postgres psql -v ON_ERROR_STOP=1 -U n8n -d postgres -v pw="$PW" \
--        < sql/v2-local-postgres.sql
--   echo "$PW"      # -> n8n credential "ACR Chatbot (local Postgres)"

-- 1. Login + database --------------------------------------------------------------
SELECT format('CREATE ROLE acr_chatbot LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acr_chatbot') \gexec
SELECT format('ALTER ROLE acr_chatbot PASSWORD %L', :'pw') \gexec
SELECT 'CREATE DATABASE acr_chatbot OWNER acr_chatbot'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'acr_chatbot') \gexec
REVOKE ALL ON DATABASE acr_chatbot FROM PUBLIC;

\connect acr_chatbot
SET ROLE acr_chatbot;

-- 2. Chat memory (n8n Postgres Chat Memory node; same layout as v1's Supabase table) --
CREATE TABLE IF NOT EXISTS chat_histories (
  id         serial PRIMARY KEY,
  session_id varchar(255) NOT NULL,
  message    jsonb        NOT NULL
);
-- v1 had no index here; memory reads by session on every message.
CREATE INDEX IF NOT EXISTS chat_histories_session ON chat_histories (session_id, id);

CREATE TABLE IF NOT EXISTS chat_histories_archive (
  id          integer,
  session_id  varchar,
  message     jsonb,
  archived_at timestamptz NOT NULL DEFAULT now()
);

-- Copied verbatim (minus schema prefix) from v1's acr_website_chatbot.archive_tool_messages.
CREATE OR REPLACE FUNCTION archive_tool_messages() RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
  INSERT INTO chat_histories_archive(id, session_id, message) VALUES (NEW.id, NEW.session_id, NEW.message);
  DELETE FROM chat_histories WHERE id = NEW.id;
  RETURN NULL;
END; $function$;

DROP TRIGGER IF EXISTS trg_archive_tool_messages ON chat_histories;
CREATE TRIGGER trg_archive_tool_messages AFTER INSERT ON chat_histories FOR EACH ROW
  WHEN (((NEW.message ->> 'type') = 'tool') OR (COALESCE(jsonb_array_length(NEW.message -> 'tool_calls'), 0) > 0))
  EXECUTE FUNCTION archive_tool_messages();
COMMENT ON TRIGGER trg_archive_tool_messages ON chat_histories IS
  'Moves AI tool-call scaffolding (tool responses + ai rows with tool_calls) to chat_histories_archive on insert. Without this, the buffer-window memory slices between an ai tool_calls row and its tool response, producing an orphaned tool message that OpenAI-compatible APIs reject with 400. Human/final-ai messages are never touched. Drop this trigger only if the agent stops persisting tool rounds.';

-- 3. Chat log (one row per visitor message) -----------------------------------------
CREATE TABLE IF NOT EXISTS chat_logs (
  id           bigserial PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),
  session_id   text        NOT NULL,
  question     text        NOT NULL,
  answer       text,
  outcome      text        NOT NULL,  -- answered | blocked | rate_limited | invalid | error
  guard_flags  jsonb,
  sanitized    jsonb,                 -- links/emails/numbers stripped from the answer
  tool_calls   int,
  pages_read   text[],
  latency_ms   int,
  model        text,
  kb_version   text,
  feedback     smallint               -- reserved: +1 / -1 from widget later
);
CREATE INDEX IF NOT EXISTS chat_logs_session_ts ON chat_logs (session_id, ts DESC);
CREATE INDEX IF NOT EXISTS chat_logs_ts ON chat_logs (ts DESC);
COMMENT ON TABLE chat_logs IS
  'ACR chatbot v2: one row per visitor message (answered, blocked, rate-limited, error). Used for analytics, eval and per-session rate limiting.';

RESET ROLE;
SELECT current_database() AS db,
       (SELECT count(*) FROM pg_tables WHERE schemaname = 'public'
          AND tablename IN ('chat_histories', 'chat_histories_archive', 'chat_logs')) AS tables_ready,
       (SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_archive_tool_messages') AS trigger_ready;
