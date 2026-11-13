"""Where the cropped photographs are kept: a private Supabase Storage bucket (or a folder, for a pilot or a test)."""

import os
import re
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

BUCKET = "sampada-images"
KEY_FILE = Path(__file__).resolve().parents[2] / ".env.supabase.local"  # git-ignored, like the database address in it
SIGNED_URL_SECONDS = 3600
TIMEOUT_SECONDS = 60


class StorageError(RuntimeError):
    """The storage service refused or failed a request. The message never contains the key."""


def storage_path(document_id: int, page: int, index: int) -> str:
    """The object name of a photo inside the bucket: grouped by document, sortable by page."""
    return f"doc-{document_id:04d}/p{page:04d}-{index}.jpg"


class LocalStore:
    """Photographs as files in a folder: for the pilot and for tests."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, path: str, data: bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get(self, path: str) -> bytes:
        return (self.root / path).read_bytes()


class SupabaseStore:
    """Supabase Storage through its HTTP API with the project's service-role key (a secret: it is never printed, logged or put in an error)."""

    def __init__(self, project_url: str, service_key: str, bucket: str = BUCKET, client: httpx.Client | None = None):
        if not project_url or not service_key:
            raise ValueError("SupabaseStore needs the project URL and the service-role key")
        self.base = project_url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self._key = service_key
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)

    def __repr__(self) -> str:
        return f"SupabaseStore({self.base!r}, bucket={self.bucket!r})"  # no key

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._key}", "apikey": self._key}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _check(self, response: httpx.Response, what: str) -> httpx.Response:
        if response.status_code >= 300:
            body = response.text.replace(self._key, "***")[:200]
            raise StorageError(f"{what} failed: HTTP {response.status_code} {body}")
        return response

    def ensure_bucket(self, max_bytes: int = 10 * 1024 * 1024) -> bool:
        """Create the private bucket if it does not exist (images only, no larger than max_bytes). Returns True if it was created, False if it was there."""
        response = self._client.post(
            f"{self.base}/bucket",
            json={"id": self.bucket, "name": self.bucket, "public": False, "file_size_limit": max_bytes, "allowed_mime_types": ["image/jpeg"]},
            headers=self._headers(),
        )
        if response.status_code in (400, 409) and "already exists" in response.text.lower():
            return False
        self._check(response, f"creating the bucket {self.bucket}")
        return True

    def put(self, path: str, data: bytes, content_type: str = "image/jpeg") -> None:
        """Upload (or replace: x-upsert) one object."""
        url = f"{self.base}/object/{self.bucket}/{quote(path)}"
        response = self._client.post(url, content=data, headers={**self._headers(content_type), "x-upsert": "true"})
        self._check(response, f"uploading {path}")

    def signed_urls(self, paths: list[str], seconds: int = SIGNED_URL_SECONDS) -> dict[str, str]:
        """Time-limited links to private objects: {path: URL}. A path that does not exist is left out."""
        if not paths:
            return {}
        response = self._client.post(f"{self.base}/object/sign/{self.bucket}", json={"expiresIn": seconds, "paths": paths}, headers=self._headers())
        urls: dict[str, str] = {}
        for item in self._check(response, "signing links").json():
            if item.get("signedURL") and item.get("path"):
                urls[item["path"]] = self.base + item["signedURL"]
        return urls


def project_url_from(database_url: str | None) -> str | None:
    """The project's address, https://<ref>.supabase.co, from a Supabase pooler database address (its user is postgres.<ref>); None if it has no such user."""
    ref = (urlparse(database_url or "").username or "").split(".", 1)[-1]
    return f"https://{ref}.supabase.co" if re.fullmatch(r"[a-z0-9]{20}", ref) else None


def from_env() -> SupabaseStore | None:
    """The store, from the environment or from .env.supabase.local; None if the service-role key is not there.

    The key is SUPABASE_SERVICE_ROLE_KEY (SUPABASE_SERVICE_KEY also works). The address is SUPABASE_URL, or else worked out from SUPABASE_DATABASE_URL,
    which is already in that file.
    """
    from dotenv import dotenv_values

    file_values = dotenv_values(KEY_FILE) if KEY_FILE.exists() else {}

    def setting(*names: str) -> str | None:
        for name in names:
            value = os.environ.get(name) or file_values.get(name)
            if value:
                return value
        return None

    key = setting("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY")
    url = setting("SUPABASE_URL") or project_url_from(setting("SUPABASE_DATABASE_URL"))
    return SupabaseStore(url, key) if url and key else None
