"""Orchestrates the full ingest pipeline from Drive sync to embedding."""

import json
from pathlib import Path

from db.connection import get_connection
from ingest.drive_sync import sync_drive
from ingest.ocr import get_or_ocr, detect_language
from ingest.chunker import split_into_chunks
from ingest.embedder import embed_and_store, update_document_status
from config import STAGING_DIR


def _insert_document(conn, filename: str, drive_file_id: str) -> int | None:
    # New document: inserted. Previously failed: reset to 'processing' for a retry.
    # Completed or processing: the WHERE skips the update, so no row is returned.
    row = conn.execute(
        """
        INSERT INTO documents (filename, drive_file_id, status)
        VALUES (%s, %s, 'processing')
        ON CONFLICT (drive_file_id) DO UPDATE
          SET status = 'processing'
          WHERE documents.status IN ('failed')
        RETURNING id
        """,
        (filename, drive_file_id),
    ).fetchone()
    return row[0] if row else None


def _update_document(conn, doc_id: int, total_pages: int, language: str) -> None:
    conn.execute(
        "UPDATE documents SET total_pages = %s, language = %s WHERE id = %s",
        (total_pages, language, doc_id),
    )


def ingest_one(pdf_path: Path, drive_file_id: str, filename: str) -> str:
    """Ingest one PDF; returns "ingested", "skipped" or "failed"."""
    doc_id = None
    try:
        with get_connection() as conn:
            doc_id = _insert_document(conn, filename, drive_file_id)
            if doc_id is None:
                print(f"[pipeline] {filename} — already ingested, skipping")
                return "skipped"
            conn.commit()

        pages = get_or_ocr(pdf_path, drive_file_id)

        first_text = next(
            (p["text"] for p in pages if p.get("text", "").strip()), ""
        )
        language = detect_language(first_text)

        with get_connection() as conn:
            _update_document(conn, doc_id, len(pages), language)
            conn.commit()

        chunks = split_into_chunks(pages, doc_id, filename)
        stored = embed_and_store(chunks)
        update_document_status(doc_id, "completed")

        print(f"[pipeline] {filename} — done ({stored} chunks)")
        return "ingested"

    except Exception as exc:
        print(f"[pipeline] {filename} — FAILED: {exc}")
        if doc_id is not None:
            try:
                update_document_status(doc_id, "failed")
            except Exception:
                pass
        return "failed"


def run_pipeline() -> dict:
    new_paths = sync_drive()

    drive_ids_path = Path(STAGING_DIR) / ".drive_ids.json"
    try:
        drive_ids: dict = json.loads(drive_ids_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        drive_ids = {}

    ingested = skipped = failed = 0

    # Documents that failed earlier are retried first, if their PDF is still in
    # staging. They are not in new_paths: sync_drive only returns new downloads.
    with get_connection() as conn:
        failed_docs = conn.execute(
            "SELECT filename, drive_file_id FROM documents WHERE status = 'failed'"
        ).fetchall()

    to_process: list[tuple[Path, str, str]] = []
    for filename, drive_file_id in failed_docs:
        pdf_path = Path(STAGING_DIR) / filename
        if not pdf_path.is_file():
            print(
                f"[pipeline] WARNING: {filename} is failed but PDF not in "
                "staging, skipping retry"
            )
            skipped += 1
            continue
        to_process.append((pdf_path, drive_file_id, filename))
    if to_process:
        print(f"[pipeline] retrying {len(to_process)} failed document(s)")

    retrying = {filename for _, _, filename in to_process}
    for pdf_path in new_paths:
        filename = pdf_path.name
        if filename in retrying:
            continue  # re-downloaded, but already queued as a retry
        drive_file_id = drive_ids.get(filename)
        if not drive_file_id:
            print(f"[pipeline] WARNING: no Drive ID for {filename}, skipping")
            skipped += 1
            continue
        to_process.append((pdf_path, drive_file_id, filename))

    for pdf_path, drive_file_id, filename in to_process:
        result = ingest_one(pdf_path, drive_file_id, filename)
        if result == "ingested":
            ingested += 1
        elif result == "skipped":
            skipped += 1
        elif result == "failed":
            failed += 1

    print(
        f"[pipeline] complete: {ingested} ingested, "
        f"{skipped} skipped, {failed} failed"
    )
    return {"ingested": ingested, "skipped": skipped, "failed": failed}
