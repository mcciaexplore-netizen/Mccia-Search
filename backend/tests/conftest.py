import sys
from pathlib import Path

# The backend modules import each other by plain name (from db.connection import ...), so tests run with backend/ on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest  # noqa: E402  (the path above must be set before the backend modules are imported)

# SAFETY: no test may ever reach the real database. The developer's .env points at the live one, so the address is replaced, for the whole run, with one
# that cannot connect. A test that needs a database sets its own (a scratch Postgres, see POOL_TEST_DATABASE_URL) with monkeypatch; even if such a
# patch is undone, what comes back is this address and never the real one. (A test once undid its patches with monkeypatch.undo() and the fill it
# ran then wrote 16,230 fake vectors into the live database; they were removed again.)
from db import connection as _connection  # noqa: E402

_connection.DATABASE_URL = "postgresql://tests-must-not-reach-the-real-database:5432/none?connect_timeout=2"


@pytest.fixture(autouse=True)
def _gemini_unless_a_test_says_otherwise(monkeypatch):
    """Every test starts on the Gemini engine, whatever EMBEDDING_ENGINE says in the developer's .env (which may be bge).

    A test that wants another engine sets it itself (monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "bge") or
    embedding_engine.using("bge")); the models already verified against a database are forgotten between tests.
    """
    import embedding_engine

    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    monkeypatch.setattr(embedding_engine, "_checked", set())
