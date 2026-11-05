# Switching the embeddings from Gemini to BAAI/bge-m3

**Status on 5 Nov 2026:** production still searches with Gemini (768 numbers per chunk). The tools for a safe switch are written and tested; bge-m3's
vectors have **not** been generated yet. Nothing below changes the live site until the last step (`deploy/bge-switch.sh switch`).

## How it works: two sets of vectors, side by side

Vectors from different models cannot be compared, so a question must be embedded by the model that made the vectors it is compared with. Instead of
replacing Gemini's vectors (which would break search for hours), bge-m3 gets **its own column**:

| | Gemini (today) | bge-m3 (new) |
|---|---|---|
| column | `chunks.embedding` `vector(768)` | `chunks.embedding_bge` `vector(1024)` |
| index | `chunks_embedding_idx` | `chunks_embedding_bge_idx` (built when the fill finishes) |
| selected by | `EMBEDDING_ENGINE=gemini` (default) | `EMBEDDING_ENGINE=bge` |
| cut-off | similarity ≥ 0.45 | similarity ≥ 0.40 (`embedding_engine.MIN_SIMILARITIES`) |

Every backend reads and writes only the column of its own engine (`embedding_engine.column()`), so:

- **Filling** the new column never touches Gemini's vectors or any document's status. The live site keeps working exactly as before.
- **Switching** is replacing the container (Gemini image → bge image); **the database is not changed**.
- **Going back** is starting the old container again. Both directions take about a minute, and no data moves.

(Checked on a scratch database: the new column is added empty without rewriting a single row, `xmin` of every existing row is unchanged, and filling it
leaves Gemini's column byte for byte as it was. See `backend/tests/test_bge_shadow.py`.)

## What it costs (measured on an Apple M2, 5 Nov 2026)

- **Filling:** about 0.43 s per chunk on the Mac's GPU, so about **4.6 hours** for 39,002 chunks. A plain CPU is about twice as slow.
  **Do not fill on the EC2 server:** measured there on 5 Nov 2026 (container limited to 1.5 CPUs at low priority), 32 chunks took 6.0 minutes including
  the model load, about 9-11 s per chunk, so **4-5 days** for the rest. Run the fill on the Mac.
- **The server then runs the model to embed every question:** about 2-3 GB of memory (the server has 7.8 GB, 6.1 GB free on 5 Nov, with the backend
  itself at 0.23 GB), and a question takes 33 ms on the M2's GPU / 90 ms on 2 CPU threads (a 2-vCPU Xeon is a few times slower: expect a few hundred ms,
  about what a call to Gemini costs from there). The model loads in the background when the server starts (10-30 s); a question arriving earlier waits
  for that one load.
- **Disk:** the model files are 2.3 GB, kept in `~/hf-models` on the server (outside the image, so rebuilding is quick). The new column adds about
  160 MB of vectors and about 190 MB of index to the database (Supabase has 8 GB).
- **New issues:** with `EMBEDDING_ENGINE=bge` a sync on the server would embed the new chunks on its 2-vCPU CPU (roughly a second per chunk, so
  10 minutes or more per issue, and the chat slows down meanwhile). Embed new issues from the Mac instead (`parallel_ingest` + `embed_pending`
  with `EMBEDDING_ENGINE=bge`), as the 1970-2017 issues were. The server's container is limited to 4 GB so it can never starve the other apps.

## Steps

### 1. On the Mac: fill the new column (about 4.6 hours)

```bash
cd backend
uv sync --extra local-embeddings          # once; the model downloads the first time (2.3 GB)
# .env: EMBEDDING_ENGINE=bge              # already set on this Mac
uv run python -m ingest.reembed_all                       # shows where things stand, changes nothing
uv run python -m ingest.reembed_all --yes --limit 64      # a 1-2 minute trial first: adds the column, embeds 64 chunks
caffeinate -i uv run python -m ingest.reembed_all --yes   # the whole thing; caffeinate keeps the Mac awake
```

It is safe to stop with Ctrl-C and run again: every batch of 10 chunks is saved as soon as it is done, and the next run carries on with the chunks that
are still empty. When the last chunk is done it builds the search index (a few minutes; searches keep working). **Run it once more right before
going live**, to pick up anything ingested meanwhile.

