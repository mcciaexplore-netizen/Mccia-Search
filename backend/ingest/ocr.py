"""OCR of PDF documents using Gemini."""

import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

import httpx
import pymupdf
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_OCR_MODEL, OCR_CACHE_DIR

# Set GEMINI_OCR_MODEL in .env to switch models (gemini-2.0-flash was shut down
# on 2026-06-01).
OCR_MODEL = GEMINI_OCR_MODEL

SYSTEM_PROMPT = (
    "You are an OCR engine. Extract ALL text from this scanned document page "
    "exactly as it appears. "
    "Preserve paragraph structure with blank lines between paragraphs. "
    "Handle mixed Marathi (Devanagari script), Hindi, and English text "
    "correctly — output each in its own script, do not transliterate. "
    "Output ONLY the extracted text. No commentary, no explanations."
)

RENDER_DPI = 200
RETRY_DELAYS = (2, 4, 8)  # seconds before retry 1, 2 and 3
REQUEST_TIMEOUT_MS = 120_000
OCR_CONCURRENCY = max(1, int(os.environ.get("OCR_CONCURRENCY") or "4"))  # pages sent to Gemini at once
PROGRESS_EVERY = 10  # print progress every this many pages

DEVANAGARI_START, DEVANAGARI_END = "ऀ", "ॿ"
LANGUAGE_SAMPLE_CHARS = 500
DEVANAGARI_DOMINANT_SHARE = 0.85

_DRIVE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_API_ERRORS = (genai_errors.APIError, httpx.HTTPError, OSError)


class OCRPageError(RuntimeError):
    """Gemini could not produce text for a single page."""


def pdf_to_images(pdf_data: bytes | Path) -> list[bytes]:
    """Render every page of a PDF (raw bytes or a file path) to PNG bytes at RENDER_DPI."""
    from_path = isinstance(pdf_data, Path)
    name = pdf_data.name if from_path else "<in-memory PDF>"
    if from_path and not pdf_data.is_file():
        raise FileNotFoundError(f"PDF not found: {name} ({pdf_data})")

    doc = None
    try:
        if from_path:
            doc = pymupdf.open(pdf_data)
        else:
            doc = pymupdf.open(stream=pdf_data, filetype="pdf")
        total_pages = doc.page_count
    except Exception as exc:
        if doc is not None:
            doc.close()
        raise RuntimeError(
            f"Cannot open PDF {name} (corrupted or not a PDF): {exc}"
        ) from exc

    with doc:
        if not doc.is_pdf:
            raise RuntimeError(f"{name} is not a valid PDF file")
        if doc.needs_pass:
            raise RuntimeError(f"{name} is password-protected and cannot be read")
        if total_pages == 0:
            raise RuntimeError(f"{name} has no pages")

        print(f"[ocr] rendering {total_pages} pages from {name}")
        images: list[bytes] = []
        for index in range(total_pages):
            try:
                images.append(doc[index].get_pixmap(dpi=RENDER_DPI).tobytes("png"))
            except Exception as exc:
                # An empty placeholder keeps page numbers aligned; ocr_pdf reports it.
                print(f"[ocr] could not render page {index + 1} of {name}: {exc}")
                images.append(b"")
    return images


def ocr_page(image_bytes: bytes, page_num: int) -> str:
    """Extract the text of one page image with Gemini; "" if the page is blank.

    Transient errors (network, 408/429/5xx) are retried after 2s, 4s and 8s.
    Raises OCRPageError if the page still fails or the error is not retryable.
    """
    if not image_bytes:
        raise OCRPageError(f"page {page_num}: no image data")

    client = _get_client()
    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
        "Extract the text from this page.",
    ]
    config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)

    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            response = client.models.generate_content(
                model=OCR_MODEL, contents=contents, config=config
            )
            break
        except _API_ERRORS as exc:
            if delay is None or not _is_retryable(exc):
                raise OCRPageError(
                    f"page {page_num}: Gemini request failed: {_brief(exc)}"
                ) from exc
            print(
                f"[ocr] page {page_num}: Gemini error ({_brief(exc)}); "
                f"retry {attempt}/{len(RETRY_DELAYS)} in {delay}s"
            )
            time.sleep(delay)

    text = response.text
    if text is None:
        reason = _empty_response_reason(response)
        if reason:
            raise OCRPageError(f"page {page_num}: Gemini returned no text ({reason})")
        return ""
    if response.candidates[0].finish_reason == types.FinishReason.MAX_TOKENS:
        print(f"[ocr] WARNING: page {page_num} output was cut off (token limit)")
    return text.strip()


