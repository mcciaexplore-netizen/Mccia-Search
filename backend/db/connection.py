"""Neon Postgres connection management."""

import atexit
import struct
import threading
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg.abc import Buffer
from psycopg.adapt import Loader
from psycopg.pq import Format
from psycopg_pool import ConnectionPool

from config import DATABASE_URL

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
CONNECT_TIMEOUT_SECONDS = 30  # allows for Neon waking a suspended compute
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


class _VectorLoader(Loader):
    """Read vector values (text format) as lists of floats."""

    format = Format.TEXT

    def load(self, data: Buffer) -> list[float]:
        return [float(x) for x in bytes(data).decode("utf8")[1:-1].split(",")]


class _VectorBinaryLoader(Loader):
    """Read vector values (binary format) as lists of floats."""

    format = Format.BINARY

    def load(self, data: Buffer) -> list[float]:
        # int16 dimension count, int16 unused, then big-endian float32 values.
        dimensions = struct.unpack_from(">H", data)[0]
        return list(struct.unpack_from(f">{dimensions}f", data, 4))


def get_connection() -> psycopg.Connection:
    """Open a connection to the database with the pgvector type registered.

    The connection is not in autocommit mode. Use it as a context manager:
    `with get_connection() as conn:` commits on success, rolls back if the block
    raises, and closes the connection either way. vector columns are read as
    lists of floats. Requires the vector extension, which init_db() creates.
    """
    conn = _connect()
    try:
        _register_vector(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def get_pool() -> ConnectionPool:
    """The shared connection pool, created (and connected) on its first call.

    Opening a connection to Neon takes seconds; the pool keeps connections open so
    a request can reuse one. Pooled connections are meant for read queries such as
    the similarity search, and differ from get_connection() in two ways, both to save
    round trips to the database:
    - autocommit: each statement commits on its own. A normal transaction adds a
      BEGIN and a COMMIT, about 0.9 s per query from Pune to Neon in Ohio.
    - the pgvector type is not registered, which costs about 3 s per new connection.
      Pass vectors as text, Vector(...).to_text() with %s::vector, as search does.
    Use get_connection() for writes, transactions, or reading vector columns.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            url = (DATABASE_URL or "").strip()
            if not url:
                raise RuntimeError(
                    "DATABASE_URL is not set. Add your Neon connection string to "
                    ".env (see .env.example)."
                )
            pool = ConnectionPool(
                conninfo=url,
                min_size=POOL_MIN_SIZE,
                max_size=POOL_MAX_SIZE,
                kwargs={
                    # See _connect(): prepared statements break behind PgBouncer.
                    "prepare_threshold": None,
                    "connect_timeout": CONNECT_TIMEOUT_SECONDS,
                    "autocommit": True,
                },
                open=True,
            )
            try:
                pool.wait(timeout=CONNECT_TIMEOUT_SECONDS)  # first connection is ready
            except BaseException:
                pool.close()
                raise
            _pool = pool
            atexit.register(close_pool)
        return _pool


def get_pooled_connection():
    """A pooled connection, as a context manager: `with get_pooled_connection() as conn:`.

    Leaving the block hands the connection back to the pool instead of closing it.
    See get_pool() for how pooled connections differ from get_connection().
    """
    return get_pool().connection()


def close_pool() -> None:
    """Close the shared pool, if there is one. A later get_pool() makes a new one."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.close()


def init_db() -> None:
    """Apply schema.sql to the database; safe to repeat as it uses IF NOT EXISTS."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    if not schema.strip():
        raise RuntimeError(f"{SCHEMA_PATH} is empty; there is no schema to apply.")

    # Not get_connection(): on a new database the vector extension that
    # registration needs is created by the schema itself.
    with _connect() as conn:
        conn.execute(schema)
    print("[db] schema applied")


def _connect() -> psycopg.Connection:
    url = (DATABASE_URL or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Add your Neon connection string to .env "
            "(see .env.example)."
        )
    return psycopg.connect(
        url,
        autocommit=False,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        # Server-side prepared statements break behind a transaction-mode
        # pooler such as the PgBouncer in Neon's pooled connection strings.
        prepare_threshold=None,
    )


def _register_vector(conn: psycopg.Connection) -> None:
    try:
        register_vector(conn)
    except psycopg.ProgrammingError as exc:
        raise RuntimeError(
            f"Could not register the pgvector type ({exc}). If the vector "
            "extension is not installed yet, run init_db() first."
        ) from exc
    # register_vector returns stock Vector objects; read lists instead.
    oid = conn.adapters.types["vector"].oid
    conn.adapters.register_loader(oid, _VectorLoader)
    conn.adapters.register_loader(oid, _VectorBinaryLoader)
    # End the transaction opened by the type lookups so callers start clean.
    conn.commit()