Close other heavy apps while it runs: the model uses about 2 GB.

### 2. Compare the two on real questions (read-only)

```bash
uv run python -m ingest.compare_embeddings --audit 40 --out ~/Desktop/gemini-vs-bge.md
```

It runs the 40 most recent real questions from the audit log through the real search once per engine and prints the top pages side by side, plus how
often they agree, how often only one finds anything, and the search time. Nobody has marked the right pages, so it cannot name a winner: read the
"ONLY GEMINI FOUND" / "ONLY BGE FOUND" / "different top page" questions by hand and decide. (The 30-question test in the first attempt had the right
chunk at a median similarity of 0.55 for bge and unrelated chunks at 0.35, which is why bge's cut-off is 0.40.)

### 3. Check that everything is ready (read-only)

```bash
uv run python -m ingest.preflight_bge
```

Checks that every chunk has its bge vector, that the index exists, that the model makes unit-length 1024-dimension vectors, that real searches find
the right issues (and respect a year), and how long they take. Any `FAIL` means: do not switch.

### 4. On the server: build, check, switch

```bash
ssh ubuntu@13.126.108.204
cd ~/mccia-search
deploy/bge-switch.sh prepare     # pulls, builds mccia-backend:bge, fetches the model, runs the preflight there. The live container is untouched.
deploy/bge-switch.sh switch      # stops the Gemini container (kept as Mccia-Sampada-Backend-gemini), starts the bge one, checks it,
                                 # and goes back by itself if anything is wrong. About a minute.
deploy/bge-switch.sh status      # which engine is live and its memory
deploy/bge-switch.sh rollback    # the Gemini container back, whenever you want
```

`prepare` builds for several minutes on the same 2 vCPUs that serve the chat, so do it when it is quiet. `switch` leaves one `smoke-check` row in the
audit log (delete it, as after every swap).

### 5. Afterwards

Keep the Gemini container (`Mccia-Sampada-Backend-gemini`) and Gemini's column for about a week as the way back. When you are sure:
`docker rm Mccia-Sampada-Backend-gemini` on the server. Gemini's column (`chunks.embedding`, about 120 MB plus 120 MB of index) can be dropped later
(`ALTER TABLE chunks DROP COLUMN embedding`), but there is no hurry and no size problem.

## Why a column and not a table, and why the index comes last

A separate table would also work; a column keeps the SQL simple (one name changes). On a 4,000-row rehearsal, filling a new column on a table that
already has an HNSW index rewrote 3,434 rows of which 83% were "HOT" updates (no new index entries), the Gemini index did not grow, and the table grew
by 22 MB, 16 MB of which is the new vectors themselves. The one thing to avoid is indexing the new column before it is full: an indexed column cannot
be updated in place, so every update would also write into all the other indexes. That is why `chunks_embedding_bge_idx` is built by
`ingest.reembed_all` when it finishes and is deliberately not in `schema.sql`.

## Things that can go wrong, and what happens

- **The Mac sleeps or the fill is interrupted:** nothing is lost; run it again.
- **The bge backend starts before the fill is complete:** `deploy/bge-switch.sh switch` refuses unless `prepare` passed, and the backend logs
  `WARNING: N chunks have no vector and cannot be found` at start-up. Chunks without a bge vector are simply not found by a bge backend.
- **An issue ingested by the Gemini backend after the fill:** it has a Gemini vector but no bge vector until `reembed_all` runs again (see step 1).
  The opposite also holds after the switch: an issue embedded by the bge backend has no Gemini vector, so run `ingest.embed_pending` with
  `EMBEDDING_ENGINE=gemini` if you roll back (it embeds only what is missing).
- **The server runs out of memory:** the container is limited to 4 GB, so it is restarted alone (`--restart=always`); `deploy/bge-switch.sh rollback`
  returns to Gemini.
- **`EMBEDDING_ENGINE` is mistyped:** the backend refuses to start searching with an unknown engine instead of quietly using Gemini.

## Mac setup notes

While `.env` says `EMBEDDING_ENGINE=bge`, every local script that searches or embeds uses bge's column. The tests do not care (they pin the engine
to Gemini unless a test chooses another). If you want the Mac to behave like production again, set `EMBEDDING_ENGINE=gemini`.
