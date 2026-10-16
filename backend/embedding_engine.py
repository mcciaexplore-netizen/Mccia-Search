"""Which model makes the embeddings: Gemini (the default) or a model on this machine.

Vectors from different models cannot be compared with each other, even when they have the
same length. So the whole database must be embedded by one model, and questions must be
embedded by that same model. The database records which model that is (embedding_meta), and
ensure_matches_database() refuses to continue if this process is set up for another one.
"""

import threading
from functools import lru_cache

from config import EMBEDDING_ENGINE, GEMINI_EMBEDDING_MODEL, LOCAL_EMBEDDING_MODEL
from db.connection import get_connection

ENGINES = ("gemini", "local")  # the values EMBEDDING_ENGINE accepts
EMBEDDING_DIMENSION = 768  # the width of chunks.embedding in the database
LOCAL_BATCH_SIZE = 16  # texts the local model reads at once

_checked = False
_check_lock = threading.Lock()


class EmbeddingModelMismatch(RuntimeError):
    """This process would embed with a different model than the one behind the stored vectors."""


def engine() -> str:
    """The engine in use; an unknown value is an error, never a quiet switch to Gemini."""
    if EMBEDDING_ENGINE not in ENGINES:
        raise RuntimeError(
            f"Unknown EMBEDDING_ENGINE {EMBEDDING_ENGINE!r}. Set it to one of: {', '.join(ENGINES)}."
        )
    return EMBEDDING_ENGINE


def model_id() -> str:
    """Names the model behind this process's vectors, e.g. "gemini:gemini-embedding-001"."""
    if engine() == "local":
        return f"local:{LOCAL_EMBEDDING_MODEL}"
    return f"gemini:{GEMINI_EMBEDDING_MODEL}"


def embed_local(texts: list[str]) -> list[list[float]]:
    """Embed texts with the local model; returns one unit-length 768-float vector per text."""
    if not texts:
        return []
    model = _local_model()
    try:
        vectors = model.encode(
            texts,
            batch_size=LOCAL_BATCH_SIZE,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
    except Exception as exc:  # the library raises whatever its tokenizer or model code raises
        raise RuntimeError(f"Local embedding failed: {str(exc)[:200]}") from exc
    return [vector.tolist() for vector in vectors]


def ensure_matches_database() -> None:
    """Raise EmbeddingModelMismatch unless the stored vectors were made by this process's model.

    A database that has chunks but no record was embedded by Gemini before this check
    existed. The check is made once per process, after it has passed.
    """
    global _checked
    if _checked:
        return
    with _check_lock:
        if _checked:
            return
        current = model_id()
        with get_connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS embedding_meta ("
                "id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id), model TEXT NOT NULL)"
            )
            row = conn.execute("SELECT model FROM embedding_meta").fetchone()
            if row is None:
                has_chunks = conn.execute("SELECT EXISTS (SELECT 1 FROM chunks)").fetchone()[0]
                first = f"gemini:{GEMINI_EMBEDDING_MODEL}" if has_chunks else current
                conn.execute(
                    "INSERT INTO embedding_meta (model) VALUES (%s) ON CONFLICT DO NOTHING", (first,)
                )
                row = conn.execute("SELECT model FROM embedding_meta").fetchone()
            conn.commit()
        recorded = row[0]
        if recorded != current:
            raise EmbeddingModelMismatch(
                f"The database's vectors were made by {recorded!r}, but this process embeds with "
                f"{current!r}. Vectors from different models cannot be compared. Set "
                "EMBEDDING_ENGINE back, or re-embed everything with the new model: "
                "uv run python -m ingest.reembed"
            )
        _checked = True


def record_model(conn) -> None:
    """Record this process's model as the one behind the stored vectors (used by re-embedding)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embedding_meta ("
        "id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id), model TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO embedding_meta (model) VALUES (%s) "
        "ON CONFLICT (id) DO UPDATE SET model = EXCLUDED.model",
        (model_id(),),
    )


@lru_cache(maxsize=1)
def _local_model():
    """The sentence-transformers model, loaded once; the first time, it is downloaded."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "EMBEDDING_ENGINE=local needs the sentence-transformers package. "
            "Run `uv sync --extra local-embeddings` in backend/."
        ) from exc
    print(f"[embed] loading the local model {LOCAL_EMBEDDING_MODEL}...")
    model = SentenceTransformer(LOCAL_EMBEDDING_MODEL, device="cpu")
    dimension = model.get_sentence_embedding_dimension()
    if dimension != EMBEDDING_DIMENSION:
        raise RuntimeError(
            f"{LOCAL_EMBEDDING_MODEL} makes {dimension}-dimension vectors, but the database "
            f"stores {EMBEDDING_DIMENSION}. Use a {EMBEDDING_DIMENSION}-dimension model, such "
            "as paraphrase-multilingual-mpnet-base-v2."
        )
    return model
