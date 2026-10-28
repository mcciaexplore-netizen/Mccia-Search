"""The pooled connections: one that Neon has dropped must be replaced, not handed to a request.

These tests need a scratch Postgres on this machine, never the real database; they are skipped unless one is given:

    POOL_TEST_DATABASE_URL=postgres://postgres@127.0.0.1:5432/pool_test uv run pytest tests/test_connection_pool.py

Only localhost / 127.0.0.1 is accepted. They close connections from the server side, as Neon does when its compute suspends.
"""

import os
import threading
import time
from urllib.parse import urlparse

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from db import connection

URL = os.environ.get("POOL_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    urlparse(URL).hostname not in ("localhost", "127.0.0.1"),
    reason="set POOL_TEST_DATABASE_URL to a scratch Postgres on this machine (localhost or 127.0.0.1)",
)


@pytest.fixture(autouse=True)
def scratch_pool(monkeypatch):
    # The module read the real DATABASE_URL from .env when it was imported: point it at the scratch database instead.
    monkeypatch.setattr(connection, "DATABASE_URL", URL)
    connection.close_pool()
    yield
    connection.close_pool()


def kill_every_connection() -> None:
    """What Neon does when its compute suspends: close every other connection to the database."""
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
    time.sleep(0.3)  # let the connections actually go


def backend_pid() -> int:
    with connection.get_pooled_connection() as conn:
        return conn.execute("SELECT pg_backend_pid()").fetchone()[0]


def sat_idle_too_long(monkeypatch) -> None:
    """Make every pooled connection count as 'idle for longer than the limit', without waiting."""
    monkeypatch.setattr(connection, "IDLE_CHECK_SECONDS", 0, raising=False)


def test_a_connection_neon_dropped_is_replaced_not_handed_out(monkeypatch):
    first = backend_pid()
    kill_every_connection()
    sat_idle_too_long(monkeypatch)
    # Before the fix this raised a psycopg.OperationalError (a server-side kill says "terminating connection due to administrator
    # command"; in production, Neon's drop shows as "consuming input failed: SSL connection has been closed unexpectedly").
    assert backend_pid() != first


def test_every_dead_connection_in_a_grown_pool_is_replaced(monkeypatch):
    # Grow the pool to four connections by holding four at once.
    held, gate = [], threading.Barrier(4)

    def hold():
        with connection.get_pooled_connection() as conn:
            held.append(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            gate.wait(timeout=20)

    threads = [threading.Thread(target=hold) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert len(set(held)) == 4

    kill_every_connection()
    sat_idle_too_long(monkeypatch)

    results, errors = [], []

    def ask():
        try:
            results.append(backend_pid())
        except Exception as exc:  # noqa: BLE001 - the test reports whatever the request would have hit
            errors.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert errors == []
    assert len(results) == 8
    assert not set(results) & set(held)  # none of the dropped connections was used


def test_only_a_connection_that_sat_idle_is_tested(monkeypatch):
    checks = []
    real = ConnectionPool.check_connection

    def counting(conn):
        checks.append(1)
        real(conn)

    monkeypatch.setattr(ConnectionPool, "check_connection", staticmethod(counting))

    backend_pid()
    backend_pid()
    assert checks == []  # used a moment ago: no extra round trip to the database

    sat_idle_too_long(monkeypatch)
    backend_pid()
    assert checks == [1]  # idle for longer than the limit: tested once


def test_a_connection_that_is_still_alive_is_kept(monkeypatch):
    first = backend_pid()
    sat_idle_too_long(monkeypatch)
    assert backend_pid() == first  # tested, found fine, reused: no needless reconnect
