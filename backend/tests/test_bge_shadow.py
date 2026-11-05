"""bge-m3 next to Gemini: a column of its own, filled while the site keeps searching with Gemini's vectors.

The pure parts run anywhere. The database parts need a scratch Postgres with pgvector on this machine: set POOL_TEST_DATABASE_URL
(the same variable the other database tests use). They make a database of their own inside it, so no other test's tables are touched.
"""

import math
import os
import sys
import threading
import time
import types
from urllib.parse import urlparse

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import embedding_engine
import main as main_module
from db import connection
from ingest import compare_embeddings, embed_pending, embedder, preflight_bge, reembed_all
from retrieval import search as search_module

GEMINI_DIM, BGE_DIM = 768, 1024
URL = os.environ.get("POOL_TEST_DATABASE_URL", "")
_parsed = urlparse(URL)
LOCAL = bool(URL) and (_parsed.hostname in ("localhost", "127.0.0.1") or (_parsed.hostname is None and "host=/" in URL))
needs_scratch = pytest.mark.skipif(not LOCAL, reason="set POOL_TEST_DATABASE_URL to a scratch Postgres with pgvector on this machine")
TEST_DB = "bge_shadow_test"


def unit(dim: int, *weights: float) -> list[float]:
    """A unit vector with the given leading components, so its cosine similarity to unit(dim, 1) is weights[0] / norm."""
    vector = [0.0] * dim
    for i, w in enumerate(weights):
        vector[i] = w
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def near(dim: int, similarity: float) -> list[float]:
    """A unit vector whose cosine similarity to unit(dim, 1) is exactly `similarity`."""
    return unit(dim, similarity, math.sqrt(1 - similarity * similarity))


def text_of(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vector) + "]"


# ---- pure: which column, and the engine override -------------------------------------------------------------------------------


@pytest.mark.parametrize("name, column", [("gemini", "embedding"), ("local", "embedding"), ("bge", "embedding_bge")])
def test_each_engine_reads_and_writes_its_own_column(monkeypatch, name, column):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", name)
    assert embedding_engine.column() == column


def test_bge_never_shares_a_column_with_gemini():
    # vectors of different widths cannot share one column, and different models must not be mixed in one even at the same width
    assert embedding_engine.COLUMNS["bge"] not in (embedding_engine.COLUMNS["gemini"], embedding_engine.COLUMNS["local"])
    assert embedding_engine.DIMENSIONS["bge"] == 1024


def test_using_switches_the_engine_for_a_block_and_restores_it(monkeypatch):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    with embedding_engine.using("bge"):
        assert embedding_engine.engine() == "bge"
        assert embedding_engine.column() == "embedding_bge"
        assert embedding_engine.min_similarity() == embedding_engine.MIN_SIMILARITIES["bge"]
        assert embedding_engine.dimension() == 1024
    assert embedding_engine.engine() == "gemini"


def test_using_restores_the_engine_after_an_error(monkeypatch):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    with pytest.raises(RuntimeError):
        with embedding_engine.using("bge"):
            raise RuntimeError("boom")
    assert embedding_engine.engine() == "gemini"


def test_using_rejects_an_unknown_engine_instead_of_falling_back(monkeypatch):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    with pytest.raises(ValueError):
        with embedding_engine.using("gemni"):
            pass
    assert embedding_engine.engine() == "gemini"


def test_the_search_sql_reads_the_column_of_the_engine(monkeypatch):
    for name, column in (("gemini", "embedding"), ("bge", "embedding_bge")):
        monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", name)
        for params in ({}, {"document_ids": [1]}):
            sql = search_module._sql(params)
            assert "{column}" not in sql
            assert f"c.{column} <=>" in sql or f"{column} AS embedding" in sql
    # Gemini's search is exactly what it was before this change
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    assert "c.embedding <=> %(query)s::vector" in search_module._sql({})
    assert "embedding_bge" not in search_module._sql({}) and "embedding_bge" not in search_module._sql({"document_ids": [1]})


# ---- pure: the model is loaded once, and warming up is only for local engines --------------------------------------------------


