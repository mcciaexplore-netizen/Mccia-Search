CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
  id            BIGSERIAL PRIMARY KEY,
  filename      TEXT NOT NULL,
  drive_file_id TEXT NOT NULL UNIQUE,
  total_pages   INTEGER,
  language      TEXT,
  ingested_at   TIMESTAMPTZ DEFAULT now(),
  status        TEXT DEFAULT 'pending'
);

-- An existing database keeps the width it has: CREATE TABLE IF NOT EXISTS does not alter a table.
-- ingest.reembed_all changes it to 1024 and re-embeds every chunk with bge-m3.
CREATE TABLE IF NOT EXISTS chunks (
  id          BIGSERIAL PRIMARY KEY,
  document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  content     TEXT NOT NULL,
  embedding   vector(1024) NOT NULL,  -- BAAI/bge-m3 (EMBEDDING_ENGINE=bge); gemini and local make 768
  page_number INTEGER,
  created_at  TIMESTAMPTZ DEFAULT now(),
  UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
  ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS chunks_document_id_idx
  ON chunks (document_id);

CREATE INDEX IF NOT EXISTS documents_drive_file_id_idx
  ON documents (drive_file_id);

-- Audit log: one row per question asked and per sync, so staff activity can be reviewed.
CREATE TABLE IF NOT EXISTS audit_log (
  id          BIGSERIAL PRIMARY KEY,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  event       TEXT NOT NULL,   -- 'chat' or 'sync'
  query       TEXT,            -- the question, or a short note for other events
  found       BOOLEAN,         -- chat: were any documents found?
  sources     INTEGER,         -- chat: how many sources were cited
  status      INTEGER,         -- HTTP-style result: 200, 400, 500
  duration_ms INTEGER,
  client      TEXT,            -- the caller's address
  detail      TEXT
);

CREATE INDEX IF NOT EXISTS audit_log_occurred_at_idx
  ON audit_log (occurred_at DESC);

-- Which model made the vectors in chunks. Vectors from different models cannot be compared, so
-- the app records the model here and refuses to mix them (see embedding_engine.py).
CREATE TABLE IF NOT EXISTS embedding_meta (
  id    BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),  -- always TRUE: this table has one row
  model TEXT NOT NULL
);

-- A chunk may be saved before it is embedded (ingest.parallel_ingest saves text only, and
-- ingest.embed_pending adds the vectors later). Search skips chunks that have no vector.
-- Checked first, so a startup does not lock the table when there is nothing to change.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'chunks' AND column_name = 'embedding' AND is_nullable = 'NO'
  ) THEN
    ALTER TABLE chunks ALTER COLUMN embedding DROP NOT NULL;
  END IF;
END $$;

-- Single sign-on from the App Hub. users: everyone who has signed in (one row per hub user, kept up to date at every
-- sign-in). sso_used_jti: the sign-in tokens already spent; a token's id (jti) can be inserted only once, which makes the
-- replay check atomic. Rows are only needed until their token expires, and are cleaned up as people sign in.
CREATE TABLE IF NOT EXISTS users (
  id          BIGSERIAL PRIMARY KEY,
  hub_user_id TEXT NOT NULL UNIQUE,
  email       TEXT NOT NULL,
  name        TEXT NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('user', 'admin')),
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sso_used_jti (
  jti        TEXT PRIMARY KEY,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sso_used_jti_expires_idx ON sso_used_jti (expires_at);

-- Who asked each question. Checked first, so a startup does not lock the table when there is nothing to change.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns WHERE table_name = 'audit_log' AND column_name = 'user_id'
  ) THEN
    ALTER TABLE audit_log ADD COLUMN user_id TEXT, ADD COLUMN user_email TEXT;
  END IF;
END $$;
