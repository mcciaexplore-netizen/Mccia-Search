"""The photo pipeline's logic, with stand-in models (no Florence-2, CLIP or Tesseract is loaded)."""

import io

import numpy as np
import pymupdf
import pytest
from PIL import Image

from photos import boxes as B
from photos.boxes import Box
from photos.caption import caption_strips, clean, has_words, read_caption
from photos.classify import KINDS, classify
from photos.detect import find_photos
from photos.pipeline import Models, process_page
from photos.render import MAX_SIDE, render_page

W, H = 100, 140


# ---- boxes -----------------------------------------------------------------------------------------------------------------------


def test_a_box_that_fills_the_page_is_page_sized_and_a_photo_is_not():
    assert B.is_page_sized(Box(0, 0, 96, 134), W, H)  # what Florence says for a text page
    assert B.is_page_sized(Box(-5, -5, 120, 150), W, H)  # beyond the page counts too
    assert not B.is_page_sized(Box(10, 10, 60, 70), W, H)


def test_specks_are_not_photos():
    assert B.is_speck(Box(10, 10, 12, 60), W, H) and B.is_speck(Box(10, 10, 60, 12), W, H)
    assert not B.is_speck(Box(10, 10, 40, 50), W, H)


def test_overlapping_answers_are_one_photo_and_the_first_wins():
    a, b, c = Box(10, 10, 50, 60), Box(11, 11, 50, 61), Box(60, 70, 95, 130)
    assert B.distinct([a, b, c]) == [a, c]
    assert B.distinct([b, c], known=[a]) == [c]


def test_an_answer_is_split_into_photos_and_a_page_sized_box():
    photos, whole = B.split_answer([Box(10, 10, 50, 60), Box(0, 0, 100, 140), Box(20, 20, 22, 90), Box(11, 11, 50, 61)], W, H)
    assert photos == [Box(10, 10, 50, 60)] and whole is True  # the speck and the duplicate are gone
    assert B.split_answer([], W, H) == ([], False)


def test_boxes_are_clipped_to_the_page_and_given_as_page_fractions():
    assert B.split_answer([Box(-10, 20, 50, 200)], W, H)[0] == [Box(0, 20, 50, 140)]
    assert Box(10, 14, 50, 70).fractions(W, H) == (0.1, 0.1, 0.5, 0.5)
    assert B.box_from([50, 70, 10, 14]) == Box(10, 14, 50, 70)  # corners in any order


# ---- CLIP kind ---------------------------------------------------------------------------------------------------------------------


def kind_vectors():
    return {kind: np.eye(4, dtype=np.float32)[i : i + 1] for i, kind in enumerate(KINDS)}  # each kind is its own axis


def test_a_picture_near_a_kinds_prompts_is_that_kind():
    vectors = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=np.float32)
    kinds = [k for k, _, _ in classify(vectors, kind_vectors())]
    assert kinds == ["photo", "advertisement", "text"]


def test_the_probability_says_how_sure_it_is():
    sure = classify(np.array([[1, 0, 0, 0]], dtype=np.float32), kind_vectors())[0]
    unsure = classify(np.array([[0.5, 0.5, 0, 0]], dtype=np.float32), kind_vectors())[0]
    assert sure[1] > 0.99 and unsure[1] == pytest.approx(0.5, abs=0.01) and sum(sure[2].values()) == pytest.approx(1.0)


def test_nothing_to_classify_gives_nothing():
    assert classify(np.zeros((0, 4), dtype=np.float32), kind_vectors()) == []


# ---- finding the photographs ---------------------------------------------------------------------------------------------------------


def blank(width=W, height=H):
    return Image.new("RGB", (width, height), (200, 200, 200))


def test_a_text_page_answers_with_a_page_sized_box_and_has_no_photo():
    assert find_photos(blank(), lambda im: [Box(0, 0, W, H)]) == ([], True)


def test_a_page_with_no_answer_at_all_has_nothing():
    assert find_photos(blank(), lambda im: []) == ([], False)


def test_one_photo_is_found_and_the_page_sized_answer_after_it_means_nothing_more():
    photo = Box(10, 10, 60, 70)
    calls = []

    def detect(image):
        calls.append(image.getpixel((30, 40)))
        return [photo] if image.getpixel((30, 40)) != (255, 255, 255) else [Box(0, 0, W, H)]

    assert find_photos(blank(), detect) == ([photo], False)
    assert len(calls) == 2  # the second question was asked with the first photo painted over


