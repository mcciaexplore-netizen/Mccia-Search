"""The Surya OCR engine (ocr.py) and the re-OCR script (ingest.reocr_surya), without the 1.8 GB model: a fake recognizer stands in for it.

The database parts need a scratch Postgres with pgvector (POOL_TEST_DATABASE_URL); they use a database of their own inside it.
"""

import io
import os
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pymupdf
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import config
from db import connection
from ingest import drive_sync, ocr, pipeline, reocr_surya

URL = os.environ.get("POOL_TEST_DATABASE_URL", "")
_parsed = urlparse(URL)
LOCAL = bool(URL) and (_parsed.hostname in ("localhost", "127.0.0.1") or (_parsed.hostname is None and "host=/" in URL))
needs_scratch = pytest.mark.skipif(not LOCAL, reason="set POOL_TEST_DATABASE_URL to a scratch Postgres with pgvector on this machine")
TEST_DB = "surya_ocr_test"


# ---- fakes ---------------------------------------------------------------------------------------------------------------------


def block(html="", order=0, skipped=False, error=False):
    return types.SimpleNamespace(html=html, reading_order=order, skipped=skipped, error=error)


def page_result(*blocks):
    return types.SimpleNamespace(blocks=list(blocks))


class FakeRecognizer:
    """Stands in for surya.recognition.RecognitionPredictor: returns what it was told to, and notes how many calls run at once."""

    def __init__(self, result=None, error=None, delay=0.0):
        self.result, self.error, self.delay = result, error, delay
        self.calls, self.active, self.most_at_once = 0, 0, 0
        self._lock = threading.Lock()

    def __call__(self, images):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.most_at_once = max(self.most_at_once, self.active)
        try:
            time.sleep(self.delay)
            if self.error:
                raise self.error
            return [self.result if self.result is not None else page_result(block("<p>text</p>"))]
        finally:
            with self._lock:
                self.active -= 1


def png() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    return pix.tobytes("png")


@pytest.fixture
def surya(monkeypatch):
    """The Surya engine selected, with a fake recognizer; returns the fake."""
    fake = FakeRecognizer()
    monkeypatch.setattr(ocr, "OCR_ENGINE", "surya")
    monkeypatch.setattr(ocr, "_surya_recognizer", lambda: fake)
    return fake


# ---- the HTML Surya writes becomes plain text --------------------------------------------------------------------------------------


def test_paragraphs_become_lines_and_inline_tags_vanish_without_splitting_words():
    assert ocr._html_to_text("<p>Hello &amp; <b>bold</b>text</p><p>दुसरा   परिच्छेद</p>") == "Hello & boldtext\nदुसरा परिच्छेद"


def test_table_cells_stay_apart_and_rows_get_their_own_line():
    assert ocr._html_to_text("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>") == "a b\nc d"


def test_line_breaks_nonbreaking_spaces_and_blank_lines():
    assert ocr._html_to_text("one<br/>two<br>&nbsp;<br>three four") == "one\ntwo\nthree four"
    assert ocr._html_to_text("") == "" and ocr._html_to_text("<p> </p>") == ""


def test_marathi_text_is_kept_exactly():
    text = "भारताचे पहिले राष्ट्रपती डॉ. राजेंद्र प्रसाद ॥ शुभ दीपावली ॥ १९६५"
    assert ocr._html_to_text(f"<p>{text}</p>") == text


def test_a_page_reads_its_blocks_in_reading_order_apart_by_a_blank_line_and_leaves_out_pictures():
    result = page_result(
        block("<p>second</p>", order=1),
        block("<p>first</p>", order=0),
        block("<img src='x'>", order=2, skipped=True),
        block("", order=3),
        block("<p>third</p>", order=4),
    )
    assert ocr._surya_page_text(result) == "first\n\nsecond\n\nthird"


# ---- the engine -------------------------------------------------------------------------------------------------------------------


def test_surya_is_one_of_the_engines():
    assert "surya" in ocr.ENGINES


def test_a_page_is_read_by_the_surya_engine(surya):
    surya.result = page_result(block("<p>एक</p>", 0), block("<p>two</p>", 1))
    assert ocr.ocr_page(png(), 1) == "एक\n\ntwo"
    assert surya.calls == 1