def ocr_pdf(pdf_data: bytes | Path, drive_file_id: str | None = None) -> list[dict]:
    """OCR every page of a PDF, OCR_CONCURRENCY pages at a time.

    Returns [{page_number, text}, ...] with 1-based page numbers. When
    `drive_file_id` is given, each page is cached on disk the moment it succeeds and
    pages that are already cached are not sent to Gemini again, so an interrupted
    run resumes where it stopped. A page that fails gets empty text and is not
    cached; the rest still run.
    """
    name = _source_name(pdf_data, drive_file_id)
    images = pdf_to_images(pdf_data)
    total = len(images)

    done = _start_cache(drive_file_id, total) if drive_file_id is not None else {}
    missing = [n for n in range(1, total + 1) if n not in done]
    if done and missing:
        print(
            f"[ocr] cache hit: {len(done)}/{total} pages already done, "
            f"resuming from page {missing[0]}"
        )
    elif done:
        print(f"[ocr] cache hit: all {total} pages already done — {name}")

    results: dict[int, str] = dict(done)
    failed: list[int] = []
    last_error: Exception | None = None
    completed = len(done)
    lock = threading.Lock()  # guards the shared state above and keeps output unscrambled
    stop = threading.Event()  # set when the run is being aborted

    def run_page(page_number: int) -> None:
        nonlocal completed, last_error
        if stop.is_set():
            return
        # Drop the page image once it is taken: memory is freed as the run goes on.
        image, images[page_number - 1] = images[page_number - 1], b""
        error: OCRPageError | None = None
        try:
            text = ocr_page(image, page_number)
        except OCRPageError as exc:
            text, error = "", exc
        except BaseException:
            stop.set()  # an unexpected error: don't start any more pages
            raise
        else:
            if drive_file_id is not None:
                try:
                    _save_page(drive_file_id, page_number, text)
                except OSError as exc:  # caching is an optimisation: keep the text
                    with lock:
                        print(f"[ocr] WARNING: could not cache page {page_number}: {exc}")
        with lock:
            results[page_number] = text
            if error is not None:
                failed.append(page_number)
                last_error = error
                print(f"[ocr] page {page_number}/{total} failed, storing empty text — {error}")
            completed += 1
            if completed % PROGRESS_EVERY == 0 or completed == total:
                print(
                    f"[ocr] {completed}/{total} — {name} "
                    f"({len(done)} from cache, {completed - len(done)} new)"
                )

    if missing:
        _get_client()  # build the shared client once, before the workers start
        with ThreadPoolExecutor(max_workers=min(OCR_CONCURRENCY, len(missing))) as pool:
            futures = [pool.submit(run_page, n) for n in missing]
            try:
                for future in as_completed(futures):
                    future.result()  # re-raises anything other than a failed page
            except BaseException:
                stop.set()
                for future in futures:
                    future.cancel()  # don't start the pages still waiting
                raise

    failed.sort()
    if len(failed) == total:
        raise RuntimeError(
            f"OCR failed for every page of {name}; last error: {last_error}"
        ) from last_error
    if failed:
        print(
            f"[ocr] WARNING: {len(failed)} of {total} pages failed in {name} "
            f"(pages {', '.join(map(str, failed))}); they were not cached, so a "
            "re-run retries only those"
        )
    return [{"page_number": n, "text": results[n]} for n in range(1, total + 1)]