def test_two_photos_are_found_because_the_first_is_painted_over_before_asking_again():
    a, b = Box(5, 5, 45, 50), Box(55, 80, 95, 130)

    def detect(image):
        for box in (a, b):
            if image.getpixel((int((box.x0 + box.x1) / 2), int((box.y0 + box.y1) / 2))) != (255, 255, 255):
                return [box]
        return [Box(0, 0, W, H)]

    found, whole = find_photos(blank(), detect)
    assert found == [a, b] and whole is False


def test_the_page_is_not_changed_by_the_painting():
    image = blank()
    find_photos(image, lambda im: [Box(10, 10, 60, 70)] if im.getpixel((30, 40)) != (255, 255, 255) else [])
    assert image.getpixel((30, 40)) == (200, 200, 200)


def test_a_detector_that_never_stops_finding_new_boxes_is_stopped_at_the_limit():
    count = {"n": 0}

    def detect(image):
        count["n"] += 1
        x = (count["n"] * 7) % 50
        return [Box(x, x, x + 20, x + 25)]

    found, _ = find_photos(blank(), detect, max_photos=3)
    assert len(found) <= 3 and count["n"] == 3


def test_a_photo_and_a_page_sized_box_in_one_answer_is_a_photo_page_not_a_full_page_picture():
    photo = Box(10, 10, 60, 70)
    found, whole = find_photos(blank(), lambda im: [photo, Box(0, 0, W, H)] if im.getpixel((30, 40)) != (255, 255, 255) else [])
    assert found == [photo] and whole is False


# ---- captions -----------------------------------------------------------------------------------------------------------------------


def test_the_caption_is_looked_for_below_the_photo_first_then_above():
    below, above = caption_strips(Box(200, 300, 800, 700), 1000, 1400)
    assert below.y0 == pytest.approx(702) and below.y1 > below.y0 and above.y1 == pytest.approx(298)
    assert below.x0 < 200 and below.x1 > 800  # a little wider than the photo


def test_a_photo_at_the_foot_of_the_page_has_no_strip_below():
    assert len(caption_strips(Box(200, 600, 800, 1399), 1000, 1400)) == 1


def test_whitespace_and_stray_marks_are_cleaned_from_a_caption():
    assert clean("  किर्लोस्कर  उद्योगांचे \n संस्थापक |\n") == "किर्लोस्कर उद्योगांचे संस्थापक"
    assert has_words("Kirloskar") and not has_words("-- 12 ||")


def test_the_caption_is_read_from_the_first_strip_that_holds_words():
    page = Image.new("RGB", (400, 560), "white")
    texts = iter(["", "Dr. Kirloskar at the jubilee"])
    assert read_caption(page, Box(100, 200, 300, 400), lambda png: next(texts)) == "Dr. Kirloskar at the jubilee"
    assert read_caption(page, Box(100, 200, 300, 400), lambda png: "  |  ") == ""


def test_a_caption_is_given_to_tesseract_as_a_png_of_just_the_strip():
    page = Image.new("RGB", (400, 560), "white")
    seen = []
    read_caption(page, Box(100, 200, 300, 400), lambda png: seen.append(Image.open(io.BytesIO(png)).size) or "")
    assert seen and all(w > 150 and h < 100 for w, h in seen)


# ---- drawing a page -----------------------------------------------------------------------------------------------------------------


def test_a_normal_page_is_drawn_at_the_dpi_asked():
    doc = pymupdf.open()
    doc.new_page(width=504, height=648)
    assert render_page(doc[0], 100).size == pytest.approx((700, 900), abs=2)


def test_a_page_whose_box_is_absurd_is_drawn_smaller_not_refused():
    doc = pymupdf.open()
    page = doc.new_page(width=504, height=648)
    page.set_mediabox(pymupdf.Rect(0, 0, 151200, 194400))
    assert max(render_page(doc[0], 200).size) <= MAX_SIDE + 1


# ---- one page through the whole pipeline ----------------------------------------------------------------------------------------------


class FakeClip:
    """Gives each crop the vector it is told to: `plan` is a list of axis numbers, one per crop in the order they come."""

    def __init__(self, *axes):
        self.axes, self.calls = list(axes), []

    def image_vectors(self, images):
        self.calls.append(len(images))
        out = np.zeros((len(images), 4), dtype=np.float32)
        for i in range(len(images)):
            out[i, self.axes.pop(0) if self.axes else 0] = 1.0
        return out


class FakeFaces:
    def __init__(self, count=0):
        self.count_value, self.seen = count, []

    def count(self, image):
        self.seen.append(image.size)
        return self.count_value


