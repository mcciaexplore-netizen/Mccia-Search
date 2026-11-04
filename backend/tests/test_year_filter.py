"""The year filter: a question that names a year is searched in the issues that cover it, and only those.

An issue covers a year when the years in its file name run from before it to after it (or are it). The label tests need nothing; the
rest need a scratch Postgres WITH pgvector on this machine (never the real database), given as POOL_TEST_DATABASE_URL (localhost,
127.0.0.1 or a local socket), and are skipped without one.
"""

import math
import os
from urllib.parse import urlparse

import psycopg
import pytest

from db import connection
from retrieval import search as search_module
from retrieval.search import YearScope, documents_for_years, volume_label, year_scope

# ---- how an issue's years read ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "label"),
    [
        ("1964-July To1965-June Part-1.pdf", "1964-65"),
        ("1965-July To1966-June.pdf", "1965-66"),
        ("1972-Jul To 1973-Jun.PDF", "1972-73"),
        ("1945 July.PDF", "1945"),
        ("1999-Jan To 2000-Dec.pdf", "1999-2000"),
        ("1970-Jul To 1973-Jun.PDF", "1970-1973"),
        ("Annual report.pdf", None),
    ],
)
def test_an_issues_years_read_the_way_they_do_in_the_answer(filename, label):
    assert volume_label(filename) == label


# ---- the scratch archive ---------------------------------------------------------------------------------------------------------

URL = os.environ.get("POOL_TEST_DATABASE_URL", "")
_parsed = urlparse(URL)
LOCAL = bool(URL) and (_parsed.hostname in ("localhost", "127.0.0.1") or (_parsed.hostname is None and "host=/" in URL))
DIM = 768
needs_scratch = pytest.mark.skipif(not LOCAL, reason="set POOL_TEST_DATABASE_URL to a scratch Postgres with pgvector on this machine")

FILES = [
    "1945 July.PDF",
    "1963-April To1964-March.pdf",
    "1964-July To1965-June Part-1.pdf",
    "1964-July To1965-June Part-2.pdf",
    "1965-July To1966-June.pdf",
    "1966-July To 1967-June.pdf",
    "1970-Jul To 1973-Jun.PDF",  # a volume that runs over several years covers the ones in between
    "1971-Jul To 1972-Jun.PDF",
    "1972-Jul To 1973-Jun.PDF",
    "Annual report with no year.pdf",
]


def sim(target: float) -> list[float]:
    vector = [0.0] * DIM
    vector[0], vector[1] = target, math.sqrt(1 - target * target)
    return vector


@pytest.fixture
def archive(monkeypatch):
    """One page, equally close to the question, in each of the files above (the question's vector is axis 0)."""
    if not LOCAL:
        pytest.skip("no scratch database")
    monkeypatch.setattr(connection, "DATABASE_URL", URL)  # the module read the real URL from .env at import
    connection.close_pool()
    with psycopg.connect(URL, autocommit=True) as admin:
        try:
            admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error:
            pytest.skip("this scratch Postgres has no pgvector")
        admin.execute("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS documents")
        admin.execute("CREATE TABLE documents (id BIGSERIAL PRIMARY KEY, filename TEXT NOT NULL, drive_file_id TEXT)")
        admin.execute(f"CREATE TABLE chunks (id BIGSERIAL PRIMARY KEY, document_id BIGINT NOT NULL REFERENCES documents(id), page_number INT, chunk_index INT, content TEXT NOT NULL, embedding vector({DIM}))")
        for name in FILES:
            doc_id = admin.execute("INSERT INTO documents (filename, drive_file_id) VALUES (%s, %s) RETURNING id", (name, name)).fetchone()[0]
            admin.execute("INSERT INTO chunks (document_id, page_number, chunk_index, content, embedding) VALUES (%s, 1, 0, %s, %s::vector)",
                          (doc_id, f"MCCIA report in {name}", str(sim(0.60))))
    monkeypatch.setattr(search_module, "ensure_matches_database", lambda: None)
    monkeypatch.setattr(search_module, "min_similarity", lambda: 0.45)
    monkeypatch.setattr(search_module, "embed_query", lambda text: [1.0] + [0.0] * (DIM - 1))
    yield
    connection.close_pool()
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS documents")


