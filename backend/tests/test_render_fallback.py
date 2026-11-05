"""MuPDF refuses some scanned pages ("code=5: Overly large image"); the fallback draws them with PDFium instead. No database is touched here.

The real PDFs that fail are hundreds of MB, so MuPDF's refusal is simulated for chosen pages of a small PDF; PDFium is the real one.
"""

import io
import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from ingest import ocr

REFUSAL = "code=5: Overly large image"


def make_pdf(pages: int = 3) -> bytes:
    doc = pymupdf.open()
    for number in range(pages):
        doc.new_page(width=595, height=842).insert_text((60, 120), f"page {number + 1}", fontsize=28)
    return doc.tobytes()


@pytest.fixture
def mupdf_refuses(monkeypatch):
    """Make MuPDF refuse the given 0-based page numbers, as it does for the failing scans; returns the set to fill in."""
    refused: set[int] = set()
    real = pymupdf.Page.get_pixmap

    def get_pixmap(self, *args, **kwargs):
        if self.number in refused:
            raise RuntimeError(REFUSAL)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", get_pixmap)
    return refused


def size_of(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as image:
        return image.size


def test_pages_mupdf_draws_never_touch_the_fallback(monkeypatch):
    monkeypatch.setattr(ocr, "_open_pdfium", lambda data: pytest.fail("PDFium must not be opened when MuPDF can draw every page"))
    images = ocr.pdf_to_images(make_pdf())
    assert len(images) == 3 and all(images)


def test_a_page_mupdf_refuses_is_drawn_by_pdfium_at_the_same_size(mupdf_refuses, capsys):
    mupdf_refuses.add(1)
    images = ocr.pdf_to_images(make_pdf())
    assert all(images)  # no empty placeholder: the page can be read
    assert size_of(images[1]) == pytest.approx(size_of(images[0]), abs=2)  # same 200 DPI page size as the one MuPDF drew
    out = capsys.readouterr().out
    assert "page 2" in out and REFUSAL in out and "drawn with PDFium instead" in out
    assert "page 1 " not in out and "page 3 " not in out  # only the refused page is mentioned


def test_the_fallback_page_has_real_content_not_a_blank_sheet(mupdf_refuses):
    mupdf_refuses.add(0)
    page = Image.open(io.BytesIO(ocr.pdf_to_images(make_pdf(1))[0])).convert("L")
    assert page.getextrema()[0] < 100  # some dark pixels: the text was drawn


def test_pdfium_is_opened_once_for_several_refused_pages_and_closed(mupdf_refuses, monkeypatch):
    opened, closed = [], []
    real_open = ocr._open_pdfium

    class Tracked:
        def __init__(self, document):
            self.document = document

        def __getitem__(self, index):
            return self.document[index]

        def close(self):
            closed.append(1)
            self.document.close()

    monkeypatch.setattr(ocr, "_open_pdfium", lambda data: opened.append(1) or Tracked(real_open(data)))
    mupdf_refuses.update({0, 2})
    images = ocr.pdf_to_images(make_pdf())
    assert all(images) and opened == [1] and closed == [1]


def test_a_pdf_given_as_a_file_path_works_too(mupdf_refuses, tmp_path):
    path = tmp_path / "issue.pdf"
    path.write_bytes(make_pdf())
    mupdf_refuses.add(2)
    images = ocr.pdf_to_images(Path(path))
    assert all(images)


def test_when_both_renderers_fail_the_page_is_an_empty_placeholder_and_the_others_are_kept(mupdf_refuses, monkeypatch, capsys):
    monkeypatch.setattr(ocr, "_render_with_pdfium", lambda document, index: (_ for _ in ()).throw(ValueError("pdfium cannot either")))
    mupdf_refuses.add(1)
    images = ocr.pdf_to_images(make_pdf())
    assert images[0] and images[2] and images[1] == b""  # page numbers stay aligned
    out = capsys.readouterr().out
    assert REFUSAL in out and "PDFium could not either: pdfium cannot either" in out
    with pytest.raises(ocr.OCRPageError, match="no image data"):
        ocr.ocr_page(images[1], 2)  # which makes it a failed page, as before


def test_without_pdfium_installed_the_page_fails_as_it_did_before(mupdf_refuses, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pypdfium2", None)  # makes "import pypdfium2" fail
    mupdf_refuses.add(0)
    images = ocr.pdf_to_images(make_pdf(2))
    assert images[0] == b"" and images[1]
    assert "PDFium could not either" in capsys.readouterr().out


def test_a_whole_pdf_with_a_refused_page_is_read_completely(mupdf_refuses, monkeypatch, tmp_path):
    """The point of it all: a PDF that used to fail on one page now comes out whole, with nothing marked failed and every page cached."""
    monkeypatch.setattr(ocr, "OCR_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ocr, "_prepare_engine", lambda: None)
    monkeypatch.setattr(ocr, "ocr_page", lambda image, number: f"text of page {number}" if image else (_ for _ in ()).throw(ocr.OCRPageError(f"page {number}: no image data")))
    mupdf_refuses.add(1)
    pages, failed = ocr.get_or_ocr(make_pdf(), "refused_page_pdf")
    assert failed == set()
    assert [p["text"] for p in pages] == ["text of page 1", "text of page 2", "text of page 3"]
    assert len(list((tmp_path / "cache" / "refused_page_pdf").glob("page_*.json"))) == 3


def test_the_same_pdf_without_the_fallback_would_have_failed_that_page(mupdf_refuses, monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "OCR_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ocr, "_prepare_engine", lambda: None)
    monkeypatch.setattr(ocr, "_open_pdfium", lambda data: (_ for _ in ()).throw(ImportError("not installed")))
    monkeypatch.setattr(ocr, "ocr_page", lambda image, number: f"text {number}" if image else (_ for _ in ()).throw(ocr.OCRPageError("no image data")))
    mupdf_refuses.add(1)
    _, failed = ocr.get_or_ocr(make_pdf(), "no_fallback_pdf")
    assert failed == {2}


# ---- a page whose box is absurdly large (page 46 of the 1961-April Part-1 volume: 151,200 x 194,400 points) ----------------------------


def make_pdf_with_absurd_box() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 120), "an ordinary page whose box is wrong", fontsize=28)
    page.set_mediabox(pymupdf.Rect(0, 0, 151200, 194400))  # what the real PDF says; the drawing is still only 595 x 842 in its corner
    return doc.tobytes()


def test_a_normal_page_is_still_drawn_at_200_dpi():
    assert ocr._render_dpi(504, 648) == ocr.RENDER_DPI
    assert ocr._render_dpi(540, 792) == ocr.RENDER_DPI
    assert ocr._render_dpi(0, 0) == ocr.RENDER_DPI  # nothing to go by: the usual resolution
    assert size_of(ocr.pdf_to_images(make_pdf(1))[0]) == pytest.approx((1654, 2339), abs=3)  # A4 at 200 DPI, as before


def test_an_absurd_page_box_is_drawn_smaller_so_that_its_longest_side_fits_the_limit():
    dpi = ocr._render_dpi(151200, 194400)
    assert dpi < 2 and 194400 / 72 * dpi == pytest.approx(ocr.MAX_RENDER_SIDE)


def test_a_page_with_an_absurd_box_is_drawn_not_refused_and_never_needs_the_fallback(monkeypatch, capsys):
    monkeypatch.setattr(ocr, "_open_pdfium", lambda data: pytest.fail("the fallback must not be needed: MuPDF can draw the page at a sane size"))
    images = ocr.pdf_to_images(make_pdf_with_absurd_box())
    assert images[0] != b""
    width, height = size_of(images[0])
    assert max(width, height) <= ocr.MAX_RENDER_SIDE and min(width, height) > 1000
    assert "says it is 151200 x 194400 points" in capsys.readouterr().out


def test_pdfium_is_held_to_the_same_limit(mupdf_refuses):
    """The fallback must not repeat the mistake: PDFium given the absurd box at 200 DPI tried to allocate gigabytes and the process was killed."""
    mupdf_refuses.add(0)
    images = ocr.pdf_to_images(make_pdf_with_absurd_box())
    assert images[0] != b""
    assert max(size_of(images[0])) <= ocr.MAX_RENDER_SIDE + 1  # PDFium rounds the size up by a pixel


def test_only_the_absurd_page_of_a_volume_is_drawn_smaller(capsys):
    doc = pymupdf.open(stream=make_pdf(2), filetype="pdf")
    doc.insert_pdf(pymupdf.open(stream=make_pdf_with_absurd_box(), filetype="pdf"))
    images = ocr.pdf_to_images(doc.tobytes())
    sizes = [size_of(i) for i in images]
    assert sizes[0] == pytest.approx(sizes[1], abs=2) and max(sizes[2]) <= ocr.MAX_RENDER_SIDE
    out = capsys.readouterr().out
    assert out.count("says it is") == 1 and "page 3 of" in out


def test_the_view_page_link_draws_a_page_with_an_absurd_box_too():
    import io as _io
    import pages

    jpeg = pages._draw(make_pdf_with_absurd_box(), 1, 1, "1961-April Part-1.pdf")
    width, height = size_of(jpeg)
    assert max(width, height) <= pages.MAX_PAGE_SIDE + 1 and min(width, height) > 1000


def test_the_view_page_link_still_draws_a_normal_page_at_150_dpi():
    import pages

    assert size_of(pages._draw(make_pdf(1), 1, 1, "x.pdf")) == pytest.approx((1240, 1754), abs=3)  # A4 at 150 DPI, as before
