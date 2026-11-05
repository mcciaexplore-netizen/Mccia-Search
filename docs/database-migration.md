# Moving the database between Postgres hosts (Neon to Supabase, 5 Nov 2026)

Sampada Search keeps everything in one Postgres database with the pgvector extension: `documents`, `chunks` (the page text and its 768-number
vectors), `embedding_meta`, `users`, `audit_log` and `sso_used_jti`. This is how it was copied from Neon to Supabase, and how to do it again.

**Status on 5 Nov 2026:** the copy on Supabase is verified and identical to Neon, but production (the EC2 backend) still reads from Neon until
the cutover below is done. Neon stays untouched as the backup for at least a week after the cutover.

## What to know first

- **Use the pooler address, not `db.<ref>.supabase.co`.** The direct host has an IPv6 address only; neither the EC2 server nor most
  networks have IPv6. The IPv4 route is Supabase's pooler: `aws-0-<region>.pooler.supabase.com`, user `postgres.<project-ref>`.
  Port 5432 is session mode (use it for the restore and for the backend); 6543 is transaction mode (works for the app too, but not for a restore).
- **The `pg_dump` client must be at least as new as the server.** Neon ran PostgreSQL 18; Homebrew's `postgresql@18` provides it
  (`brew install postgresql@18`, keg-only). Dump from Neon's direct address (the host name without `-pooler`).
- **pgvector must live in schema `public`.** The dump writes the column as `public.vector(768)`. `CREATE EXTENSION IF NOT EXISTS vector;`
  on Supabase does that; enabling it from the dashboard puts it in `extensions`, which breaks the restore (move it with
  `ALTER EXTENSION vector SET SCHEMA public;`).
- **Passwords:** keep connection strings in git-ignored files (`.env`, `.env*.local`). Never paste one into a chat, a commit or a command line;
  `pg_dump` and `psql` read `PGHOST`, `PGUSER`, `PGPASSWORD` and `PGDATABASE` from the environment.

## Copy

1. Dump (read-only): `pg_dump --format=custom --compress=5 --no-owner --no-privileges --schema=public --file neon.dump` with the `PG*` variables
   set for Neon. About 223 MB, 6 minutes from Pune.
2. Make the restore list without the schema itself (every target already has `public`):
   `pg_restore -l neon.dump | grep -vE ' SCHEMA - public | COMMENT - SCHEMA public ' > list.txt`
3. On the empty target, over a session connection: `CREATE EXTENSION IF NOT EXISTS vector;`
4. Restore in one transaction, with the 2-minute statement timeout lifted for that session so the search index can build:
   `(printf "SET statement_timeout = 0;\nSET maintenance_work_mem = '256MB';\n"; pg_restore --no-owner --no-privileges -L list.txt -f - neon.dump) | psql -X -q -1 -v ON_ERROR_STOP=1`
   (For a target older than PostgreSQL 17, also drop the `SET transaction_timeout` line with `grep -v transaction_timeout`.)
5. Close Supabase's public data API to the tables (new tables there are readable by its `anon` and `authenticated` roles by default):
   `ALTER TABLE public.<each of the six> ENABLE ROW LEVEL SECURITY;`
   `REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;` (and the same for sequences),
   `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;`
   The backend connects as `postgres`, which bypasses row-level security, so it is unaffected. Also remove `public` from the exposed schemas under Settings > API.

## Verify

Compare both databases with the same queries: counts of `documents` (85) and `chunks` (39,002); `sum(length(content))`; the number of distinct
(document, page); `sum(id)` of chunks; `sum(vector_norm(embedding))`; rows in `users`, `audit_log`, `sso_used_jti`; and the list of indexes (14).
Then check that vector search finds a chunk from its own vector through `chunks_embedding_idx`, and that the application works against the copy
(hybrid search, a real answer, the sign-in and audit writes). Also check the database size against the plan's limit (500 MB on the free plan; this one
is about 413 MB with Supabase's own 10 MB).

## Cutover (production)

1. Stop writes or accept a small catch-up: sign-ins and questions write to `users`, `audit_log` and `sso_used_jti`. Copy those three tables again just before switching.
2. On the server, set `DATABASE_URL` in `~/mccia-search/.env` (the file is baked into the image) to the session-pooler string, then rebuild and swap the
   backend container with the usual deploy script (the previous image is kept as `mccia-backend:prev`).
3. Check `/health`, a chat, the Documents page and a sign-in, and look at the container log for database errors.
4. Keep Neon for a week. To go back: restore the old `DATABASE_URL` line, rebuild, swap.
