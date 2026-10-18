"""pgvector similarity search for relevant chunks."""

import math
import re
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
from embedding_engine import (
    GEMINI_DIMENSION,
    embed_local,
    ensure_matches_database,
    min_similarity,
    uses_local_model,
)

TOP_K = 5  # number of chunks to return
EMBEDDING_MODEL = GEMINI_EMBEDDING_MODEL  # the same setting ingest/embedder.py uses
EMBEDDING_DIMENSION = GEMINI_DIMENSION  # what Gemini is asked for; bge-m3 makes 1024, see dimension()
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

# The same search over the chunks of some documents only. The approximate (HNSW) index finds
# its nearest chunks across the whole archive and only then drops those outside the documents,
# which can leave too few; the MATERIALIZED pool is an exact search over just those documents.
_DOCUMENT_SEARCH_SQL = """
    WITH pool AS MATERIALIZED (
      SELECT id, content, page_number, chunk_index, document_id, embedding
      FROM chunks
      WHERE document_id = ANY(%(document_ids)s)
    )
    SELECT
      c.id, c.content, c.page_number, c.chunk_index,
      d.filename, d.drive_file_id, c.document_id,
      1 - (c.embedding <=> %(query)s::vector) AS similarity
    FROM pool c
    JOIN documents d ON d.id = c.document_id
    WHERE 1 - (c.embedding <=> %(query)s::vector) >= %(min_similarity)s
    ORDER BY c.embedding <=> %(query)s::vector
    LIMIT %(top_k)s
"""

# A year in a question, in Latin or Devanagari digits (Marathi and Hindi write १९६५ for 1965).
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_LATIN_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def embed_query(query: str) -> list[float]:
    """Embed a search query as a retrieval query; returns a unit vector (dimension() floats).

    Transient errors are retried after 2s, 4s and 8s; a RuntimeError is raised if
    the query still cannot be embedded. With EMBEDDING_ENGINE=local or bge it is embedded
    on this machine instead.
    """
    if uses_local_model():
        return embed_local([query])[0]
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


def years_in(text: str) -> list[int]:
    """The years (1900-2099) a question names, each once, in the order they appear."""
    years = (int(year) for year in _YEAR.findall(text.translate(_LATIN_DIGITS)))
    return list(dict.fromkeys(years))


def volume_years(filename: str) -> tuple[int, int] | None:
    """The first and last year an issue covers, read from its file name.

    "1964-July To1965-June Part-1.pdf" is (1964, 1965); "1945 July.PDF" is (1945, 1945).
    None when the name holds no year.
    """
    years = [int(year) for year in _YEAR.findall(filename)]
    return (min(years), max(years)) if years else None


def documents_for_years(years: list[int]) -> list[int]:
    """The ids of the documents whose issues cover any of the years."""
    with get_pooled_connection() as conn:
        documents = conn.execute("SELECT id, filename FROM documents").fetchall()
    ids = []
    for document_id, filename in documents:
        span = volume_years(filename)
        if span and any(span[0] <= year <= span[1] for year in years):
            ids.append(document_id)
    return ids


def search(query: str, top_k: int = TOP_K) -> list[dict]:
    """Return the chunks most similar to the query, best first.

    Each result is {chunk_id, content, page_number, chunk_index, filename,
    drive_file_id, document_id, similarity}, with cosine similarity of at least the
    engine's min_similarity() (0.45 for Gemini, 0.40 for bge-m3). When the query names a
    year (such as 1965), only chunks of the issues that cover that year are searched.
    Returns an empty list when nothing is similar enough, no issue covers the year, or the
    query is blank.
    """
    if top_k < 1:
        raise ValueError(f"top_k must be at least 1, got {top_k}")

    results: list[dict] = []
    rows: list[tuple] = []
    if query.strip():
        ensure_matches_database()  # the query must come from the model behind the stored vectors
        years = years_in(query)
        document_ids = documents_for_years(years) if years else None
        if years:
            print(f"[search] year filter {years}: {len(document_ids)} matching document(s)")
        if document_ids != []:  # a year that no issue covers has nothing to search
            params = {
                "query": Vector(embed_query(query)).to_text(),
                "min_similarity": min_similarity(),  # per engine: each model scores differently
                "top_k": top_k,
            }
            if document_ids is not None:
                params["document_ids"] = document_ids
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
            sql = _DOCUMENT_SEARCH_SQL if "document_ids" in params else _SEARCH_SQL
            with get_pooled_connection() as conn:
                return conn.execute(sql, params).fetchall()
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
