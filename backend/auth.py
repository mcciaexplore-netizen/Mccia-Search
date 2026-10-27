"""API keys: every route except /health needs one.

Two keys, sent by the web app's server and never by a browser:
  API_KEY        header X-API-Key    chat and the page images
  ADMIN_API_KEY  header X-Admin-Key  the document list, ingestion and the audit log
The server refuses to start without both (see check_configured), so a missing key can never
leave the API open.
"""

import re
import secrets
from typing import NamedTuple

from fastapi import Header, HTTPException

from config import ADMIN_API_KEY, API_KEY

MIN_KEY_LENGTH = 24  # `openssl rand -hex 32` makes 64


def check_configured() -> None:
    """Raise RuntimeError unless both keys are set, long enough, and different from each other."""
    problems = [
        f"{name} must be a random string of at least {MIN_KEY_LENGTH} characters"
        for name, value in (("API_KEY", API_KEY), ("ADMIN_API_KEY", ADMIN_API_KEY))
        if len(value) < MIN_KEY_LENGTH
    ]
    if API_KEY and API_KEY == ADMIN_API_KEY:
        problems.append("ADMIN_API_KEY must differ from API_KEY")
    if problems:
        raise RuntimeError("API keys are not configured: " + "; ".join(problems) + " (see .env.example)")


def _matches(sent: str | None, expected: str) -> bool:
    """Whether the key that was sent is the expected one, in constant time."""
    if not sent or not expected:
        return False
    return secrets.compare_digest(sent.encode("utf-8"), expected.encode("utf-8"))


def _refuse() -> HTTPException:
    # The same answer whether the key is missing, wrong or the wrong kind: it says nothing about which.
    return HTTPException(status_code=401, detail="A valid API key is required.", headers={"WWW-Authenticate": "ApiKey"})


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Dependency for the routes the chat uses."""
    if not _matches(x_api_key, API_KEY):
        raise _refuse()


def require_admin_key(x_admin_key: str | None = Header(default=None)) -> None:
    """Dependency for the document list, ingestion and the audit log."""
    if not _matches(x_admin_key, ADMIN_API_KEY):
        raise _refuse()


class User(NamedTuple):
    id: str
    email: str | None


_USER_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,100}$")
_EMAIL = re.compile(r"^[^\s<>\x00-\x1f]{3,254}$")


def require_user(
    x_user_id: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None),
) -> User:
    """Who is asking, as the web app's server says. Only meaningful behind require_api_key, which runs first on
    the routes that use both, so a caller without the key never gets this far. The web app sends the user it
    signed in; a request with no user id is refused."""
    if not x_user_id or not _USER_ID.match(x_user_id):
        raise HTTPException(status_code=400, detail="X-User-Id is required.")
    email = x_user_email.strip() if x_user_email else None
    return User(id=x_user_id, email=email if email and _EMAIL.match(email) else None)
