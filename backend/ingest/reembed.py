"""Re-embed every stored chunk with the model this process is set up for.

Needed once when EMBEDDING_ENGINE (or LOCAL_EMBEDDING_MODEL) changes: vectors from different
models cannot be compared, so the database must be embedded by a single model. It only works
between models of the same width (768 dimensions); for bge-m3 (1024) use ingest.reembed_all.

    uv run python -m ingest.reembed          # shows the plan and changes nothing
    uv run python -m ingest.reembed --yes    # does it

The new vectors are written to a spare column. The old ones stay in use, and search keeps
working, until every chunk has a new one; then all of them are swapped in one transaction and
the new model is recorded. An interrupted run is simply started again and carries on.
Do not ingest while it runs: a chunk added with the old model would be missed.
"""

import argparse
import sys

from pgvector import Vector

import embedding_engine
from db.connection import get_connection
from ingest.embedder import embed_texts

BATCH_SIZE = 64


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--yes", action="store_true", help="really re-embed (without it, only show the plan)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    if embedding_engine.dimension() != 768:
        print(
            f"EMBEDDING_ENGINE={embedding_engine.engine()} makes {embedding_engine.dimension()}-dimension "
            "vectors, which do not fit the 768-dimension column. Use: uv run python -m ingest.reembed_all"
        )
        return 2

    current = embedding_engine.model_id()
    with get_connection() as conn:
        recorded = _recorded_model(conn)
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        todo = _count_todo(conn)
    print(f"Embedding model of this process: {current}")
    print(f"Model behind the stored vectors: {recorded or '(none recorded)'}")
    print(f"Chunks: {total}")

    if recorded == current:
        print("Nothing to do: the stored vectors already come from this model.")
        return 0
    if not args.yes:
        print(f"This would re-embed {total} chunks. Run again with --yes to do it.")
        return 0

    with get_connection() as conn:
        conn.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_new vector(768)")
        conn.commit()
        todo = _count_todo(conn)
    print(f"Re-embedding {todo} chunks ({total - todo} already done by an earlier run)...")

    done = 0
    while True:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, content FROM chunks WHERE embedding_new IS NULL ORDER BY id LIMIT %s",
                (args.batch_size,),
            ).fetchall()
        if not rows:
            break
        vectors = embed_texts([content for _, content in rows])
        with get_connection() as conn, conn.transaction():
            conn.cursor().executemany(
                "UPDATE chunks SET embedding_new = %s::vector WHERE id = %s",
                [(Vector(vector).to_text(), chunk_id) for (chunk_id, _), vector in zip(rows, vectors)],
            )
        done += len(rows)
        print(f"  {done}/{todo}", flush=True)

    with get_connection() as conn, conn.transaction():
        conn.execute("UPDATE chunks SET embedding = embedding_new")
        conn.execute("ALTER TABLE chunks DROP COLUMN embedding_new")
        embedding_engine.record_model(conn)
    print(f"Done: {total} chunks are now embedded by {current}.")
    return 0


def _recorded_model(conn) -> str | None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embedding_meta ("
        "id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id), model TEXT NOT NULL)"
    )
    row = conn.execute("SELECT model FROM embedding_meta").fetchone()
    if row is not None:
        return row[0]
    # Chunks with no record were embedded by Gemini, before the record existed.
    has_chunks = conn.execute("SELECT EXISTS (SELECT 1 FROM chunks)").fetchone()[0]
    return f"gemini:{embedding_engine.GEMINI_EMBEDDING_MODEL}" if has_chunks else None


def _count_todo(conn) -> int:
    """Chunks still to re-embed: all of them, or those a half-finished run has not reached."""
    has_column = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'chunks' AND column_name = 'embedding_new')"
    ).fetchone()[0]
    if not has_column:
        return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding_new IS NULL").fetchone()[0]


if __name__ == "__main__":
    sys.exit(main())
