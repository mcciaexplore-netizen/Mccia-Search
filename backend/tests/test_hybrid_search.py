"""Hybrid search: the keyword words of a question, the scoring, and (on a scratch database) the real SQL end to end.

The word and scoring tests need nothing. The SQL tests need a scratch Postgres WITH the pgvector extension on this machine (never the
real database), given as POOL_TEST_DATABASE_URL (localhost, 127.0.0.1 or a local socket); they are skipped without one.
"""

import math
import os
from urllib.parse import urlparse

import psycopg
import pytest

from db import connection
from retrieval import search as search_module
from retrieval.search import CANDIDATES, KEYWORD_WEIGHT, MAX_KEYWORDS, TOP_K, VECTOR_WEIGHT, keyword_terms, rank

# ---- the question's words -------------------------------------------------------------------------------------------------------


def test_stop_words_and_the_archives_own_names_are_not_searched_for():
    assert keyword_terms("Who was MCCIA president in 1965?") == ["president", "1965"]
    assert keyword_terms("What industries were prominent in Pune in 1972?") == ["industries", "prominent", "pune", "1972"]


def test_marathi_and_hindi_questions_are_read_too():
    assert keyword_terms("MCCIA च्या स्थापनेची माहिती द्या") == ["स्थापनेची"]
    assert keyword_terms("१९६५ मध्ये अध्यक्ष कोण होते?") == ["1965", "अध्यक्ष"]
    assert keyword_terms("1965 में अध्यक्ष कौन थे?") == ["1965", "अध्यक्ष"]


def test_words_are_cleaned_deduplicated_and_in_order():
    assert keyword_terms('"Exports", exports (EXPORTS)! textiles;') == ["exports", "textiles"]
    assert keyword_terms("  exports...   TEXTILES,  exports  ") == ["exports", "textiles"]


def test_one_letter_words_and_blank_questions_give_no_words():
    assert keyword_terms("a I ?") == []
    assert keyword_terms("") == [] and keyword_terms("   ") == []
    assert keyword_terms("Who was the ...") == []


def test_a_long_question_is_cut_at_the_limit():
    assert len(keyword_terms(" ".join(f"word{i}" for i in range(40)))) == MAX_KEYWORDS


def test_the_search_syntax_cannot_be_reached_through_a_word():
    # Words go to websearch_to_tsquery one at a time; a leading minus or a quote is trimmed, not obeyed.
    assert keyword_terms("-exports") == ["exports"] and keyword_terms('"exports"') == ["exports"]


# ---- scoring --------------------------------------------------------------------------------------------------------------------


def row(chunk_id, similarity, keyword_score, filename="a.pdf", page=1):
    return (chunk_id, f"text {chunk_id}", page, 0, filename, "drive", 1, similarity, keyword_score)


def test_the_weights_are_sixty_forty_and_five_chunks_are_returned():
    assert (VECTOR_WEIGHT, KEYWORD_WEIGHT, TOP_K) == (0.6, 0.4, 5) and CANDIDATES >= TOP_K


def test_the_score_is_sixty_percent_meaning_and_forty_percent_keywords():
    [best] = rank([row(1, 0.70, 0.5)], 5)
    assert math.isclose(best["score"], 0.6 * 0.70 + 0.4 * 0.5)
    assert (best["similarity"], best["keyword_score"]) == (0.70, 0.5)  # `similarity` stays the pure cosine similarity


def test_a_page_with_the_words_can_beat_a_page_that_is_only_somewhat_closer_in_meaning():
    ranked = rank([row(1, 0.70, 0.0), row(2, 0.62, 1.0), row(3, 0.66, 0.5)], 5)
    assert [r["chunk_id"] for r in ranked] == [2, 3, 1]


def test_only_top_k_are_kept_and_ties_are_broken_by_meaning_then_id():
    rows = [row(i, 0.50 + i / 100, 0.0) for i in range(1, 9)]
    assert [r["chunk_id"] for r in rank(rows, 5)] == [8, 7, 6, 5, 4]
    assert [r["chunk_id"] for r in rank([row(2, 0.5, 0.5), row(1, 0.5, 0.5)], 5)] == [1, 2]
    assert rank([], 5) == []


# ---- the real SQL on a scratch database -----------------------------------------------------------------------------------------

URL = os.environ.get("POOL_TEST_DATABASE_URL", "")
_parsed = urlparse(URL)
LOCAL = bool(URL) and (_parsed.hostname in ("localhost", "127.0.0.1") or (_parsed.hostname is None and "host=/" in URL))
DIM = 768  # the width of chunks.embedding (db/schema.sql)

needs_scratch = pytest.mark.skipif(not LOCAL, reason="set POOL_TEST_DATABASE_URL to a scratch Postgres with pgvector on this machine")


def axis(*weights: float) -> list[float]:
    """A unit vector whose cosine similarity to axis(1) is weights[0] / norm: handy for choosing a chunk's similarity exactly."""
    vector = [0.0] * DIM
    for i, w in enumerate(weights):
        vector[i] = w
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def sim(target: float) -> list[float]:
    """A unit vector whose cosine similarity to the query vector axis(1) is exactly `target`."""
    return axis(target, math.sqrt(1 - target * target))


