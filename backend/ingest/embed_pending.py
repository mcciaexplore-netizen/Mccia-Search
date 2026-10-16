"""Embed the chunks that were saved without embeddings.

ingest.parallel_ingest saves the text of each PDF without calling any embedding model, so
the OCR phase is free and uses no Gemini quota. Run this once OCR is done:

    uv run python -m ingest.embed_pending

It embeds in small batches with a pause between them, so a free key's limits are respected.
A document becomes searchable (status 'completed') as soon as its last chunk has its vector.
It is safe to stop it (Ctrl-C) and run it again: it carries on with the chunks still without
a vector. Two runs at once share the work without embedding any chunk twice.
"""

import argparse
import sys
import time

from pgvector import Vector

from db.connection import get_connection, init_db
from embedding_engine import ensure_matches_database
from ingest.embedder import embed_texts

BATCH_SIZE = 5  # chunks per embedding request
DELAY_SECONDS = 3.0  # pause between batches
RETRY_WAIT_SECONDS = 60  # pause before a failed batch is tried again (a rate limit needs time)
MAX_BATCH_RETRIES = 3


class EmbeddingStopped(RuntimeError):
    """A batch kept failing, so the run stops; running it again continues from there."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"chunks per request (default {BATCH_SIZE})")
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS, help=f"seconds between batches (default {DELAY_SECONDS:g})")
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.delay < 0:
        parser.error("--delay cannot be negative")

    init_db()  # makes sure chunks.embedding may be empty, however this database was made
    ensure_matches_database()  # new vectors must come from the model behind the stored ones

    with get_connection() as conn:
        todo = _count_missing(conn)
        documents = conn.execute(
            "SELECT COUNT(DISTINCT document_id) FROM chunks WHERE embedding IS NULL"
        ).fetchone()[0]
        _finish_documents(conn)  # a document whose last chunk was embedded before an interruption
        conn.commit()
    if todo == 0:
        print("Nothing to embed: every chunk already has its embedding.")
        return 0
    print(f"{todo} chunks in {documents} documents to embed, {args.batch_size} at a time, {args.delay:g}s apart")

    done = 0
    try:
        while True:
            claimed = _embed_one_batch(args.batch_size)
            if claimed == 0:
                break
            done += claimed
            print(f"  {done}/{todo}", flush=True)
            if args.delay:
                time.sleep(args.delay)
    except EmbeddingStopped as exc:
        print(f"Stopped after {done} chunks: {exc}")
        print("Run it again later to carry on from here.")
        return 1
    except KeyboardInterrupt:
        print(f"Interrupted after {done} chunks. Run it again to carry on.")
        return 130

    print(f"Done: {done} chunks embedded.")
    return 0


def _embed_one_batch(batch_size: int) -> int:
    """Embed the next chunks that have no vector; returns how many it handled (0 when none left).

    The chunks are locked while they are embedded (SKIP LOCKED), so a second run takes
    other chunks instead of paying for the same ones. A failed batch changes nothing.
    """
    with get_connection() as conn, conn.transaction():
        rows = conn.execute(
            "SELECT id, content FROM chunks WHERE embedding IS NULL "
            "ORDER BY document_id, chunk_index LIMIT %s FOR UPDATE SKIP LOCKED",
            (batch_size,),
        ).fetchall()
        if not rows:
            return 0
        vectors = _embed_with_retries([content for _, content in rows])
        conn.cursor().executemany(
            "UPDATE chunks SET embedding = %s::vector WHERE id = %s",
            [(Vector(vector).to_text(), chunk_id) for (chunk_id, _), vector in zip(rows, vectors)],
        )
        _finish_documents(conn)
    return len(rows)


def _embed_with_retries(texts: list[str]) -> list[list[float]]:
    """embed_texts already retries briefly; this waits longer, which a rate limit needs."""
    for attempt in range(1, MAX_BATCH_RETRIES + 2):
        try:
            return embed_texts(texts)
        except RuntimeError as exc:
            if attempt > MAX_BATCH_RETRIES:
                raise EmbeddingStopped(str(exc)[:200]) from exc
            print(f"  batch failed ({str(exc)[:120]}); waiting {RETRY_WAIT_SECONDS}s, retry {attempt}/{MAX_BATCH_RETRIES}")
            time.sleep(RETRY_WAIT_SECONDS)
    raise AssertionError("unreachable")


def _finish_documents(conn) -> int:
    """Mark documents 'completed' once none of their chunks lacks a vector; returns how many."""
    cur = conn.execute(
        """
        UPDATE documents SET status = 'completed'
        WHERE status = 'text_saved'
          AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = documents.id AND c.embedding IS NULL)
        """
    )
    return cur.rowcount


def _count_missing(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]


if __name__ == "__main__":
    sys.exit(main())
