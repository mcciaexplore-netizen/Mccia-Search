"""Embed every chunk with BAAI/bge-m3 on this machine (1024 dimensions, free), without disturbing the live site.

    uv run python -m ingest.reembed_all                # shows how far it is, changes nothing
    uv run python -m ingest.reembed_all --yes          # fills the new column, then builds its search index
    uv run python -m ingest.reembed_all --yes --limit 64   # a short trial first: embeds only 64 chunks

Needs EMBEDDING_ENGINE=bge in .env and `uv sync --extra local-embeddings`.

The vectors go into a column of their own, chunks.embedding_bge, next to Gemini's chunks.embedding. Nothing of Gemini's is deleted
or changed, and no document's status is touched, so a site that searches with Gemini keeps working, exactly as before, for as long as
this runs (and after it). Only a backend started with EMBEDDING_ENGINE=bge reads the new column. Going live is therefore just starting
that backend, and going back is starting the Gemini one again: the database needs no change either way.

What it does:
  1. Adds the column if the database does not have it yet (empty, nullable: instant).
  2. Embeds every chunk whose embedding_bge is empty, 10 at a time (the model reads 8 at once), each batch saved as soon as it is done, and prints a
     progress line every 10 chunks:  [bge] 576/39002 chunks embedded (1.5%) — 110.5 chunks/min — ETA: 5h 48m
  3. When no chunk is left, builds the search index on the column (a few minutes; writes to chunks wait meanwhile, searches do not).

It is safe to stop (Ctrl-C) and run again: it carries on with the chunks that are still empty, and so it also catches up with issues
ingested after an earlier run. Run it once more right before going live to pick up anything added meanwhile.

Speed, measured on an Apple M2 on 5 Nov 2026: about 0.43 s per chunk, so about 4.6 hours for the 39,002 chunks. A MacBook that goes to
sleep stops it (the work already saved is kept); keep it awake with: caffeinate -i uv run python -m ingest.reembed_all --yes
"""

import argparse
import sys
import time

import embedding_engine
from db.connection import get_connection, init_db
from ingest import embedder
from ingest.embed_pending import EmbeddingStopped, _embed_one_batch

FETCH_SIZE = 10  # chunks taken and saved together; the model itself reads embedding_engine.BATCH_SIZES["bge"] of them at once
PROGRESS_EVERY = 10  # chunks between progress lines (a line is printed after the batch that reaches this many more)
INDEX_NAME = "chunks_embedding_bge_idx"
INDEX_MEMORY = "256MB"  # maintenance_work_mem for the index build: faster than the default, and safe on a small database server


