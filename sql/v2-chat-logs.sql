-- ACR chatbot v2 — chat log table. Applied to prod on 2026-11-16 (idempotent; safe to re-run).
-- Chat memory itself reuses v1's acr_website_chatbot.chat_histories, which carries the
-- trg_archive_tool_messages trigger (moves tool-call scaffolding rows to
-- chat_histories_archive so the memory window never starts on an orphaned tool message).

SET search_path TO acr_website_chatbot;

CREATE TABLE IF NOT EXISTS acr_website_chatbot.chat_logs (
  id           bigserial PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),
  session_id   text        NOT NULL,
  question     text        NOT NULL,
  answer       text,
  outcome      text        NOT NULL,  -- answered | blocked | rate_limited | invalid | error
  guard_flags  jsonb,                 -- guardrail verdicts
  sanitized    jsonb,                 -- links/emails stripped from the answer
  tool_calls   int,
  pages_read   text[],
  latency_ms   int,
  model        text,
  kb_version   text,
  feedback     smallint               -- reserved: +1 / -1 from widget later
);
CREATE INDEX IF NOT EXISTS chat_logs_session_ts ON acr_website_chatbot.chat_logs (session_id, ts DESC);
CREATE INDEX IF NOT EXISTS chat_logs_ts ON acr_website_chatbot.chat_logs (ts DESC);
COMMENT ON TABLE acr_website_chatbot.chat_logs IS
  'ACR chatbot v2: one row per visitor message (answered, blocked, rate-limited, error). Written by the v2 chat workflow; used for analytics, eval and per-session rate limiting.';
