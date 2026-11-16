-- ACR Chatbot Knowledge Base — Supabase (Postgres + pgvector) schema
-- Standard n8n LangChain "Supabase Vector Store" layout so the n8n AI nodes
-- (Embeddings + Vector Store + Chat Model sub-nodes) work plug-and-play and
-- models can be swapped in the UI without touching SQL or workflow code.
-- Run once in the Supabase SQL editor before running the n8n workflows.
--
-- Structured fields (post_id, source_type, url, title, modified) live inside
-- the `metadata` JSONB column that the Vector Store node writes automatically
-- from the Default Data Loader metadata mapping.

-- 1. pgvector extension
create extension if not exists vector;

-- 2. Knowledge base: one row per content chunk (n8n Vector Store schema)
create table if not exists documents (
  id        uuid primary key default gen_random_uuid(),
  content   text,                 -- chunk text (LangChain pageContent)
  metadata  jsonb,                -- { post_id, source_type, url, title, modified }
  embedding vector(1536)          -- dim must match the embedding model you pick
);

-- Fast similarity search
create index if not exists documents_embedding_idx
  on documents using ivfflat (embedding vector_cosine_ops);

-- Fast metadata filtering (retrieval filter + lifecycle delete/upsert by item)
create index if not exists documents_metadata_idx
  on documents using gin (metadata);

-- Speeds up delete-by-item and orphan reconcile (Workflows A / A2)
create index if not exists documents_post_source_idx
  on documents ((metadata->>'post_id'), (metadata->>'source_type'));

-- 3. Incremental-sync watermark (Workflow A reads/writes 'last_sync')
create table if not exists sync_state (
  key   text primary key,
  value timestamptz
);
insert into sync_state (key, value)
  values ('last_sync', '1970-01-01T00:00:00Z')
  on conflict (key) do nothing;

-- 3b. URL blocklist — pages to skip on ingest and purge from the KB.
--     Store the normalized pathname, no trailing slash (e.g. '/resources').
--     Read by Workflow A (Sectionize: skip insert) and A2 (Fetch Live IDs:
--     treat as orphan → nightly delete). Add/remove rows to manage the list.
create table if not exists acr_website_chatbot.kb_blocklist (
  path     text primary key,      -- URL pathname, no trailing slash
  note     text,                  -- why it's blocked (optional)
  added_at timestamptz default now()
);
insert into acr_website_chatbot.kb_blocklist (path, note) values
  ('/resources', 'empty page — menus only'),
  ('/icic-merchant-response', 'empty page — junk content')
  on conflict (path) do nothing;

-- 4. Similarity search RPC used by the n8n Vector Store node (Workflow C).
--    Signature matches what the node's "Query Name" (match_documents) expects:
--    (query_embedding, match_count, filter) -> (id, content, metadata, similarity).
--    url/title are read from metadata by the Build Context node.
create or replace function match_documents(
  query_embedding vector(1536),
  match_count     int default null,
  filter          jsonb default '{}'
) returns table (
  id         uuid,
  content    text,
  metadata   jsonb,
  similarity float
)
language plpgsql stable as $$
#variable_conflict use_column
begin
  return query
  select
    id,
    content,
    metadata,
    1 - (documents.embedding <=> query_embedding) as similarity
  from documents
  where metadata @> filter
  order by documents.embedding <=> query_embedding
  limit match_count;
end;
$$;
