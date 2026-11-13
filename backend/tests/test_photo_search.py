"""Photo search: understanding the request, the scoring, the search on a scratch Postgres (stand-in CLIP, spellings and page search), and the endpoints."""

import json
import os
from urllib.parse import urlparse

import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import auth
import main
from db import connection
from photos import db as photo_db
from photos import runtime
from photos import search as S
from photos.clip_text import ClipText
from photos.query import Spellings, photo_subject, spellings, wants_photos
from photos.store import StorageError

URL = os.environ.get("POOL_TEST_DATABASE_URL", "")
_parsed = urlparse(URL)
LOCAL = bool(URL) and (_parsed.hostname in ("localhost", "127.0.0.1") or (_parsed.hostname is None and "host=/" in URL))
needs_scratch = pytest.mark.skipif(not LOCAL, reason="set POOL_TEST_DATABASE_URL to a scratch Postgres with pgvector on this machine")
TEST_DB = "photo_search_test"


# ---- understanding the request -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["show photo of Kirloskar", "Photos of the Pune Expo 2010", "any pictures of the 1965 exhibition?", "give me the image of Dr. Rao",
                                   "किर्लोस्करांचा फोटो दाखवा", "लक्ष्मणराव किर्लोस्कर यांचे छायाचित्र", "मुझे तस्वीर दिखाओ", "Portrait of a founder"])
def test_requests_for_pictures_are_recognised(query):
    assert wants_photos(query)


@pytest.mark.parametrize("query", ["Who was the MCCIA president in 1965?", "चित्रपट उद्योग", "MCCIA ची स्थापना कधी झाली", "photography course fees", "imagery in Sampada", ""])
def test_other_questions_are_not_taken_for_photo_requests(query):
    assert not wants_photos(query)  # "चित्रपट" is a film, "photography" and "imagery" are other words


@pytest.mark.parametrize("query, subject", [("show photo of Kirloskar", "Kirloskar"), ("Photos of the Pune Expo 2010", "Pune Expo 2010"), ("किर्लोस्करांचा फोटो दाखवा", "किर्लोस्करांचा"),
                                            ("show me a photo", ""), ("Dr. D'Souza's portrait", "Dr D'Souza's")])
def test_the_subject_is_what_is_left_without_the_request_words(query, subject):
    assert photo_subject(query) == subject


def test_the_spellings_come_from_gemini_with_the_subject_always_among_them():
    asked = {}

    def generate(prompt, **kw):
        asked.update(prompt=prompt, **kw)
        return json.dumps({"english": "Kirloskar", "spellings": ["किर्लोस्कर", "किर्लोस्करांचा", "Kirloskar", " ", "x"]})

    result = spellings("Kirloskar", generate)
    assert result.english == "Kirloskar" and result.names == ("किर्लोस्कर", "किर्लोस्करांचा", "Kirloskar")  # blanks and one-letter entries dropped, no repeats
    assert asked["json_output"] is True and asked["temperature"] == 0.0 and "Kirloskar" in asked["prompt"]
    assert spellings("Phadke", lambda p, **kw: '```json\n{"english": "Phadke", "spellings": ["फडके"]}\n```').names == ("फडके", "Phadke")  # a fenced reply is read too


@pytest.mark.parametrize("reply", ["not json", '{"english": "", "spellings": []}', '{"spellings": "x"}', "[1, 2]", ""])
def test_a_reply_that_cannot_be_used_falls_back_to_the_subject_itself(reply):
    assert spellings("Kirloskar", lambda p, **kw: reply) == Spellings("Kirloskar", ("Kirloskar",))


def test_gemini_failing_never_stops_the_search():
    def boom(prompt, **kw):
        raise RuntimeError("quota")

    assert spellings("Kirloskar", boom) == Spellings("Kirloskar", ("Kirloskar",))
    assert spellings("", boom) == Spellings("", ())


# ---- the scoring -----------------------------------------------------------------------------------------------------------------------------


