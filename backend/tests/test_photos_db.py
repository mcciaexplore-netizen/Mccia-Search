"""photo_index and photo_scan on a scratch Postgres with pgvector (POOL_TEST_DATABASE_URL), in a database of their own."""

import os
from urllib.parse import urlparse

import numpy as np
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from db import connection
from photos import db as photo_db
from photos.boxes import Box
from photos.pipeline import Photo

URL = os.environ.get("POOL_TEST_DATABASE_URL", "")
_parsed = urlparse(URL)
LOCAL = bool(URL) and (_parsed.hostname in ("localhost", "127.0.0.1") or (_parsed.hostname is None and "host=/" in URL))
needs_scratch = pytest.mark.skipif(not LOCAL, reason="set POOL_TEST_DATABASE_URL to a scratch Postgres with pgvector on this machine")
TEST_DB = "photos_db_test"


@pytest.fixture
def db(monkeypatch):
    """A fresh database with Supabase's habits (the roles anon and authenticated, and default grants to them) and the real schema."""
    if not LOCAL:
        pytest.skip("no scratch database")
    with psycopg.connect(URL, autocommit=True) as admin:
        try:
            admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error:
            pytest.skip("this scratch Postgres has no pgvector")
        for role in ("anon", "authenticated"):
            admin.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN CREATE ROLE {role} NOLOGIN; END IF; END $$")
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    url = make_conninfo(**{**conninfo_to_dict(URL), "dbname": TEST_DB})
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated")  # what Supabase does for every new table
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated")
    monkeypatch.setattr(connection, "DATABASE_URL", url)
    connection.close_pool()
    connection.init_db()
    assert psycopg.connect(url).execute("SELECT to_regclass('photo_index')").fetchone()[0] is None  # the shared schema must not create them
    with connection.get_connection() as conn:
        photo_db.init_tables(conn)
    yield url
    connection.close_pool()
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


def one(url, query, params=None):
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute(query, params).fetchone()[0]


def add_document(url, name="1961-April Part-1.pdf") -> int:
    return one(url, "INSERT INTO documents (filename, drive_file_id, status) VALUES (%s, %s, 'completed') RETURNING id", (name, name))


def photo(index=0, kind="photo", faces=1, caption="Dr. Kirloskar", axis=0) -> Photo:
    vector = np.zeros(512, dtype=np.float32)
    vector[axis] = 1.0
    return Photo(index=index, box=Box(10, 20, 110, 220), fractions=(0.1, 0.2, 0.6, 0.7), kind=kind, kind_score=0.93, jpeg=b"JPEG" * 100, width=100, height=200,
                 vector=vector, faces=faces, caption=caption, whole_page=False)


@needs_scratch
def test_the_tables_are_closed_to_the_public_api_even_though_supabase_opens_new_tables(db):
    for table in ("photo_index", "photo_scan"):
        assert one(db, "SELECT relrowsecurity FROM pg_class WHERE oid = %s::regclass", (table,)) is True
        for role in ("anon", "authenticated"):
            assert one(db, "SELECT has_table_privilege(%s, %s, 'SELECT')", (role, table)) is False
            assert one(db, "SELECT has_table_privilege(%s, %s, 'INSERT')", (role, table)) is False
    assert one(db, "SELECT has_sequence_privilege('anon', 'photo_index_id_seq', 'USAGE')") is False


@needs_scratch
def test_starting_again_changes_nothing(db):
    connection.init_db()
    with connection.get_connection() as conn:
        photo_db.init_tables(conn)
        photo_db.init_tables(conn)
    assert one(db, "SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'photo_index'") == 4  # pkey, unique, page, clip