def test_two_questions_arriving_while_the_model_loads_share_one_load(monkeypatch):
    built = []

    class FakeModel:
        def __init__(self, name, **kwargs):
            built.append(name)
            time.sleep(0.2)  # the real load takes seconds: long enough for the other threads to arrive

        def get_embedding_dimension(self):
            return 1024

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=FakeModel))
    embedding_engine._load_local_model.cache_clear()
    try:
        threads = [threading.Thread(target=embedding_engine._local_model, args=("bge",)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        embedding_engine._load_local_model.cache_clear()
    assert built == ["BAAI/bge-m3"]  # one copy in memory (about 2 GB), not five


def test_warm_up_loads_nothing_for_gemini(monkeypatch):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    monkeypatch.setattr(embedding_engine, "embed_local", lambda texts: pytest.fail("must not load a model for Gemini"))
    embedding_engine.warm_up()


def test_warm_up_runs_one_question_through_a_local_model(monkeypatch):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "bge")
    seen = []
    monkeypatch.setattr(embedding_engine, "embed_local", lambda texts: seen.append(texts) or [[0.0]])
    embedding_engine.warm_up()
    assert len(seen) == 1 and len(seen[0]) == 1


def test_a_failing_startup_check_never_stops_the_server(monkeypatch, capsys):
    monkeypatch.setattr(embedding_engine, "coverage", lambda: (_ for _ in ()).throw(RuntimeError("database is away")))
    main_module._prepare_embeddings()  # must return, not raise
    assert "embedding check failed" in capsys.readouterr().out


def test_the_startup_check_warns_when_chunks_have_no_vector(monkeypatch, capsys):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    monkeypatch.setattr(embedding_engine, "coverage", lambda: (90, 100))
    main_module._prepare_embeddings()
    assert "WARNING: 10 chunks have no vector" in capsys.readouterr().out
    monkeypatch.setattr(embedding_engine, "coverage", lambda: (100, 100))
    main_module._prepare_embeddings()
    assert "WARNING" not in capsys.readouterr().out


# ---- pure: the comparison report -----------------------------------------------------------------------------------------------


def result(file="1965 Jan.pdf", page=1, score=0.8):
    return {"filename": file, "page_number": page, "score": score}


def test_agreement_tells_apart_the_ways_two_searches_can_differ():
    a = result(page=1)
    b = result(page=2)
    c = result(page=3)
    same = compare_embeddings._agreement([a, b], [a, c])
    assert same["both"] and same["same_top"] and same["overlap"] == pytest.approx(1 / 3)
    assert not compare_embeddings._agreement([a], [b])["same_top"]
    assert compare_embeddings._agreement([a], [])["only_gemini"]
    assert compare_embeddings._agreement([], [a])["only_bge"]
    nothing = compare_embeddings._agreement([], [])
    assert nothing["neither"] and nothing["overlap"] == 1.0


def test_the_same_page_in_another_issue_is_not_the_same_page():
    assert not compare_embeddings._agreement([result("1965 Jan.pdf", 4)], [result("1966 Jan.pdf", 4)])["same_top"]


def test_the_report_names_each_question_and_summarises_them_all():
    rows = [
        ("who was president", [result(page=1)], [result(page=1)], 900, 120),
        ("what happened in 1972", [result(page=1)], [], 800, 100),
        ("tell me about x", [], [result(page=2)], 700, 90),
        ("nothing at all", [], [], 600, 80),
    ]
    text = compare_embeddings._describe(1, rows[0][0], rows[0][1], rows[0][2], 900, 120)
    assert "who was president" in text and "same top page" in text and "p.1" in text
    assert "ONLY GEMINI FOUND" in compare_embeddings._describe(2, "q", [result()], [], 1, 1)
    assert "ONLY BGE FOUND" in compare_embeddings._describe(3, "q", [], [result()], 1, 1)
    summary = compare_embeddings._summary(rows)
    assert "both found something:   1" in summary and "only gemini found:      1" in summary
    assert "only bge found:         1" in summary and "neither found:          1" in summary


def test_a_questions_file_keeps_real_questions_only(tmp_path):
    path = tmp_path / "q.txt"
    path.write_text("hi\n\nWho was the first president of India?\n   \nतुम्ही कोण आहात आज\n", encoding="utf-8")
    assert compare_embeddings._read_file(str(path)) == ["Who was the first president of India?", "तुम्ही कोण आहात आज"]


# ---- database: the scratch database ---------------------------------------------------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    """A fresh database with the real schema (init_db), separate from the other tests' tables, and the engine set to Gemini."""
    if not LOCAL:
        pytest.skip("no scratch database")
    with psycopg.connect(URL, autocommit=True) as admin:
        try:
            admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error:
            pytest.skip("this scratch Postgres has no pgvector")
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    test_url = make_conninfo(**{**conninfo_to_dict(URL), "dbname": TEST_DB})
    monkeypatch.setattr(connection, "DATABASE_URL", test_url)
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    monkeypatch.setattr(embedding_engine, "_checked", set())
    connection.close_pool()
    yield test_url
    connection.close_pool()
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


def sql(db_url, statement, params=None, fetch=False):
    with psycopg.connect(db_url, autocommit=True) as conn:
        cur = conn.execute(statement, params)
        return cur.fetchall() if fetch else None


def add_document(db_url, name="1965 Jan To1965 Dec.pdf", status="completed") -> int:
    return sql(db_url, "INSERT INTO documents (filename, drive_file_id, status) VALUES (%s, %s, %s) RETURNING id", (name, name, status), True)[0][0]


def add_chunk(db_url, doc, index, content, gemini=None, bge=None) -> int:
    return sql(
        db_url,
        "INSERT INTO chunks (document_id, chunk_index, content, page_number, embedding, embedding_bge) "
        "VALUES (%s, %s, %s, %s, %s::vector, %s::vector) RETURNING id",
        (doc, index, content, index + 1, text_of(gemini) if gemini else None, text_of(bge) if bge else None),
        True,
    )[0][0]


def column_state(db_url) -> dict:
    rows = sql(db_url, "SELECT id, embedding::text, embedding_bge::text FROM chunks ORDER BY id", fetch=True)
    return {r[0]: (r[1], r[2]) for r in rows}


@needs_scratch
def test_a_fresh_database_gets_both_columns_and_no_bge_index(db):
    connection.init_db()
    connection.init_db()  # repeating it is harmless
    widths = dict(sql(db, "SELECT attname, atttypmod FROM pg_attribute WHERE attrelid = 'chunks'::regclass "
                          "AND attname IN ('embedding', 'embedding_bge')", fetch=True))
    assert widths == {"embedding": GEMINI_DIM, "embedding_bge": BGE_DIM}
    assert sql(db, "SELECT to_regclass('chunks_embedding_idx') IS NOT NULL, to_regclass('chunks_embedding_bge_idx') IS NOT NULL", fetch=True) == [(True, False)]


@needs_scratch
def test_an_existing_gemini_database_only_gains_an_empty_column(db):
    """The live database: 768-dimension vectors, no bge column. Starting the new backend must not rewrite or change a single row."""
    sql(db, "CREATE EXTENSION IF NOT EXISTS vector")
    sql(db, "CREATE TABLE documents (id BIGSERIAL PRIMARY KEY, filename TEXT NOT NULL, drive_file_id TEXT NOT NULL UNIQUE, "
            "total_pages INTEGER, language TEXT, ingested_at TIMESTAMPTZ DEFAULT now(), status TEXT DEFAULT 'pending')")
    sql(db, "CREATE TABLE chunks (id BIGSERIAL PRIMARY KEY, document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, "
            "chunk_index INTEGER NOT NULL, content TEXT NOT NULL, embedding vector(768) NOT NULL, page_number INTEGER, "
            "created_at TIMESTAMPTZ DEFAULT now(), UNIQUE (document_id, chunk_index))")
    doc = add_document_old(db)
    for i in range(3):
        sql(db, "INSERT INTO chunks (document_id, chunk_index, content, embedding, page_number) VALUES (%s, %s, %s, %s::vector, 1)",
            (doc, i, f"page {i}", text_of(near(GEMINI_DIM, 0.5 + i / 10))))
    before = sql(db, "SELECT id, xmin::text, embedding::text FROM chunks ORDER BY id", fetch=True)
    connection.init_db()
    after = sql(db, "SELECT id, xmin::text, embedding::text FROM chunks ORDER BY id", fetch=True)
    assert before == after  # the same row versions (xmin) and the same Gemini vectors: nothing was rewritten
    assert sql(db, "SELECT COUNT(embedding_bge) FROM chunks", fetch=True) == [(0,)]


def add_document_old(db_url) -> int:
    return sql(db_url, "INSERT INTO documents (filename, drive_file_id, status) VALUES ('1965.pdf', 'd1', 'completed') RETURNING id", fetch=True)[0][0]


# ---- database: search reads the column of the engine ---------------------------------------------------------------------------


@pytest.fixture
def archive(db, monkeypatch):
    """Two chunks the two engines disagree about: Gemini finds the first, bge finds the second."""
    connection.init_db()
    doc = add_document(db)
    # chunk A: close to the question for Gemini, unrelated for bge.   chunk B: the other way round.
    a = add_chunk(db, doc, 0, "Alpha page about the first president", gemini=near(GEMINI_DIM, 0.9), bge=unit(BGE_DIM, 0, 1))
    b = add_chunk(db, doc, 1, "Beta page about the first president", gemini=unit(GEMINI_DIM, 0, 1), bge=near(BGE_DIM, 0.9))
    monkeypatch.setattr(search_module, "embed_query", lambda text: unit(embedding_engine.dimension(), 1))
    return {"db": db, "doc": doc, "a": a, "b": b}


@needs_scratch
def test_gemini_search_still_reads_geminis_vectors(archive):
    found = search_module.search("first president", 5)
    assert [r["chunk_id"] for r in found] == [archive["a"]]


@needs_scratch
def test_bge_search_reads_the_new_column_only(archive):
    with embedding_engine.using("bge"):
        found = search_module.search("first president", 5)
    assert [r["chunk_id"] for r in found] == [archive["b"]]


@needs_scratch
def test_bge_search_with_a_year_filter_reads_the_new_column_too(archive):
    with embedding_engine.using("bge"):
        found = search_module.search("first president in 1965", 5)
    assert [r["chunk_id"] for r in found] == [archive["b"]]
    assert [r["chunk_id"] for r in search_module.search("first president in 1965", 5)] == [archive["a"]]


@needs_scratch
def test_a_chunk_without_a_bge_vector_is_invisible_to_bge_but_not_to_gemini(archive):
    c = add_chunk(archive["db"], archive["doc"], 2, "Gamma page about the first president", gemini=near(GEMINI_DIM, 0.95), bge=None)
    with embedding_engine.using("bge"):
        assert c not in [r["chunk_id"] for r in search_module.search("first president", 5)]
    assert c in [r["chunk_id"] for r in search_module.search("first president", 5)]


@needs_scratch
def test_bge_search_can_use_its_index_once_it_is_built(archive):
    sql(archive["db"], "CREATE INDEX chunks_embedding_bge_idx ON chunks USING hnsw (embedding_bge vector_cosine_ops)")
    with psycopg.connect(archive["db"], autocommit=True) as conn:
        # two rows would never choose the index on their own; with the alternatives off this asks "can the query use it?"
        conn.execute("SET enable_seqscan = off")
        conn.execute("SET enable_sort = off")
        with embedding_engine.using("bge"):
            plan = " ".join(r[0] for r in conn.execute("EXPLAIN " + search_module._sql({}), {
                "query": text_of(unit(BGE_DIM, 1)), "min_similarity": 0.4, "candidates": 30, "terms": ["president"],
            }).fetchall())
    assert "chunks_embedding_bge_idx" in plan


# ---- database: the model guard ---------------------------------------------------------------------------------------------------


@needs_scratch
def test_bge_needs_no_record_of_a_model_and_leaves_geminis_alone(archive, monkeypatch):
    # the database says its (Gemini-column) vectors are Gemini's: a bge process must not mind, because it reads another column
    sql(archive["db"], "INSERT INTO embedding_meta (model) VALUES ('gemini:gemini-embedding-001') ON CONFLICT (id) DO NOTHING")
    with embedding_engine.using("bge"):
        embedding_engine.ensure_matches_database()
    # but a process that would write 768-dimension vectors from another model into Gemini's column is still stopped
    with embedding_engine.using("local"):
        with pytest.raises(embedding_engine.EmbeddingModelMismatch):
            embedding_engine.ensure_matches_database()
    with embedding_engine.using("gemini"):
        embedding_engine.ensure_matches_database()


@needs_scratch
def test_bge_refuses_a_database_without_the_column(db):
    sql(db, "CREATE EXTENSION IF NOT EXISTS vector")
    sql(db, "CREATE TABLE chunks (id BIGSERIAL PRIMARY KEY, content TEXT, embedding vector(768))")
    with embedding_engine.using("bge"):
        with pytest.raises(embedding_engine.EmbeddingModelMismatch, match="embedding_bge"):
            embedding_engine.ensure_matches_database()


@needs_scratch
def test_coverage_counts_the_engines_own_column(archive):
    add_chunk(archive["db"], archive["doc"], 2, "no bge yet", gemini=near(GEMINI_DIM, 0.6), bge=None)
    assert embedding_engine.coverage() == (3, 3)
    with embedding_engine.using("bge"):
        assert embedding_engine.coverage() == (2, 3)


# ---- database: writing vectors goes to the engine's column -----------------------------------------------------------------------


@needs_scratch
def test_new_chunks_are_stored_in_the_column_of_the_engine(db):
    connection.init_db()
    doc = add_document(db)
    chunks = [{"document_id": doc, "chunk_index": 0, "content": "g", "page_number": 1}]
    embedder.store_chunks(chunks, [near(GEMINI_DIM, 0.8)])
    with embedding_engine.using("bge"):
        embedder.store_chunks([{"document_id": doc, "chunk_index": 1, "content": "b", "page_number": 2}], [near(BGE_DIM, 0.8)])
        with pytest.raises(ValueError):  # a 768-dimension vector is not a bge vector
            embedder.store_chunks([{"document_id": doc, "chunk_index": 2, "content": "x", "page_number": 3}], [near(GEMINI_DIM, 0.8)])
    rows = sql(db, "SELECT content, embedding IS NOT NULL, embedding_bge IS NOT NULL FROM chunks ORDER BY chunk_index", fetch=True)
    assert rows == [("g", True, False), ("b", False, True)]


@pytest.fixture
def fake_bge(monkeypatch):
    """The bge engine, with a fake model that makes 1024-dimension vectors (no 2 GB model in a test)."""
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "bge")
    calls = []

    def fake(texts):
        calls.append(len(texts))
        return [near(BGE_DIM, 0.5 + (len(t) % 40) / 100) for t in texts]

    monkeypatch.setattr(embed_pending, "_embed_with_retries", fake)
    return calls


