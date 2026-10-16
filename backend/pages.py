"""Render one page of a magazine PDF as an image, for the "View page" link.

The PDFs are huge (100 to 1000 MB) and Google Drive is slow (about 1 MB a second here), so a
PDF is never downloaded whole. Its index is read from the end of the file, and only the bytes
of the one page wanted are fetched, with range requests: a few MB in all, a few seconds. That
page is copied into a tiny one-page PDF, which pymupdf draws. The JPEG is cached under
PAGE_CACHE_DIR, so every later view is instant; the cache is trimmed to MAX_CACHE_BYTES,
least recently viewed first.
"""

import io
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

import pymupdf
from googleapiclient.errors import HttpError
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import IndirectObject, NameObject

from config import PAGE_CACHE_DIR
from db.connection import get_pooled_connection
from ingest.drive_sync import _fetch_chunk, build_drive_service, stream_pdf

PAGE_DPI = 150  # sharp enough to read small print; the OCR itself reads at 200
JPEG_QUALITY = 80
MAX_CACHE_BYTES = 500 * 1024 * 1024
BLOCK_SIZE = 1024 * 1024  # bytes fetched per range request
MAX_BLOCKS = 16  # blocks kept per PDF, so a few MB of its index and recent pages
MAX_REQUESTS_PER_PAGE = 30  # a normal page needs about 4; more means the PDF is being scanned
KEEP_REMOTES = 4  # PDFs whose index is kept, so the next page of an issue is quicker
MAX_DOWNLOADS = 2  # pages fetched from Drive at the same time
# A PDF the page-by-page reader cannot make sense of is downloaded whole instead, but only
# when that is quick: a big one would take minutes.
FALLBACK_MAX_BYTES = 60 * 1024 * 1024

_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]+")
_INHERITED = ("/Resources", "/MediaBox", "/CropBox", "/Rotate")  # a page may take these from its parents

_remotes: OrderedDict[str, "_RemotePdf"] = OrderedDict()
_remotes_lock = threading.Lock()
_download_slots = threading.Semaphore(MAX_DOWNLOADS)
_file_locks: dict[str, threading.Lock] = {}
_file_locks_lock = threading.Lock()
_trim_lock = threading.Lock()


class PageNotFound(LookupError):
    """No such document, or no such page in it."""


class PageUnavailable(RuntimeError):
    """The page exists, but it could not be fetched from Drive or drawn."""


def get_page_image(document_id: int, page_number: int) -> Path:
    """The JPEG file of one page (1-based, like a source's page_number), drawn first if needed.

    Raises PageNotFound for an unknown document or a page outside it, and
    PageUnavailable when Google Drive cannot provide the page.
    """
    if page_number < 1:
        raise PageNotFound(f"There is no page {page_number}.")
    drive_file_id, filename, total_pages = _find_document(document_id)
    if total_pages is not None and page_number > total_pages:
        raise PageNotFound(f"{filename} has only {total_pages} pages.")

    path = _page_path(drive_file_id, page_number)
    if _touch(path):
        return path
    with _lock_for(drive_file_id):  # one request at a time per PDF, however many pages are asked for
        if _touch(path):  # drawn by another request while this one waited
            return path
        with _download_slots:
            pdf, index = _fetch_page(drive_file_id, filename, page_number)
        _write_atomic(path, _draw(pdf, index, page_number, filename))
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


# ------------------------------------------------------------------ reading a page from Drive
class _RemotePdf(io.RawIOBase):
    """A read-only, seekable file whose bytes are fetched from Drive a block at a time, only when read."""

    def __init__(self, service, file_id: str, name: str, size: int):
        self._service, self._file_id, self._name, self.size = service, file_id, name, size
        self._pos = 0
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self.requests_left = MAX_REQUESTS_PER_PAGE
        self.drive_error: Exception | None = None  # why a block could not be fetched, if it could not

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = max(0, min(base + offset, self.size))
        return self._pos

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            size = self.size - self._pos
        size = min(size, self.size - self._pos)
        out = bytearray()
        while size > 0:
            index, offset = divmod(self._pos, BLOCK_SIZE)
            chunk = self._block(index)[offset : offset + size]
            if not chunk:
                break
            out += chunk
            self._pos += len(chunk)
            size -= len(chunk)
        return bytes(out)

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def _block(self, index: int) -> bytes:
        block = self._blocks.get(index)
        if block is not None:
            self._blocks.move_to_end(index)
            return block
        if self.requests_left <= 0:
            raise OSError("this page needed too many requests to Google Drive")
        start = index * BLOCK_SIZE
        try:
            block = _fetch_chunk(
                self._service, self._file_id, self._name, start, min(start + BLOCK_SIZE, self.size) - 1
            ) or b""
        except Exception as exc:  # network, quota or permission trouble: told apart from a PDF we cannot read
            self.drive_error = exc
            raise
        self.requests_left -= 1
        self._blocks[index] = block
        while len(self._blocks) > MAX_BLOCKS:
            self._blocks.popitem(last=False)
        return block


