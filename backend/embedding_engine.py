"""Which model makes the embeddings: Gemini (the default) or a model on this machine.

The engines (EMBEDDING_ENGINE): "gemini" (768 dimensions), "local" (LOCAL_EMBEDDING_MODEL,
768) and "bge" (BAAI/bge-m3, 1024). The last two run on this machine, free and offline.

Vectors from different models cannot be compared with each other, even when they have the
same length. So the whole database must be embedded by one model, and questions must be
embedded by that same model. The database records which model that is (embedding_meta), and
ensure_matches_database() refuses to continue if this process is set up for another one.
"""

import threading
from functools import lru_cache

from config import EMBEDDING_ENGINE, GEMINI_EMBEDDING_MODEL, LOCAL_EMBEDDING_MODEL
from db.connection import get_connection

ENGINES = ("gemini", "local", "bge")  # the values EMBEDDING_ENGINE accepts
LOCAL_ENGINES = ("local", "bge")  # the engines that run a model on this machine
BGE_MODEL = "BAAI/bge-m3"
# The width of the vectors each engine makes, which chunks.embedding in the database must match.
DIMENSIONS = {"gemini": 768, "local": 768, "bge": 1024}
GEMINI_DIMENSION = DIMENSIONS["gemini"]
BATCH_SIZES = {"local": 16, "bge": 8}  # texts a local model reads at once
# The lowest cosine similarity at which a chunk still counts as a match. Each model spreads its
# scores differently. Measured on 30 real questions: Gemini scores run higher, bge-m3 puts the
# right chunk at a median 0.55 (0.41 at worst) and unrelated chunks around 0.35 (0.45 at the 95th
# percentile), so 0.50 would drop 4 of those 30 right chunks and 0.40 drops none.
MIN_SIMILARITIES = {"gemini": 0.50, "local": 0.50, "bge": 0.40}

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


def dimension() -> int:
    """The width of the vectors the engine in use makes."""
    return DIMENSIONS[engine()]


def min_similarity() -> float:
    """The lowest similarity that counts as a match for the engine in use."""
    return MIN_SIMILARITIES[engine()]


def uses_local_model() -> bool:
    """Whether the engine in use embeds on this machine instead of calling Gemini."""
    return engine() in LOCAL_ENGINES


def model_id() -> str:
    """Names the model behind this process's vectors, e.g. "gemini:gemini-embedding-001"."""
    if engine() == "bge":
        return f"bge:{BGE_MODEL}"
    if engine() == "local":
        return f"local:{LOCAL_EMBEDDING_MODEL}"
    return f"gemini:{GEMINI_EMBEDDING_MODEL}"


def embed_local(texts: list[str]) -> list[list[float]]:
    """Embed texts with the local model; returns one unit-length vector per text (dimension())."""
    if not texts:
        return []
    name = engine()
    model = _local_model(name)
    try:
        vectors = model.encode(
            texts,
            batch_size=BATCH_SIZES[name],
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
                "EMBEDDING_ENGINE back, or re-embed everything with the new model "
                "(uv run python -m ingest.reembed_all for bge, ingest.reembed for the others)."
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


@lru_cache(maxsize=2)
def _local_model(name: str):
    """The sentence-transformers model of a local engine, loaded once; the first time, downloaded."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            f"EMBEDDING_ENGINE={name} needs the sentence-transformers package. "
            "Run `uv sync` in backend/."
        ) from exc
    model_name = BGE_MODEL if name == "bge" else LOCAL_EMBEDDING_MODEL
    print(f"[embed] loading the local model {model_name}...")
    # bge-m3 picks the fastest device itself (a GPU, Apple's MPS, or the CPU); "local" stays on the CPU.
    model = SentenceTransformer(model_name, **({} if name == "bge" else {"device": "cpu"}))
    found = model.get_embedding_dimension()
    if found != DIMENSIONS[name]:
        raise RuntimeError(
            f"{model_name} makes {found}-dimension vectors, but the database stores "
            f"{DIMENSIONS[name]} for EMBEDDING_ENGINE={name}. Use a {DIMENSIONS[name]}-dimension model."
        )
    return model