@needs_scratch
def test_embed_pending_in_bge_mode_fills_the_new_column_and_finishes_the_document(db, fake_bge):
    connection.init_db()
    doc = add_document(db, status="text_saved")
    for i in range(3):
        add_chunk(db, doc, i, f"text {i}", gemini=None, bge=None)
    assert embed_pending.main(["--delay", "0", "--batch-size", "2"]) == 0
    assert sql(db, "SELECT COUNT(embedding_bge), COUNT(embedding) FROM chunks", fetch=True) == [(3, 0)]
    assert sql(db, "SELECT status FROM documents", fetch=True) == [("completed",)]


# ---- database: the fill (ingest.reembed_all) -------------------------------------------------------------------------------------


@pytest.fixture
def live(db):
    """What production looks like: completed documents, Gemini vectors everywhere, no bge vectors, a document still waiting for Gemini's."""
    connection.init_db()
    done = add_document(db, "1965 Jan.pdf", "completed")
    waiting = add_document(db, "1966 Jan.pdf", "text_saved")
    ids = [add_chunk(db, done, i, f"done chunk {i} with some words", gemini=near(GEMINI_DIM, 0.5 + i / 20)) for i in range(5)]
    ids += [add_chunk(db, waiting, i, f"waiting chunk {i}", gemini=None) for i in range(2)]
    return {"db": db, "ids": ids, "done": done, "waiting": waiting}


