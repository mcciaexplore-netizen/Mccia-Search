"""The pilot loader and the full runner, on a scratch Postgres (POOL_TEST_DATABASE_URL) with stand-in models, a stand-in bucket and tiny real PDFs."""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import psycopg
import pymupdf
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from PIL import Image

from db import connection
from photos import db as photo_db
from photos import load_pilot, run
from photos.boxes import Box
from photos.store import StorageError, storage_path

URL = os.environ.get("POOL_TEST_DATABASE_URL", "")
_parsed = urlparse(URL)
LOCAL = bool(URL) and (_parsed.hostname in ("localhost", "127.0.0.1") or (_parsed.hostname is None and "host=/" in URL))
needs_scratch = pytest.mark.skipif(not LOCAL, reason="set POOL_TEST_DATABASE_URL to a scratch Postgres with pgvector on this machine")
TEST_DB = "photos_run_test"


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


def scalar(url, query, params=None):
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute(query, params).fetchone()[0]


def add_document(url, name, status="completed", pages=3) -> int:
    return scalar(url, "INSERT INTO documents (filename, drive_file_id, status, total_pages) VALUES (%s, %s, %s, %s) RETURNING id", (name, "drive-" + name, status, pages))


class FakeStore:
    def __init__(self, fail_for=()):
        self.objects, self.fail_for, self.attempts = {}, set(fail_for), 0

    def put(self, path, data, content_type="image/jpeg"):
        self.attempts += 1
        if path in self.fail_for:
            raise StorageError("uploading failed: HTTP 500")
        self.objects[path] = data


class FakeClip:
    def image_vectors(self, images):
        out = np.zeros((len(images), 512), dtype=np.float32)
        out[:, 0] = 1.0
        return out


# ---- the pilot loader ----------------------------------------------------------------------------------------------------------------