def test_a_caption_with_the_whole_name_scores_one_and_a_surname_alone_about_half():
    names = ("लक्ष्मणराव किर्लोस्कर", "किर्लोस्कर")
    assert S.caption_score("उद्योगमहर्षी कै. लक्ष्मणराव किर्लोस्कर", ["laxmanrao", "kirloskar"], names) == 1.0
    assert S.caption_score("किर्लोस्कर कारखाना", ["laxmanrao", "kirloskar"], names) == 0.5
    assert S.caption_score("एक कारखाना", ["laxmanrao", "kirloskar"], names) == 0.0


def test_a_request_word_found_literally_in_the_caption_counts_by_its_share():
    assert S.caption_score("Pune Expo 2010 opening", ["pune", "expo", "2010"], ()) == 1.0
    assert S.caption_score("the Pune fair", ["pune", "expo", "2010"], ()) == pytest.approx(1 / 3)


def test_a_marathi_ending_still_matches_the_name_inside_the_word():
    assert S.caption_score("किर्लोस्करांच्या कारखान्यात", ["kirloskar"], ("किर्लोस्कर",)) == 1.0


def test_very_short_words_never_count_and_nothing_scores_zero():
    assert S.caption_score("an of to", ["of", "to"], ()) == 0.0
    assert S.caption_score("", ["pune"], ("पुणे",)) == 0.0 and S.caption_score("Pune", [], ()) == 0.0


def test_the_page_and_picture_signals_are_ramps_from_nothing_to_certain():
    assert S.page_score(None) == 0 and S.page_score(0.40) == 0 and S.page_score(0.75) == 1 and S.page_score(0.575) == pytest.approx(0.5)
    assert S.picture_score(None) == 0 and S.picture_score(0.18) == 0 and S.picture_score(0.32) == 1 and S.picture_score(0.05) == 0


def test_the_weights_add_up_to_one_and_a_name_in_the_caption_outweighs_a_lookalike():
    assert S.W_CAPTION + S.W_PAGE + S.W_PICTURE == pytest.approx(1.0)
    assert S.combine(1.0, 0.8, 0.1) > S.combine(0.0, 0.5, 1.0)


def test_a_photograph_needs_at_least_one_clear_signal_to_be_shown():
    assert S.qualifies(0.5, None, None) and S.qualifies(0.0, 0.5, None) and S.qualifies(0.0, None, 0.26)
    assert not S.qualifies(0.3, 0.49, 0.25) and not S.qualifies(0.0, None, None)


def test_the_like_characters_in_a_request_are_taken_literally():
    assert S.like_pattern("100%") == "%100\\%%" and S.like_pattern("a_b") == "%a\\_b%" and S.like_pattern("Kirloskar") == "%Kirloskar%"


def test_clip_text_makes_a_unit_vector_from_a_photograph_sentence():
    import torch

    seen = {}

    class Tokenizer:
        def __call__(self, texts, **kw):
            seen["texts"] = texts
            return {"input_ids": torch.zeros(1, 3, dtype=torch.long)}

    class Model:
        def __call__(self, **tokens):
            return type("Out", (), {"text_embeds": torch.full((1, 512), 3.0)})()

    clip = ClipText()
    clip._tokenizer, clip._model = Tokenizer(), Model()
    vector = clip.embed("a factory ")
    assert vector.shape == (512,) and float(np.linalg.norm(vector)) == pytest.approx(1.0) and seen["texts"] == ["a photograph of a factory"]
    clip._model = type("Bad", (), {"__call__": lambda self, **t: type("O", (), {"text_embeds": torch.ones(1, 7)})()})()
    with pytest.raises(RuntimeError, match="expected 512"):
        clip.embed("x")


# ---- the search, on a scratch database ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    if not LOCAL:
        pytest.skip("no scratch database")
    with psycopg.connect(URL, autocommit=True) as admin:
        try:
            admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error:
            pytest.skip("this scratch Postgres has no pgvector")
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    url = make_conninfo(**{**conninfo_to_dict(URL), "dbname": TEST_DB})
    monkeypatch.setattr(connection, "DATABASE_URL", url)
    connection.close_pool()
    connection.init_db()
    with connection.get_connection() as conn:
        photo_db.init_tables(conn)
    yield url
    connection.close_pool()
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