def test_a_blank_page_is_an_empty_string_not_an_error(surya):
    surya.result = page_result()
    assert ocr.ocr_page(png(), 1) == ""


def test_an_unreadable_block_fails_the_whole_page_so_that_it_is_retried_not_stored_with_a_hole(surya):
    surya.result = page_result(block("<p>ok</p>", 0), block("", 1, error=True))
    with pytest.raises(ocr.OCRPageError, match="could not read 1 block"):
        ocr.ocr_page(png(), 7)


def test_a_server_or_model_error_fails_just_that_page(surya):
    surya.error = ConnectionError("server went away")
    with pytest.raises(ocr.OCRPageError, match=r"page 3: Surya failed: server went away"):
        ocr.ocr_page(png(), 3)


def test_an_unreadable_image_fails_just_that_page(surya):
    with pytest.raises(ocr.OCRPageError, match="page 2: Surya failed"):
        ocr.ocr_page(b"not an image", 2)


def test_an_empty_image_is_an_error_like_for_every_engine(surya):
    with pytest.raises(ocr.OCRPageError, match="no image data"):
        ocr.ocr_page(b"", 4)


def test_a_whole_pdf_is_read_in_parallel_and_cached(surya, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "OCR_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ocr, "_prepare_engine", lambda: None)
    monkeypatch.setattr(ocr, "OCR_CONCURRENCY", 3)
    surya.delay = 0.05
    doc = pymupdf.open()
    for number in range(6):
        doc.new_page().insert_text((50, 100), f"page {number}")
    pdf = doc.tobytes()
    pages, failed = ocr.get_or_ocr(pdf, "drive_id_1")
    assert failed == set() and [p["page_number"] for p in pages] == [1, 2, 3, 4, 5, 6] and all(p["text"] == "text" for p in pages)
    assert surya.calls == 6 and surya.most_at_once > 1  # several pages at once, as OCR_CONCURRENCY allows
    assert len(list((tmp_path / "cache" / "drive_id_1").glob("page_*.json"))) == 6
    again, _ = ocr.get_or_ocr(pdf, "drive_id_1")
    assert surya.calls == 6 and again == pages  # the second time nothing is read


# ---- what must be installed -------------------------------------------------------------------------------------------------------


def test_without_llama_server_the_run_stops_with_what_to_install(monkeypatch):
    monkeypatch.delenv("SURYA_INFERENCE_URL", raising=False)
    monkeypatch.delenv("SURYA_INFERENCE_BACKEND", raising=False)
    monkeypatch.delenv("LLAMA_CPP_BINARY", raising=False)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="brew install llama.cpp"):
        ocr._check_surya()


@pytest.mark.parametrize("env", [{"SURYA_INFERENCE_URL": "http://gpu:8000/v1"}, {"SURYA_INFERENCE_BACKEND": "vllm"}, {"SURYA_INFERENCE_BACKEND": "mlx"}])
def test_an_existing_server_or_another_backend_needs_no_llama_server(monkeypatch, env):
    for name in ("SURYA_INFERENCE_URL", "SURYA_INFERENCE_BACKEND", "LLAMA_CPP_BINARY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    ocr._check_surya()


def test_llama_server_found_on_the_path_is_enough(monkeypatch):
    for name in ("SURYA_INFERENCE_URL", "SURYA_INFERENCE_BACKEND", "LLAMA_CPP_BINARY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/opt/homebrew/bin/llama-server")
    ocr._check_surya()


def test_preparing_the_engine_checks_the_install_and_starts_the_server_once(monkeypatch):
    seen = []
    monkeypatch.setattr(ocr, "OCR_ENGINE", "surya")
    monkeypatch.setattr(ocr, "_check_surya", lambda: seen.append("check"))
    monkeypatch.setattr(ocr, "_surya_recognizer", lambda: seen.append("start"))
    ocr._prepare_engine()
    assert seen == ["check", "start"]


def test_the_server_is_started_once_and_told_how_many_pages_to_decode_at_once(monkeypatch):
    started = []

    class Manager:
        def start(self):
            started.append(1)

    class Recognizer:
        def __init__(self, manager):
            self.manager = manager

    monkeypatch.setitem(sys.modules, "surya", types.ModuleType("surya"))
    monkeypatch.setitem(sys.modules, "surya.inference", types.SimpleNamespace(SuryaInferenceManager=Manager))
    monkeypatch.setitem(sys.modules, "surya.recognition", types.SimpleNamespace(RecognitionPredictor=Recognizer))
    monkeypatch.delenv("SURYA_INFERENCE_PARALLEL", raising=False)
    ocr._surya_recognizer.cache_clear()
    try:
        first, second = ocr._surya_recognizer(), ocr._surya_recognizer()
        assert first is second and started == [1]
        assert os.environ["SURYA_INFERENCE_PARALLEL"] == str(ocr.SURYA_PARALLEL)
    finally:
        ocr._surya_recognizer.cache_clear()
        os.environ.pop("SURYA_INFERENCE_PARALLEL", None)


def test_without_the_package_the_run_says_which_extra_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "surya.inference", None)  # makes the import fail
    ocr._surya_recognizer.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="uv sync --extra surya"):
            ocr._surya_recognizer()
    finally:
        ocr._surya_recognizer.cache_clear()


