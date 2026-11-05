"""Saving what the pipeline found: photo_index rows and photo_scan marks, one page at a time, so a stopped run carries on where it stopped."""

from pathlib import Path

from pgvector import Vector

from photos.store import storage_path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def init_tables(conn) -> None:
    """Create photo_index and photo_scan (and close them to Supabase's public API) if they are not there; safe to repeat. Not called at backend start-up."""
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def scanned_pages(conn, document_id: int) -> set[int]:
    return {row[0] for row in conn.execute("SELECT page_num FROM photo_scan WHERE document_id = %s", (document_id,)).fetchall()}


def save_page(conn, document_id: int, page_number: int, photos, seconds: float | None = None) -> list[str]:
    """Record one page: its photographs (replacing any earlier rows for the page) and the fact that it was looked at. Returns the storage paths.

    One transaction: either the page's rows and its scan mark are all there, or none of them.
    """
    paths = []
    with conn.transaction():
        conn.execute("DELETE FROM photo_index WHERE document_id = %s AND page_num = %s", (document_id, page_number))
        for photo in photos:
            path = storage_path(document_id, page_number, photo.index)
            paths.append(path)
            conn.execute(
                """
                INSERT INTO photo_index (document_id, page_num, box_index, kind, kind_score, storage_path, clip_embedding, description,
                                         contains_face, face_count, whole_page, bbox, width, height, file_size)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    document_id, page_number, photo.index, photo.kind, photo.kind_score, path, Vector(photo.vector.tolist()).to_text(),
                    photo.caption or None, photo.faces > 0, photo.faces, photo.whole_page, list(photo.fractions), photo.width, photo.height, len(photo.jpeg),
                ),
            )
        conn.execute(
            "INSERT INTO photo_scan (document_id, page_num, n_photos, seconds) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (document_id, page_num) DO UPDATE SET n_photos = EXCLUDED.n_photos, seconds = EXCLUDED.seconds, scanned_at = now()",
            (document_id, page_number, len(photos), seconds),
        )
    return paths
