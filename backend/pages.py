"""Render one page of a magazine PDF as an image, for the "View page" link.

The PDF is fetched from Google Drive on first use (the app keeps no PDFs on disk), the
page is drawn with pymupdf, and the JPEG is cached under PAGE_CACHE_DIR so every later
view is instant. The cache is trimmed to MAX_CACHE_BYTES, least recently viewed first.
"""

import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

import pymupdf
from googleapiclient.errors import HttpError

from config import PAGE_CACHE_DIR
from db.connection import get_pooled_connection
from ingest.drive_sync import build_drive_service, stream_pdf

PAGE_DPI = 150  # sharp enough to read small print; the OCR itself reads at 200
JPEG_QUALITY = 80
MAX_CACHE_BYTES = 500 * 1024 * 1024
KEEP_PDFS = 2  # PDFs held in memory, so nearby pages of one issue cost a single download
MAX_DOWNLOADS = 2  # PDFs downloaded at the same time

_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]+")

_pdfs: OrderedDict[str, bytes] = OrderedDict()  # drive_file_id -> PDF bytes, least recent first
_pdfs_lock = threading.Lock()
_download_slots = threading.Semaphore(MAX_DOWNLOADS)
_file_locks: dict[str, threading.Lock] = {}
_file_locks_lock = threading.Lock()
_trim_lock = threading.Lock()


class PageNotFound(LookupError):
    """No such document, or no such page in it."""


class PageUnavailable(RuntimeError):
    """The page exists, but its PDF could not be fetched from Drive or drawn."""


def get_page_image(document_id: int, page_number: int) -> Path:
    """The JPEG file of one page (1-based, like a source's page_number), drawn first if needed.

    Raises PageNotFound for an unknown document or a page outside it, and
    PageUnavailable when Google Drive cannot provide the PDF.
    """
    if page_number < 1:
        raise PageNotFound(f"There is no page {page_number}.")
    drive_file_id, filename, total_pages = _find_document(document_id)
    if total_pages is not None and page_number > total_pages:
        raise PageNotFound(f"{filename} has only {total_pages} pages.")

    path = _page_path(drive_file_id, page_number)
    if _touch(path):
        return path
    with _lock_for(drive_file_id):  # one download per PDF, however many pages are asked for
        if _touch(path):  # drawn by another request while this one waited
            return path
        pdf = _pdf_bytes(drive_file_id, filename)
        try:
            image = _draw(pdf, page_number, filename)
        except PageUnavailable:
            _forget_pdf(drive_file_id)  # do not keep a PDF that cannot be drawn
            raise
        _write_atomic(path, image)
    _trim_cache()
    return path


def _find_document(document_id: int) -> tuple[str, str, int | None]:
    """(drive_file_id, filename, total_pages) of a document; PageNotFound if it is unknown.

    Only documents in the database can be asked for, never an arbitrary Drive file.
    """
    with get_pooled_connection() as conn:
        row = conn.execute(
            "SELECT drive_file_id, filename, total_pages FROM documents WHERE id = %s",
            (document_id,),
        ).fetchone()
    if row is None or not _DRIVE_ID.fullmatch(row[0]):
        raise PageNotFound("That document was not found.")
    return row[0], row[1], row[2]


def _page_path(drive_file_id: str, page_number: int) -> Path:
    return Path(PAGE_CACHE_DIR) / drive_file_id / f"{page_number:04d}.jpg"


def _touch(path: Path) -> bool:
    """Mark a cached page as just viewed; False if it is not cached."""
    try:
        os.utime(path)
    except FileNotFoundError:
        return False
    return True


def _lock_for(drive_file_id: str) -> threading.Lock:
    with _file_locks_lock:
        return _file_locks.setdefault(drive_file_id, threading.Lock())


def _pdf_bytes(drive_file_id: str, filename: str) -> bytes:
    """The PDF, from the small in-memory store if it was used recently, else from Drive."""
    with _pdfs_lock:
        if drive_file_id in _pdfs:
            _pdfs.move_to_end(drive_file_id)
            return _pdfs[drive_file_id]
    with _download_slots:
        data = _download(drive_file_id, filename)
    with _pdfs_lock:
        _pdfs[drive_file_id] = data
        while len(_pdfs) > KEEP_PDFS:
            _pdfs.popitem(last=False)
    return data


def _forget_pdf(drive_file_id: str) -> None:
    with _pdfs_lock:
        _pdfs.pop(drive_file_id, None)


def _download(drive_file_id: str, filename: str) -> bytes:
    try:
        service = build_drive_service()
        meta = service.files().get(
            fileId=drive_file_id, fields="size", supportsAllDrives=True
        ).execute()
        return stream_pdf(service, drive_file_id, filename, int(meta["size"]))
    except Exception as exc:  # whatever Drive, the network or the credentials throw
        print(f"[pages] could not download {filename}: {type(exc).__name__}: {exc}")
        if isinstance(exc, HttpError) and exc.resp.status == 404:
            raise PageUnavailable("This PDF is no longer in Google Drive.") from exc
        raise PageUnavailable(
            "Could not download this PDF from Google Drive. Please try again."
        ) from exc


def _draw(pdf: bytes, page_number: int, filename: str) -> bytes:
    """JPEG bytes of one page of a PDF."""
    try:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            if page_number > doc.page_count:
                raise PageNotFound(f"{filename} has only {doc.page_count} pages.")
            pixmap = doc[page_number - 1].get_pixmap(dpi=PAGE_DPI)
            return pixmap.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    except PageNotFound:
        raise
    except Exception as exc:  # a PDF pymupdf cannot open or draw
        print(f"[pages] could not draw page {page_number} of {filename}: {type(exc).__name__}: {exc}")
        raise PageUnavailable("This page could not be drawn from its PDF.") from exc


def _write_atomic(path: Path, data: bytes) -> None:
    """Write the file so that a reader never sees it half written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _trim_cache() -> None:
    """Delete the least recently viewed pages once the cache is over MAX_CACHE_BYTES."""
    with _trim_lock:
        files = []
        for path in Path(PAGE_CACHE_DIR).glob("*/*.jpg"):
            try:
                stat = path.stat()
            except OSError:  # deleted since it was listed
                continue
            files.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in files)
        if total <= MAX_CACHE_BYTES:
            return
        for _, size, path in sorted(files):  # oldest first
            if total <= MAX_CACHE_BYTES * 0.8:  # leave room, so this is not run on every page
                break
            path.unlink(missing_ok=True)
            total -= size
