"""Google Drive access: list the PDFs in a folder and stream them into memory.

PDF bytes are never written to disk.
"""

import re
import time
from http.client import HTTPException
from pathlib import Path
from typing import Iterator

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from config import GOOGLE_DRIVE_FOLDER_ID, GOOGLE_SERVICE_ACCOUNT_FILE
from db.connection import get_connection

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
PDF_MIME = "application/pdf"
FOLDER_MIME = "application/vnd.google-apps.folder"

PAGE_SIZE = 200
CHUNK_SIZE = 8 * 1024 * 1024  # bytes per ranged request; a failed chunk is retried alone
PROGRESS_EVERY_BYTES = 50 * 1024 * 1024
MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 5
BYTES_PER_MB = 1024 * 1024

_NETWORK_ERRORS = (HttpError, OSError, HttpLib2Error, HTTPException)


def build_drive_service() -> Resource:
    """Return a Drive API client authenticated with the service account."""
    if not GOOGLE_SERVICE_ACCOUNT_FILE:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_FILE is not set. Point it at your service "
            "account JSON key file in .env."
        )
    key_path = Path(GOOGLE_SERVICE_ACCOUNT_FILE)
    if not key_path.is_file():
        raise RuntimeError(
            f"Google service account file not found: {key_path}. "
            "Check GOOGLE_SERVICE_ACCOUNT_FILE in .env and that the key file exists."
        )
    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=SCOPES
        )
    except (ValueError, KeyError) as exc:
        raise RuntimeError(
            f"Could not load service account credentials from {key_path}: {exc}"
        ) from exc
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")  # a 4-digit year, not part of a longer number
_MONTH = re.compile(r"(?<![a-z])(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _extract_year(filename: str) -> int:
    """Extract the first 4-digit year from a PDF filename.

    Returns 9999 if no year found (sorts to end).
    Examples:
      '1945 July.PDF' -> 1945
      '1963-April To1964-March Part-1.pdf' -> 1963
      '2011-Jan.PDF' -> 2011
    """
    match = _YEAR.search(filename)
    return int(match.group()) if match else 9999


def _extract_month(filename: str) -> int:
    """The first month named in a filename as 1-12, or 13 if there is none.

    Spelling mistakes after the first three letters do not matter ('Januaray' is
    January). The first month is the start of the issues the file covers.
    """
    match = _MONTH.search(filename)
    return _MONTHS.index(match.group(1).lower()) + 1 if match else 13


def _chronological_key(pdf: dict) -> tuple:
    """Sort key: first year, then first month, then the name with numbers in numeric order."""
    name = pdf["name"]
    # re.split with a group alternates text and digits, so the parts always compare like with like
    natural = [int(part) if i % 2 else part.lower() for i, part in enumerate(re.split(r"(\d+)", name))]
    return (_extract_year(name), _extract_month(name), natural, name)


def list_pdfs(service: Resource) -> list[dict]:
    """List every PDF under GOOGLE_DRIVE_FOLDER_ID, including subfolders.

    Returns dicts of {file_id, name, modified_time, size}; size is in bytes. The list
    is in chronological order: by the year in the file name, then the month, then
    the name ('Part-2' after 'Part-1'), so a sync processes the oldest issues first.
    """
    root_id = (GOOGLE_DRIVE_FOLDER_ID or "").strip()
    if not root_id:
        raise RuntimeError(
            "GOOGLE_DRIVE_FOLDER_ID is not set. Set it in .env to the folder ID "
            "from your Google Drive folder URL."
        )

    pdfs: list[dict] = []
    seen_folders: set[str] = set()
    pending_folders = [root_id]

    while pending_folders:
        folder_id = pending_folders.pop()
        if folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)

        page_token = None
        while True:
            try:
                response = (
                    service.files()
                    .list(
                        q=(
                            f"'{folder_id}' in parents and trashed = false and "
                            f"(mimeType = '{PDF_MIME}' or mimeType = '{FOLDER_MIME}')"
                        ),
                        fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
                        pageSize=PAGE_SIZE,
                        pageToken=page_token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute(num_retries=3)
                )
            except HttpError as exc:
                if folder_id == root_id and exc.resp.status == 404:
                    raise RuntimeError(
                        f"Drive folder {root_id} was not found. Check "
                        "GOOGLE_DRIVE_FOLDER_ID and that the folder is shared with "
                        "the service account's email address."
                    ) from exc
                raise

            for item in response.get("files", []):
                if item["mimeType"] == FOLDER_MIME:
                    pending_folders.append(item["id"])
                elif item["mimeType"] == PDF_MIME:
                    pdfs.append(
                        {
                            "file_id": item["id"],
                            "name": item["name"],
                            "modified_time": item.get("modifiedTime"),
                            "size": int(item["size"]) if "size" in item else None,
                        }
                    )

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    pdfs.sort(key=_chronological_key)
    print(f"Found {len(pdfs)} PDFs in Drive folder")
    return pdfs


def stream_pdf(
    service: Resource,
    file_id: str,
    filename: str | None = None,
    size: int | None = None,
) -> bytes:
    """Stream a Drive PDF into memory and return its bytes; nothing touches the disk.

    The file is fetched in CHUNK_SIZE ranged requests (see _fetch_to_memory), each
    retried on its own, so a dropped connection costs one chunk and not the file.
    `filename` is only used in log messages. Pass `size` (bytes, from list_pdfs) to
    know when the file is complete and to reject a wrong-sized result. If a chunk
    still fails after its retries, the download is retried up to MAX_RETRIES more
    times, RETRY_WAIT_SECONDS apart, each time resuming after the bytes already
    received. A RuntimeError is raised if every attempt fails; other API errors
    (403, 404, ...) are raised straight away.
    """
    label = filename or file_id
    print(f"[drive] streaming {label} into memory...")

    buf = bytearray()  # kept across attempts, so a retry resumes where the last one stopped
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            data = _fetch_to_memory(service, file_id, label, size or 0, buf)
            if size is not None and len(data) != size:
                buf.clear()  # not the file we expected: a retry has to start over
                raise OSError(f"size mismatch: got {len(data)} bytes, expected {size}")
            return data
        except _NETWORK_ERRORS as exc:
            if not _is_retryable(exc):
                raise
            last_error = exc.with_traceback(None)
            if attempt <= MAX_RETRIES:
                print(
                    f"[retry] {label} — attempt {attempt}/{MAX_RETRIES + 1} failed "
                    f"({_brief(exc)}); retrying in {RETRY_WAIT_SECONDS}s, "
                    f"resuming at {len(buf) / BYTES_PER_MB:.0f} MB"
                )
                time.sleep(RETRY_WAIT_SECONDS)

    raise RuntimeError(
        f"Giving up on {label} after {MAX_RETRIES + 1} attempts: {last_error}"
    ) from last_error


def get_completed_drive_ids() -> set[str]:
    """Drive file IDs of every document already fully ingested.

    Returns an empty set if the database cannot be reached, so the caller streams
    every file instead of failing.
    """
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT drive_file_id FROM documents WHERE status = 'completed'"
            ).fetchall()
    except Exception as exc:
        # Only the error type is printed: a malformed DATABASE_URL can end up in the message.
        print(
            "[drive] WARNING: could not load completed IDs from DB, "
            f"will stream all files ({type(exc).__name__})"
        )
        return set()
    return {row[0] for row in rows}


