"""Orchestrates the full ingest pipeline from Drive sync to embedding."""

from db.connection import get_connection
from ingest.drive_sync import build_drive_service, iter_drive_pdfs
from ingest.ocr import get_or_ocr, detect_language
from ingest.chunker import split_into_chunks
from ingest.embedder import embed_and_store, update_document_status


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


def ingest_one(pdf_data: bytes, drive_file_id: str, filename: str) -> str:
    """Ingest one PDF; returns "ingested", "skipped" or "failed"."""
    doc_id = None
    try:
        with get_connection() as conn:
            doc_id = _insert_document(conn, filename, drive_file_id)
            if doc_id is None:
                print(f"[pipeline] {filename} — already ingested, skipping")
                return "skipped"
            conn.commit()

        pages = get_or_ocr(pdf_data, drive_file_id)

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
    service = build_drive_service()

    ingested = skipped = failed = 0

    # PDFs stream from Drive one at a time. A document that failed earlier is
    # retried when its PDF comes up: _insert_document resets it to 'processing'.
    for item in iter_drive_pdfs(service, skip_completed=True):
        result = ingest_one(item["bytes"], item["file_id"], item["filename"])
        del item  # frees the PDF bytes before the next one streams in
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
