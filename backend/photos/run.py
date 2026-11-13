"""The full photo extraction: every page of every PDF, photographs cut out, typed, embedded and stored (bucket first, then the database).

    uv run python -m photos.run                              # all documents not yet finished
    uv run python -m photos.run --documents 12 40            # only these document ids
    uv run python -m photos.run --start-year 1961 --end-year 1965

Needs the Supabase service-role key (.env.supabase.local) and a machine with a GPU for speed: about 4 s a page on an Apple M2 (the same work takes 39 s on the
server's CPU). It can be stopped (Ctrl-C) and started again at any time: a page is recorded only after its photographs are in the bucket, and recorded
pages are skipped. Two runs at once are kept off the same document by a database lock, so they share the work instead of repeating it.
"""

import argparse
import sys
import time
from collections import deque
from pathlib import Path

import pymupdf

from photos.db import init_tables, scanned_pages
from photos.pipeline import CUT_DPI, DETECT_DPI, process_page
from photos.render import render_page
from photos.upload import store_page

PHOTO_LOCK_CLASS = 4208  # the first half of the database lock key (4207 is the Surya run's); the second half is the document id
MAX_CONSECUTIVE_FAILURES = 25  # this many pages in a row failing means something is wrong (storage, network, a model): stop instead of working through all
PROGRESS_EVERY = 10  # pages between progress lines
RECENT = 50  # pages the speed (and so the ETA) is averaged over


class RunAborted(RuntimeError):
    """Too many pages failed in a row."""


