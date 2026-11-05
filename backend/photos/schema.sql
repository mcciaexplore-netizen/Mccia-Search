-- The photo pipeline's tables. Applied by photos.db.init_tables() when the pipeline really writes, NOT by db/schema.sql, so that the live database
-- gets nothing of this until it is wanted.

-- Photographs cut out of the scanned pages (see photos/): one row per photograph, advertisement or illustration. clip_embedding (CLIP ViT-B/16, 512
-- numbers) finds a picture by what it shows; description is the caption printed next to it (read by OCR), which is where names are searched. The picture
-- itself lives in the private Supabase Storage bucket "sampada-images" at storage_path (NULL until uploaded). contains_face is detection only.
CREATE TABLE IF NOT EXISTS photo_index (
  id             BIGSERIAL PRIMARY KEY,
  document_id    BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_num       INTEGER NOT NULL,
  box_index      INTEGER NOT NULL,  -- which picture on the page: 0, 1, ...
  kind           TEXT NOT NULL CHECK (kind IN ('photo', 'advertisement', 'illustration')),
  kind_score     REAL,
  storage_path   TEXT,
  clip_embedding vector(512),
  description    TEXT,
  contains_face  BOOLEAN NOT NULL DEFAULT FALSE,
  face_count     INTEGER NOT NULL DEFAULT 0,
  whole_page     BOOLEAN NOT NULL DEFAULT FALSE,  -- the picture fills the page (a cover, a full-page advertisement)
  bbox           REAL[],                          -- left, top, right, bottom as shares of the page (0-1)
  width          INTEGER,
  height         INTEGER,
  file_size      INTEGER,
  created_at     TIMESTAMPTZ DEFAULT now(),
  UNIQUE (document_id, page_num, box_index)
);

CREATE INDEX IF NOT EXISTS photo_index_page_idx ON photo_index (document_id, page_num);
CREATE INDEX IF NOT EXISTS photo_index_clip_idx ON photo_index USING hnsw (clip_embedding vector_cosine_ops);

-- Which pages have been looked at, so a stopped run carries on and a page with no photograph is not looked at again.
CREATE TABLE IF NOT EXISTS photo_scan (
  document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_num    INTEGER NOT NULL,
  n_photos    INTEGER NOT NULL,
  seconds     REAL,
  scanned_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (document_id, page_num)
);

-- Supabase hands every table in "public" to its public Data API unless told otherwise: row-level security on (no policy = no rows) and the API
-- roles stripped of access. The backend connects as the database owner, which is not affected. A database that is not Supabase has no such roles.
-- Each step is checked first, so a startup does not lock a table when there is nothing to change.
DO $$
DECLARE
  t TEXT;
  r TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['photo_index', 'photo_scan'] LOOP
    IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid = t::regclass) THEN
      EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    END IF;
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) AND has_table_privilege(r, t, 'SELECT, INSERT, UPDATE, DELETE') THEN
        EXECUTE format('REVOKE ALL ON %I FROM %I', t, r);
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) AND t = 'photo_index' AND has_sequence_privilege(r, 'photo_index_id_seq', 'USAGE') THEN
        EXECUTE format('REVOKE ALL ON SEQUENCE photo_index_id_seq FROM %I', r);
      END IF;
    END LOOP;
  END LOOP;
END $$;