def models(detect, clip, faces=None, ocr=lambda png: "Dr. Kirloskar"):
    return Models(detect=detect, clip=clip, kind_vectors=kind_vectors(), faces=faces or FakeFaces(), ocr_png=ocr)


def page_pair():
    """The page drawn for detection (500 x 700, grey so that painted-over white shows) and drawn sharper (1000 x 1400)."""
    return Image.new("RGB", (500, 700), (200, 200, 200)), Image.new("RGB", (1000, 1400), (120, 120, 120))


PAGE = Box(0, 0, 500, 700)


def one_photo(image, box=Box(50, 50, 250, 300)):
    return [box] if image.getpixel((100, 150)) != (255, 255, 255) else []


def test_a_photo_is_cut_from_the_sharp_page_at_the_scaled_box_with_its_kind_faces_and_caption():
    small, big = page_pair()
    clip, faces = FakeClip(0), FakeFaces(3)
    photos = process_page(small, big, models(one_photo, clip, faces))
    assert len(photos) == 1
    p = photos[0]
    assert (p.kind, p.faces, p.caption, p.whole_page, p.index) == ("photo", 3, "Dr. Kirloskar", False, 0)
    assert p.box == Box(100, 100, 500, 600) and (p.width, p.height) == (400, 500)  # the box scaled to the sharper drawing
    assert p.fractions == pytest.approx((0.1, 1 / 14, 0.5, 3 / 7), abs=1e-3)
    assert Image.open(io.BytesIO(p.jpeg)).size == (400, 500) and p.vector.shape == (4,)
    assert clip.calls == [1]  # one CLIP pass over all the page's crops


def test_a_text_page_gives_no_photo_and_costs_no_caption_reading():
    small, big = page_pair()
    ocr_calls = []
    result = process_page(small, big, models(lambda im: [PAGE], FakeClip(3), ocr=lambda png: ocr_calls.append(1) or "x"))
    assert result == [] and ocr_calls == []


def test_a_full_page_advertisement_is_kept_as_one_and_no_caption_is_looked_for():
    small, big = page_pair()
    ocr_calls = []
    [p] = process_page(small, big, models(lambda im: [PAGE], FakeClip(1), ocr=lambda png: ocr_calls.append(1) or "x"))
    assert (p.kind, p.whole_page, p.caption) == ("advertisement", True, "") and ocr_calls == []


def test_a_full_page_picture_such_as_a_cover_is_kept_as_a_photo():
    small, big = page_pair()
    [p] = process_page(small, big, models(lambda im: [PAGE], FakeClip(0)))
    assert p.kind == "photo" and p.whole_page and (p.width, p.height) == (1000, 1400)


def test_a_detected_box_that_clip_says_is_text_is_dropped():
    small, big = page_pair()
    assert process_page(small, big, models(one_photo, FakeClip(3))) == []


def test_a_doubtful_text_verdict_keeps_the_box_as_the_best_other_kind():
    small, big = page_pair()

    class Unsure(FakeClip):
        def image_vectors(self, images):  # text barely ahead of the photo axis: top kind, but a probability (about 0.55) below TEXT_DROP
            return np.array([[0.5, 0.0, 0.0, 0.502]], dtype=np.float32)

    [p] = process_page(small, big, models(one_photo, Unsure()))
    assert p.kind == "photo" and p.scores["text"] > p.scores["photo"]


def test_two_photos_on_a_page_are_numbered_and_each_has_its_own_caption():
    small, big = page_pair()
    a, b = Box(25, 25, 225, 250), Box(275, 400, 475, 650)

    def detect(image):
        for box in (a, b):
            if image.getpixel((int((box.x0 + box.x1) / 2), int((box.y0 + box.y1) / 2))) != (255, 255, 255):
                return [box]
        return [PAGE]

    captions = iter(["first caption", "second caption"])
    photos = process_page(small, big, models(detect, FakeClip(0, 1), ocr=lambda png: next(captions)))
    assert [(p.index, p.kind, p.caption) for p in photos] == [(0, "photo", "first caption"), (1, "advertisement", "second caption")]


def test_a_large_photo_is_stored_smaller_but_its_box_stays_exact():
    small = Image.new("RGB", (500, 700), (200, 200, 200))
    big = Image.new("RGB", (3000, 4200), (120, 120, 120))
    [p] = process_page(small, big, models(lambda im: [Box(0, 0, 450, 600)] if im.getpixel((250, 250)) != (255, 255, 255) else [], FakeClip(0)))
    assert max(Image.open(io.BytesIO(p.jpeg)).size) <= 1600 and (p.width, p.height) == (2700, 3600)