@needs_scratch
def test_the_plan_changes_nothing(live, fake_bge, capsys):
    before = column_state(live["db"])
    assert reembed_all.main([]) == 0
    assert column_state(live["db"]) == before
    assert "Run again with --yes" in capsys.readouterr().out
    assert fake_bge == []  # no model was asked for anything


@needs_scratch
def test_the_plan_does_not_even_add_the_column(db, fake_bge, capsys):
    sql(db, "CREATE EXTENSION IF NOT EXISTS vector")
    sql(db, "CREATE TABLE chunks (id BIGSERIAL PRIMARY KEY, content TEXT, embedding vector(768))")
    sql(db, "INSERT INTO chunks (content, embedding) VALUES ('a', %s::vector)", (text_of(near(GEMINI_DIM, 0.5)),))
    assert reembed_all.main([]) == 0
    assert "not there yet" in capsys.readouterr().out
    assert sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'chunks' AND column_name = 'embedding_bge'", fetch=True) == [(0,)]


@needs_scratch
def test_the_fill_embeds_every_chunk_without_touching_geminis_vectors_or_any_status(live, fake_bge):
    gemini_before = {i: v[0] for i, v in column_state(live["db"]).items()}
    statuses_before = sql(live["db"], "SELECT id, status FROM documents ORDER BY id", fetch=True)
    assert reembed_all.main(["--yes", "--fetch-size", "3"]) == 0
    after = column_state(live["db"])
    assert all(v[1] is not None for v in after.values())  # every chunk, including those of the document that waits for Gemini
    assert {i: v[0] for i, v in after.items()} == gemini_before  # Gemini's column is byte for byte what it was
    assert sql(live["db"], "SELECT id, status FROM documents ORDER BY id", fetch=True) == statuses_before  # nothing marked completed
    assert max(fake_bge) <= 3  # the batches it asked for


