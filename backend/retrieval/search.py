"""Hybrid search for relevant chunks: pgvector similarity plus a PostgreSQL full-text keyword score."""

import math
import re
import string
import time
from dataclasses import dataclass
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
# Hybrid scoring: the CANDIDATES chunks most similar to the question (and at least min_similarity() similar) are re-scored as
#   score = VECTOR_WEIGHT * similarity + KEYWORD_WEIGHT * keyword_score
# where keyword_score is the share of the question's words (0..1) that a chunk contains, found with PostgreSQL full-text search.
# A chunk that is not semantically close enough is never returned, however many of the words it has.
CANDIDATES = 30
VECTOR_WEIGHT = 0.6
KEYWORD_WEIGHT = 0.4
MAX_KEYWORDS = 12  # the question's words used for the keyword score; a longer question is cut here
EMBEDDING_MODEL = GEMINI_EMBEDDING_MODEL  # the same setting ingest/embedder.py uses
EMBEDDING_DIMENSION = GEMINI_DIMENSION  # what Gemini is asked for; bge-m3 makes 1024, see dimension()
TASK_TYPE = "RETRIEVAL_QUERY"  # documents are embedded as RETRIEVAL_DOCUMENT

RETRY_DELAYS = (2, 4, 8)  # seconds before retry 1, 2 and 3
REQUEST_TIMEOUT_MS = 120_000

_API_ERRORS = (genai_errors.APIError, httpx.HTTPError, OSError)

# The keyword half, shared by both searches below. For each candidate chunk it is the share (0..1) of the question's words,
# %(terms)s (a text[] of lower-case words, digits written 0-9), that the chunk contains. The match is PostgreSQL full-text search (the
# 'simple' configuration: no stemming, no stop words, so it suits Marathi, Hindi and English alike) run on the candidates only, so it
# needs no index and no schema change. Two things make it work for Marathi and Hindi pages: the chunk's Devanagari digits are written
# 0-9 first (the tokenizer ignores digits like १९६५, so a year in them could never match), and a word that is not plain
# ASCII also matches as plain text (how a tokenizer splits Devanagari depends on the database's locale).
_KEYWORD_SCORE = """
    SELECT
      k.id, k.content, k.page_number, k.chunk_index, k.filename, k.drive_file_id, k.document_id, k.similarity,
      CASE WHEN cardinality(%(terms)s::text[]) = 0 THEN 0.0 ELSE
        (SELECT count(*) FROM unnest(%(terms)s::text[]) AS t(term)
         WHERE v.tsv @@ websearch_to_tsquery('simple', t.term)
            OR (t.term !~ '^[ -~]+$' AND position(t.term IN v.norm) > 0))::float8
        / cardinality(%(terms)s::text[])
      END AS keyword_score
    FROM candidates k
    CROSS JOIN LATERAL (SELECT lower(translate(k.content, '०१२३४५६७८९', '0123456789')) AS norm) n
    CROSS JOIN LATERAL (SELECT n.norm AS norm, to_tsvector('simple', n.norm) AS tsv) v
"""

_SEARCH_SQL = """
    WITH candidates AS (
      SELECT
        c.id, c.content, c.page_number, c.chunk_index,
        d.filename, d.drive_file_id, c.document_id,
        1 - (c.embedding <=> %(query)s::vector) AS similarity
      FROM chunks c
      JOIN documents d ON d.id = c.document_id
      WHERE 1 - (c.embedding <=> %(query)s::vector) >= %(min_similarity)s
      ORDER BY c.embedding <=> %(query)s::vector
      LIMIT %(candidates)s
    )
""" + _KEYWORD_SCORE

# The same search over the chunks of some documents only. The approximate (HNSW) index finds
# its nearest chunks across the whole archive and only then drops those outside the documents,
# which can leave too few; the MATERIALIZED pool is an exact search over just those documents.
_DOCUMENT_SEARCH_SQL = """
    WITH pool AS MATERIALIZED (
      SELECT id, content, page_number, chunk_index, document_id, embedding
      FROM chunks
      WHERE document_id = ANY(%(document_ids)s)
    ),
    candidates AS (
      SELECT
        c.id, c.content, c.page_number, c.chunk_index,
        d.filename, d.drive_file_id, c.document_id,
        1 - (c.embedding <=> %(query)s::vector) AS similarity
      FROM pool c
      JOIN documents d ON d.id = c.document_id
      WHERE 1 - (c.embedding <=> %(query)s::vector) >= %(min_similarity)s
      ORDER BY c.embedding <=> %(query)s::vector
      LIMIT %(candidates)s
    )
""" + _KEYWORD_SCORE

# A year in a question, in Latin or Devanagari digits (Marathi and Hindi write १९६५ for 1965).
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_LATIN_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Words that say nothing about what a question is about (English, Marathi, Hindi), and the names that nearly every page of this archive
# carries: they are left out of the keyword score so that it measures the words that tell one page from another.
_PUNCTUATION = string.punctuation + "“”‘’…–—।॥"
_STOPWORDS = frozenset(
    """a an the of in on at to for from by with and or is are was were be been being who whom whose what which when where why how
    did do does done has have had it its this that these those about tell me more please give list name any there their his her he
    she they them also as than then into during after before between mccia sampada magazine issue
    काय कोण कोणी कुठे केव्हा कसे कशी का आहे आहेत होते होता होती आणि व किंवा या ते ती तो त्या हा ही हे चे ची चा च्या मध्ये साठी कडे
    पासून बद्दल माहिती सांगा द्या संपदा
    क्या कौन कहाँ कब कैसे क्यों है हैं था थे थी और या का की के को में से पर लिए बारे जानकारी बताइए बताओ""".split()
)


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


