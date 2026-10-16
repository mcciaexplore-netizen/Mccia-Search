"""OCR the Drive PDFs of a range of years with a chosen engine, and save their text.

No embedding model is called: the text is saved without embeddings, so with a free OCR
engine this costs nothing and uses no Gemini quota. When the OCR is finished, add the
embeddings once with:  uv run python -m ingest.embed_pending

Several of these can run at once, one per range of years, each with its own engine:

    uv run python -m ingest.parallel_ingest --engine tesseract --start-year 1945 --end-year 1970
    uv run python -m ingest.parallel_ingest --engine easyocr --start-year 1971 --end-year 1990

Keep the ranges apart, and do not press Sync Drive while workers are running: the
pipeline's one-run-at-a-time lock only works inside one process, so two runs that reach
the same PDF would both OCR it.
"""

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--engine", required=True, help="gemini, tesseract, easyocr or paddle")
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    args = parser.parse_args(argv)
    engine = args.engine.strip().lower()

    # The engine is read once, when ingest.ocr is first imported, so it is set before any
    # ingest module is imported. ocr.OCR_ENGINE is set again below in case one already was.
    os.environ["OCR_ENGINE"] = engine
    from db.connection import init_db
    from ingest import ocr
    from ingest.drive_sync import _extract_year, build_drive_service, get_completed_drive_ids, list_pdfs, stream_pdf
    from ingest.pipeline import ingest_one

    if engine not in ocr.ENGINES:
        parser.error(f"unknown engine {args.engine!r}; use one of: {', '.join(ocr.ENGINES)}")
    if args.start_year > args.end_year:
        parser.error(f"--start-year {args.start_year} is after --end-year {args.end_year}")
    ocr.OCR_ENGINE = engine
    ocr._prepare_engine()  # a missing install or model stops the run here, before any download
    init_db()  # chunks may be saved without an embedding, which an older database does not allow

    service = build_drive_service()
    completed = get_completed_drive_ids()
    pdfs = [
        pdf
        for pdf in list_pdfs(service)
        if args.start_year <= _extract_year(pdf["name"]) <= args.end_year
        and pdf["file_id"] not in completed
    ]
    print(f"Engine={engine} | {len(pdfs)} PDFs to process ({args.start_year}-{args.end_year})")

    counts = {"text_saved": 0, "skipped": 0, "failed": 0}
    try:
        for number, pdf in enumerate(pdfs, start=1):
            print(f"[{number}/{len(pdfs)}] Starting: {pdf['name']}")
            try:
                data = stream_pdf(service, pdf["file_id"], pdf["name"], pdf["size"])
            except Exception as exc:  # one PDF that cannot be downloaded must not stop the rest
                print(f"Download FAILED: {pdf['name']} — {exc}")
                counts["failed"] += 1
                continue
            result = ingest_one(data, pdf["file_id"], pdf["name"], embed=False)
            del data  # frees the PDF before the next one is downloaded
            counts[result] += 1
            print(f"Done: {pdf['name']} — {result}")
    except KeyboardInterrupt:
        # The PDF being read stays 'processing', and the pages already read are cached, so
        # the next run picks it up where it stopped.
        print("Interrupted.")
        _print_summary(counts)
        return 130

    _print_summary(counts)
    return 1 if counts["failed"] else 0


def _print_summary(counts: dict[str, int]) -> None:
    print(f"Finished: {counts['text_saved']} text saved, {counts['skipped']} skipped, {counts['failed']} failed")
    if counts["text_saved"]:
        print("They cannot be searched until embedded. When all OCR is done, run: uv run python -m ingest.embed_pending")


if __name__ == "__main__":
    sys.exit(main())