@needs_scratch
def test_the_fill_builds_the_search_index_at_the_end_and_search_uses_it(live, fake_bge):
    assert sql(live["db"], "SELECT to_regclass('chunks_embedding_bge_idx') IS NOT NULL", fetch=True) == [(False,)]
    assert reembed_all.main(["--yes"]) == 0
    assert sql(live["db"], "SELECT to_regclass('chunks_embedding_bge_idx') IS NOT NULL", fetch=True) == [(True,)]
    assert sql(live["db"], "SELECT indexdef FROM pg_indexes WHERE indexname = 'chunks_embedding_bge_idx'", fetch=True)[0][0].count("hnsw") == 1


@needs_scratch
def test_a_trial_run_does_only_what_was_asked_and_a_second_run_carries_on(live, fake_bge, capsys):
    assert reembed_all.main(["--yes", "--limit", "2"]) == 0
    assert sql(live["db"], "SELECT COUNT(embedding_bge) FROM chunks", fetch=True) == [(2,)]
    assert sql(live["db"], "SELECT to_regclass('chunks_embedding_bge_idx') IS NOT NULL", fetch=True) == [(False,)]  # not finished: no index yet
    assert "trial run" in capsys.readouterr().out
    assert reembed_all.main(["--yes"]) == 0
    assert sql(live["db"], "SELECT COUNT(embedding_bge) FROM chunks", fetch=True) == [(7,)]
    assert sum(fake_bge) == 7  # nothing was embedded twice


