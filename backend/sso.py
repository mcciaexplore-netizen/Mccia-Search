"""Single sign-on bookkeeping: record a verified hub sign-in, and refuse a token that was already used."""

import threading
import time

import psycopg

from db.connection import get_pooled_connection

MAX_TOKEN_LIFETIME_SECONDS = 300  # a hub token lives 60 seconds; anything much longer is not one of ours
EXPIRED_GRACE_SECONDS = 60
CLEANUP_EVERY_SECONDS = 3600  # spent tokens are swept this often at most, not on every sign-in

_last_cleanup: float | None = None
_cleanup_lock = threading.Lock()


class TokenAlreadyUsed(Exception):
    """This token's id (jti) was recorded before."""


class TokenExpiryInvalid(ValueError):
    """The expiry is already long past, or implausibly far away."""


def register_login(*, jti: str, exp: int, hub_user_id: str, email: str, name: str, role: str) -> dict:
    """Spend the token and create or update the user, in one statement.

    The token's id is inserted into a table where it is the primary key, so two requests with the same token can
    never both succeed: the second one inserts nothing, so the user part never runs, and it is refused.

    It is ONE statement on purpose: every round trip to the database costs about 0.2 s from here to Neon, and a person is
    waiting at a "signing you in" screen. One statement is atomic by itself (autocommit), so the guarantees are the same as
    with an explicit transaction: if the user part fails (say, a bad role), the token is not spent and can be used again.
    """
    now = int(time.time())
    if exp < now - EXPIRED_GRACE_SECONDS or exp > now + MAX_TOKEN_LIFETIME_SECONDS:
        raise TokenExpiryInvalid("exp is not a plausible expiry for a sign-in token")
    with get_pooled_connection() as conn:
        row = conn.execute(
            """
            WITH spent AS (
              INSERT INTO sso_used_jti (jti, expires_at) VALUES (%s, to_timestamp(%s))
              ON CONFLICT (jti) DO NOTHING
              RETURNING jti
            )
            INSERT INTO users (hub_user_id, email, name, role)
            SELECT %s, %s, %s, %s WHERE EXISTS (SELECT 1 FROM spent)
            ON CONFLICT (hub_user_id) DO UPDATE
              SET email = EXCLUDED.email, name = EXCLUDED.name, role = EXCLUDED.role, last_seen = now()
            RETURNING id, hub_user_id, email, name, role, first_seen, last_seen
            """,
            (jti, exp, hub_user_id, email, name, role),
        ).fetchone()
        if row is None:  # the token was spent before, so nothing was written
            raise TokenAlreadyUsed(jti)
        _sweep_spent_tokens(conn)
    return {
        "id": row[0], "hub_user_id": row[1], "email": row[2], "name": row[3], "role": row[4],
        "first_seen": row[5].isoformat(), "last_seen": row[6].isoformat(),
    }


def _sweep_spent_tokens(conn: psycopg.Connection) -> None:
    """Spent tokens are only needed until they expire; keep the table small, but not at the price of a round trip per sign-in."""
    global _last_cleanup
    now = time.monotonic()
    with _cleanup_lock:
        if _last_cleanup is not None and now - _last_cleanup < CLEANUP_EVERY_SECONDS:
            return
        _last_cleanup = now
    try:
        conn.execute("DELETE FROM sso_used_jti WHERE expires_at < now() - interval '1 day'")
    except psycopg.Error:
        pass  # a failed sweep must not fail a sign-in that already succeeded; the next hour tries again