# ---- the pilot runner's helpers ------------------------------------------------------------------------------------------------------


def test_page_specs_for_the_pilot():
    from photos.pilot import parse_pages

    assert parse_pages("all", 5) == [1, 2, 3, 4, 5]
    assert parse_pages("every:2", 7) == [1, 3, 5, 7]
    assert parse_pages("3,9,27", 20) == [3, 9]  # a page the PDF does not have is left out
    assert parse_pages("2-4,10", 20) == [2, 3, 4, 10]
    assert parse_pages("every:3,5,every:3", 9) == [1, 4, 5, 7]  # mixed, each page once, in order
    with pytest.raises(ValueError):
        parse_pages("every:0", 5)


def test_an_argument_may_carry_its_pages_after_the_file_name():
    from photos.pilot import split_argument

    assert split_argument("/a b/2011-Jan.PDF:every:6,12") == ("/a b/2011-Jan.PDF", "every:6,12")
    assert split_argument("/a/issue.pdf:3-9") == ("/a/issue.pdf", "3-9")
    assert split_argument("/a/issue.pdf") == ("/a/issue.pdf", "all")


def test_the_contact_sheet_labels_each_photo(tmp_path):
    from photos.pilot import contact_sheets

    (tmp_path / "crops").mkdir()
    Image.new("RGB", (300, 200), (90, 90, 90)).save(tmp_path / "crops" / "a.jpg")
    entries = [{"pdf": "1961-April Part-1", "page": 214, "kind": "photo", "faces": 1, "file": "crops/a.jpg"}]
    [sheet] = contact_sheets(entries, tmp_path)
    assert sheet.exists() and Image.open(sheet).size[1] == 4 * 260


# ---- storage ------------------------------------------------------------------------------------------------------------------------

import httpx

from photos import store as store_module
from photos.store import LocalStore, StorageError, SupabaseStore, storage_path

SECRET = "sb_secret_this_must_never_appear_anywhere_1234567890"


def fake_supabase(handler):
    return SupabaseStore("https://abc.supabase.co/", SECRET, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_objects_are_named_by_document_and_page():
    assert storage_path(12, 45, 1) == "doc-0012/p0045-1.jpg"
    assert sorted([storage_path(1, 10, 0), storage_path(1, 9, 0)]) == [storage_path(1, 9, 0), storage_path(1, 10, 0)]  # pages sort in order


def test_a_photo_is_uploaded_with_the_service_key_as_a_replaceable_object():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), headers=dict(request.headers), body=request.content)
        return httpx.Response(200, json={"Key": "sampada-images/doc-0001/p0002-0.jpg"})

    fake_supabase(handler).put("doc-0001/p0002-0.jpg", b"JPEGDATA")
    assert seen["url"] == "https://abc.supabase.co/storage/v1/object/sampada-images/doc-0001/p0002-0.jpg"
    assert seen["headers"]["authorization"] == f"Bearer {SECRET}" and seen["headers"]["x-upsert"] == "true" and seen["headers"]["content-type"] == "image/jpeg"
    assert seen["body"] == b"JPEGDATA"


def test_a_refused_upload_is_an_error_that_does_not_leak_the_key():
    store = fake_supabase(lambda request: httpx.Response(403, text=f"denied for {SECRET}"))
    with pytest.raises(StorageError) as error:
        store.put("doc-0001/p0001-0.jpg", b"x")
    assert "403" in str(error.value) and SECRET not in str(error.value) and "***" in str(error.value)


def test_the_key_is_not_in_the_stores_printed_form():
    assert SECRET not in repr(fake_supabase(lambda r: httpx.Response(200)))


def test_signed_links_are_made_in_one_request_and_a_missing_object_is_left_out():
    seen = {}

    def handler(request):
        import json

        seen.update(url=str(request.url), body=json.loads(request.content))
        return httpx.Response(200, json=[{"path": "doc-0001/p0001-0.jpg", "signedURL": "/object/sign/sampada-images/doc-0001/p0001-0.jpg?token=T"}, {"path": "doc-0001/p0002-0.jpg", "signedURL": None, "error": "not found"}])

    urls = fake_supabase(handler).signed_urls(["doc-0001/p0001-0.jpg", "doc-0001/p0002-0.jpg"], seconds=600)
    assert seen["body"] == {"expiresIn": 600, "paths": ["doc-0001/p0001-0.jpg", "doc-0001/p0002-0.jpg"]}
    assert urls == {"doc-0001/p0001-0.jpg": "https://abc.supabase.co/storage/v1/object/sign/sampada-images/doc-0001/p0001-0.jpg?token=T"}
    assert fake_supabase(lambda r: pytest.fail("no request for no paths")).signed_urls([]) == {}


