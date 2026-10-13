"""Orchestrates the full ingest pipeline from Drive sync to embedding."""

import threading
from queue import Empty, Full, Queue

from db.connection import get_connection
from ingest.drive_sync import (
    BYTES_PER_MB,
    build_drive_service,
    get_completed_drive_ids,
    list_pdfs,
    stream_pdf,
)
from ingest.ocr import get_or_ocr, detect_language
from ingest.chunker import split_into_chunks
from ingest.embedder import embed_and_store, update_document_status

# Finished downloads that may wait in the queue while OCR catches up. The queue is
# bounded so a slow OCR cannot let the downloader fill memory with PDFs: at most
# this many wait, plus the one being downloaded and the one being processed.
DOWNLOAD_PREFETCH = 2


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

        pages, failed_pages = get_or_ocr(pdf_data, drive_file_id)
        if failed_pages:
            # The pages that did work are in the OCR cache, so the next sync only has
            # to redo these. Ingesting now would leave a permanent hole in the document.
            shown = sorted(failed_pages)
            listing = ", ".join(map(str, shown[:20])) + (
                f", … (+{len(shown) - 20} more)" if len(shown) > 20 else ""
            )
            print(
                f"[pipeline] {filename} — {len(failed_pages)} pages failed OCR: [{listing}]"
            )
            update_document_status(doc_id, "failed")
            return "failed"

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


def _put(out_queue: Queue, item: dict | None, stop: threading.Event) -> bool:
    """Put `item` on a full-capable queue, waiting for room; False if `stop` was set first."""
    while not stop.is_set():
        try:
            out_queue.put(item, timeout=0.5)
            return True
        except Full:
            continue
    return False


def _download_worker(
    service, todo: list[dict], out_queue: Queue, stop: threading.Event | None = None
) -> None:
    """Stream each PDF in `todo` into memory and queue it for OCR, then queue None.

    Items are {file_id, filename, size, bytes}, or {file_id, filename, error} for a
    PDF that could not be streamed. The queue is bounded, so this waits whenever
    OCR is behind. It returns early if `stop` is set.
    """
    stop = stop or threading.Event()
    try:
        for index, pdf in enumerate(todo, start=1):
            if stop.is_set():
                return
            filename = pdf["name"]
            print(f"[drive] {index}/{len(todo)} — {filename} ({(pdf['size'] or 0) / BYTES_PER_MB:.1f} MB)")
            try:
                data = stream_pdf(service, pdf["file_id"], filename, pdf["size"])
            except Exception as exc:  # one bad file must not stop the run
                item = {"file_id": pdf["file_id"], "filename": filename, "error": str(exc)}
            else:
                item = {
                    "file_id": pdf["file_id"],
                    "filename": filename,
                    "size": pdf["size"] if pdf["size"] is not None else len(data),
                    "bytes": data,
                }
                del data
            if not _put(out_queue, item, stop):
                return
            del item  # the consumer now holds the only reference to the PDF bytes
    finally:
        _put(out_queue, None, stop)  # always tell the consumer there is no more


def _drain(out_queue: Queue) -> None:
    """Throw away anything still queued, freeing the PDFs and unblocking the producer."""
    while True:
        try:
            out_queue.get_nowait()
        except Empty:
            return


def run_pipeline() -> dict:
    service = build_drive_service()

    completed_ids = get_completed_drive_ids()
    all_pdfs = list_pdfs(service)
    to_process = [pdf for pdf in all_pdfs if pdf["file_id"] not in completed_ids]
    skipped = len(all_pdfs) - len(to_process)  # completed: never downloaded
    ingested = failed = received = 0
    print(f"[pipeline] {len(to_process)} to process, {skipped} already done")

    # A downloader thread streams the next PDFs from Drive while this thread runs OCR
    # on the current one. A document that failed earlier is retried when its PDF comes
    # up: _insert_document resets it to 'processing'.
    out_queue: Queue = Queue(maxsize=DOWNLOAD_PREFETCH)
    stop = threading.Event()
    downloader = threading.Thread(
        target=_download_worker,
        args=(service, to_process, out_queue, stop),
        daemon=True,
        name="pdf-downloader",
    )
    downloader.start()
    try:
        while True:
            item = out_queue.get()
            if item is None:
                break
            received += 1
            if "error" in item:
                print(f"[pipeline] {item['filename']} — download FAILED: {item['error']}")
                failed += 1
                continue
            result = ingest_one(item["bytes"], item["file_id"], item["filename"])
            del item  # frees the PDF bytes before the next one is taken
            if result == "ingested":
                ingested += 1
            elif result == "skipped":
                skipped += 1
            elif result == "failed":
                failed += 1
        downloader.join()
        if received < len(to_process):  # the downloader died: don't pass off a partial run as complete
            lost = len(to_process) - received
            print(f"[pipeline] WARNING: the downloader stopped early; {lost} PDF(s) were not processed")
            failed += lost
    finally:
        stop.set()  # an error or Ctrl+C: tell the downloader to stop and free its queue
        _drain(out_queue)

    print(
        f"[pipeline] complete: {ingested} ingested, "
        f"{skipped} skipped (already done), {failed} failed"
    )
    return {"ingested": ingested, "skipped": skipped, "failed": failed}
