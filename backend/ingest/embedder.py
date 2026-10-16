"""Generate Gemini embeddings for chunks and store them in Postgres."""

import math
import time
from functools import lru_cache

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pgvector import Vector

from config import GEMINI_API_KEY, GEMINI_EMBEDDING_MODEL
from db.connection import get_connection

# text-embedding-004 was shut down on 2026-01-14. gemini-embedding-001 is its
# API-compatible successor and keeps task_type; Google's newer gemini-embedding-2
# has no task_type and needs different prompts. Set GEMINI_EMBEDDING_MODEL in .env
# to switch; stored chunks must then be re-embedded with the same model.
EMBEDDING_MODEL = GEMINI_EMBEDDING_MODEL
EMBEDDING_DIMENSION = 768
BATCH_SIZE = 20  # chunks to embed per API call
TASK_TYPE = "RETRIEVAL_DOCUMENT"

RETRY_DELAYS = (2, 4, 8)  # seconds before retry 1, 2 and 3
REQUEST_TIMEOUT_MS = 120_000

# "text_saved": the text is stored without embeddings, which ingest.embed_pending adds later.
VALID_STATUSES = ("pending", "processing", "text_saved", "completed", "failed")

_API_ERRORS = (genai_errors.APIError, httpx.HTTPError, OSError)

_INSERT_CHUNK = """
    INSERT INTO chunks (document_id, chunk_index, content, page_number, embedding)
    VALUES (%s, %s, %s, %s, %s::vector)
    ON CONFLICT (document_id, chunk_index) DO NOTHING
"""

_INSERT_CHUNK_TEXT = """
    INSERT INTO chunks (document_id, chunk_index, content, page_number)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (document_id, chunk_index) DO NOTHING
"""


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts as retrieval documents; returns one 768-float vector per text.

    Texts go to Gemini BATCH_SIZE at a time. Transient errors are retried after
    2s, 4s and 8s; a RuntimeError is raised if a batch still fails.
    """
    if not texts:
        return []

    client = _get_client()
    config = types.EmbedContentConfig(
        task_type=TASK_TYPE, output_dimensionality=EMBEDDING_DIMENSION
    )
    total_batches = math.ceil(len(texts) / BATCH_SIZE)

    embeddings: list[list[float]] = []
    for batch_number, start in enumerate(range(0, len(texts), BATCH_SIZE), start=1):
        batch = texts[start : start + BATCH_SIZE]
        print(f"[embed] batch {batch_number}/{total_batches} ({len(batch)} chunks)")
        embeddings.extend(_embed_batch(client, batch, config, batch_number))
    return embeddings


def store_chunks(chunks: list[dict], embeddings: list[list[float]]) -> int:
    """Insert chunks and their embeddings in a single transaction.

    Rows whose (document_id, chunk_index) already exist are skipped. Returns the
    number of rows actually inserted; if any insert fails, none are kept.
    """
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"Got {len(chunks)} chunks but {len(embeddings)} embeddings; they must match"
        )
    for index, embedding in enumerate(embeddings):
        if len(embedding) != EMBEDDING_DIMENSION:
            raise ValueError(
                f"Embedding {index} has {len(embedding)} values, "
                f"expected {EMBEDDING_DIMENSION}"
            )

    inserted = 0
    if chunks:
        rows = [
            (
                chunk["document_id"],
                chunk["chunk_index"],
                chunk["content"],
                chunk["page_number"],
                Vector(embedding).to_text(),
            )
            for chunk, embedding in zip(chunks, embeddings)
        ]
        with get_connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(_INSERT_CHUNK, rows)
                inserted = cur.rowcount

    print(f"[embed] stored {inserted} chunks in database")
    return inserted


def store_chunk_text(chunks: list[dict]) -> int:
    """Insert chunks without embeddings (the vector stays NULL) in a single transaction.

    No embedding model is called, so this costs nothing. ingest.embed_pending fills in the
    vectors later; search skips chunks that have none. Rows whose (document_id,
    chunk_index) already exist are skipped. Returns the number of rows inserted.
    """
    inserted = 0
    if chunks:
        rows = [
            (chunk["document_id"], chunk["chunk_index"], chunk["content"], chunk["page_number"])
            for chunk in chunks
        ]
        with get_connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(_INSERT_CHUNK_TEXT, rows)
                inserted = cur.rowcount
    print(f"[embed] stored the text of {inserted} chunks, without embeddings")
    return inserted


def embed_and_store(chunks: list[dict]) -> int:
    """Embed the chunks' text and store everything; returns the rows inserted."""
    embeddings = embed_texts([chunk["content"] for chunk in chunks])
    return store_chunks(chunks, embeddings)


def update_document_status(document_id: int, status: str) -> None:
    """Set documents.status for a document (pending/processing/completed/failed)."""
    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}; must be one of: {', '.join(VALID_STATUSES)}"
        )
    with get_connection() as conn, conn.transaction():
        cur = conn.execute(
            "UPDATE documents SET status = %s WHERE id = %s", (status, document_id)
        )
        if cur.rowcount == 0:
            raise LookupError(f"No document with id {document_id}")


@lru_cache(maxsize=1)
def _get_client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to .env "
            "(get one from https://aistudio.google.com)."
        )
    return genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )


def _embed_batch(
    client: genai.Client,
    batch: list[str],
    config: types.EmbedContentConfig,
    batch_number: int,
) -> list[list[float]]:
    """Embed one batch with retries; returns normalised, validated vectors."""
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            response = client.models.embed_content(
                model=EMBEDDING_MODEL, contents=batch, config=config
            )
            break
        except _API_ERRORS as exc:
            if delay is None or not _is_retryable(exc):
                raise RuntimeError(
                    f"Embedding batch {batch_number} failed: {_brief(exc)}"
                ) from exc
            print(
                f"[embed] batch {batch_number}: Gemini error ({_brief(exc)}); "
                f"retry {attempt}/{len(RETRY_DELAYS)} in {delay}s"
            )
            time.sleep(delay)

    vectors = [list(e.values or []) for e in response.embeddings or []]
    if len(vectors) != len(batch):
        raise RuntimeError(
            f"Embedding batch {batch_number}: sent {len(batch)} texts, "
            f"got {len(vectors)} embeddings"
        )
    for vector in vectors:
        if len(vector) != EMBEDDING_DIMENSION:
            raise RuntimeError(
                f"Embedding batch {batch_number}: got {len(vector)} dimensions, "
                f"expected {EMBEDDING_DIMENSION}"
            )
    # gemini-embedding-001 only returns unit-length vectors at its full 3072
    # dimensions; shorter ones must be normalised by the caller.
    return [_normalize(vector) for vector in vectors]


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def _is_retryable(exc: Exception) -> bool:
    """Retry network failures, rate limits and 5xx; fail fast on other API errors."""
    if isinstance(exc, genai_errors.APIError):
        return exc.code in (408, 429) or exc.code >= 500
    return True


def _brief(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:200]
