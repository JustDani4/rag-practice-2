CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    path text PRIMARY KEY,
    sha256 text NOT NULL,
    embedding_revision text NOT NULL,
    pipeline_revision text NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id text PRIMARY KEY,
    path text NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
    locator text NOT NULL,
    content text NOT NULL,
    embedding vector(768) NOT NULL,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('russian'::regconfig, content)) STORED
);

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_fts_gin ON chunks USING gin (fts);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