@needs_scratch
def test_running_it_again_when_everything_is_done_embeds_nothing(live, fake_bge, capsys):
    assert reembed_all.main(["--yes"]) == 0
    calls = len(fake_bge)
    assert reembed_all.main(["--yes"]) == 0
    assert len(fake_bge) == calls
    assert "Nothing to embed" in capsys.readouterr().out


@needs_scratch
def test_a_chunk_added_later_is_picked_up_by_the_next_run(live, fake_bge):
    assert reembed_all.main(["--yes"]) == 0
    add_chunk(live["db"], live["done"], 99, "an issue ingested meanwhile", gemini=near(GEMINI_DIM, 0.7))
    assert reembed_all.main(["--yes"]) == 0
    assert sql(live["db"], "SELECT COUNT(*) FROM chunks WHERE embedding_bge IS NULL", fetch=True) == [(0,)]


@needs_scratch
def test_a_failing_batch_stops_the_run_and_keeps_what_was_saved(live, monkeypatch, capsys):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "bge")
    calls = {"n": 0}

    def flaky(texts):
        calls["n"] += 1
        if calls["n"] == 3:
            raise embed_pending.EmbeddingStopped("out of memory")
        return [near(BGE_DIM, 0.6) for _ in texts]

    monkeypatch.setattr(embed_pending, "_embed_with_retries", flaky)
    assert reembed_all.main(["--yes", "--fetch-size", "2"]) == 1
    assert sql(live["db"], "SELECT COUNT(embedding_bge) FROM chunks", fetch=True) == [(4,)]  # the two batches before it are kept
    assert "Run it again" in capsys.readouterr().out
    assert sql(live["db"], "SELECT COUNT(*) FROM documents WHERE status = 'completed'", fetch=True) == [(1,)]  # still only the original one


