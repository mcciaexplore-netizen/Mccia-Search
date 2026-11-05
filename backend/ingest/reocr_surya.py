"""Re-OCR the PDFs that are not finished with Surya, and save their text (status 'text_saved'). Never touches a completed document.

    uv run python -m ingest.reocr_surya --list                                  # what it would do, nothing is changed
    uv run python -m ingest.reocr_surya --start-year 2003 --end-year 2017       # on the Mac
    uv run python -m ingest.reocr_surya --start-year 1955 --end-year 2002       # on a second machine

Needs `uv sync --extra surya` and llama.cpp (`brew install llama.cpp`). When the OCR is finished, add the embeddings and make the documents
searchable with:  uv run python -m ingest.embed_pending

Which documents: the ones that are 'failed' (or were left 'processing' or 'pending' by a run that stopped) and whose first year is in the range,
oldest first. A 'completed' document is LIVE (people search it right now): replacing its text would make it unsearchable until it is embedded
again, so it is never selected, whatever the range. (The database does not record which engine read a document, so "the Tesseract ones" cannot
be told apart from the rest. Re-reading live documents needs a swap that keeps the old text searchable until the new one is ready: not built.)

Safe to stop (Ctrl-C) and run again. The pages already read are kept on disk, and the PDF in progress is picked up where it stopped.

Two machines, no overlap: give them different year ranges. As a second guard, whoever starts a PDF takes a database lock on it (released when
that run ends or dies), so a PDF that another machine is reading is skipped even if the ranges overlap, and one it has already finished
is not read again.

Speed (Apple M2, 5 Nov 2026, Marathi pages): about 100 s per page alone and 44 s per page with four at once (OCR_CONCURRENCY=4, the default):
more than 4 days of non-stop running for the 8,788 pages of 2003-2017. Tesseract reads the same pages in about 3.5 s each. Check that this
is worth it for the PDFs you select (--list shows their page counts) before starting.
"""

import argparse
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from config import OCR_CACHE_DIR
from db.connection import get_connection

LOCK_CLASS = 4207  # the first half of the database lock key; the second half is the document's id
HEARTBEAT_SECONDS = 30  # how often the lock's connection is touched so that nothing in between drops it
SURYA_CACHE_SUBDIR = "surya"  # Surya's pages are cached apart from the other engines': the cache is keyed by PDF alone
UNFINISHED = ("failed", "processing", "pending")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start-year", type=int, default=0, help="first year to include (by the first year in the file name)")
    parser.add_argument("--end-year", type=int, default=9998, help="last year to include")
    parser.add_argument("--list", action="store_true", help="only list the PDFs it would read")
    args = parser.parse_args(argv)
    if args.start_year > args.end_year:
        parser.error(f"--start-year {args.start_year} is after --end-year {args.end_year}")

    documents = select_documents(args.start_year, args.end_year)
    pages = sum(d["total_pages"] or 0 for d in documents)
    print(f"{len(documents)} unfinished PDFs in {args.start_year}-{args.end_year} ({pages} pages known); completed documents are never touched")
    for d in documents:
        print(f"  {d['status']:10s} {d['total_pages'] or '?':>5} pages  {d['filename']}")
    if args.list or not documents:
        return 0

    os.environ["OCR_ENGINE"] = "surya"
    from ingest import ocr
    from ingest.drive_sync import build_drive_service, list_pdfs, stream_pdf
    from ingest.pipeline import ingest_one

    ocr.OCR_ENGINE = "surya"
    ocr.OCR_CACHE_DIR = Path(OCR_CACHE_DIR) / SURYA_CACHE_SUBDIR  # never reuse another engine's text as Surya's
    ocr._prepare_engine()  # a missing install or model stops the run here, before any download

    service = build_drive_service()
    on_drive = {pdf["file_id"]: pdf for pdf in list_pdfs(service)}
    counts = {"text_saved": 0, "skipped": 0, "failed": 0, "busy": 0, "missing": 0}
    try:
        for number, doc in enumerate(documents, start=1):
            print(f"[{number}/{len(documents)}] {doc['filename']}")
            pdf = on_drive.get(doc["drive_file_id"])
            if pdf is None:
                print("  not on Google Drive any more: skipped")
                counts["missing"] += 1
                continue
            with claim(doc["id"]) as got:
                if not got:
                    print("  another run is reading this PDF: skipped")
                    counts["busy"] += 1
                    continue
                if _status(doc["id"]) not in UNFINISHED:  # finished by someone else since the list was made
                    print("  already done meanwhile: skipped")
                    counts["skipped"] += 1
                    continue
                try:
                    data = stream_pdf(service, pdf["file_id"], pdf["name"], pdf["size"])
                except Exception as exc:  # one PDF that cannot be downloaded must not stop the rest
                    print(f"  download FAILED: {exc}")
                    counts["failed"] += 1
                    continue
                result = ingest_one(data, pdf["file_id"], pdf["name"], embed=False)
                del data
            counts[result] = counts.get(result, 0) + 1
            print(f"  {result}")
    except KeyboardInterrupt:
        print("Interrupted: the pages already read are kept, run it again to carry on.")
        _summary(counts)
        return 130

    _summary(counts)
    return 1 if counts["failed"] else 0


def select_documents(start_year: int, end_year: int) -> list[dict]:
    """The unfinished documents whose first year is in the range, oldest first. A completed document is never one of them."""
    from ingest.drive_sync import _extract_year

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, filename, drive_file_id, status, total_pages FROM documents WHERE status = ANY(%s)", (list(UNFINISHED),)
        ).fetchall()
    documents = [
        {"id": r[0], "filename": r[1], "drive_file_id": r[2], "status": r[3], "total_pages": r[4]}
        for r in rows
        if start_year <= _extract_year(r[1]) <= end_year
    ]
    documents.sort(key=lambda d: (_extract_year(d["filename"]), d["filename"]))
    return documents


@contextmanager
def claim(document_id: int):
    """Take the database lock on one document: yields True if this run now owns it, False if another run does.

    A session lock on a connection of its own, held while the document is read and released when the block ends, or by the database if
    this process dies or loses its connection. The connection is touched every HEARTBEAT_SECONDS so that an idle-connection timeout
    in front of the database does not take the lock away in the middle of a long document.
    """
    with get_connection() as conn:
        conn.autocommit = True
        got = conn.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_CLASS, document_id)).fetchone()[0]
        stop = threading.Event()
        beat = None
        if got:
            def keep_alive() -> None:
                while not stop.wait(HEARTBEAT_SECONDS):
                    try:
                        conn.execute("SELECT 1")
                    except Exception:  # the lock is gone with the connection; the work itself carries on
                        return

            beat = threading.Thread(target=keep_alive, name=f"lock-{document_id}", daemon=True)
            beat.start()
        try:
            yield got
        finally:
            stop.set()
            if beat is not None:
                beat.join(timeout=5)
            if got:
                try:
                    conn.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_CLASS, document_id))
                except Exception:
                    pass  # closing the connection releases it anyway


def _status(document_id: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT status FROM documents WHERE id = %s", (document_id,)).fetchone()
    return row[0] if row else None


def _summary(counts: dict[str, int]) -> None:
    print(
        f"Finished: {counts['text_saved']} text saved, {counts['skipped']} already done, {counts['busy']} being read by another run, "
        f"{counts['missing']} gone from Drive, {counts['failed']} failed"
    )
    if counts["text_saved"]:
        print("They cannot be searched until embedded. When all OCR is done, run: uv run python -m ingest.embed_pending")


if __name__ == "__main__":
    sys.exit(main())