def _remote_pdf(drive_file_id: str, filename: str) -> _RemotePdf:
    """The remote PDF, kept for a while so the next page of the issue need not start from nothing."""
    with _remotes_lock:
        remote = _remotes.get(drive_file_id)
        if remote is not None:
            _remotes.move_to_end(drive_file_id)
            return remote
    service = build_drive_service()
    size = int(
        service.files().get(fileId=drive_file_id, fields="size", supportsAllDrives=True).execute()["size"]
    )
    remote = _RemotePdf(service, drive_file_id, filename, size)
    with _remotes_lock:
        _remotes[drive_file_id] = remote
        while len(_remotes) > KEEP_REMOTES:
            _remotes.popitem(last=False)
    return remote


def _forget_remote(drive_file_id: str) -> None:
    with _remotes_lock:
        _remotes.pop(drive_file_id, None)


def _fetch_page(drive_file_id: str, filename: str, page_number: int) -> tuple[bytes, int]:
    """(a PDF, the 1-based index of the page in it): a one-page PDF, or the whole PDF if it is small."""
    remote = None
    try:
        remote = _remote_pdf(drive_file_id, filename)
        remote.requests_left = MAX_REQUESTS_PER_PAGE
        return _one_page_pdf(remote, page_number), 1
    except PageNotFound:
        raise
    except Exception as exc:
        drive_error = remote.drive_error if remote is not None else exc
        if remote is None or drive_error is not None:
            _raise_unavailable(filename, drive_error or exc)
        print(f"[pages] {filename}: cannot read page {page_number} on its own ({type(exc).__name__}: {exc})")
        if remote.size > FALLBACK_MAX_BYTES:
            raise PageUnavailable("This page could not be read from its PDF.") from exc
    # A small PDF that cannot be read a page at a time: fetch all of it, which is quick.
    try:
        return stream_pdf(remote._service, drive_file_id, filename, remote.size), page_number
    except Exception as exc:
        _raise_unavailable(filename, exc)


def _raise_unavailable(filename: str, exc: Exception) -> None:
    print(f"[pages] could not fetch {filename} from Drive: {type(exc).__name__}: {exc}")
    if isinstance(exc, HttpError) and exc.resp.status == 404:
        raise PageUnavailable("This PDF is no longer in Google Drive.") from exc
    raise PageUnavailable("Could not fetch this page from Google Drive. Please try again.") from exc


def _one_page_pdf(remote: _RemotePdf, page_number: int) -> bytes:
    """A PDF of just this page, made by reading only the parts of the big file that it needs."""
    reader = _open_reader(remote)
    found = _page_reference(reader, page_number - 1)
    if isinstance(found, IndirectObject):
        page = PageObject(reader, found)
    else:  # the page dictionary itself, which is what pypdf hands back after repairing a damaged index
        page = PageObject(reader)
        page.update(found)
    for key in _INHERITED:  # pypdf only fills these in when it reads every page, which we avoid
        parent = page.get("/Parent")
        while key not in page and parent is not None:
            node = parent.get_object()
            if key in node:
                page[NameObject(key)] = node[key]
            parent = node.get("/Parent")
    writer = PdfWriter()
    writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _open_reader(remote: _RemotePdf) -> PdfReader:
    # Strict mode skips pypdf's "double check the index" step, which reads the file from
    # the start. If strict mode finds a fault, the lenient mode gets a go, within the
    # request budget, so a damaged PDF cannot make it read everything.
    try:
        return PdfReader(remote, strict=True)
    except Exception:
        if remote.drive_error is not None:
            raise
        return PdfReader(remote, strict=False)


def _page_reference(reader: PdfReader, index: int):
    """The page at `index` (0-based), found by reading only the page-tree nodes on the way to it."""
    node = reader.trailer["/Root"]["/Pages"].get_object()
    if index < 0 or index >= int(node.get("/Count", 0)):
        raise PageNotFound(f"There is no page {index + 1} in this PDF.")
    while True:
        kids = node["/Kids"]
        if int(node.get("/Count", len(kids))) == len(kids):  # every kid is a page: pick it directly
            return kids[index]
        for kid in kids:
            child = kid.get_object()
            if child.get("/Type") == "/Pages":
                count = int(child["/Count"])
                if index < count:
                    node = child
                    break
                index -= count
            else:
                if index == 0:
                    return kid
                index -= 1
        else:
            raise PageNotFound("This PDF's page list is shorter than it says.")


def _draw(pdf: bytes, index: int, page_number: int, filename: str) -> bytes:
    """JPEG bytes of one page (1-based `index`) of a PDF."""
    try:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            if index > doc.page_count:
                raise PageNotFound(f"{filename} has only {doc.page_count} pages.")
            pixmap = doc[index - 1].get_pixmap(dpi=PAGE_DPI)
            return pixmap.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    except PageNotFound:
        raise
    except Exception as exc:  # a PDF pymupdf cannot open or draw
        print(f"[pages] could not draw page {page_number} of {filename}: {type(exc).__name__}: {exc}")
        raise PageUnavailable("This page could not be drawn from its PDF.") from exc


# ------------------------------------------------------------------------------ the cache
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
