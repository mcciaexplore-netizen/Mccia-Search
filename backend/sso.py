"""Single sign-on bookkeeping: record a verified hub sign-in, and refuse a token that was already used."""

import time

from db.connection import get_pooled_connection

MAX_TOKEN_LIFETIME_SECONDS = 300  # a hub token lives 60 seconds; anything much longer is not one of ours
EXPIRED_GRACE_SECONDS = 60


class TokenAlreadyUsed(Exception):
    """This token's id (jti) was recorded before."""


class TokenExpiryInvalid(ValueError):
    """The expiry is already long past, or implausibly far away."""


def register_login(*, jti: str, exp: int, hub_user_id: str, email: str, name: str, role: str) -> dict:
    """Spend the token and create or update the user, in one transaction.

    The token's id is inserted into a table where it is the primary key, so two requests with the same token can
    never both succeed: the second one inserts nothing and is refused. If anything fails afterwards, the insert is
    rolled back with it and the token is still unused.
    """
    now = int(time.time())
    if exp < now - EXPIRED_GRACE_SECONDS or exp > now + MAX_TOKEN_LIFETIME_SECONDS:
        raise TokenExpiryInvalid("exp is not a plausible expiry for a sign-in token")
    with get_pooled_connection() as conn:
        # Pooled connections commit every statement on its own (autocommit), so the three statements are wrapped in one
        # transaction explicitly: all of them happen, or none do.
        with conn.transaction():
            spent = conn.execute(
                "INSERT INTO sso_used_jti (jti, expires_at) VALUES (%s, to_timestamp(%s)) ON CONFLICT (jti) DO NOTHING",
                (jti, exp),
            )
            if spent.rowcount == 0:
                raise TokenAlreadyUsed(jti)
            row = conn.execute(
                """
                INSERT INTO users (hub_user_id, email, name, role) VALUES (%s, %s, %s, %s)
                ON CONFLICT (hub_user_id) DO UPDATE
                  SET email = EXCLUDED.email, name = EXCLUDED.name, role = EXCLUDED.role, last_seen = now()
                RETURNING id, hub_user_id, email, name, role, first_seen, last_seen
                """,
                (hub_user_id, email, name, role),
            ).fetchone()
            # Spent tokens are only needed until they expire; keep the table small.
            conn.execute("DELETE FROM sso_used_jti WHERE expires_at < now() - interval '1 day'")
    return {
        "id": row[0], "hub_user_id": row[1], "email": row[2], "name": row[3], "role": row[4],
        "first_seen": row[5].isoformat(), "last_seen": row[6].isoformat(),
    }