# ---- database: which PDFs the script takes, and how two machines stay apart ------------------------------------------------------


@pytest.fixture
def archive(monkeypatch):
    """A scratch database with the real schema and documents in every state and year, and Google Drive and the OCR pipeline faked."""
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

    rows = [
        ("1955-April To 1956 -March.PDF", "failed", 600),
        ("1961-April To1962-March Part-1.pdf", "failed", 500),
        ("1961-April To1962-March Part-2.PDF", "completed", 500),  # live: must never be touched
        ("1966-Jul To 1967-Jun.PDF", "processing", 700),  # a run that stopped
        ("1970-Jul To 1971-Jun_2.PDF", "text_saved", 400),  # already read, waiting for embeddings
        ("2003-Jan To 2003-Dec.PDF", "failed", 588),
        ("2004-Jan To 2004-Dec.PDF", "pending", 584),
        ("2011-Jan.PDF", "completed", 44),
    ]
    ids = {}
    with psycopg.connect(url, autocommit=True) as c:
        for name, status, pages in rows:
            ids[name] = c.execute(
                "INSERT INTO documents (filename, drive_file_id, status, total_pages) VALUES (%s, %s, %s, %s) RETURNING id",
                (name, "drive-" + name, status, pages),
            ).fetchone()[0]
            c.execute("INSERT INTO chunks (document_id, chunk_index, content, page_number) VALUES (%s, 0, %s, 1)", (ids[name], "old text of " + name))

    handled = []

    def fake_ingest_one(data, drive_file_id, filename, embed=True):
        handled.append(filename)
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("UPDATE documents SET status = 'text_saved' WHERE drive_file_id = %s", (drive_file_id,))
        return "text_saved"

    monkeypatch.setattr(drive_sync, "build_drive_service", lambda: "service")
    monkeypatch.setattr(drive_sync, "list_pdfs", lambda service: [{"file_id": "drive-" + n, "name": n, "size": 1} for n, _, _ in rows])
    monkeypatch.setattr(drive_sync, "stream_pdf", lambda service, file_id, name, size: b"%PDF-fake")
    monkeypatch.setattr(pipeline, "ingest_one", fake_ingest_one)
    monkeypatch.setattr(ocr, "_prepare_engine", lambda: None)
    monkeypatch.setattr(ocr, "OCR_ENGINE", "gemini")  # main() sets these; monkeypatch puts them back
    monkeypatch.setattr(ocr, "OCR_CACHE_DIR", Path("/nonexistent/original"))
    monkeypatch.setenv("OCR_ENGINE", "gemini")  # main() changes it; monkeypatch puts it back
    yield types.SimpleNamespace(url=url, ids=ids, handled=handled)
    connection.close_pool()
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


def rows_of(url, query):
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute(query).fetchall()


def names(documents):
    return [d["filename"] for d in documents]


@needs_scratch
def test_only_unfinished_documents_in_the_range_are_selected_oldest_first(archive):
    assert names(reocr_surya.select_documents(0, 9998)) == [
        "1955-April To 1956 -March.PDF", "1961-April To1962-March Part-1.pdf", "1966-Jul To 1967-Jun.PDF",
        "2003-Jan To 2003-Dec.PDF", "2004-Jan To 2004-Dec.PDF",
    ]
    assert names(reocr_surya.select_documents(2003, 2017)) == ["2003-Jan To 2003-Dec.PDF", "2004-Jan To 2004-Dec.PDF"]
    assert names(reocr_surya.select_documents(1955, 2002)) == [
        "1955-April To 1956 -March.PDF", "1961-April To1962-March Part-1.pdf", "1966-Jul To 1967-Jun.PDF",
    ]


