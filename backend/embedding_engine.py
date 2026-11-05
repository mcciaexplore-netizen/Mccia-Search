"""Which model makes the embeddings: Gemini (the default) or a model on this machine.

The engines (EMBEDDING_ENGINE): "gemini" (768 dimensions), "local" (LOCAL_EMBEDDING_MODEL,
768) and "bge" (BAAI/bge-m3, 1024). The last two run on this machine, free and offline.

Vectors from different models cannot be compared with each other, even when they have the
same length. So a question must be embedded by the model that made the vectors it is compared
with. Gemini and "local" share chunks.embedding, and the database records which of the two made
it (embedding_meta); ensure_matches_database() refuses to continue if this process is set up for
the other. bge-m3 has a column of its own, chunks.embedding_bge, next to the Gemini one: the two
sets of vectors live side by side, so a backend can be switched between them (and back) just by
changing EMBEDDING_ENGINE, with no change to the database (see docs/embeddings-bge-m3.md).
"""

import threading
from contextlib import contextmanager
from functools import lru_cache

from config import EMBEDDING_ENGINE, GEMINI_EMBEDDING_MODEL, LOCAL_EMBEDDING_MODEL
from db.connection import get_connection

ENGINES = ("gemini", "local", "bge")  # the values EMBEDDING_ENGINE accepts
LOCAL_ENGINES = ("local", "bge")  # the engines that run a model on this machine
BGE_MODEL = "BAAI/bge-m3"
# The width of the vectors each engine makes, which chunks.embedding in the database must match.
DIMENSIONS = {"gemini": 768, "local": 768, "bge": 1024}
GEMINI_DIMENSION = DIMENSIONS["gemini"]
# The column of chunks each engine reads and writes. bge-m3's is wider, so it cannot share chunks.embedding.
COLUMNS = {"gemini": "embedding", "local": "embedding", "bge": "embedding_bge"}
BATCH_SIZES = {"local": 16, "bge": 8}  # texts a local model reads at once
# The lowest cosine similarity at which a chunk still counts as a match. Each model spreads its
# scores differently: Gemini's run higher. For bge-m3, measured on 30 real questions, the right
# chunk scores a median 0.55 (0.41 at worst) and unrelated chunks around 0.35 (0.45 at the 95th
# percentile), so 0.50 would drop 4 of those 30 right chunks and 0.40 drops none.
MIN_SIMILARITIES = {"gemini": 0.45, "local": 0.50, "bge": 0.40}

_checked: set[str] = set()  # the models this process has already verified against the database
_check_lock = threading.Lock()
_model_lock = threading.Lock()
_override: str | None = None  # set by using(): an engine other than EMBEDDING_ENGINE, for comparing them in one process


class EmbeddingModelMismatch(RuntimeError):
    """This process would embed with a different model than the one behind the stored vectors."""


def engine() -> str:
    """The engine in use; an unknown value is an error, never a quiet switch to Gemini."""
    name = _override or EMBEDDING_ENGINE
    if name not in ENGINES:
        raise RuntimeError(f"Unknown EMBEDDING_ENGINE {name!r}. Set it to one of: {', '.join(ENGINES)}.")
    return name


@contextmanager
def using(name: str):
    """Run a block as if EMBEDDING_ENGINE were `name` (to search with two engines in one process, as ingest.compare_embeddings does).

    Process-wide, not per thread: use it from a single-threaded script, never inside the server.
    """
    global _override
    if name not in ENGINES:
        raise ValueError(f"Unknown engine {name!r}. Use one of: {', '.join(ENGINES)}.")
    previous, _override = _override, name
    try:
        yield
    finally:
        _override = previous


def column() -> str:
    """The column of chunks that holds the vectors of the engine in use (a fixed name, so it is safe to put in SQL)."""
    return COLUMNS[engine()]


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
    """Raise EmbeddingModelMismatch unless this process's model matches the vectors it would be compared with.

    Gemini and "local" share chunks.embedding: a database that has chunks but no record was embedded by
    Gemini before this check existed, and the record must name this process's model. bge-m3 keeps its vectors in
    chunks.embedding_bge, so for it the column must exist (it is added when the backend starts, and filled by
    ingest.reembed_all). The check is made once per model and process, after it has passed.
    """
    current = model_id()
    if current in _checked:
        return
    with _check_lock:
        if current in _checked:
            return
        if engine() == "bge":
            _ensure_bge_column()
        else:
            _ensure_recorded_model(current)
        _checked.add(current)


def _ensure_bge_column() -> None:
    with get_connection() as conn:
        width = conn.execute(
            "SELECT a.atttypmod FROM pg_attribute a WHERE a.attrelid = 'chunks'::regclass "
            "AND a.attname = %s AND NOT a.attisdropped",
            (COLUMNS["bge"],),
        ).fetchone()
    if width is None or width[0] != DIMENSIONS["bge"]:
        raise EmbeddingModelMismatch(
            f"EMBEDDING_ENGINE=bge needs the column chunks.{COLUMNS['bge']} (vector({DIMENSIONS['bge']})), which this database "
            "does not have. It is added when the backend starts; fill it with: uv run python -m ingest.reembed_all --yes"
        )


def _ensure_recorded_model(current: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS embedding_meta ("
            "id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id), model TEXT NOT NULL)"
        )
        row = conn.execute("SELECT model FROM embedding_meta").fetchone()
        if row is None:
            has_chunks = conn.execute("SELECT EXISTS (SELECT 1 FROM chunks)").fetchone()[0]
            first = f"gemini:{GEMINI_EMBEDDING_MODEL}" if has_chunks else current
            conn.execute("INSERT INTO embedding_meta (model) VALUES (%s) ON CONFLICT DO NOTHING", (first,))
            row = conn.execute("SELECT model FROM embedding_meta").fetchone()
        conn.commit()
    recorded = row[0]
    if recorded != current:
        raise EmbeddingModelMismatch(
            f"The database's vectors in chunks.embedding were made by {recorded!r}, but this process embeds with "
            f"{current!r}. Vectors from different models cannot be compared. Set EMBEDDING_ENGINE back, or re-embed "
            "with the new model (uv run python -m ingest.reembed). bge-m3 is the exception: it has its own column and "
            "never needs this (EMBEDDING_ENGINE=bge)."
        )


def coverage() -> tuple[int, int]:
    """(chunks that have a vector from the engine in use, all chunks): how far a fill has got, and what a search can see."""
    with get_connection() as conn:
        row = conn.execute(f"SELECT COUNT({column()}), COUNT(*) FROM chunks").fetchone()
    return row[0], row[1]


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


def warm_up() -> None:
    """Load the local model (if the engine uses one) and run one question through it, so the first real question is not slow.

    Loading takes 10-30 s and about 2 GB of memory; the server does this in the background while it starts.
    """
    if not uses_local_model():
        return
    embed_local(["warm up"])


def _local_model(name: str):
    """The sentence-transformers model of a local engine, loaded once; the first time, downloaded.

    Under a lock, so two questions that arrive together while it loads wait for one load instead of each loading
    their own copy (about 2 GB each).
    """
    with _model_lock:
        return _load_local_model(name)


@lru_cache(maxsize=2)
def _load_local_model(name: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            f"EMBEDDING_ENGINE={name} needs the sentence-transformers package. "
            "Run `uv sync --extra local-embeddings` in backend/."
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