def run_sql(url, query, params=None):
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchone()[0] if cur.description else None


def unit(axis: int) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    v[axis] = 1.0
    return v


def near(axis: int, similarity: float) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    v[axis] = similarity
    v[(axis + 1) % 512] = float(np.sqrt(1 - similarity**2))
    return v


def vec(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def add_doc(url, name, status="completed") -> int:
    return run_sql(url, "INSERT INTO documents (filename, drive_file_id, status, total_pages) VALUES (%s, %s, %s, 100) RETURNING id", (name, name, status))


def add_photo(url, doc, page, index=0, caption="", axis=100, similarity=0.0, kind="photo", face=False) -> int:
    return run_sql(url, "INSERT INTO photo_index (document_id, page_num, box_index, kind, storage_path, clip_embedding, description, contains_face) "
                        "VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s) RETURNING id",
                   (doc, page, index, kind, f"doc-{doc:04d}/p{page:04d}-{index}.jpg", vec(near(axis, similarity) if similarity else unit(axis)), caption or None, face))


class FakeClipText:
    def __init__(self, vector=None, fail=False):
        self.vector, self.fail, self.asked = vector, fail, []

    def embed(self, text):
        self.asked.append(text)
        if self.fail:
            raise RuntimeError("model missing")
        return self.vector if self.vector is not None else unit(0)


def names(*spellings_):
    return lambda subject: Spellings(english=subject, names=tuple(spellings_))


def no_pages(subject, scope):
    return []


def chunks(*items):  # (document_id, page_number, score)
    return lambda subject, scope: [{"document_id": d, "page_number": p, "score": s} for d, p, s in items]


@needs_scratch
def test_a_name_typed_in_latin_letters_finds_the_photo_whose_caption_spells_it_in_devanagari(db):
    doc = add_doc(db, "1961-April To1962-March Part-1.pdf")
    portrait = add_photo(db, doc, 214, caption="उद्योगमहर्षी कै. लक्ष्मणराव किर्लोस्कर", axis=10, face=True)
    other = add_photo(db, doc, 300, caption="एक कारखाना", axis=11)
    hits = S.search_photos("show photo of Kirloskar", 5, clip_text=None, spellings=names("किर्लोस्कर", "Kirloskar"), pages_for=no_pages)
    assert [h.id for h in hits] == [portrait]
    hit = hits[0]
    assert (hit.document_id, hit.page_number, hit.year, hit.contains_face, hit.kind) == (doc, 214, "1961-62", True, "photo") and hit.signals["caption"] == 1.0
    assert other not in [h.id for h in hits]


@needs_scratch
def test_a_photo_without_a_caption_is_found_through_the_article_on_its_page(db):
    doc = add_doc(db, "1952 April.PDF")
    on_the_page = add_photo(db, doc, 41, axis=12)
    elsewhere = add_photo(db, doc, 99, axis=13)
    hits = S.search_photos("photo of Kirloskar", 5, spellings=names("किर्लोस्कर"), pages_for=chunks((doc, 41, 0.72)))
    assert [h.id for h in hits] == [on_the_page] and hits[0].signals["page"] > 0.8 and elsewhere not in [h.id for h in hits]


@needs_scratch
def test_a_concept_is_found_by_what_the_picture_looks_like(db):
    doc = add_doc(db, "2011-Jan.PDF")
    dinner = add_photo(db, doc, 32, axis=20, similarity=0.30)
    unrelated = add_photo(db, doc, 33, axis=300, similarity=0.0)
    clip = FakeClipText(unit(20))
    hits = S.search_photos("photo of people at a dinner", 5, clip_text=clip, spellings=lambda s: Spellings("people at a dinner", ()), pages_for=no_pages)
    assert clip.asked == ["people at a dinner"]  # CLIP gets the English phrase
    assert [h.id for h in hits] == [dinner] and unrelated not in [h.id for h in hits]
    assert hits[0].signals["picture"] > 0.5 and hits[0].signals["caption"] == 0.0


@needs_scratch
def test_the_best_match_comes_first_and_every_signal_is_counted_for_every_photo(db):
    doc = add_doc(db, "1961-April To1962-March Part-1.pdf")
    by_all = add_photo(db, doc, 214, caption="किर्लोस्कर", axis=20, similarity=0.30)
    by_caption = add_photo(db, doc, 215, caption="किर्लोस्कर कारखाना", axis=400)
    by_page = add_photo(db, doc, 216, axis=401)
    hits = S.search_photos("Kirloskar", 5, clip_text=FakeClipText(unit(20)), spellings=names("किर्लोस्कर"), pages_for=chunks((doc, 214, 0.7), (doc, 216, 0.7)))
    # all three signals (0.92) beat the caption alone (0.45), which beats the page alone (0.26)
    assert [h.id for h in hits] == [by_all, by_caption, by_page]
    assert [round(h.score, 2) for h in hits] == [0.92, 0.45, 0.26] and len({h.id for h in hits}) == len(hits)  # no photo twice, though several signals found it


@needs_scratch
def test_a_request_naming_a_year_is_searched_in_that_years_issues_only(db):
    d65, d72 = add_doc(db, "1965-July To1966-June Part-1.PDF"), add_doc(db, "1972-Jul To 1973-Jun.PDF")
    in65 = add_photo(db, d65, 10, caption="किर्लोस्कर कारखाना", axis=30)
    in72 = add_photo(db, d72, 10, caption="किर्लोस्कर कारखाना", axis=31)
    hits = S.search_photos("photos of Kirloskar in 1965", 5, spellings=names("किर्लोस्कर"), pages_for=no_pages)
    assert [h.id for h in hits] == [in65] and in72 not in [h.id for h in hits]
    assert S.search_photos("photos of Kirloskar in 1930", 5, spellings=names("किर्लोस्कर"), pages_for=no_pages) == []  # no issue covers it: nothing, and no other year instead


@needs_scratch
def test_nothing_that_matches_well_gives_nothing_rather_than_the_least_bad(db):
    doc = add_doc(db, "1952 April.PDF")
    add_photo(db, doc, 5, caption="एक कारखाना", axis=40, similarity=0.0)
    assert S.search_photos("photo of a submarine", 5, clip_text=FakeClipText(unit(500)), spellings=lambda s: Spellings("a submarine", ()), pages_for=no_pages) == []
    assert S.search_photos("show me a photo", 5, pages_for=no_pages) == []  # a request that says of what: nothing


@needs_scratch
def test_at_most_top_k_are_returned(db):
    doc = add_doc(db, "1952 April.PDF")
    for page in range(1, 9):
        add_photo(db, doc, page, caption="किर्लोस्कर", axis=50 + page)
    assert len(S.search_photos("Kirloskar", 3, spellings=names("किर्लोस्कर"), pages_for=no_pages)) == 3


@needs_scratch
def test_the_page_search_or_clip_failing_still_leaves_the_caption_match(db):
    doc = add_doc(db, "1952 April.PDF")
    pid = add_photo(db, doc, 5, caption="किर्लोस्कर", axis=60)
    said = []

    def broken_pages(subject, scope):
        raise ConnectionError("embedding service down")

    hits = S.search_photos("Kirloskar", 5, clip_text=FakeClipText(fail=True), spellings=names("किर्लोस्कर"), pages_for=broken_pages, say=said.append)
    assert [h.id for h in hits] == [pid] and any("page search failed" in m for m in said) and any("CLIP failed" in m for m in said)


@needs_scratch
def test_a_percent_sign_in_the_request_is_not_a_wildcard(db):
    doc = add_doc(db, "1952 April.PDF")
    add_photo(db, doc, 5, caption="एक कारखाना ज्याचे उत्पादन वाढले", axis=61)
    assert S.search_photos("photo of 100%", 5, spellings=lambda s: Spellings(s, ()), pages_for=no_pages) == []  # "%100%%" would have matched every caption


# ---- the endpoints -----------------------------------------------------------------------------------------------------------------------------

KEY = "k" * 32
HEADERS = {"X-API-Key": KEY, "X-User-Id": "tester"}


def hit(pid=1, page=214, caption="किर्लोस्कर", score=0.8):
    return S.PhotoHit(id=pid, document_id=3, page_number=page, filename="1961-April To1962-March Part-1.pdf", year="1961-62", caption=caption, kind="photo",
                      contains_face=True, storage_path=f"doc-0003/p{page:04d}-0.jpg", score=score, signals={"caption": 1.0, "page": 0.5, "picture": 0.1})


class FakeStore:
    def __init__(self, files=None, fail=False):
        self.files, self.fail, self.signed = files or {}, fail, []

    def get(self, path):
        if self.fail:
            raise StorageError("reading failed: HTTP 500")
        return self.files[path]

    def signed_urls(self, paths, seconds=3600):
        self.signed.append(paths)
        return {p: f"https://x.supabase.co/storage/v1/object/sign/sampada-images/{p}?token=T" for p in paths}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "API_KEY", KEY)
    state = {"store": FakeStore(), "hits": [hit()], "searches": [], "audit": [], "rag": []}
    monkeypatch.setattr(runtime, "get_store", lambda: state["store"])
    monkeypatch.setattr(runtime, "get_clip", lambda: "clip")
    monkeypatch.setattr(main.audit, "record", lambda *a, **kw: state["audit"].append({"args": a, **kw}))

    def fake_search(query, top_k, *, clip_text=None, **kw):
        state["searches"].append({"query": query, "top_k": top_k, "clip": clip_text})
        if isinstance(state["hits"], Exception):
            raise state["hits"]
        return state["hits"]

    monkeypatch.setattr(main, "search_photos", fake_search)

    def fake_rag(query, top_k=None, chat_history=None):
        state["rag"].append(query)
        return {"answer": "Kirloskar founded ...", "sources": [], "found": True, "clarify": state.get("clarify", False), "search_query": state.get("resolved"), "resolved": ""}

    monkeypatch.setattr(main, "rag", fake_rag)
    test_client = TestClient(main.app)
    test_client.state = state
    return test_client