def get_or_ocr(pdf_data: bytes | Path, drive_file_id: str) -> list[dict]:
    """Return the OCR result for a PDF, using the cache when it is complete.

    A partial cache (an interrupted run) is not a result: ocr_pdf picks it up and
    OCRs only the missing pages.
    """
    cached = _load_cache(drive_file_id)
    total = _load_total(drive_file_id)
    if cached is not None and total is not None and _is_complete(cached, total):
        print(f"[ocr] cache hit for {_source_name(pdf_data, drive_file_id)}, skipping OCR")
        return cached
    return ocr_pdf(pdf_data, drive_file_id)


def detect_language(text: str) -> str:
    """Guess the language of a text from its first 500 characters.

    Returns "mr" when the sample is almost all Devanagari (Hindi is also labelled
    "mr"), "en" when it has no Devanagari, and "mixed" when it has both Devanagari
    and a meaningful share of Latin letters.
    """
    sample = text[:LANGUAGE_SAMPLE_CHARS]
    devanagari = sum(DEVANAGARI_START <= ch <= DEVANAGARI_END for ch in sample)
    latin = sum(ch.isascii() and ch.isalpha() for ch in sample)
    if devanagari == 0:
        return "en"
    if devanagari / (devanagari + latin) >= DEVANAGARI_DOMINANT_SHARE:
        return "mr"
    return "mixed"


@lru_cache(maxsize=1)
def _get_client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to .env "
            "(get one from https://aistudio.google.com)."
        )
    return genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )


def _is_retryable(exc: Exception) -> bool:
    """Retry network failures, rate limits and 5xx; fail fast on other API errors."""
    if isinstance(exc, genai_errors.APIError):
        return exc.code in (408, 429) or exc.code >= 500
    return True


def _brief(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:200]


def _source_name(pdf_data: bytes | Path, drive_file_id: str | None) -> str:
    """A name for log lines: the file name for a path, the Drive ID for raw bytes."""
    if isinstance(pdf_data, Path):
        return pdf_data.name
    return drive_file_id or "<in-memory PDF>"


def _empty_response_reason(response: types.GenerateContentResponse) -> str | None:
    """Why a response has no text, or None if it is just a blank page."""
    feedback = response.prompt_feedback
    if feedback and feedback.block_reason:
        return f"prompt blocked: {feedback.block_reason}"
    if not response.candidates:
        return "no candidates returned"
    reason = response.candidates[0].finish_reason
    if reason not in (None, types.FinishReason.STOP):
        return f"finish reason {reason}"
    return None


def _cache_path(drive_file_id: str) -> Path:
    """The directory holding one PDF's cache: a file per page, plus meta.json."""
    if not _DRIVE_ID_PATTERN.fullmatch(drive_file_id):
        raise ValueError(f"Invalid Drive file ID: {drive_file_id!r}")
    return Path(OCR_CACHE_DIR) / drive_file_id


def _page_name(page_number: int) -> str:
    return f"page_{page_number:04d}.json"


def _page_cache_path(drive_file_id: str, page_number: int) -> Path:
    """The cache file for one page of a PDF."""
    return _cache_path(drive_file_id) / _page_name(page_number)


def _read_page(path: Path) -> dict | None:
    """One cached page, or None (with a note) if its file is unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[ocr] ignoring unreadable cache {path.name} ({exc})")
        return None
    valid = (
        isinstance(data, dict)
        and isinstance(data.get("page_number"), int)
        and data["page_number"] >= 1
        and isinstance(data.get("text"), str)
        and path.name == _page_name(data["page_number"])
    )
    if not valid:
        print(f"[ocr] ignoring malformed cache {path.name}")
        return None
    return {"page_number": data["page_number"], "text": data["text"]}


def _read_pages(drive_file_id: str) -> list[dict]:
    """Every readable page file in a PDF's cache folder, sorted by page number."""
    cache_dir = _cache_path(drive_file_id)
    if not cache_dir.is_dir():
        return []
    pages = [
        page
        for path in cache_dir.glob("page_*.json")
        if (page := _read_page(path)) is not None
    ]
    pages.sort(key=lambda page: page["page_number"])
    return pages