@needs_scratch
def test_the_two_machines_ranges_do_not_overlap_and_together_cover_every_unfinished_document(archive):
    mac, other = names(reocr_surya.select_documents(2003, 2017)), names(reocr_surya.select_documents(1955, 2002))
    assert not set(mac) & set(other)
    assert sorted(mac + other) == sorted(names(reocr_surya.select_documents(0, 9998)))


@needs_scratch
def test_completed_and_text_saved_documents_are_never_selected_whatever_the_range(archive):
    everything = names(reocr_surya.select_documents(0, 9998))
    assert "1961-April To1962-March Part-2.PDF" not in everything and "2011-Jan.PDF" not in everything
    assert "1970-Jul To 1971-Jun_2.PDF" not in everything


@needs_scratch
def test_listing_changes_and_starts_nothing(archive, monkeypatch, capsys):
    monkeypatch.setattr(ocr, "_prepare_engine", lambda: pytest.fail("the engine must not start for a listing"))
    monkeypatch.setattr(drive_sync, "build_drive_service", lambda: pytest.fail("Drive must not be touched for a listing"))
    assert reocr_surya.main(["--list", "--start-year", "2003", "--end-year", "2017"]) == 0
    out = capsys.readouterr().out
    assert "2 unfinished PDFs" in out and "2003-Jan" in out and "1955" not in out
    assert archive.handled == []


@needs_scratch
def test_a_run_reads_only_its_range_and_leaves_live_documents_alone(archive):
    before = rows_of(archive.url, "SELECT id, status FROM documents ORDER BY id")
    assert reocr_surya.main(["--start-year", "2003", "--end-year", "2017"]) == 0
    assert archive.handled == ["2003-Jan To 2003-Dec.PDF", "2004-Jan To 2004-Dec.PDF"]
    after = dict(rows_of(archive.url, "SELECT id, status FROM documents"))
    for doc_id, status in before:
        if doc_id not in (archive.ids["2003-Jan To 2003-Dec.PDF"], archive.ids["2004-Jan To 2004-Dec.PDF"]):
            assert after[doc_id] == status  # nobody else's status changed
    assert rows_of(archive.url, "SELECT count(*) FROM chunks") == [(8,)]  # every document still has its old text: nothing was deleted


@needs_scratch
def test_a_run_uses_the_surya_engine_and_a_cache_of_its_own(archive):
    reocr_surya.main(["--start-year", "2003", "--end-year", "2003"])
    assert ocr.OCR_ENGINE == "surya" and os.environ["OCR_ENGINE"] == "surya"
    # a folder of its own below the usual one, never the folder the other engines wrote their text into
    assert Path(ocr.OCR_CACHE_DIR) == Path(config.OCR_CACHE_DIR) / "surya"


@needs_scratch
def test_a_pdf_another_machine_is_reading_is_skipped_and_taken_later(archive, capsys):
    busy = archive.ids["2003-Jan To 2003-Dec.PDF"]
    other_machine = psycopg.connect(archive.url, autocommit=True)
    assert other_machine.execute("SELECT pg_try_advisory_lock(%s, %s)", (reocr_surya.LOCK_CLASS, busy)).fetchone()[0]
    try:
        assert reocr_surya.main(["--start-year", "2003", "--end-year", "2017"]) == 0
        assert archive.handled == ["2004-Jan To 2004-Dec.PDF"]
        assert "another run is reading this PDF" in capsys.readouterr().out
    finally:
        other_machine.close()  # the lock goes with the connection
    assert reocr_surya.main(["--start-year", "2003", "--end-year", "2017"]) == 0
    assert archive.handled == ["2004-Jan To 2004-Dec.PDF", "2003-Jan To 2003-Dec.PDF"]


@needs_scratch
def test_a_second_run_over_an_overlapping_range_finds_nothing_left_to_read(archive):
    assert reocr_surya.main(["--start-year", "1955", "--end-year", "2010"]) == 0
    first = list(archive.handled)
    assert reocr_surya.main(["--start-year", "1990", "--end-year", "2017"]) == 0  # the overlap, and a second machine in effect
    assert len(first) == 5 and archive.handled == first  # each PDF was read once, by the first run