class Progress:
    """Counts what has been done and estimates what is left from the recent speed."""

    def __init__(self, total_pages: int):
        self.total_pages = total_pages
        self.pages = self.photos = self.failures = 0
        self._recent: deque[float] = deque(maxlen=RECENT)

    def page_done(self, seconds: float, photos: int) -> None:
        self.pages += 1
        self.photos += photos
        self._recent.append(seconds)

    def seconds_per_page(self) -> float | None:
        return sum(self._recent) / len(self._recent) if self._recent else None

    def eta(self) -> str:
        speed = self.seconds_per_page()
        if speed is None:
            return "measuring"
        minutes = round(max(self.total_pages - self.pages, 0) * speed / 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def fetch_pdf(service, pdf: dict, staging: Path) -> Path:
    """The PDF as a file in the staging folder (kept if it is already there in full), so a gigabyte is never held in memory while pages are read."""
    from ingest.drive_sync import stream_pdf

    target = staging / f"{pdf['file_id']}.pdf"
    if target.exists() and target.stat().st_size == int(pdf["size"] or -1):
        return target
    staging.mkdir(parents=True, exist_ok=True)
    data = stream_pdf(service, pdf["file_id"], pdf["name"], pdf["size"])
    tmp = target.with_suffix(".part")
    tmp.write_bytes(data)
    del data
    tmp.replace(target)
    return target


def process_document(conn_factory, store, models, doc: dict, pdf_path: Path, progress: Progress, say=print, max_pages: int | None = None) -> None:
    """Every page of one PDF that has not been recorded yet. A page that fails is reported and left for the next run; many in a row abort the run."""
    consecutive = 0
    with conn_factory() as conn:
        done = scanned_pages(conn, doc["id"])
    with pymupdf.open(pdf_path) as pdf:
        total = pdf.page_count
        todo = [n for n in range(1, total + 1) if n not in done]
        if max_pages is not None:
            todo = todo[:max_pages]
        say(f"[photos] {doc['filename']}: {total} pages, {len(done)} already done, {len(todo)} to do")
        for number in todo:
            started = time.monotonic()
            try:
                page = pdf[number - 1]
                photos = process_page(render_page(page, DETECT_DPI), render_page(page, CUT_DPI), models)
                with conn_factory() as conn:
                    store_page(conn, store, doc["id"], number, photos, seconds=time.monotonic() - started)
                    conn.commit()
            except Exception as exc:  # one bad page must not end a run that takes days
                progress.failures += 1
                consecutive += 1
                say(f"[photos] {doc['filename']} page {number}: FAILED {type(exc).__name__}: {str(exc)[:160]}")
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    raise RunAborted(f"{consecutive} pages in a row failed; the last was {doc['filename']} page {number}") from exc
                continue
            consecutive = 0
            progress.page_done(time.monotonic() - started, len(photos))
            if progress.pages % PROGRESS_EVERY == 0:
                say(f"[photos] {doc['filename'][:34]} — page {number}/{total} — {progress.photos} photos so far — "
                    f"{progress.seconds_per_page():.1f} s/page — ETA {progress.eta()}")


def select_documents(conn, ids: list[int] | None, start_year: int, end_year: int) -> list[dict]:
    """The documents to look at, oldest first: (id, filename, drive_file_id, total_pages, pages already recorded)."""
    from ingest.drive_sync import _extract_year

    rows = conn.execute(
        "SELECT d.id, d.filename, d.drive_file_id, d.total_pages, "
        "(SELECT COUNT(*) FROM photo_scan s WHERE s.document_id = d.id) FROM documents d WHERE d.status = 'completed'"
    ).fetchall()
    docs = [{"id": r[0], "filename": r[1], "drive_file_id": r[2], "total_pages": r[3] or 0, "scanned": r[4]} for r in rows]
    docs = [d for d in docs if (ids is None or d["id"] in ids) and start_year <= _extract_year(d["filename"]) <= end_year]
    docs.sort(key=lambda d: (_extract_year(d["filename"]), d["filename"]))
    return docs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--documents", type=int, nargs="*", help="only these document ids")
    parser.add_argument("--start-year", type=int, default=0)
    parser.add_argument("--end-year", type=int, default=9998)
    parser.add_argument("--keep-pdfs", action="store_true", help="keep each downloaded PDF in the staging folder instead of deleting it when it is done")
    parser.add_argument("--staging", default=None, help="folder for the downloaded PDFs (default: staging/photos_pdfs in the project)")
    args = parser.parse_args(argv)

    from config import PROJECT_ROOT
    from db.connection import get_connection
    from ingest.drive_sync import build_drive_service, list_pdfs
    from ingest.reocr_surya import claim
    from photos.pipeline import default_models
    from photos.store import from_env

    store = from_env()
    if store is None:
        print("No Supabase service-role key found (SUPABASE_SERVICE_ROLE_KEY in .env.supabase.local).")
        return 2
    staging = Path(args.staging).expanduser() if args.staging else PROJECT_ROOT / "staging" / "photos_pdfs"
    with get_connection() as conn:
        init_tables(conn)
        print("[photos] bucket created" if store.ensure_bucket() else "[photos] bucket ready")
        docs = select_documents(conn, args.documents, args.start_year, args.end_year)
    todo_pages = sum(max(d["total_pages"] - d["scanned"], 0) for d in docs)
    print(f"[photos] {len(docs)} documents, about {todo_pages} pages still to look at", flush=True)
    if not docs:
        return 0

    models = default_models()
    service = build_drive_service()
    on_drive = {pdf["file_id"]: pdf for pdf in list_pdfs(service)}
    progress = Progress(todo_pages)
    started = time.monotonic()
    try:
        for number, doc in enumerate(docs, start=1):
            print(f"[photos] document {number}/{len(docs)}", flush=True)
            if doc["total_pages"] and doc["scanned"] >= doc["total_pages"]:
                print(f"[photos] {doc['filename']}: all {doc['total_pages']} pages already done")
                continue
            pdf = on_drive.get(doc["drive_file_id"])
            if pdf is None:
                print(f"[photos] {doc['filename']}: not on Google Drive any more, skipped")
                continue
            with claim(doc["id"], PHOTO_LOCK_CLASS) as got:
                if not got:
                    print(f"[photos] {doc['filename']}: another run is on it, skipped")
                    continue
                path = fetch_pdf(service, pdf, staging)
                try:
                    process_document(get_connection, store, models, doc, path, progress)
                finally:
                    if not args.keep_pdfs:
                        path.unlink(missing_ok=True)
    except KeyboardInterrupt:
        print("[photos] interrupted: everything recorded is kept; run it again to carry on", flush=True)
        return 130
    except RunAborted as exc:
        print(f"[photos] ABORTED: {exc}", flush=True)
        return 1
    print(f"[photos] finished in {(time.monotonic() - started) / 3600:.1f} h: {progress.pages} pages, {progress.photos} photos, {progress.failures} pages failed (run again to retry those)")
    return 1 if progress.failures else 0


if __name__ == "__main__":
    sys.exit(main())