@needs_scratch
def test_a_page_is_saved_with_its_photos_and_marked_as_looked_at(db):
    doc = add_document(db)
    with connection.get_connection() as conn:
        paths = photo_db.save_page(conn, doc, 214, [photo(0), photo(1, kind="advertisement", faces=0, caption="")], seconds=3.2)
        conn.commit()
    assert paths == ["doc-%04d/p0214-0.jpg" % doc, "doc-%04d/p0214-1.jpg" % doc]
    rows = psycopg.connect(db, autocommit=True).execute(
        "SELECT box_index, kind, contains_face, face_count, description, storage_path, file_size, bbox, width FROM photo_index ORDER BY box_index").fetchall()
    assert rows[0][:5] == (0, "photo", True, 1, "Dr. Kirloskar") and rows[0][6] == 400 and rows[0][7] == pytest.approx([0.1, 0.2, 0.6, 0.7]) and rows[0][8] == 100
    assert rows[1][:5] == (1, "advertisement", False, 0, None)  # an empty caption is stored as NULL
    with connection.get_connection() as conn:
        assert photo_db.scanned_pages(conn, doc) == {214}


@needs_scratch
def test_a_page_with_no_photo_is_still_marked_so_it_is_not_looked_at_again(db):
    doc = add_document(db)
    with connection.get_connection() as conn:
        photo_db.save_page(conn, doc, 61, [], seconds=1.0)
        conn.commit()
        assert photo_db.scanned_pages(conn, doc) == {61}
    assert one(db, "SELECT COUNT(*) FROM photo_index") == 0 and one(db, "SELECT n_photos FROM photo_scan") == 0


@needs_scratch
def test_doing_a_page_again_replaces_its_rows_instead_of_doubling_them(db):
    doc = add_document(db)
    with connection.get_connection() as conn:
        photo_db.save_page(conn, doc, 5, [photo(0), photo(1)])
        photo_db.save_page(conn, doc, 5, [photo(0, kind="illustration")])
        conn.commit()
    assert one(db, "SELECT COUNT(*) FROM photo_index") == 1 and one(db, "SELECT kind FROM photo_index") == "illustration"
    assert one(db, "SELECT n_photos FROM photo_scan WHERE page_num = 5") == 1 and one(db, "SELECT COUNT(*) FROM photo_scan") == 1


@needs_scratch
def test_a_failure_in_the_middle_of_a_page_leaves_nothing_half_saved(db):
    doc = add_document(db)
    with connection.get_connection() as conn:
        photo_db.save_page(conn, doc, 7, [photo(0)])
        conn.commit()
    with connection.get_connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            photo_db.save_page(conn, doc, 7, [photo(0), photo(1, kind="cover")])  # a kind the table does not know
    assert one(db, "SELECT COUNT(*) FROM photo_index") == 1 and one(db, "SELECT kind FROM photo_index") == "photo"  # the earlier rows are still there
    assert one(db, "SELECT n_photos FROM photo_scan WHERE page_num = 7") == 1


@needs_scratch
def test_pictures_are_found_by_what_they_look_like(db):
    doc = add_document(db)
    with connection.get_connection() as conn:
        photo_db.save_page(conn, doc, 1, [photo(0, axis=0)])
        photo_db.save_page(conn, doc, 2, [photo(0, axis=1)])
        photo_db.save_page(conn, doc, 3, [photo(0, axis=2)])
        conn.commit()
    query = np.zeros(512, dtype=np.float32)
    query[1] = 1.0
    nearest = one(db, "SELECT page_num FROM photo_index ORDER BY clip_embedding <=> %s::vector LIMIT 1", ("[" + ",".join(map(str, query)) + "]",))
    assert nearest == 2


@needs_scratch
def test_removing_a_document_removes_its_photos_and_marks(db):
    doc = add_document(db)
    with connection.get_connection() as conn:
        photo_db.save_page(conn, doc, 1, [photo(0)])
        conn.commit()
    psycopg.connect(db, autocommit=True).execute("DELETE FROM documents WHERE id = %s", (doc,))
    assert one(db, "SELECT COUNT(*) FROM photo_index") == 0 and one(db, "SELECT COUNT(*) FROM photo_scan") == 0


@needs_scratch
def test_two_photos_cannot_share_a_page_and_number(db):
    doc = add_document(db)
    psycopg.connect(db, autocommit=True).execute("INSERT INTO photo_index (document_id, page_num, box_index, kind) VALUES (%s, 1, 0, 'photo')", (doc,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        psycopg.connect(db, autocommit=True).execute("INSERT INTO photo_index (document_id, page_num, box_index, kind) VALUES (%s, 1, 0, 'photo')", (doc,))