@pytest.fixture
def archive(monkeypatch):
    """Three issues and a handful of chunks in a scratch database, and a query vector of axis(1) in place of Gemini."""
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
    monkeypatch.setattr(search_module, "ensure_matches_database", lambda: None)
    monkeypatch.setattr(search_module, "min_similarity", lambda: 0.45)
    monkeypatch.setattr(search_module, "embed_query", lambda text: axis(1))
    ids = {}
    with psycopg.connect(URL, autocommit=True) as admin:
        for name in ("1965 Jan To1965 Dec.pdf", "1972 July.PDF", "1960 Jan.PDF"):
            ids[name] = admin.execute("INSERT INTO documents (filename, drive_file_id) VALUES (%s, %s) RETURNING id", (name, name)).fetchone()[0]

    def add(doc, content, similarity, page=1):
        with psycopg.connect(URL, autocommit=True) as admin:
            admin.execute("INSERT INTO chunks (document_id, page_number, chunk_index, content, embedding) VALUES (%s, %s, 0, %s, %s::vector)",
                          (ids[doc], page, content, str(sim(similarity))))

    yield add
    connection.close_pool()
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS documents")


@needs_scratch
def test_pages_with_the_questions_words_move_up_but_meaning_still_sets_the_floor(archive):
    archive("1965 Jan To1965 Dec.pdf", "A long report on trade fairs and exhibitions held across the city.", 0.70, page=1)
    archive("1965 Jan To1965 Dec.pdf", "The annual meeting elected the new president of the chamber in 1965.", 0.62, page=2)
    archive("1965 Jan To1965 Dec.pdf", "president elected president 1965 president", 0.40, page=3)  # perfect words, too far in meaning
    results = search_module.search("Who was the president in 1965?")
    assert [r["page_number"] for r in results] == [2, 1]  # page 3 is below the 0.45 floor and never returned
    top = results[0]
    assert math.isclose(top["similarity"], 0.62, abs_tol=1e-6) and top["keyword_score"] == 1.0
    assert math.isclose(top["score"], 0.6 * 0.62 + 0.4, abs_tol=1e-6)
    assert results[1]["keyword_score"] == 0.0


@needs_scratch
def test_a_year_in_the_question_still_limits_the_search_to_the_issues_that_cover_it(archive):
    archive("1960 Jan.PDF", "president 1965 president", 0.90)  # far better on both counts, but from the wrong issue
    archive("1965 Jan To1965 Dec.pdf", "The president was elected in 1965.", 0.55)
    results = search_module.search("Who was the president in 1965?")
    assert [r["filename"] for r in results] == ["1965 Jan To1965 Dec.pdf"]
    # With no year named, the better page of the other issue is found too.
    assert [r["filename"] for r in search_module.search("Who was the president?")][0] == "1960 Jan.PDF"


@needs_scratch
def test_a_year_no_issue_covers_finds_nothing(archive):
    archive("1965 Jan To1965 Dec.pdf", "The president was elected in 1965.", 0.80)
    assert search_module.search("What happened in 2010?") == []


@needs_scratch
def test_five_results_by_default_and_the_number_asked_for_otherwise(archive):
    for i in range(8):
        archive("1972 July.PDF", f"exports page {i}", 0.50 + i / 100, page=i)
    assert len(search_module.search("exports")) == 5
    assert len(search_module.search("exports", top_k=7)) == 7
    assert [r["page_number"] for r in search_module.search("exports", top_k=2)] == [7, 6]


@needs_scratch
def test_devanagari_digits_and_words_are_matched(archive):
    archive("1965 Jan To1965 Dec.pdf", "अध्यक्ष म्हणून श्री. क यांची १९६५ मध्ये निवड झाली.", 0.60, page=1)
    archive("1965 Jan To1965 Dec.pdf", "व्यापार प्रदर्शनाचा अहवाल.", 0.62, page=2)
    results = search_module.search("१९६५ मध्ये अध्यक्ष कोण होते?")
    assert results[0]["page_number"] == 1 and results[0]["keyword_score"] == 1.0
    assert results[1]["keyword_score"] == 0.0


@needs_scratch
def test_blank_and_word_free_questions_do_not_break_the_search(archive):
    archive("1972 July.PDF", "some text about textiles", 0.70)
    assert search_module.search("   ") == []
    [only] = search_module.search("?!")  # no keywords at all: ordered by meaning alone, keyword score 0
    assert only["keyword_score"] == 0.0 and math.isclose(only["score"], 0.6 * 0.70, abs_tol=1e-6)


@needs_scratch
def test_a_year_written_in_devanagari_digits_matches_a_year_typed_in_either(archive):
    archive("1965 Jan To1965 Dec.pdf", "श्री. क यांची १९६५ मध्ये निवड झाली.", 0.60, page=1)
    archive("1965 Jan To1965 Dec.pdf", "व्यापार प्रदर्शनाचा अहवाल.", 0.62, page=2)
    for question in ("निवड 1965", "निवड १९६५"):
        results = search_module.search(question)
        assert results[0]["page_number"] == 1 and results[0]["keyword_score"] == 1.0, question


@needs_scratch
def test_a_marathi_word_also_matches_inside_a_longer_word_but_an_english_word_only_whole(archive):
    archive("1972 July.PDF", "नवीन अध्यक्षपदी श्री. क यांची निवड झाली.", 0.60, page=1)  # अध्यक्षपदी contains अध्यक्ष
    archive("1972 July.PDF", "The vicepresident addressed the meeting.", 0.60, page=2)  # vicepresident contains president
    marathi = {r["page_number"]: r["keyword_score"] for r in search_module.search("अध्यक्ष")}
    english = {r["page_number"]: r["keyword_score"] for r in search_module.search("president")}
    assert marathi[1] == 1.0  # plain-text fallback: the tokenizer's locale cannot hide a Devanagari word
    assert english[2] == 0.0  # full-text search: whole words only
