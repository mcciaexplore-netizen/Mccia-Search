"""Google Drive sync: download new and changed files into the staging area."""

import json
import sys
import time
from collections import Counter
from http.client import HTTPException
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from httplib2 import HttpLib2Error

from config import GOOGLE_DRIVE_FOLDER_ID, GOOGLE_SERVICE_ACCOUNT_FILE, STAGING_DIR

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
PDF_MIME = "application/pdf"
FOLDER_MIME = "application/vnd.google-apps.folder"

PAGE_SIZE = 200
CHUNK_SIZE = 4 * 1024 * 1024
MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 5
IDS_FILENAME = ".drive_ids.json"

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


def download_pdf(
    service: Resource, file_id: str, filename: str, size: int | None = None
) -> Path:
    """Download a Drive PDF to STAGING_DIR/<filename> and return its local path.

    The download is skipped when a file of the same size is already present.
    Pass `size` (bytes, from list_pdfs) to avoid an extra metadata request.
    Network errors are retried up to MAX_ATTEMPTS times; a RuntimeError is raised
    if every attempt fails.
    """
    staging_dir = Path(STAGING_DIR)
    staging_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(filename)
    dest = staging_dir / filename
    part = staging_dir / f"{filename}.part"

    if size is None and dest.exists():
        size = int(
            service.files()
            .get(fileId=file_id, fields="size", supportsAllDrives=True)
            .execute(num_retries=3)["size"]
        )

    if _is_present(dest, size):
        _record_drive_id(filename, file_id)
        return dest

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _fetch_to_file(service, file_id, filename, part)
            if size is not None and part.stat().st_size != size:
                raise OSError(
                    f"size mismatch: got {part.stat().st_size} bytes, expected {size}"
                )
            part.replace(dest)
        except _NETWORK_ERRORS as exc:
            if not _is_retryable(exc):
                raise
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                print(
                    f"[retry] {filename} — attempt {attempt}/{MAX_ATTEMPTS} failed "
                    f"({exc}); retrying in {RETRY_WAIT_SECONDS}s"
                )
                time.sleep(RETRY_WAIT_SECONDS)
        else:
            _record_drive_id(filename, file_id)
            return dest
        finally:
            part.unlink(missing_ok=True)

    raise RuntimeError(
        f"Giving up on {filename} after {MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def sync_drive() -> list[Path]:
    """Download every Drive PDF not already in staging; return the new paths.

    A file that fails all its retries is reported and skipped so the rest of the
    sync can finish.
    """
    service = build_drive_service()
    pdfs = list_pdfs(service)
    local_names = _local_filenames(pdfs)

    downloaded: list[Path] = []
    skipped = 0
    failed: list[str] = []

    for pdf, local_name in zip(pdfs, local_names):
        was_present = _is_present(Path(STAGING_DIR) / local_name, pdf["size"])
        try:
            path = download_pdf(service, pdf["file_id"], local_name, size=pdf["size"])
        except Exception as exc:  # one bad file must not stop the sync
            print(f"[failed] {local_name} — {exc}", file=sys.stderr)
            failed.append(local_name)
            continue
        if was_present:
            skipped += 1
        else:
            downloaded.append(path)

    print(
        f"Sync complete: {len(downloaded)} downloaded, "
        f"{skipped} skipped (already present)"
    )
    if failed:
        print(f"{len(failed)} file(s) failed: {', '.join(failed)}", file=sys.stderr)
    return downloaded


def _fetch_to_file(service: Resource, file_id: str, filename: str, part: Path) -> None:
    """Stream a Drive file into `part`, printing progress per chunk."""
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with part.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request, chunksize=CHUNK_SIZE)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"[downloading] {filename} — {int(status.progress() * 100)}%")


def _is_retryable(exc: Exception) -> bool:
    """Retry network failures, rate limits and 5xx; fail fast on other API errors."""
    if isinstance(exc, HttpError):
        status = exc.resp.status
        return status in (408, 429) or status >= 500
    return True


def _is_present(path: Path, size: int | None) -> bool:
    return size is not None and path.is_file() and path.stat().st_size == size


def _safe_filename(name: str) -> str:
    """Make a Drive file name safe to use as a single local path component."""
    name = name.replace("/", "_").replace("\\", "_").replace("\x00", "_").strip()
    if not name:
        name = "untitled"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def _local_filenames(pdfs: list[dict]) -> list[str]:
    """Pick a local filename for each PDF, in the same order as `pdfs`.

    PDFs from different Drive folders can share a name (and macOS file systems
    ignore case). Every file in such a group gets its Drive ID appended, so no
    file overwrites another and the names don't depend on listing order.
    """
    names = [_safe_filename(pdf["name"]) for pdf in pdfs]
    counts = Counter(name.casefold() for name in names)
    return [
        f"{name[:-4]}__{pdf['file_id']}.pdf" if counts[name.casefold()] > 1 else name
        for name, pdf in zip(names, pdfs)
    ]


def _load_drive_ids(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"[warn] could not read {path} ({exc}); starting a new map", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _record_drive_id(filename: str, file_id: str) -> None:
    """Record filename -> Drive file ID in the sidecar, written atomically."""
    path = Path(STAGING_DIR) / IDS_FILENAME
    ids = _load_drive_ids(path)
    if ids.get(filename) == file_id:
        return
    ids[filename] = file_id
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(ids, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


if __name__ == "__main__":
    paths = sync_drive()
    print(f"Downloaded {len(paths)} new PDFs")
