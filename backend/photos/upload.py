"""Putting one page's photographs into the bucket and the database, in that order: a row never points at a picture that is not there."""

import time
from typing import Callable

import httpx

from photos.db import save_page
from photos.store import StorageError, storage_path

UPLOAD_ATTEMPTS = 4
RETRY_PAUSE_SECONDS = 3.0


def upload_photos(store, document_id: int, page: int, photos, sleep: Callable[[float], None] = time.sleep) -> None:
    """Upload every photo of a page, trying each a few times with a growing pause (a network blip must not end a run that takes days)."""
    for photo in photos:
        path = storage_path(document_id, page, photo.index)
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                store.put(path, photo.jpeg)
                break
            except (StorageError, httpx.HTTPError):
                if attempt == UPLOAD_ATTEMPTS:
                    raise
                sleep(RETRY_PAUSE_SECONDS * attempt)


def store_page(conn, store, document_id: int, page: int, photos, seconds: float | None = None, sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Upload the page's photos, and only then record the page. If an upload fails for good, nothing is recorded for the page (it is done again next run)."""
    upload_photos(store, document_id, page, photos, sleep)
    return save_page(conn, document_id, page, photos, seconds)