def iter_drive_pdfs(service: Resource, skip_completed: bool = True) -> Iterator[dict]:
    """Yield every Drive PDF as {file_id, filename, size, bytes}, streamed one at a time.

    With `skip_completed` (the default), PDFs whose document is already 'completed'
    in the database are skipped before they are streamed; pass False to stream
    everything. A PDF is only downloaded when the caller asks for the next item, so
    just one is held in memory. The caller should drop its reference to an item
    before asking for the next, so the bytes can be freed. A file that cannot be
    streamed is reported and skipped so the rest can continue.
    """
    pdfs = list_pdfs(service)
    total = len(pdfs)
    completed_ids = get_completed_drive_ids() if skip_completed else set()

    for index, pdf in enumerate(pdfs, start=1):
        filename, size = pdf["name"], pdf["size"]
        if pdf["file_id"] in completed_ids:
            print(f"[drive] {index}/{total} — {filename} already ingested, skipping")
            continue
        print(f"[drive] {index}/{total} — {filename} ({(size or 0) / BYTES_PER_MB:.1f} MB)")
        try:
            data = stream_pdf(service, pdf["file_id"], filename, size)
        except Exception as exc:  # one bad file must not stop the run
            print(f"[drive] WARNING: failed to stream {filename}, skipping — {exc}")
            continue
        yield {
            "file_id": pdf["file_id"],
            "filename": filename,
            "size": size if size is not None else len(data),
            "bytes": data,
        }
        del data  # the caller now holds the only reference