def test_the_store_needs_both_the_address_and_the_key():
    with pytest.raises(ValueError):
        SupabaseStore("https://abc.supabase.co", "")


def test_the_store_comes_from_the_environment_or_the_local_file_and_is_none_without_them(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.setattr(store_module, "KEY_FILE", tmp_path / "missing")
    assert store_module.from_env() is None
    (tmp_path / "keys").write_text(f"SUPABASE_URL=https://xyz.supabase.co\nSUPABASE_SERVICE_KEY={SECRET}\n")
    monkeypatch.setattr(store_module, "KEY_FILE", tmp_path / "keys")
    assert store_module.from_env().base == "https://xyz.supabase.co/storage/v1"
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
    assert store_module.from_env().base == "https://env.supabase.co/storage/v1"  # the environment wins


def test_a_local_store_keeps_files_in_a_folder(tmp_path):
    local = LocalStore(tmp_path)
    local.put("doc-0001/p0001-0.jpg", b"abc")
    assert local.get("doc-0001/p0001-0.jpg") == b"abc"


DB_URL = "postgresql://postgres.abcdefghijklmnopqrst:not-a-real-password@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"


def test_the_project_address_is_worked_out_from_the_pooler_database_address():
    assert store_module.project_url_from(DB_URL) == "https://abcdefghijklmnopqrst.supabase.co"
    assert store_module.project_url_from("postgresql://postgres:pw@db.example.com/postgres") is None  # an ordinary user: no project
    assert store_module.project_url_from("postgresql://postgres.short:pw@h/db") is None
    assert store_module.project_url_from(None) is None


def test_the_store_is_built_from_the_service_role_key_and_the_database_address_in_the_local_file(monkeypatch, tmp_path):
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "keys").write_text(f"SUPABASE_DATABASE_URL={DB_URL}\nSUPABASE_SERVICE_ROLE_KEY={SECRET}\n")
    monkeypatch.setattr(store_module, "KEY_FILE", tmp_path / "keys")
    built = store_module.from_env()
    assert built.base == "https://abcdefghijklmnopqrst.supabase.co/storage/v1" and SECRET not in repr(built)


def test_without_the_key_there_is_no_store_even_if_the_address_is_known(monkeypatch, tmp_path):
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "keys").write_text(f"SUPABASE_DATABASE_URL={DB_URL}\n")
    monkeypatch.setattr(store_module, "KEY_FILE", tmp_path / "keys")
    assert store_module.from_env() is None


def test_a_pdf_argument_that_is_a_file_is_used_as_it_is(tmp_path):
    from photos.pilot import resolve_pdf

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")
    assert resolve_pdf(str(pdf), None) == pdf


def test_a_pdf_that_is_not_a_file_is_fetched_from_drive_once_and_kept(tmp_path, monkeypatch):
    from ingest import drive_sync
    from photos.pilot import resolve_pdf

    fetched = []
    monkeypatch.setattr(drive_sync, "build_drive_service", lambda: "service")
    monkeypatch.setattr(drive_sync, "list_pdfs", lambda service: [{"file_id": "X1", "name": "1961-April To1962-March Part-1.pdf", "size": 4}])
    monkeypatch.setattr(drive_sync, "stream_pdf", lambda service, file_id, name, size: fetched.append(file_id) or b"%PDF")
    first = resolve_pdf("1961-April To1962-March Part-1.pdf", tmp_path / "cache")
    again = resolve_pdf("1961-April To1962-March Part-1.pdf", tmp_path / "cache")
    assert first == again == tmp_path / "cache" / "1961-April_To1962-March_Part-1.pdf" and first.read_bytes() == b"%PDF" and fetched == ["X1"]  # downloaded once
    assert not list((tmp_path / "cache").glob("*.part"))


def test_an_unknown_pdf_is_a_clear_error(tmp_path, monkeypatch):
    from ingest import drive_sync
    from photos.pilot import resolve_pdf

    monkeypatch.setattr(drive_sync, "build_drive_service", lambda: "service")
    monkeypatch.setattr(drive_sync, "list_pdfs", lambda service: [])
    with pytest.raises(FileNotFoundError, match="not on Google Drive"):
        resolve_pdf("nothing.pdf", tmp_path)
    with pytest.raises(FileNotFoundError, match="--cache"):
        resolve_pdf("nothing.pdf", None)