def _load_cache(drive_file_id: str) -> list[dict] | None:
    """The pages cached so far, sorted by page number, or None if there is no cache.

    A cache needs its meta.json (the PDF's page count); without one nothing is known
    about it. Fewer pages than that count means a partial cache, whatever an earlier,
    possibly interrupted, run managed to save: ocr_pdf fills in the gaps.
    """
    total = _load_total(drive_file_id)
    if total is None:
        return None
    pages = [page for page in _read_pages(drive_file_id) if page["page_number"] <= total]
    if not pages:
        return None
    print(f"[ocr] cache: {len(pages)}/{total} pages found for {drive_file_id}")
    return pages


def _load_total(drive_file_id: str) -> int | None:
    """The page count of the PDF the cache was made for, or None if unknown."""
    try:
        data = json.loads((_cache_path(drive_file_id) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    total = data.get("total_pages") if isinstance(data, dict) else None
    return total if isinstance(total, int) and total >= 1 else None


def _is_complete(pages: list[dict], total: int) -> bool:
    return [page["page_number"] for page in pages] == list(range(1, total + 1))


def _start_cache(drive_file_id: str, total: int) -> dict[int, str]:
    """Prepare the cache for a PDF with `total` pages; return its usable pages.

    A cache made for a PDF with a different page count belongs to another version of
    the file, so it is discarded.
    """
    saved_total = _load_total(drive_file_id)
    if saved_total is not None and saved_total != total:
        print(
            f"[ocr] cache was made for a {saved_total}-page PDF but this one has "
            f"{total}; discarding it"
        )
        shutil.rmtree(_cache_path(drive_file_id), ignore_errors=True)
    try:
        _save_meta(drive_file_id, total)
    except OSError as exc:
        print(f"[ocr] WARNING: could not write the cache: {exc}")
    return {
        page["page_number"]: page["text"]
        for page in _read_pages(drive_file_id)
        if page["page_number"] <= total
    }


def _save_page(drive_file_id: str, page_number: int, text: str) -> None:
    """Write one page's OCR text to its cache file."""
    _write_json(
        _page_cache_path(drive_file_id, page_number),
        {"page_number": page_number, "text": text},
    )


def _save_meta(drive_file_id: str, total_pages: int) -> None:
    """Write the PDF's page count to its meta.json."""
    _write_json(_cache_path(drive_file_id) / "meta.json", {"total_pages": total_pages})


def _write_json(path: Path, data: dict) -> None:
    """Write `data` as JSON atomically, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def migrate_old_cache() -> None:
    """One-time conversion of old single-file caches to the per-page layout.

    An old cache is OCR_CACHE_DIR/<drive_file_id>.json holding every page. It becomes
    a folder <drive_file_id>/ with one page_NNNN.json per page plus meta.json, and
    the old file is deleted once the new files read back identical. A file that is
    not a complete list of pages is left alone. Run by hand: python -m ingest.ocr
    """
    root = Path(OCR_CACHE_DIR)
    if not root.is_dir():
        return
    for old in sorted(root.glob("*.json")):  # files directly in the folder only
        if not old.is_file():
            continue
        drive_file_id = old.stem
        try:
            data = json.loads(old.read_text(encoding="utf-8"))
            complete = (
                isinstance(data, list)
                and len(data) > 0
                and all(
                    isinstance(page, dict)
                    and isinstance(page.get("page_number"), int)
                    and isinstance(page.get("text"), str)
                    for page in data
                )
                and [page["page_number"] for page in data] == list(range(1, len(data) + 1))
            )
            if not complete:
                raise ValueError("not a complete list of pages")
            pages = [{"page_number": p["page_number"], "text": p["text"]} for p in data]
            _save_meta(drive_file_id, len(pages))
            for page in pages:
                _save_page(drive_file_id, page["page_number"], page["text"])
            if _read_pages(drive_file_id) != pages:
                raise OSError("the new files do not read back the same")
        except (OSError, ValueError) as exc:
            print(f"[ocr] could not migrate cache {old.name}: {exc}")
            continue
        old.unlink()
        print(f"[ocr] migrated cache: {drive_file_id}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1:
        migrate_old_cache()  # run once by hand: python -m ingest.ocr
    else:
        result = get_or_ocr(Path(sys.argv[1]), sys.argv[2])
        print(f"Extracted {len(result)} pages")
        print(result[0]["text"][:500])
