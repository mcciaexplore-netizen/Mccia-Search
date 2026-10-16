"""pgvector similarity search for relevant chunks."""

import math
import time
from functools import lru_cache

import httpx
import psycopg
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pgvector import Vector
from psycopg_pool import PoolTimeout

from config import GEMINI_API_KEY, GEMINI_EMBEDDING_MODEL
from db.connection import get_pooled_connection

TOP_K = 8  # number of chunks to return
MIN_SIMILARITY = 0.25  # discard chunks below this score
EMBEDDING_MODEL = GEMINI_EMBEDDING_MODEL  # the same setting ingest/embedder.py uses
EMBEDDING_DIMENSION = 768
TASK_TYPE = "RETRIEVAL_QUERY"  # documents are embedded as RETRIEVAL_DOCUMENT

RETRY_DELAYS = (2, 4, 8)  # seconds before retry 1, 2 and 3
REQUEST_TIMEOUT_MS = 120_000

_API_ERRORS = (genai_errors.APIError, httpx.HTTPError, OSError)

_SEARCH_SQL = """
    SELECT
      c.id, c.content, c.page_number, c.chunk_index,
      d.filename, d.drive_file_id, c.document_id,
      1 - (c.embedding <=> %(query)s::vector) AS similarity
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE 1 - (c.embedding <=> %(query)s::vector) >= %(min_similarity)s
    ORDER BY c.embedding <=> %(query)s::vector
    LIMIT %(top_k)s
"""


def embed_query(query: str) -> list[float]:
    """Embed a search query as a retrieval query; returns a 768-float unit vector.

    Transient errors are retried after 2s, 4s and 8s; a RuntimeError is raised if
    the query still cannot be embedded.
    """
    client = _get_client()
    config = types.EmbedContentConfig(
        task_type=TASK_TYPE, output_dimensionality=EMBEDDING_DIMENSION
    )

    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            response = client.models.embed_content(
                model=EMBEDDING_MODEL, contents=[query], config=config
            )
            break
        except _API_ERRORS as exc:
            if delay is None or not _is_retryable(exc):
                raise RuntimeError(f"Embedding the query failed: {_brief(exc)}") from exc
            print(
                f"[search] Gemini error ({_brief(exc)}); "
                f"retry {attempt}/{len(RETRY_DELAYS)} in {delay}s"
            )
            time.sleep(delay)

    embeddings = response.embeddings or []
    values = list(embeddings[0].values or []) if len(embeddings) == 1 else []
    if len(values) != EMBEDDING_DIMENSION:
        raise RuntimeError(
            f"Expected one {EMBEDDING_DIMENSION}-dimension embedding for the query, "
            f"got {len(embeddings)} embedding(s) with {len(values)} dimensions"
        )
    # gemini-embedding-001 only returns unit-length vectors at its full 3072
    # dimensions; shorter ones must be normalised by the caller.
    return _normalize(values)


def search(query: str, top_k: int = TOP_K) -> list[dict]:
    """Return the chunks most similar to the query, best first.

    Each result is {chunk_id, content, page_number, chunk_index, filename,
    drive_file_id, document_id, similarity}, with cosine similarity of at least
    MIN_SIMILARITY.
    Returns an empty list when nothing is similar enough or the query is blank.
    """
    if top_k < 1:
        raise ValueError(f"top_k must be at least 1, got {top_k}")

    results: list[dict] = []
    if query.strip():
        params = {
            "query": Vector(embed_query(query)).to_text(),
            "min_similarity": MIN_SIMILARITY,
            "top_k": top_k,
        }
        rows = _run_search(params)
        results = [
            {
                "chunk_id": chunk_id,
                "content": content,
                "page_number": page_number,
                "chunk_index": chunk_index,
                "filename": filename,
                "drive_file_id": drive_file_id,
                "document_id": document_id,
                "similarity": similarity,
            }
            for (
                chunk_id,
                content,
                page_number,
                chunk_index,
                filename,
                drive_file_id,
                document_id,
                similarity,
            ) in rows
        ]

    top = f"{results[0]['similarity']:.2f}" if results else "n/a"
    print(f"[search] query returned {len(results)} chunks (top similarity: {top})")
    return results


def _run_search(params: dict) -> list[tuple]:
    """Run the similarity query on a pooled connection.

    A pooled connection can go stale while it sits idle (Neon or the network drops
    it). The pool discards a connection that fails, so one retry gets a fresh one.
    """
    for attempt in (1, 2):
        try:
            with get_pooled_connection() as conn:
                return conn.execute(_SEARCH_SQL, params).fetchall()
        except PoolTimeout:
            raise  # every connection is busy; retrying would only wait again
        except psycopg.OperationalError:
            if attempt == 2:
                raise
            print("[search] database connection was stale; retrying once")


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