def make_pilot(folder: Path):
    (folder / "crops").mkdir(parents=True)
    entries = []
    for pdf, page, index, kind, faces, caption in (("1961-April_To1962-March_Part-1", 214, 0, "photo", 1, "किर्लोस्कर"), ("1961-April_To1962-March_Part-1", 214, 1, "illustration", 0, ""),
                                                   ("2011-Jan", 5, 0, "photo", 3, "Dr. Rao"), ("Unknown_Volume", 7, 0, "photo", 0, "")):
        name = f"crops/{pdf}_p{page:04d}_{index}_{kind}.jpg"
        Image.new("RGB", (60, 80), (90, 90, 90)).save(folder / name, format="JPEG")
        entries.append({"pdf": pdf, "page": page, "index": index, "kind": kind, "kind_score": 0.9, "faces": faces, "caption": caption, "whole_page": False,
                        "box": [0.1, 0.2, 0.5, 0.6], "size": [60, 80], "file": name, "seconds": 1.0})
    (folder / "results.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries), encoding="utf-8")


def test_a_pilot_name_matches_a_document_name_whatever_its_underscores_extension_and_case():
    assert load_pilot.normalise("1952_April") == load_pilot.normalise("1952 April.PDF") == "1952 april"
    assert load_pilot.normalise("1961-April_To1962-March_Part-1") == load_pilot.normalise("1961-April To1962-March Part-1.pdf")


@needs_scratch
def test_the_loader_only_plans_unless_told_to_write(db, tmp_path, capsys):
    make_pilot(tmp_path / "pilot")
    add_document(db, "1961-April To1962-March Part-1.pdf")
    store = FakeStore()
    with connection.get_connection() as conn:
        result = load_pilot.load(conn, store, tmp_path / "pilot", FakeClip(), write=False)
    assert result["written"] == 0 and result["photos"] == 2 and result["unmatched"] == ["2011-Jan", "Unknown_Volume"]
    assert store.objects == {} and scalar(db, "SELECT COUNT(*) FROM photo_index") == 0


@needs_scratch
def test_the_loader_uploads_then_writes_rows_with_vectors_and_skips_unknown_pdfs(db, tmp_path):
    make_pilot(tmp_path / "pilot")
    d1, d2 = add_document(db, "1961-April To1962-March Part-1.pdf"), add_document(db, "2011-Jan.PDF")
    store = FakeStore()
    with connection.get_connection() as conn:
        result = load_pilot.load(conn, store, tmp_path / "pilot", FakeClip(), write=True)
    assert result["written"] == 3 and result["unmatched"] == ["Unknown_Volume"]
    assert sorted(store.objects) == sorted([storage_path(d1, 214, 0), storage_path(d1, 214, 1), storage_path(d2, 5, 0)])
    rows = psycopg.connect(db, autocommit=True).execute("SELECT document_id, page_num, box_index, kind, contains_face, face_count, description, storage_path, "
                                                         "vector_dims(clip_embedding), bbox FROM photo_index ORDER BY document_id, page_num, box_index").fetchall()
    assert [r[:7] for r in rows] == [(d1, 214, 0, "photo", True, 1, "किर्लोस्कर"), (d1, 214, 1, "illustration", False, 0, None), (d2, 5, 0, "photo", True, 3, "Dr. Rao")]
    assert all(r[8] == 512 for r in rows) and rows[0][9] == pytest.approx([0.1, 0.2, 0.5, 0.6])
    assert scalar(db, "SELECT COUNT(*) FROM photo_scan") == 2  # the two pages that had photographs


@needs_scratch
def test_loading_twice_changes_nothing(db, tmp_path):
    make_pilot(tmp_path / "pilot")
    add_document(db, "1961-April To1962-March Part-1.pdf")
    add_document(db, "2011-Jan.PDF")
    for _ in range(2):
        with connection.get_connection() as conn:
            load_pilot.load(conn, FakeStore(), tmp_path / "pilot", FakeClip(), write=True)
    assert scalar(db, "SELECT COUNT(*) FROM photo_index") == 3 and scalar(db, "SELECT COUNT(*) FROM photo_scan") == 2


# ---- the full runner -------------------------------------------------------------------------------------------------------------------


class WhiteMeansText:
    """CLIP stand-in: an all-white crop is "text" (axis 3), anything else a "photo" (axis 0)."""

    def image_vectors(self, images):
        out = np.zeros((len(images), 512), dtype=np.float32)
        for i, image in enumerate(images):
            out[i, 3 if np.asarray(image.convert("L")).mean() > 250 else 0] = 1.0
        return out


class FakeModels:
    """Detects a photo only on pages that have a dark square drawn on them (page 2 below); everything else is a text page."""

    def __init__(self):
        self.pages_seen = 0
        self.detect = self._detect
        self.clip = WhiteMeansText()
        self.kind_vectors = {k: np.eye(512, dtype=np.float32)[i : i + 1] for i, k in enumerate(("photo", "advertisement", "illustration", "text"))}
        self.faces = type("F", (), {"count": staticmethod(lambda image: 1)})()
        self.ocr_png = lambda png: "A caption long enough"

    def _detect(self, image):
        self.pages_seen += 1
        w, h = image.size
        probe = image.getpixel((int(w * 0.3), int(h * 0.3)))
        return [Box(w * 0.2, h * 0.2, w * 0.6, h * 0.6)] if sum(probe) < 200 and probe != (255, 255, 255) else [Box(0, 0, w, h)]


def make_pdf(path: Path, pages=3, photo_on=(2,)) -> Path:
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        page = doc.new_page(width=504, height=648)
        if n in photo_on:
            page.draw_rect(pymupdf.Rect(100, 130, 300, 390), color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(path)
    return path


@needs_scratch
def test_a_document_is_processed_page_by_page_uploading_before_recording(db, tmp_path):
    doc_id = add_document(db, "1961-X.pdf", pages=3)
    pdf = make_pdf(tmp_path / "a.pdf")
    store, models, progress = FakeStore(), FakeModels(), run.Progress(3)
    run.process_document(connection.get_connection, store, models, {"id": doc_id, "filename": "1961-X.pdf"}, pdf, progress, say=lambda m: None)
    with connection.get_connection() as conn:
        assert photo_db.scanned_pages(conn, doc_id) == {1, 2, 3}  # every page is recorded, with or without photographs
    assert progress.pages == 3 and progress.failures == 0
    assert scalar(db, "SELECT COUNT(*) FROM photo_index WHERE page_num = 2") >= 1 and scalar(db, "SELECT COUNT(*) FROM photo_index WHERE page_num = 1") == 0
    assert storage_path(doc_id, 2, 0) in store.objects


@needs_scratch
def test_a_second_run_skips_the_recorded_pages(db, tmp_path):
    doc_id = add_document(db, "1961-X.pdf", pages=3)
    pdf = make_pdf(tmp_path / "a.pdf")
    models = FakeModels()
    run.process_document(connection.get_connection, FakeStore(), models, {"id": doc_id, "filename": "x"}, pdf, run.Progress(3), say=lambda m: None)
    first_run = models.pages_seen
    assert first_run == 4  # three pages, and the page with a photo is asked twice (find the photo, then ask again with it painted over)
    progress = run.Progress(3)
    run.process_document(connection.get_connection, FakeStore(), models, {"id": doc_id, "filename": "x"}, pdf, progress, say=lambda m: None)
    assert models.pages_seen == first_run and progress.pages == 0  # nothing was looked at again


@needs_scratch
def test_a_page_whose_upload_fails_is_not_recorded_and_the_others_are(db, tmp_path, monkeypatch):
    doc_id = add_document(db, "1961-X.pdf", pages=3)
    pdf = make_pdf(tmp_path / "a.pdf", photo_on=(1, 2, 3))
    monkeypatch.setattr("photos.upload.RETRY_PAUSE_SECONDS", 0.0)
    store = FakeStore(fail_for={storage_path(doc_id, 2, 0)})
    progress, said = run.Progress(3), []
    run.process_document(connection.get_connection, store, FakeModels(), {"id": doc_id, "filename": "x"}, pdf, progress, say=said.append)
    with connection.get_connection() as conn:
        assert photo_db.scanned_pages(conn, doc_id) == {1, 3}  # page 2 is left for the next run
    assert progress.failures == 1 and any("page 2: FAILED StorageError" in m for m in said)
    assert store.attempts >= 4 + 2  # page 2 was tried several times before giving up
    assert scalar(db, "SELECT COUNT(*) FROM photo_index WHERE page_num = 2") == 0  # no row points at a picture that is not there


@needs_scratch
def test_many_pages_failing_in_a_row_aborts_the_run(db, tmp_path, monkeypatch):
    doc_id = add_document(db, "1961-X.pdf", pages=6)
    pdf = make_pdf(tmp_path / "a.pdf", pages=6, photo_on=(1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(run, "MAX_CONSECUTIVE_FAILURES", 3)
    monkeypatch.setattr("photos.upload.RETRY_PAUSE_SECONDS", 0.0)
    store = FakeStore(fail_for={storage_path(doc_id, n, 0) for n in range(1, 7)})
    with pytest.raises(run.RunAborted):
        run.process_document(connection.get_connection, store, FakeModels(), {"id": doc_id, "filename": "x"}, pdf, run.Progress(6), say=lambda m: None)
    with connection.get_connection() as conn:
        assert photo_db.scanned_pages(conn, doc_id) == set()


@needs_scratch
def test_documents_are_chosen_oldest_first_with_their_progress(db):
    a = add_document(db, "1965-Jan.pdf", pages=10)
    b = add_document(db, "1945 July.PDF", pages=127)
    add_document(db, "1990 Failed.pdf", status="failed")
    with connection.get_connection() as conn:
        photo_db.save_page(conn, a, 1, [])
        conn.commit()
        docs = run.select_documents(conn, None, 0, 9998)
        assert [d["id"] for d in docs] == [b, a] and docs[1]["scanned"] == 1  # a failed document is not looked at
        assert [d["id"] for d in run.select_documents(conn, [a], 0, 9998)] == [a]
        assert [d["id"] for d in run.select_documents(conn, None, 1960, 1970)] == [a]


# ---- progress and the PDF file --------------------------------------------------------------------------------------------------------


def test_the_eta_comes_from_the_recent_speed():
    progress = run.Progress(1000)
    assert progress.eta() == "measuring"
    for _ in range(10):
        progress.page_done(4.0, 1)
    assert progress.seconds_per_page() == 4.0 and progress.eta() == "1h 6m"  # 990 pages at 4 s


def test_a_downloaded_pdf_is_kept_as_a_file_and_not_fetched_again(tmp_path, monkeypatch):
    from ingest import drive_sync

    fetched = []
    monkeypatch.setattr(drive_sync, "stream_pdf", lambda service, fid, name, size: fetched.append(fid) or b"%PDF-1.4 fake")
    pdf = {"file_id": "X1", "name": "a.pdf", "size": 13}
    first = run.fetch_pdf("svc", pdf, tmp_path)
    again = run.fetch_pdf("svc", pdf, tmp_path)
    assert first == again == tmp_path / "X1.pdf" and fetched == ["X1"] and not list(tmp_path.glob("*.part"))
