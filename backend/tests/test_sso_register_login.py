"""Recording a hub sign-in: the token is spent once, the user is written, and it costs one round trip to the database.

Needs a scratch Postgres on this machine (never the real database); skipped unless one is given:

    POOL_TEST_DATABASE_URL=postgres://postgres@127.0.0.1:5432/pool_test uv run pytest tests/test_sso_register_login.py

It creates the two tables it needs (users, sso_used_jti) and drops them afterwards. Only localhost / 127.0.0.1 is accepted.
"""

import os
import threading
import time
from urllib.parse import urlparse

import psycopg
import pytest

import sso
from db import connection

URL = os.environ.get("POOL_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    urlparse(URL).hostname not in ("localhost", "127.0.0.1"),
    reason="set POOL_TEST_DATABASE_URL to a scratch Postgres on this machine (localhost or 127.0.0.1)",
)

DDL = """
DROP TABLE IF EXISTS sso_used_jti; DROP TABLE IF EXISTS users;
CREATE TABLE users (
  id BIGSERIAL PRIMARY KEY, hub_user_id TEXT NOT NULL UNIQUE, email TEXT NOT NULL, name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'admin')), first_seen TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE sso_used_jti (jti TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""


@pytest.fixture(autouse=True)
def scratch_tables(monkeypatch):
    monkeypatch.setattr(connection, "DATABASE_URL", URL)  # the module read the real URL from .env at import
    connection.close_pool()
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(DDL)
    monkeypatch.setattr(sso, "_last_cleanup", None, raising=False)  # (absent in the previous version of sso.py)
    yield
    connection.close_pool()
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute("DROP TABLE IF EXISTS sso_used_jti; DROP TABLE IF EXISTS users")


def login(jti="jti-00000001", uid="hub-1", name="Asha", role="user", exp=None):
    return sso.register_login(jti=jti, exp=exp or int(time.time()) + 60, hub_user_id=uid, email=f"{uid}@example.test", name=name, role=role)


def scalar(sql, *args):
    with psycopg.connect(URL) as conn:
        return conn.execute(sql, args).fetchone()[0]


def test_a_sign_in_creates_the_user_and_spends_the_token():
    result = login()
    assert (result["hub_user_id"], result["email"], result["name"], result["role"]) == ("hub-1", "hub-1@example.test", "Asha", "user")
    assert scalar("SELECT count(*) FROM users") == 1
    assert scalar("SELECT count(*) FROM sso_used_jti WHERE jti = 'jti-00000001'") == 1


def test_the_same_person_signing_in_again_updates_their_row():
    first = login("jti-00000001", name="Asha", role="user")
    second = login("jti-00000002", name="Asha K", role="admin")
    assert second["id"] == first["id"]
    assert (second["name"], second["role"]) == ("Asha K", "admin")
    assert scalar("SELECT count(*) FROM users") == 1


def test_a_token_that_was_used_is_refused_and_changes_nothing():
    login("jti-00000001", name="Asha", role="user")
    with pytest.raises(sso.TokenAlreadyUsed):
        login("jti-00000001", name="Changed", role="admin")
    assert scalar("SELECT name FROM users WHERE hub_user_id = 'hub-1'") == "Asha"
    assert scalar("SELECT role FROM users WHERE hub_user_id = 'hub-1'") == "user"


def test_when_the_user_part_fails_the_token_is_not_spent():
    with pytest.raises(psycopg.errors.CheckViolation):
        login("jti-00000001", role="superuser")  # not a role the table allows
    assert scalar("SELECT count(*) FROM sso_used_jti") == 0
    assert login("jti-00000001", role="user")["role"] == "user"  # the same token still works


def test_twelve_simultaneous_uses_of_one_token_have_exactly_one_winner():
    wins, refused, errors = [], [], []

    def go():
        try:
            login("jti-00000001", uid="hub-2")
            wins.append(1)
        except sso.TokenAlreadyUsed:
            refused.append(1)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))

    threads = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert errors == []
    assert (len(wins), len(refused)) == (1, 11)
    assert scalar("SELECT count(*) FROM users WHERE hub_user_id = 'hub-2'") == 1


def test_an_implausible_expiry_is_refused_before_the_database_is_asked():
    with pytest.raises(sso.TokenExpiryInvalid):
        login(exp=int(time.time()) - 3600)
    with pytest.raises(sso.TokenExpiryInvalid):
        login(exp=int(time.time()) + 3600)
    assert scalar("SELECT count(*) FROM sso_used_jti") == 0


def test_a_sign_in_is_one_round_trip(monkeypatch):
    login("jti-00000001")  # the first one also does the hourly sweep, and opens the pooled connection
    statements = []
    real = psycopg.Connection.execute

    def counting(self, query, *args, **kwargs):
        statements.append(str(query).split()[0] if not isinstance(query, bytes) else "?")
        return real(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", counting)
    login("jti-00000002", uid="hub-3")
    assert statements == ["WITH"]  # one statement: no BEGIN, no separate user insert, no COMMIT, no sweep


def test_old_spent_tokens_are_swept_but_at_most_once_an_hour():
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute("INSERT INTO sso_used_jti (jti, expires_at) VALUES ('old-1', now() - interval '3 days')")
    login("jti-00000001")  # the first sign-in after a start sweeps
    assert scalar("SELECT count(*) FROM sso_used_jti WHERE jti = 'old-1'") == 0
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute("INSERT INTO sso_used_jti (jti, expires_at) VALUES ('old-2', now() - interval '3 days')")
    login("jti-00000002", uid="hub-4")  # within the hour: no sweep
    assert scalar("SELECT count(*) FROM sso_used_jti WHERE jti = 'old-2'") == 1