def searched_files(question: str, top_k: int = 20) -> set[str]:
    return {r["filename"] for r in search_module.search(question, top_k)}


@needs_scratch
def test_1965_means_the_issues_whose_file_names_cover_1965_and_no_others(archive):
    scope = year_scope("What happened in MCCIA in 1965?")
    assert scope.years == (1965,)
    assert scope.issues == ("1964-65", "1965-66")  # both volumes that include 1965, each once (Part-1 and Part-2 are one label)
    assert searched_files("What happened in MCCIA in 1965?") == {
        "1964-July To1965-June Part-1.pdf",  # contains 1965: in
        "1964-July To1965-June Part-2.pdf",
        "1965-July To1966-June.pdf",  # contains 1965: in
    }  # not 1963-April To1964-March, not 1966-July To 1967-June, nothing else


@needs_scratch
def test_1972_means_the_1972_issues_and_the_volume_that_runs_across_it(archive):
    assert searched_files("Who was president in 1972?") == {"1970-Jul To 1973-Jun.PDF", "1971-Jul To 1972-Jun.PDF", "1972-Jul To 1973-Jun.PDF"}
    assert year_scope("Who was president in 1972?").issues == ("1970-1973", "1971-72", "1972-73")


@needs_scratch
def test_a_question_with_no_year_is_searched_across_everything(archive):
    scope = year_scope("What is MCCIA?")
    assert scope == YearScope() and scope.document_ids is None and scope.issues == ()
    assert searched_files("What is MCCIA?") == set(FILES)  # every issue, including the one whose name holds no year


@needs_scratch
def test_two_years_search_the_issues_of_both(archive):
    scope = year_scope("Compare 1945 and 1972")
    assert scope.years == (1945, 1972)
    assert scope.issues == ("1945", "1970-1973", "1971-72", "1972-73")
    assert searched_files("Compare 1945 and 1972") == {"1945 July.PDF", "1970-Jul To 1973-Jun.PDF", "1971-Jul To 1972-Jun.PDF", "1972-Jul To 1973-Jun.PDF"}


@needs_scratch
def test_a_year_no_issue_covers_finds_nothing_and_never_falls_back_to_other_years(archive):
    scope = year_scope("What happened in 1990?")
    assert scope.years == (1990,) and scope.document_ids == () and scope.issues == ()
    assert search_module.search("What happened in 1990?") == []  # ten equally close pages exist; none is offered instead


@needs_scratch
def test_the_year_is_read_in_devanagari_digits_too(archive):
    assert year_scope("१९६५ मध्ये काय झाले?").issues == ("1964-65", "1965-66")
    assert searched_files("१९६५ मध्ये काय झाले?") == searched_files("What happened in 1965?")


@needs_scratch
def test_a_scope_worked_out_beforehand_is_the_one_used(archive):
    only = year_scope("1945")
    assert searched_files_with(only) == {"1945 July.PDF"}


def searched_files_with(scope: YearScope) -> set[str]:
    return {r["filename"] for r in search_module.search("What is MCCIA?", 20, scope)}  # the question names no year; the scope decides


@needs_scratch
def test_documents_for_years_still_gives_the_ids(archive):
    ids = documents_for_years([1965])
    with psycopg.connect(URL) as conn:
        names = {r[0] for r in conn.execute("SELECT filename FROM documents WHERE id = ANY(%s)", (ids,)).fetchall()}
    assert names == {"1964-July To1965-June Part-1.pdf", "1964-July To1965-June Part-2.pdf", "1965-July To1966-June.pdf"}
