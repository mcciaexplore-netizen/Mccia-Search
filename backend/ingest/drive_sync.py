"""Google Drive access: list the PDFs in a folder and stream them into memory.

PDF bytes are never written to disk.
"""

import io
import time
from http.client import HTTPException
from pathlib import Path
from typing import Iterator

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from httplib2 import HttpLib2Error

from config import GOOGLE_DRIVE_FOLDER_ID, GOOGLE_SERVICE_ACCOUNT_FILE
from db.connection import get_connection

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
PDF_MIME = "application/pdf"
FOLDER_MIME = "application/vnd.google-apps.folder"

PAGE_SIZE = 200
CHUNK_SIZE = 4 * 1024 * 1024
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


def list_pdfs(service: Resource) -> list[dict]:
    """List every PDF under GOOGLE_DRIVE_FOLDER_ID, including subfolders.

    Returns dicts of {file_id, name, modified_time, size}; size is in bytes.
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

    print(f"Found {len(pdfs)} PDFs in Drive folder")
    return pdfs


def stream_pdf(
    service: Resource,
    file_id: str,
    filename: str | None = None,
    size: int | None = None,
) -> bytes:
    """Stream a Drive PDF into memory and return its bytes; nothing touches the disk.

    `filename` is only used in log messages. Pass `size` (bytes, from list_pdfs) to
    reject a truncated download. Network errors, rate limits and 5xx are retried up
    to MAX_RETRIES times, RETRY_WAIT_SECONDS apart, each retry starting from an empty
    buffer. A RuntimeError is raised if every attempt fails; other API errors
    (403, 404, ...) are raised straight away.
    """
    label = filename or file_id
    print(f"[drive] streaming {label} into memory...")

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            data = _fetch_to_memory(service, file_id)
            if size is not None and len(data) != size:
                raise OSError(f"size mismatch: got {len(data)} bytes, expected {size}")
            return data
        except _NETWORK_ERRORS as exc:
            if not _is_retryable(exc):
                raise
            # Without its traceback the error no longer keeps the failed download's
            # buffer alive while the next attempt fills a new one.
            last_error = exc.with_traceback(None)
            if attempt <= MAX_RETRIES:
                print(
                    f"[retry] {label} — attempt {attempt}/{MAX_RETRIES + 1} failed "
                    f"({exc}); retrying in {RETRY_WAIT_SECONDS}s"
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


def _fetch_to_memory(service: Resource, file_id: str) -> bytes:
    """Download a Drive file chunk by chunk into a BytesIO buffer."""
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=CHUNK_SIZE)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def _is_retryable(exc: Exception) -> bool:
    """Retry network failures, rate limits and 5xx; fail fast on other API errors."""
    if isinstance(exc, HttpError):
        status = exc.resp.status
        return status in (408, 429) or status >= 500
    return True
