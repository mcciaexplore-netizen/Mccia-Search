# Switching the embeddings from Gemini to BAAI/bge-m3

**Status on 5 Nov 2026:** the code supports both (`EMBEDDING_ENGINE=gemini|local|bge`, added in commit 7ba2771 on 17 Oct and made selectable again
the next day). Production still uses Gemini (768 numbers per chunk), and nothing has been re-embedded. Setting `EMBEDDING_ENGINE=bge` in a `.env`
only changes what that process would do; the stored vectors stay Gemini's until every chunk is embedded again, because vectors from different models
cannot be compared. The backend refuses to run with the wrong model (`embedding_meta` records which one made the stored vectors).

## Why it is a real project, not a setting

- **All 39,002 chunks must be embedded again** with bge-m3 (1024 numbers each). Measured on an Apple M2 with its GPU, on real chunks (about 940 tokens
  each): **0.43 s per chunk, about 4.6 hours**. Run it on a Mac, not on the EC2 server (2 vCPU).
- **The server must run the model to embed every question.** Measured: about 2 GB of memory per process; one short question takes 33 ms on the M2's
  GPU and 90 ms on 2 CPU threads (a 2-vCPU Xeon will be a few times slower, so expect a few hundred ms, about what a call to Gemini costs from there).
  The server (7.8 GB, 2 vCPU) had 6.1 GB free on 5 Nov, with our backend at 0.23 GB and the other apps at about 0.8 GB.
- **The image grows.** `sentence-transformers` is an optional extra on purpose (`uv sync --extra local-embeddings`), so production does not carry
  torch while it uses Gemini. For bge the image needs torch (use the CPU wheels: `--extra-index-url https://download.pytorch.org/whl/cpu`, not the
  CUDA ones) and the model files (about 2.3 GB, baked in or on a volume).
- **Storage is not a problem:** 39,002 x 1024 numbers is about 160 MB against 120 MB now; with the index the chunks table grows from about 400 MB to about
  540 MB. New issues are embedded on the server's CPU (a few seconds per chunk) instead of by a Gemini call.
- **Search quality:** on 30 real questions in the first attempt, the right chunk scored a median 0.55 (0.41 at worst) and unrelated chunks about 0.35,
  so bge's cut-off is 0.40 (Gemini's is 0.45; see `embedding_engine.MIN_SIMILARITIES`). It has not been compared side by side with Gemini on the
  current hybrid search; do that before switching (step 4 below).

## Two ways to do it

**A. `python -m ingest.reembed_all --yes`** (exists). It deletes every vector first, marks every completed document "waiting for embeddings", then
embeds for about 4.6 hours. While it runs, **search finds nothing**, and the running backend (still on Gemini) fails every question once the column
changes. Only use it for a database nobody is using. Without `--yes` it only prints what it would do.

**B. Shadow column (recommended for the live site; not written yet).** Search keeps working throughout.
1. `ALTER TABLE chunks ADD COLUMN embedding_bge vector(1024);` (empty, nullable: search ignores it).
2. Fill it from a Mac in batches of 8 (`WHERE embedding_bge IS NULL`, so it can be stopped and restarted): about 4.6 hours.
3. Build an HNSW index on it (minutes).
4. Compare both columns on real questions side by side, and decide with evidence.
5. Switch in one short step: in one transaction rename `embedding` to `embedding_gemini` and `embedding_bge` to `embedding`, rename the index, set
   `embedding_meta` to `bge:BAAI/bge-m3`; deploy the backend image that has bge and `EMBEDDING_ENGINE=bge` at the same moment (about a minute).
6. Keep `embedding_gemini` for about a week as the way back, then drop it.

## Setting up a Mac for it

`cd backend && uv sync --extra local-embeddings`, then `EMBEDDING_ENGINE=bge` in `.env`. The first run downloads the model (about 2.2 GB). While
`.env` says bge and the database still says Gemini, every local script that searches or embeds stops with a clear "model mismatch" message instead of
writing vectors of the wrong kind.