def test_the_photo_search_returns_photographs_with_one_hour_signed_links(client):
    response = client.post("/search/photos", json={"query": "photo of Kirloskar"}, headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["searched_for"] == "Kirloskar"
    [p] = body["photos"]
    assert (p["id"], p["page_number"], p["year"], p["caption"], p["contains_face"], p["kind"]) == (1, 214, "1961-62", "किर्लोस्कर", True, "photo")
    assert p["signed_url"].startswith("https://x.supabase.co/storage/v1/object/sign/") and "token=" in p["signed_url"]
    assert client.state["searches"][0]["top_k"] == 5 and client.state["searches"][0]["clip"] == "clip"
    assert client.state["audit"][0]["detail"] == "photo search" and client.state["audit"][0]["query"] == "photo of Kirloskar"


def test_the_photo_search_needs_the_api_key_and_a_sensible_request(client):
    assert client.post("/search/photos", json={"query": "x"}).status_code == 401
    assert client.post("/search/photos", json={"query": "x"}, headers={"X-API-Key": "wrong" * 8, "X-User-Id": "t"}).status_code == 401
    assert client.post("/search/photos", json={"query": ""}, headers=HEADERS).status_code == 422
    assert client.post("/search/photos", json={"query": "x" * 301}, headers=HEADERS).status_code == 422
    assert client.post("/search/photos", json={"query": "x", "top_k": 99}, headers=HEADERS).status_code == 422
    assert client.post("/search/photos", json={"query": "   "}, headers=HEADERS).status_code == 400


def test_top_k_is_passed_on(client):
    client.post("/search/photos", json={"query": "photo of x", "top_k": 3}, headers=HEADERS)
    assert client.state["searches"][0]["top_k"] == 3


def test_a_server_without_the_storage_key_says_photo_search_is_not_available(client):
    client.state["store"] = None
    response = client.post("/search/photos", json={"query": "photo of Kirloskar"}, headers=HEADERS)
    assert response.status_code == 503 and "not available" in response.json()["detail"]


def test_a_search_that_fails_is_a_500_and_leaks_nothing(client):
    client.state["hits"] = ValueError("secret detail")
    response = client.post("/search/photos", json={"query": "photo of Kirloskar"}, headers=HEADERS)
    assert response.status_code == 500 and "secret" not in response.text


def test_the_search_still_answers_if_signing_fails(client):
    client.state["store"].signed_urls = lambda paths, seconds=3600: (_ for _ in ()).throw(StorageError("signing failed"))
    [p] = client.post("/search/photos", json={"query": "photo of Kirloskar"}, headers=HEADERS).json()["photos"]
    assert p["signed_url"] is None and p["id"] == 1  # the picture is still available through /photos/{id}/image


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.params = params
        return type("R", (), {"fetchone": lambda s: self.rows.get(params[0])})()


def test_a_photograph_is_served_from_private_storage_with_a_day_of_caching(client, monkeypatch):
    monkeypatch.setattr(main, "get_pooled_connection", lambda: FakeConn({7: ("doc-0003/p0214-0.jpg",)}))
    client.state["store"] = FakeStore({"doc-0003/p0214-0.jpg": b"\xff\xd8JPEG"})
    response = client.get("/photos/7/image", headers=HEADERS)
    assert response.status_code == 200 and response.content == b"\xff\xd8JPEG"
    assert response.headers["content-type"] == "image/jpeg" and "max-age=86400" in response.headers["cache-control"] and "private" in response.headers["cache-control"]


def test_an_unknown_photograph_is_a_404_a_storage_failure_a_502_and_no_key_a_503_or_401(client, monkeypatch):
    monkeypatch.setattr(main, "get_pooled_connection", lambda: FakeConn({7: ("doc-0003/p0214-0.jpg",)}))
    assert client.get("/photos/8/image", headers=HEADERS).status_code == 404
    client.state["store"] = FakeStore(fail=True)
    assert client.get("/photos/7/image", headers=HEADERS).status_code == 502
    client.state["store"] = None
    assert client.get("/photos/7/image", headers=HEADERS).status_code == 503
    assert client.get("/photos/7/image").status_code == 401
    assert client.get("/photos/abc/image", headers=HEADERS).status_code == 422


# ---- photographs in the chat ----------------------------------------------------------------------------------------------------------------------


def test_asking_for_a_photo_in_the_chat_returns_the_answer_with_photographs(client):
    body = client.post("/chat", json={"query": "show photo of Kirloskar"}, headers=HEADERS).json()
    assert body["answer"].startswith("Kirloskar") and [p["id"] for p in body["photos"]] == [1] and body["photos"][0]["signed_url"] is None  # chat uses /photos/{id}/image
    assert client.state["searches"][0]["query"] == "show photo of Kirloskar"
    assert client.state["audit"][0]["detail"] == "photos: 1"


def test_an_ordinary_question_does_not_search_photographs(client):
    body = client.post("/chat", json={"query": "Who founded Kirloskar Brothers?"}, headers=HEADERS).json()
    assert body["photos"] == [] and client.state["searches"] == []


def test_a_question_back_does_not_search_photographs(client):
    client.state["clarify"] = True
    assert client.post("/chat", json={"query": "show his photo"}, headers=HEADERS).json()["photos"] == [] and client.state["searches"] == []


def test_a_follow_up_is_searched_as_it_was_made_specific(client):
    client.state["resolved"] = "photo of Laxmanrao Kirloskar, founder of Kirloskar Brothers"
    client.post("/chat", json={"query": "show his photo", "history": [{"question": "Who founded Kirloskar Brothers?", "answer": "Laxmanrao Kirloskar."}]}, headers=HEADERS)
    assert client.state["searches"][0]["query"] == "photo of Laxmanrao Kirloskar, founder of Kirloskar Brothers"


@pytest.mark.parametrize("failure", [RuntimeError("no key"), ValueError("bad"), ConnectionError("db down")])
def test_a_photo_search_that_fails_never_costs_the_chat_its_answer(client, failure):
    client.state["hits"] = failure
    response = client.post("/chat", json={"query": "show photo of Kirloskar"}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["photos"] == [] and response.json()["answer"].startswith("Kirloskar")


def test_a_slow_photo_search_is_given_up_on_so_the_answer_is_not_held_back(client, monkeypatch):
    import time

    monkeypatch.setattr(main, "PHOTO_SEARCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(main, "find_photos", lambda query, top_k=5, signed=False: time.sleep(2) or [])
    started = time.monotonic()
    response = client.post("/chat", json={"query": "show photo of Kirloskar"}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["photos"] == [] and time.monotonic() - started < 1.5


# ---- names spelt differently in the caption (consonant skeletons) ------------------------------------------------------------------------------

from photos.devanagari import matches, significant_skeletons, skeleton


@pytest.mark.parametrize("a, b", [("पारखे", "पार्खे"), ("किर्लोस्कर", "किर्लास्कर"), ("किर्लोस्कर", "किर्लोस्कर‍"), ("सोवनी", "सोवनी")])
def test_names_that_differ_only_in_vowel_signs_and_joiners_have_the_same_skeleton(a, b):
    assert skeleton(a) == skeleton(b) and len(skeleton(a)) >= 3


def test_a_skeleton_keeps_only_devanagari_consonants():
    assert skeleton("Parkhe") == "" and skeleton("१९६५") == "" and skeleton("") == "" and skeleton(None) == ""
    assert skeleton("पारखे, श्री.") == skeleton("परख श्र")


def test_words_with_fewer_than_three_consonants_are_not_used():
    assert significant_skeletons("चा श्री किर्लोस्कर") == ["करलसकर"]  # चा (1 consonant) and श्री (2) are left out
    assert matches("चा", skeleton("चा चा")) == 0


def test_a_spelling_with_several_words_must_match_all_its_long_words():
    caption = skeleton("उद्योग प्रदर्शन")
    assert matches("पार्खे उद्योग समूह", caption) == 0  # one common word is not enough
    assert matches("उद्योग प्रदर्शन", caption) == 2


def test_a_name_spelt_one_way_finds_the_caption_that_spells_it_another():
    caption = "श्री.म.स. पारखे, श्री. शे. वा. किर्लोस्कर, ना, जी."
    assert S.caption_score(caption, ["parkhe"], ("पार्खे", "Parkhe")) == 1.0  # Gemini spelt it पार्खे, the magazine printed पारखे
    assert S.caption_score("चहापान-समारंभार्चे एक दृश्य", ["parkhe"], ("पार्खे", "Parkhe")) == 0.0
    assert S.caption_score("श्री. किर्लास्कर", ["kirloskar"], ("किर्लोस्कर",)) == 1.0  # an OCR slip in the caption
    assert S.caption_score("उद्योग प्रदर्शन", ["parkhe"], ("पार्खे उद्योग समूह",)) == 0.0


def test_a_latin_only_caption_and_latin_spellings_are_unaffected():
    assert S.caption_score("Pune Expo 2010 opening", ["pune", "expo", "2010"], ("Pune Expo",)) == 1.0
    assert S.caption_score("Dr. Rao", ["rao"], ("Rao",)) == 1.0


@needs_scratch
def test_the_caption_lookup_in_the_database_finds_a_differently_spelt_name(db):
    doc = add_doc(db, "1947 April.PDF")
    parkhe = add_photo(db, doc, 158, caption="श्री.म.स. पारखे, श्री. शे. वा. किर्लोस्कर", axis=70)
    add_photo(db, doc, 13, caption="चहापान-समारंभार्चे एक दृश्य", axis=71)
    hits = S.search_photos("show photo of Parkhe", 5, spellings=names("पार्खे", "Parkhe"), pages_for=no_pages)  # the spelling does not occur in the caption
    assert [h.id for h in hits] == [parkhe] and hits[0].signals["caption"] == 1.0


@needs_scratch
def test_the_skeleton_lookup_is_literal_about_the_database_regex(db):
    doc = add_doc(db, "1947 April.PDF")
    add_photo(db, doc, 5, caption="Latin caption only", axis=72)
    assert S.search_photos("photo of Parkhe", 5, spellings=names("पार्खे"), pages_for=no_pages) == []  # a caption with no Devanagari has no skeleton and matches nothing
