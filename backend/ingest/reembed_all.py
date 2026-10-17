"""Re-embed every chunk with BAAI/bge-m3, on this machine (1024 dimensions, free).

Needed once, when the database still holds Gemini vectors (768 dimensions): vectors from
different models cannot be compared, so every chunk has to be embedded again.

    uv run python -m ingest.reembed_all          # shows the plan and changes nothing
    uv run python -m ingest.reembed_all --yes    # does it (EMBEDDING_ENGINE=bge must be set)

What it does:
  1. Once: drops every stored vector, widens chunks.embedding to vector(1024) and records
     bge-m3 as the model behind the vectors. Documents that were completed become
     'text_saved' (shown as waiting for embeddings) until all their chunks have a new vector.
  2. Embeds every chunk that has no vector, 8 at a time. This covers the chunks of completed and
     text_saved documents, and any other chunk (a failed document's, or one saved meanwhile).
  3. Builds the search index and marks the documents completed again.

It is safe to stop (Ctrl-C) and run again, with or without --yes: the first step happens only
once, so a run carries on with the chunks that still have no vector. Two runs at once share
the work.

The old vectors cannot be brought back (Gemini would have to be paid again), and while this
runs, search finds only the chunks that are already done. A server still set up for Gemini
cannot search at all until it runs the new code with EMBEDDING_ENGINE=bge.
Roughly 0.5 s per chunk on a Mac with Apple's GPU, 1 s on a plain CPU: hours for the full archive.
"""

import argparse
import sys
import time

import embedding_engine
from db.connection import get_connection
from ingest.embed_pending import EmbeddingStopped, _embed_one_batch, _finish_documents

PROGRESS_EVERY = 5  # batches between progress lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--yes", action="store_true", help="really start (without it, only show the plan)")
    parser.add_argument(
        "--batch-size", type=int, default=embedding_engine.BATCH_SIZES["bge"],
        help="chunks the model reads at once (default %(default)s)",
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    if embedding_engine.engine() != "bge":
        print(f"EMBEDDING_ENGINE is {embedding_engine.engine()!r}. Set EMBEDDING_ENGINE=bge in .env first.")
        return 2
    width, model = embedding_engine.dimension(), embedding_engine.model_id()

    with get_connection() as conn:
        found_width = _column_width(conn)
        recorded = _recorded_model(conn)
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        completed = conn.execute("SELECT COUNT(*) FROM documents WHERE status = 'completed'").fetchone()[0]
        missing = _count_missing(conn)
    migrated = found_width == width and recorded == model
    print(f"Model for new vectors: {model} ({width} dimensions)")
    print(f"Database: vectors are {found_width} dimensions, made by {recorded or '(none recorded)'}")
    print(f"Chunks: {total} ({missing if migrated else total} to embed)")

    if not migrated:
        if not args.yes:
            print(
                f"\nThis would DELETE the vectors of all {total} chunks, set the column to "
                f"vector({width}) and re-embed every chunk with {model}.\n"
                "Search is incomplete until it has finished. Run again with --yes to do it."
            )
            return 0
        with get_connection() as conn, conn.transaction():
            _drop_vectors(conn, width)
            embedding_engine.record_model(conn)
        print(f"Dropped the old vectors; chunks.embedding is now vector({width}). {completed} documents wait for their new vectors.")
        missing = total
    elif missing == 0:
        print("Nothing to embed: every chunk already has its bge-m3 vector.")
        return _finish() if _needs_finishing() else 0
    else:
        print("Carrying on from an earlier run.")

    done, started, batches = 0, time.monotonic(), 0
    try:
        while True:
            claimed = _embed_one_batch(args.batch_size)
            if claimed == 0:
                break
            done, batches = done + claimed, batches + 1
            if batches % PROGRESS_EVERY == 0:
                rate = done / (time.monotonic() - started)
                left = max(missing - done, 0)
                print(f"  {done}/{missing}  {rate * 60:.0f} chunks/min  about {left / rate / 3600:.1f} h to go", flush=True)
    except EmbeddingStopped as exc:
        print(f"Stopped after {done} chunks: {exc}\nRun it again to carry on from here.")
        return 1
    except KeyboardInterrupt:
        print(f"Interrupted after {done} chunks. Run it again to carry on.")
        return 130

    print(f"Embedded {done} chunks.")
    return _finish()


def _drop_vectors(conn, width: int) -> None:
    """Empty chunks.embedding and set its width, and mark completed documents as waiting.

    The column is rewritten, which also gives back the space the old vectors took. Run in one
    transaction by the caller: either all of this happens or none of it.
    """
    conn.execute("DROP INDEX IF EXISTS chunks_embedding_idx")  # rebuilt at the end, not once per update
    conn.execute("ALTER TABLE chunks ALTER COLUMN embedding DROP NOT NULL")
    conn.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({width}) USING NULL::vector({width})")
    conn.execute("UPDATE documents SET status = 'text_saved' WHERE status = 'completed'")


def _finish() -> int:
    """Build the search index, mark documents completed, and check that no chunk lacks a vector."""
    print("Building the search index...")
    with get_connection() as conn:
        conn.autocommit = True  # an index build is not part of a transaction here
        conn.execute("CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops)")
    with get_connection() as conn:
        finished = _finish_documents(conn)
        conn.commit()
        missing = _count_missing(conn)
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if missing:
        print(f"{missing} chunks still have no vector (saved while this ran?). Run it again.")
        return 1
    print(f"Done: all {total} chunks are embedded by {embedding_engine.model_id()}; {finished} documents are completed again.")
    return 0


def _needs_finishing() -> bool:
    """Whether the index or the documents' status were left unfinished by an interrupted run."""
    with get_connection() as conn:
        has_index = conn.execute("SELECT to_regclass('chunks_embedding_idx') IS NOT NULL").fetchone()[0]
        waiting = conn.execute("SELECT COUNT(*) FROM documents WHERE status = 'text_saved'").fetchone()[0]
    return not has_index or waiting > 0


def _column_width(conn) -> int | None:
    """The number of dimensions chunks.embedding is declared with (None if it has no fixed width)."""
    width = conn.execute(
        "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()[0]
    return width if width > 0 else None


def _recorded_model(conn) -> str | None:
    """The model recorded behind the stored vectors; reads only, so a plan changes nothing."""
    if conn.execute("SELECT to_regclass('embedding_meta') IS NOT NULL").fetchone()[0]:
        row = conn.execute("SELECT model FROM embedding_meta").fetchone()
        if row is not None:
            return row[0]
    # Chunks with no record were embedded by Gemini, before the record existed.
    has_chunks = conn.execute("SELECT EXISTS (SELECT 1 FROM chunks)").fetchone()[0]
    return f"gemini:{embedding_engine.GEMINI_EMBEDDING_MODEL}" if has_chunks else None


def _count_missing(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]


if __name__ == "__main__":
    sys.exit(main())