def test_the_fill_refuses_to_run_for_another_engine(monkeypatch, capsys):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    assert reembed_all.main(["--yes"]) == 2
    assert "EMBEDDING_ENGINE=bge" in capsys.readouterr().out


# ---- database: the audit questions the comparison uses ------------------------------------------------------------------------


@needs_scratch
def test_comparison_questions_come_from_real_answered_chat_entries(db):
    connection.init_db()
    rows = [
        ("chat", "Who was the first president of India?", 200, "alice"),
        ("chat", "who was the first president of india?", 200, "bob"),  # the same question again: once
        ("chat", "hi", 200, "alice"),  # too short
        ("chat", "smoke test question here", 200, "smoke-check"),
        ("chat", "A question that failed to answer", 500, "alice"),
        ("sync", "Sync finished with no new files", 200, None),
        ("chat", "Tell me about the 1972 flood in Pune", 200, "carol"),
    ]
    for event, query, status, user in rows:
        sql(db, "INSERT INTO audit_log (event, query, status, user_id) VALUES (%s, %s, %s, %s)", (event, query, status, user))
    found = compare_embeddings._from_audit(10)
    # the most recent wording of a repeated question is the one kept
    assert sorted(found) == sorted(["Tell me about the 1972 flood in Pune", "who was the first president of india?"])
    assert len(compare_embeddings._from_audit(1)) == 1


# ---- database: the check before going live ------------------------------------------------------------------------------------


@pytest.fixture
def ready(db, monkeypatch):
    """A bge-ready archive (every chunk has its vector, the index exists) and a fake model that puts every question on axis 1."""
    connection.init_db()
    doc = add_document(db, "1965 Jan To1965 Dec.pdf")
    other = add_document(db, "1972 July.PDF")
    for i in range(3):
        add_chunk(db, doc, i, f"page {i} of 1965", gemini=near(GEMINI_DIM, 0.6), bge=near(BGE_DIM, 0.9 - i / 20))
    add_chunk(db, other, 0, "page of 1972", gemini=near(GEMINI_DIM, 0.6), bge=near(BGE_DIM, 0.85))
    sql(db, "CREATE INDEX chunks_embedding_bge_idx ON chunks USING hnsw (embedding_bge vector_cosine_ops)")
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "bge")
    fake_model = lambda texts: [unit(BGE_DIM, 1) for _ in texts]
    monkeypatch.setattr(embedding_engine, "embed_local", fake_model)
    monkeypatch.setattr(search_module, "embed_local", fake_model)
    return db


@needs_scratch
def test_preflight_passes_when_everything_is_in_place(ready, capsys):
    assert preflight_bge.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("ok  ") == 5 and "FAIL" not in out and "Ready for bge-m3" in out


@needs_scratch
def test_preflight_fails_while_a_chunk_has_no_vector(ready, capsys):
    add_chunk(ready, 1, 50, "added after the fill", gemini=near(GEMINI_DIM, 0.6), bge=None)
    assert preflight_bge.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL database: 1 of 5 chunks have no bge vector" in out and "do not switch yet" in out


@needs_scratch
def test_preflight_fails_without_the_search_index(ready, capsys):
    sql(ready, "DROP INDEX chunks_embedding_bge_idx")
    assert preflight_bge.main([]) == 1
    assert "FAIL search index" in capsys.readouterr().out


@needs_scratch
def test_preflight_fails_when_the_model_makes_the_wrong_width(ready, monkeypatch, capsys):
    monkeypatch.setattr(embedding_engine, "embed_local", lambda texts: [unit(768, 1) for _ in texts])
    assert preflight_bge.main([]) == 1
    assert "FAIL model: expected two 1024-dimension vectors" in capsys.readouterr().out


