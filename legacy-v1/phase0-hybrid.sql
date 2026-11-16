-- Phase 0 — Production RAG migration (hybrid retrieval + recency)
-- Additive to schema.sql. Idempotent. Run once in Supabase SQL editor.
-- Keeps url/title/modified inside metadata JSONB (n8n Vector Store node stays plug-and-play).
-- Leaves match_documents() untouched for backward compat; adds hybrid_search().

-- 1. Contextual-retrieval prefix (debug/transparency).
--    Ingest (Workflow A) also PREPENDS this prefix into `content` before embedding,
--    so the standard embed node embeds context+content together. This column just
--    keeps the raw prefix visible for eval/debug; it is not embedded on its own.
alter table documents add column if not exists context text;

-- 2. BM25 keyword arm: full-text search vector generated from content.
--    (content already includes the contextual prefix at ingest time.)
alter table documents
  add column if not exists fts tsvector
  generated always as (to_tsvector('english', coalesce(content, ''))) stored;

-- 3. Indexes
create index if not exists documents_fts_idx
  on documents using gin (fts);

-- Recency ordering. ISO-8601 timestamps sort correctly as text, so a btree on the
-- raw metadata field is enough for filtering/ordering; the RPC casts for decay math.
create index if not exists documents_modified_idx
  on documents ((metadata->>'modified'));

-- 4. Hybrid search RPC: semantic (pgvector) + keyword (BM25), fused with RRF,
--    then multiplied by a recency time-decay. No external reranker.
--
--    final = [ 1/(rrf_k+sem_rank) + 1/(rrf_k+kw_rank) ] * exp(-lambda * age_days)
--
--    Params:
--      query_text       raw user query (for BM25 / websearch_to_tsquery)
--      query_embedding  embedding of the same query (1536 dims)
--      match_count      rows to return (default 5)
--      full_count       candidates pulled per arm before fusion (default 30)
--      rrf_k            RRF damping constant (default 50; higher = flatter)
--      lambda           recency decay per day (default 0.01 ≈ ~half weight at 70 days;
--                       set 0 to disable recency)
--      filter           metadata @> filter, e.g. '{"source_type":"faq"}'
create or replace function hybrid_search(
  query_text      text,
  query_embedding vector(1536),
  match_count     int   default 5,
  full_count      int   default 30,
  rrf_k           int   default 50,
  lambda          float default 0.01,
  filter          jsonb default '{}'
) returns table (
  id       uuid,
  content  text,
  metadata jsonb,
  score    float
)
language sql stable as $$
  with sem as (
    select d.id,
           row_number() over (order by d.embedding <=> query_embedding) as rank
    from documents d
    where d.metadata @> filter
    order by d.embedding <=> query_embedding
    limit full_count
  ),
  kw as (
    select d.id,
           row_number() over (
             order by ts_rank(d.fts, websearch_to_tsquery('english', query_text)) desc
           ) as rank
    from documents d
    where d.metadata @> filter
      and d.fts @@ websearch_to_tsquery('english', query_text)
    limit full_count
  ),
  fused as (
    select coalesce(sem.id, kw.id) as id,
           coalesce(1.0 / (rrf_k + sem.rank), 0.0)
         + coalesce(1.0 / (rrf_k + kw.rank),  0.0) as rrf
    from sem
    full outer join kw on sem.id = kw.id
  )
  select
    d.id,
    d.content,
    d.metadata,
    (
      f.rrf * exp(
        -lambda * greatest(
          extract(epoch from now()
            - coalesce((d.metadata->>'modified')::timestamptz, now())
          ) / 86400.0,
          0.0
        )
      )
    )::float as score
  from fused f
  join documents d on d.id = f.id
  order by score desc
  limit match_count;
$$;

-- Quick smoke test (replace the embedding with a real 1536-vector):
--   select id, metadata->>'url' as url, metadata->>'modified' as modified, score
--   from hybrid_search('payment request', '[...1536 floats...]'::vector, 5);