def format_eta(seconds: float) -> str:
    """How long to go, as a person reads it: "4h 32m", "45m", or "<1m"."""
    if seconds < 60:
        return "<1m"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def progress_line(embedded: int, total: int, rate_per_minute: float | None) -> str:
    """The progress line: "[bge] 576/39002 chunks embedded (1.5%) — 110.5 chunks/min — ETA: 5h 48m".

    `embedded` counts every chunk that has its vector, including those of earlier runs. The speed is None until it has been measured, and the
    ETA is for the whole archive, not for a --limit.
    """
    percent = 100 * embedded / total if total else 100.0
    line = f"[bge] {embedded}/{total} chunks embedded ({percent:.1f}%)"
    if not rate_per_minute or rate_per_minute <= 0:
        return line + " — measuring speed…"
    left = max(total - embedded, 0)
    eta = "done" if left == 0 else format_eta(left / rate_per_minute * 60)
    return f"{line} — {rate_per_minute:.1f} chunks/min — ETA: {eta}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--yes", action="store_true", help="really start (without it, only show where things stand)")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many chunks (a trial run)")
    parser.add_argument("--fetch-size", type=int, default=FETCH_SIZE, help="chunks taken and saved together (default %(default)s)")
    args = parser.parse_args(argv)
    if args.fetch_size < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--fetch-size and --limit must be at least 1")

    if embedding_engine.engine() != "bge":
        print(f"EMBEDDING_ENGINE is {embedding_engine.engine()!r}. Set EMBEDDING_ENGINE=bge in .env first.")
        return 2

    state = _state()
    _print_state(state)
    if not args.yes:
        if state["missing"] == 0 and state["has_column"]:
            print("\nNothing to embed." + ("" if state["has_index"] else " Run with --yes to build the search index."))
        else:
            print(
                f"\nThis would embed {state['missing']} chunks into chunks.embedding_bge "
                f"(about {state['missing'] * 0.43 / 3600:.1f} hours on an Apple M2) and leave everything else alone.\n"
                "Gemini's vectors and the documents are not touched: the live site keeps working. Run again with --yes to do it."
            )
        return 0

    init_db()  # adds chunks.embedding_bge if this database does not have it yet
    embedding_engine.ensure_matches_database()
    state = _state()
    missing, total, already = state["missing"], state["total"], state["filled"]
    if missing == 0:
        print("Nothing to embed: every chunk already has its bge-m3 vector.")
        return _finish()
    target = min(missing, args.limit) if args.limit else missing
    print(f"\nEmbedding {target} of {missing} chunks that have no bge-m3 vector yet. Ctrl-C stops it; run it again to carry on.")
    print(progress_line(already, total, None), flush=True)

    done, started = 0, time.monotonic()
    shown = 0  # chunks done when the last progress line was printed
    first_at, first_done = None, 0  # the speed is measured from the end of the first batch, which leaves out loading the model
    try:
        with embedder.quiet():  # the progress line says it all; the model's own line per batch would double the log
            while done < target:
                claimed = _embed_one_batch(min(args.fetch_size, target - done), finish_documents=False)
                if claimed == 0:
                    break
                done += claimed
                now = time.monotonic()
                if first_at is None:
                    first_at, first_done = now, done
                if done - shown >= PROGRESS_EVERY:
                    print(progress_line(already + done, total, _rate(done - first_done, now - first_at)), flush=True)
                    shown = done
    except EmbeddingStopped as exc:
        print(f"Stopped after {done} chunks: {exc}\nRun it again to carry on from here.")
        return 1
    except KeyboardInterrupt:
        print(f"Interrupted after {done} chunks (they are saved). Run it again to carry on.")
        return 130

    if done != shown:
        print(progress_line(already + done, total, _rate(done - first_done, time.monotonic() - first_at) if first_at else None), flush=True)
    print(f"Embedded {done} chunks in {(time.monotonic() - started) / 60:.1f} min.")
    if args.limit and done < missing:
        print(f"{missing - done} chunks still to do (this was a trial run: run again without --limit).")
        return 0
    return _finish()


def _rate(chunks: int, seconds: float) -> float | None:
    """Chunks per minute over a stretch of time; None while there is nothing to measure."""
    return chunks / (seconds / 60) if chunks > 0 and seconds > 0 else None


def _finish() -> int:
    """Build the search index once every chunk has its vector, and report where things stand."""
    state = _state()
    if state["missing"]:
        print(f"{state['missing']} chunks still have no bge-m3 vector (saved while this ran?). Run it again.")
        return 1
    if not state["has_index"]:
        print(f"Building the search index {INDEX_NAME} (a few minutes; searches keep working)...")
        started = time.monotonic()
        with get_connection() as conn:
            conn.autocommit = True  # an index build cannot run inside a transaction block here
            conn.execute("SET statement_timeout = 0")
            conn.execute(f"SET maintenance_work_mem = '{INDEX_MEMORY}'")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON chunks USING hnsw (embedding_bge vector_cosine_ops)")
        print(f"Index built in {(time.monotonic() - started) / 60:.1f} min.")
    state = _state()
    print(f"Done: all {state['total']} chunks have a bge-m3 vector and the search index exists.")
    print("The live site is unchanged. Compare the two:  uv run python -m ingest.compare_embeddings --audit 40")
    return 0


def _state() -> dict:
    """Where the fill stands (reads only): whether the column and its index exist, and how many chunks have a vector."""
    with get_connection() as conn:
        has_column = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'chunks' AND column_name = 'embedding_bge')"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        filled = conn.execute("SELECT COUNT(embedding_bge) FROM chunks").fetchone()[0] if has_column else 0
        has_index = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (INDEX_NAME,)).fetchone()[0]
    return {"has_column": has_column, "total": total, "filled": filled, "missing": total - filled, "has_index": has_index}


def _print_state(state: dict) -> None:
    print(f"Model for the new vectors: {embedding_engine.model_id()} ({embedding_engine.dimension()} dimensions)")
    if not state["has_column"]:
        print("Column chunks.embedding_bge: not there yet (--yes adds it)")
    else:
        print(f"Column chunks.embedding_bge: {state['filled']} of {state['total']} chunks filled; search index {'built' if state['has_index'] else 'not built yet'}")


if __name__ == "__main__":
    sys.exit(main())