@needs_scratch
def test_preflight_fails_when_a_search_finds_nothing(ready, monkeypatch, capsys):
    monkeypatch.setattr(search_module, "embed_local", lambda texts: [unit(BGE_DIM, 0, 0, 1) for _ in texts])  # far from every chunk (near() uses axes 1 and 2)
    assert preflight_bge.main([]) == 1
    assert "FAIL searches: no result" in capsys.readouterr().out


def test_preflight_refuses_another_engine(monkeypatch, capsys):
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "gemini")
    assert preflight_bge.main([]) == 2


# ---- the progress line of the fill ------------------------------------------------------------------------------------------------


def test_the_progress_line_has_the_wanted_shape():
    assert reembed_all.progress_line(576, 39002, 110.5) == "[bge] 576/39002 chunks embedded (1.5%) — 110.5 chunks/min — ETA: 5h 48m"


def test_the_progress_line_before_the_speed_is_known():
    assert reembed_all.progress_line(576, 39002, None) == "[bge] 576/39002 chunks embedded (1.5%) — measuring speed…"
    assert reembed_all.progress_line(576, 39002, 0) == "[bge] 576/39002 chunks embedded (1.5%) — measuring speed…"


def test_the_progress_line_when_everything_is_done():
    assert reembed_all.progress_line(39002, 39002, 120.0) == "[bge] 39002/39002 chunks embedded (100.0%) — 120.0 chunks/min — ETA: done"
    assert reembed_all.progress_line(0, 0, None).startswith("[bge] 0/0 chunks embedded (100.0%)")  # an empty archive does not divide by zero


@pytest.mark.parametrize("seconds, text", [(0, "<1m"), (59, "<1m"), (60, "1m"), (45 * 60, "45m"), (3600, "1h 0m"), (4 * 3600 + 32 * 60, "4h 32m"), (43 * 3600 + 10 * 60, "43h 10m"), (3599.6, "1h 0m")])
def test_the_eta_reads_as_a_person_would_say_it(seconds, text):
    assert reembed_all.format_eta(seconds) == text


def test_the_speed_is_none_until_something_has_been_measured():
    assert reembed_all._rate(0, 5) is None and reembed_all._rate(10, 0) is None
    assert reembed_all._rate(20, 60) == 20.0


@needs_scratch
def test_a_line_is_printed_every_ten_chunks_and_a_last_one_at_the_end(live, fake_bge, capsys):
    for i in range(18):  # 7 chunks exist: make 25
        add_chunk(live["db"], live["done"], 100 + i, f"more {i}", gemini=near(GEMINI_DIM, 0.6))
    assert reembed_all.main(["--yes"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("[bge]")]
    assert [l.split(" chunks embedded")[0] for l in lines] == ["[bge] 0/25", "[bge] 10/25", "[bge] 20/25", "[bge] 25/25"]
    assert lines[0].endswith("measuring speed…") and "(100.0%)" in lines[-1]
    assert all("chunks/min — ETA:" in l for l in lines[2:])  # the speed is known from the second batch on
    assert fake_bge == [10, 10, 5]  # the batches the model was asked for


@needs_scratch
def test_the_count_includes_what_earlier_runs_did(live, fake_bge, capsys):
    assert reembed_all.main(["--yes", "--limit", "3"]) == 0
    capsys.readouterr()
    assert reembed_all.main(["--yes"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("[bge]")]
    assert lines[0].startswith("[bge] 3/7 chunks embedded (42.9%)")  # the 3 of the first run count from the start
    assert lines[-1].startswith("[bge] 7/7 chunks embedded (100.0%)")


@needs_scratch
def test_the_fill_leaves_out_the_models_own_line_per_batch(live, capsys, monkeypatch):
    # the real embed_texts (it prints that line) with a fake model underneath: no 2 GB model in a test
    monkeypatch.setattr(embedding_engine, "EMBEDDING_ENGINE", "bge")
    monkeypatch.setattr(embedder, "embed_local", lambda texts: [near(BGE_DIM, 0.6) for _ in texts])
    assert reembed_all.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "[embed] " not in out and "[bge] 7/7" in out
    embedder.embed_texts(["x"])  # outside the fill the line is back
    assert "[embed] 1 chunks with the bge model" in capsys.readouterr().out


def test_no_test_can_reach_the_real_database_by_default():
    assert "tests-must-not-reach-the-real-database" in connection.DATABASE_URL
