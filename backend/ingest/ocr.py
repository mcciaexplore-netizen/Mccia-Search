"""OCR of staged documents using Gemini."""

import json
import re
import time
from functools import lru_cache
from pathlib import Path

import httpx
import pymupdf
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import GEMINI_API_KEY, STAGING_DIR

# gemini-2.0-flash was shut down on 2026-06-01; gemini-3.6-flash is Google's
# documented replacement. Change this one constant to switch models.
OCR_MODEL = "gemini-3.6-flash"

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
CACHE_DIRNAME = ".ocr_cache"

DEVANAGARI_START, DEVANAGARI_END = "ऀ", "ॿ"
LANGUAGE_SAMPLE_CHARS = 500
DEVANAGARI_DOMINANT_SHARE = 0.85

_DRIVE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_API_ERRORS = (genai_errors.APIError, httpx.HTTPError, OSError)


class OCRPageError(RuntimeError):
    """Gemini could not produce text for a single page."""


def pdf_to_images(pdf_path: Path) -> list[bytes]:
    """Render every page of a PDF to PNG bytes at RENDER_DPI."""
    name = pdf_path.name
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {name} ({pdf_path})")

    doc = None
    try:
        doc = pymupdf.open(pdf_path)
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


def ocr_pdf(pdf_path: Path, drive_file_id: str | None = None) -> list[dict]:
    """OCR every page of a PDF, one page at a time.

    Returns [{page_number, text}, ...] with 1-based page numbers. A page that
    fails gets empty text and the rest still run. When `drive_file_id` is given
    and every page succeeded, the result is saved to the OCR cache.
    """
    name = pdf_path.name
    images = pdf_to_images(pdf_path)
    total = len(images)

    results: list[dict] = []
    failed: list[int] = []
    last_error: Exception | None = None

    for page_number, image in enumerate(images, start=1):
        print(f"[ocr] page {page_number}/{total} — {name}")
        try:
            text = ocr_page(image, page_number)
        except OCRPageError as exc:
            print(f"[ocr] page {page_number}/{total} failed, storing empty text — {exc}")
            text, last_error = "", exc
            failed.append(page_number)
        results.append({"page_number": page_number, "text": text})

    if len(failed) == total:
        raise RuntimeError(
            f"OCR failed for every page of {name}; last error: {last_error}"
        ) from last_error
    if failed:
        print(
            f"[ocr] WARNING: {len(failed)} of {total} pages failed in {name} "
            f"(pages {', '.join(map(str, failed))}); result not cached so the "
            "next run retries"
        )
    elif drive_file_id is not None:
        _save_cache(drive_file_id, results)
    return results


def get_or_ocr(pdf_path: Path, drive_file_id: str) -> list[dict]:
    """Return the OCR result for a PDF, using the cache when it has one."""
    cached = _load_cache(_cache_path(drive_file_id))
    if cached is not None:
        print(f"[ocr] cache hit for {pdf_path.name}, skipping OCR")
        return cached
    return ocr_pdf(pdf_path, drive_file_id)


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
    if not _DRIVE_ID_PATTERN.fullmatch(drive_file_id):
        raise ValueError(f"Invalid Drive file ID: {drive_file_id!r}")
    return Path(STAGING_DIR) / CACHE_DIRNAME / f"{drive_file_id}.json"


def _load_cache(path: Path) -> list[dict] | None:
    """Read a cached OCR result, or None if it is missing or unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"[ocr] ignoring unreadable cache {path.name} ({exc})")
        return None
    valid = (
        isinstance(data, list)
        and len(data) > 0
        and all(
            isinstance(page, dict)
            and isinstance(page.get("page_number"), int)
            and isinstance(page.get("text"), str)
            for page in data
        )
    )
    if not valid:
        print(f"[ocr] ignoring malformed cache {path.name}")
        return None
    return data


def _save_cache(drive_file_id: str, results: list[dict]) -> None:
    """Write the OCR result to its cache file atomically."""
    path = _cache_path(drive_file_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


if __name__ == "__main__":
    import sys

    result = get_or_ocr(Path(sys.argv[1]), sys.argv[2])
    print(f"Extracted {len(result)} pages")
    print(result[0]["text"][:500])