@needs_scratch
def test_a_pdf_finished_by_someone_else_since_the_list_was_made_is_skipped(archive, monkeypatch, capsys):
    finishing = archive.ids["2004-Jan To 2004-Dec.PDF"]
    real = reocr_surya.select_documents

    def list_then_finish(start, end):
        documents = real(start, end)
        with psycopg.connect(archive.url, autocommit=True) as conn:
            conn.execute("UPDATE documents SET status = 'text_saved' WHERE id = %s", (finishing,))
        return documents

    monkeypatch.setattr(reocr_surya, "select_documents", list_then_finish)
    reocr_surya.main(["--start-year", "2003", "--end-year", "2017"])
    assert archive.handled == ["2003-Jan To 2003-Dec.PDF"]
    assert "already done meanwhile" in capsys.readouterr().out


@needs_scratch
def test_a_pdf_that_left_drive_is_skipped_and_the_rest_carry_on(archive, monkeypatch, capsys):
    monkeypatch.setattr(drive_sync, "list_pdfs", lambda service: [{"file_id": "drive-2004-Jan To 2004-Dec.PDF", "name": "2004-Jan To 2004-Dec.PDF", "size": 1}])
    assert reocr_surya.main(["--start-year", "2003", "--end-year", "2017"]) == 0
    assert archive.handled == ["2004-Jan To 2004-Dec.PDF"]
    assert "not on Google Drive any more" in capsys.readouterr().out


@needs_scratch
def test_one_pdf_that_cannot_be_downloaded_does_not_stop_the_others(archive, monkeypatch, capsys):
    def stream(service, file_id, name, size):
        if "2003" in name:
            raise OSError("connection reset")
        return b"%PDF"

    monkeypatch.setattr(drive_sync, "stream_pdf", stream)
    assert reocr_surya.main(["--start-year", "2003", "--end-year", "2017"]) == 1  # a failure is reported in the exit code
    assert archive.handled == ["2004-Jan To 2004-Dec.PDF"]
    assert "download FAILED: connection reset" in capsys.readouterr().out


@needs_scratch
def test_ctrl_c_stops_cleanly_and_lets_go_of_the_lock(archive, monkeypatch, capsys):
    def interrupted(data, drive_file_id, filename, embed=True):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "ingest_one", interrupted)
    assert reocr_surya.main(["--start-year", "2003", "--end-year", "2017"]) == 130
    assert "run it again to carry on" in capsys.readouterr().out
    other = psycopg.connect(archive.url, autocommit=True)
    try:
        assert other.execute("SELECT pg_try_advisory_lock(%s, %s)", (reocr_surya.LOCK_CLASS, archive.ids["2003-Jan To 2003-Dec.PDF"])).fetchone()[0]
    finally:
        other.close()


@needs_scratch
def test_the_lock_is_one_document_wide_and_is_let_go_when_the_block_ends(archive):
    doc, other_doc = archive.ids["2003-Jan To 2003-Dec.PDF"], archive.ids["2004-Jan To 2004-Dec.PDF"]
    with reocr_surya.claim(doc) as first:
        assert first is True
        with reocr_surya.claim(doc) as second:
            assert second is False  # the same document is taken
        with reocr_surya.claim(other_doc) as third:
            assert third is True  # another one is free
    with reocr_surya.claim(doc) as again:
        assert again is True  # let go when the block ended


@needs_scratch
def test_the_lock_connection_is_kept_awake_while_a_long_pdf_is_read(archive, monkeypatch):
    monkeypatch.setattr(reocr_surya, "HEARTBEAT_SECONDS", 0.05)
    statements = []
    real = psycopg.Connection.execute

    def noting(self, query, *args, **kwargs):
        statements.append(str(query))
        return real(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", noting)
    with reocr_surya.claim(archive.ids["2003-Jan To 2003-Dec.PDF"]):
        time.sleep(0.4)
    assert statements.count("SELECT 1") >= 3


def test_the_range_must_make_sense(capsys):
    with pytest.raises(SystemExit):
        reocr_surya.main(["--start-year", "2010", "--end-year", "2003"])
