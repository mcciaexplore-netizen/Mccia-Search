import sys
from pathlib import Path

# The backend modules import each other by plain name (from db.connection import ...), so tests run with backend/ on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest  # noqa: E402  (the path above must be set before the backend modules are imported)


@pytest.fixture(autouse=True)
def _gemini_unless_a_test_says_otherwise(monkeypatch):
    """Every test starts on the Gemini engine, whatever EMBEDDING_ENGINE says in the developer's .env (which may be bge).

    A test that wants another engine sets it itself (monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "bge") or
    embedding_engine.using("bge")); the models already verified against a database are forgotten between tests.
    """
    import embedding_engine

    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    monkeypatch.setattr(embedding_engine, "_checked", set())