class _RangeNotHonoured(RuntimeError):
    """The server answered a ranged request with more bytes than were asked for."""


def _fetch_to_memory(
    service: Resource,
    file_id: str,
    filename: str = "",
    total_size: int = 0,
    buf: bytearray | None = None,
) -> bytes:
    """Download a Drive file in CHUNK_SIZE ranged requests and return its bytes.

    Each chunk is retried on its own (see _fetch_chunk), from its own first byte.
    Bytes already in `buf` are kept and the download continues right after them, so
    a caller can resume after a failure. With `total_size` the loop runs until every
    byte has arrived, even if a response was short. Without it, the first short
    chunk (or a 416 past the end) is taken as the end of the file.
    """
    buf = bytearray() if buf is None else buf
    label = filename or file_id
    while True:
        offset = len(buf)
        if total_size and offset >= total_size:
            break
        end = offset + CHUNK_SIZE - 1
        if total_size:
            end = min(end, total_size - 1)
        chunk = _fetch_chunk(service, file_id, label, offset, end)
        if chunk is None:  # a 416 past the end of a file of unknown size
            break
        buf.extend(chunk)
        if total_size and offset // PROGRESS_EVERY_BYTES != len(buf) // PROGRESS_EVERY_BYTES:
            print(
                f"[drive] {label}: {len(buf) / total_size * 100:.0f}% "
                f"({len(buf) / BYTES_PER_MB:.0f}/{total_size / BYTES_PER_MB:.0f} MB)"
            )
        if not total_size and len(chunk) < end - offset + 1:
            break  # unknown size: a short chunk is the last one
    return bytes(buf)


def _fetch_chunk(
    service: Resource, file_id: str, label: str, offset: int, end: int
) -> bytes | None:
    """Fetch bytes `offset`..`end` of a file with a Range header, retrying this chunk only.

    Returns None for a 416 at a non-zero offset (the file ended there). Network
    errors, rate limits and 5xx are retried up to MAX_RETRIES times, waiting
    RETRY_WAIT_SECONDS longer each time; other errors are raised straight away.
    """
    expected = end - offset + 1
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
            request.headers["Range"] = f"bytes={offset}-{end}"
            chunk = request.execute()
            if not chunk:
                raise OSError("the server sent an empty chunk")
            if len(chunk) > expected:
                raise _RangeNotHonoured(
                    f"asked for {expected} bytes at {offset} but got {len(chunk)}: "
                    "the Range header was ignored"
                )
            return chunk
        except _NETWORK_ERRORS as exc:
            if isinstance(exc, HttpError) and exc.resp.status == 416 and offset > 0:
                return None
            if attempt > MAX_RETRIES or not _is_retryable(exc):
                raise
            wait = RETRY_WAIT_SECONDS * attempt
            print(
                f"[drive] {label} chunk at {offset / BYTES_PER_MB:.0f} MB: error "
                f"({_brief(exc)}), retry {attempt}/{MAX_RETRIES} in {wait}s"
            )
            time.sleep(wait)


def _is_retryable(exc: Exception) -> bool:
    """Retry network failures, rate limits and 5xx; fail fast on other API errors."""
    if isinstance(exc, HttpError):
        status = exc.resp.status
        return status in (408, 429) or status >= 500
    return True


def _brief(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:200]