def keyword_terms(query: str) -> list[str]:
    """The words of a question that the keyword score looks for, each once, in order (at most MAX_KEYWORDS).

    Lower-cased, with stop words and one-letter words dropped and digits written 0-9 (a year typed as १९६५ is looked for as
    1965; the search side writes the pages' digits the same way).
    """
    terms: list[str] = []
    for raw in query.lower().split():
        word = raw.strip(_PUNCTUATION).translate(_LATIN_DIGITS)
        if len(word) < 2 or word in _STOPWORDS:
            continue
        terms.append(word)
    return list(dict.fromkeys(terms))[:MAX_KEYWORDS]


def volume_label(filename: str) -> str | None:
    """How an issue's years read in the answer: "1964-65" for a volume that runs over two years, "1945" for one year.

    "1964-July To1965-June Part-1.pdf" is "1964-65"; "1945 July.PDF" is "1945"; "1999-Jan To 2000-Dec.pdf" is "1999-2000".
    None when the file name holds no year.
    """
    span = volume_years(filename)
    if span is None:
        return None
    first, last = span
    if first == last:
        return str(first)
    return f"{first}-{str(last)[2:]}" if last - first == 1 and first // 100 == last // 100 else f"{first}-{last}"


@dataclass(frozen=True)
class YearScope:
    """Which part of the archive a question is limited to.

    `years` are the years the question names (none: nothing is limited). `document_ids` are the issues that cover any of them
    (None when no year is named; an empty tuple when no issue covers the year, so there is nothing to search). `issues` are those
    issues' years as they read in an answer, oldest first, each once ("1964-65", "1965-66").
    """

    years: tuple[int, ...] = ()
    document_ids: tuple[int, ...] | None = None
    issues: tuple[str, ...] = ()


def year_scope(query: str) -> YearScope:
    """The years the question names and the issues that cover them: a year limits the search to those issues alone.

    An issue covers a year when the years in its file name run from before it to after it (or are it): "1964-July To1965-June" and
    "1965-July To1966-June" both cover 1965, "1963-April To1964-March" does not. No year in the question: no limit.
    """
    years = years_in(query)
    if not years:
        return YearScope()
    with get_pooled_connection() as conn:
        documents = conn.execute("SELECT id, filename FROM documents").fetchall()
    ids, spans = [], {}
    for document_id, filename in documents:
        span = volume_years(filename)
        if span and any(span[0] <= year <= span[1] for year in years):
            ids.append(document_id)
            spans[volume_label(filename)] = span
    return YearScope(tuple(years), tuple(ids), tuple(sorted(spans, key=lambda label: (spans[label], label))))


def documents_for_years(years: list[int]) -> list[int]:
    """The ids of the documents whose issues cover any of the years."""
    return list(year_scope(" ".join(map(str, years))).document_ids or ())


def search(query: str, top_k: int = TOP_K, scope: YearScope | None = None) -> list[dict]:
    """Return the chunks that best answer the query, best first (hybrid: meaning plus keywords).

    Each result is {chunk_id, content, page_number, chunk_index, filename, drive_file_id, document_id,
    similarity, keyword_score, score}. `similarity` is the cosine similarity of the chunk to the question and is at least
    the engine's min_similarity() (0.45 for Gemini, 0.40 for bge-m3): a chunk below it is never returned. `keyword_score` is
    the share (0..1) of the question's words the chunk contains (PostgreSQL full-text search), and the results are ordered by
    `score` = VECTOR_WEIGHT * similarity + KEYWORD_WEIGHT * keyword_score, among the CANDIDATES most similar chunks. When the
    query names a year (such as 1965), only chunks of the issues that cover that year are searched, and never any other year's:
    there is no fallback to the rest of the archive. `scope` is that limit when the caller has already worked it out (year_scope).
    Returns an empty list when nothing is similar enough, no issue covers the year, or the query is blank.
    """
    if top_k < 1:
        raise ValueError(f"top_k must be at least 1, got {top_k}")

    results: list[dict] = []
    if query.strip():
        ensure_matches_database()  # the query must come from the model behind the stored vectors
        scope = scope or year_scope(query)
        if scope.years:
            print(f"[search] year filter {list(scope.years)}: {len(scope.document_ids)} matching document(s)")
        if scope.document_ids is None or scope.document_ids:  # a year that no issue covers has nothing to search
            params = {
                "query": Vector(embed_query(query)).to_text(),
                "min_similarity": min_similarity(),  # per engine: each model scores differently
                "candidates": max(CANDIDATES, top_k),
                "terms": keyword_terms(query),
            }
            if scope.document_ids is not None:
                params["document_ids"] = list(scope.document_ids)
            results = rank(_run_search(params), top_k)

    top = f"{results[0]['similarity']:.2f}" if results else "n/a"
    print(f"[search] query returned {len(results)} chunks (top similarity: {top})")
    return results


def rank(rows: list[tuple], top_k: int) -> list[dict]:
    """Score the candidate rows (see _KEYWORD_SCORE for their columns) and keep the best top_k, best first."""
    results = []
    for chunk_id, content, page_number, chunk_index, filename, drive_file_id, document_id, similarity, keyword_score in rows:
        similarity, keyword_score = float(similarity), float(keyword_score)
        results.append(
            {
                "chunk_id": chunk_id,
                "content": content,
                "page_number": page_number,
                "chunk_index": chunk_index,
                "filename": filename,
                "drive_file_id": drive_file_id,
                "document_id": document_id,
                "similarity": similarity,
                "keyword_score": keyword_score,
                "score": VECTOR_WEIGHT * similarity + KEYWORD_WEIGHT * keyword_score,
            }
        )
    results.sort(key=lambda r: (-r["score"], -r["similarity"], r["chunk_id"]))
    return results[:top_k]


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
