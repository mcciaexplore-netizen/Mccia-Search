"""A pilot of the photo pipeline on local PDFs: nothing is written to the database or to storage, only to a folder you can look at.

    uv run python -m photos.pilot --out ~/Desktop/photo-pilot  "/path/2011-Jan.PDF:all"  "/path/1961.pdf:every:6,27,214"  ...

Each PDF may be followed by `:pages`: `all`, `every:N` (every Nth page), page numbers `3,9,27` and ranges `10-20`, mixed with commas (`every:6,214,350`).
It writes, per photo, the cropped JPEG, and for the whole run results.jsonl, contact sheets (the photos with their kind, face count and caption) and a summary.
"""

import argparse
import io
import json
import sys
import time
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from photos.pipeline import CUT_DPI, DETECT_DPI, default_models, process_page
from photos.render import render_page

SHEET_COLUMNS, SHEET_ROWS, THUMB_HEIGHT = 6, 4, 260


def parse_pages(spec: str, total: int) -> list[int]:
    """Page numbers (1-based, within the PDF, ascending, each once) for a spec like "all", "every:6,214" or "3,9,10-12"."""
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "all":
            pages.update(range(1, total + 1))
        elif part.startswith("every:"):
            step = int(part.split(":", 1)[1])
            if step < 1:
                raise ValueError("every:N needs N of at least 1")
            pages.update(range(1, total + 1, step))
        elif "-" in part:
            first, last = (int(x) for x in part.split("-", 1))
            pages.update(range(first, last + 1))
        else:
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= total)


def resolve_pdf(name_or_path: str, cache: Path | None) -> Path:
    """The PDF file for an argument: the path itself if it exists, else (with a cache folder) the Google Drive file of that exact name, downloaded once."""
    path = Path(name_or_path).expanduser()
    if path.exists():
        return path
    if cache is None:
        raise FileNotFoundError(f"{name_or_path} is not a file; give --cache to fetch a PDF from Google Drive by its name")
    target = cache / Path(name_or_path).name.replace(" ", "_")
    if target.exists():
        return target
    from ingest.drive_sync import build_drive_service, list_pdfs, stream_pdf

    service = build_drive_service()
    found = next((pdf for pdf in list_pdfs(service) if pdf["name"] == name_or_path), None)
    if found is None:
        raise FileNotFoundError(f"{name_or_path} is not on Google Drive")
    cache.mkdir(parents=True, exist_ok=True)
    data = stream_pdf(service, found["file_id"], found["name"], found["size"])
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return target


def split_argument(argument: str) -> tuple[str, str]:
    """"/path/file.pdf:every:6" -> ("/path/file.pdf", "every:6"); no spec means all pages."""
    for marker in (".pdf:", ".PDF:"):
        if marker in argument:
            path, spec = argument.split(marker, 1)
            return path + marker[:-1], spec
    return argument, "all"


def contact_sheets(entries: list[dict], out: Path) -> list[Path]:
    """Sheets of thumbnails of the cropped photos, each labelled: [kind] faces, source page."""
    sheets = []
    per_sheet = SHEET_COLUMNS * SHEET_ROWS
    for number, start in enumerate(range(0, len(entries), per_sheet), start=1):
        batch = entries[start : start + per_sheet]
        tiles = []
        for e in batch:
            with Image.open(out / e["file"]) as im:
                im = im.convert("RGB")
                im = im.resize((max(int(im.width * THUMB_HEIGHT / im.height), 40), THUMB_HEIGHT))
            draw = ImageDraw.Draw(im)
            label = f"{e['pdf'][:10]} p{e['page']} {e['kind'][:5]}{' F' + str(e['faces']) if e['faces'] else ''}"
            draw.rectangle((0, 0, min(im.width, 9 * len(label) + 6), 14), fill="yellow")
            draw.text((3, 1), label, fill="black")
            tiles.append(im)
        cell = max(t.width for t in tiles)
        canvas = Image.new("RGB", (SHEET_COLUMNS * cell, SHEET_ROWS * THUMB_HEIGHT), "white")
        for i, tile in enumerate(tiles):
            canvas.paste(tile, ((i % SHEET_COLUMNS) * cell, (i // SHEET_COLUMNS) * THUMB_HEIGHT))
        path = out / f"sheet_{number:02d}.jpg"
        canvas.save(path, quality=80)
        sheets.append(path)
    return sheets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pdfs", nargs="+", help='PDF files, each optionally followed by ":pages" (see above)')
    parser.add_argument("--out", required=True, help="the folder for the crops, results.jsonl and contact sheets")
    parser.add_argument("--cache", help="a folder for PDFs fetched from Google Drive: a PDF argument that is not a file is then fetched by its exact name")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser()
    (out / "crops").mkdir(parents=True, exist_ok=True)

    jobs = []
    for argument in args.pdfs:
        path, spec = split_argument(argument)
        path = str(resolve_pdf(path, Path(args.cache).expanduser() if args.cache else None))
        with pymupdf.open(path) as doc:
            jobs.append((Path(path), parse_pages(spec, doc.page_count)))
    total = sum(len(pages) for _, pages in jobs)
    print(f"[photos] pilot: {total} pages from {len(jobs)} PDFs", flush=True)

    models = default_models()
    entries: list[dict] = []
    started, done = time.monotonic(), 0
    results = (out / "results.jsonl").open("w", encoding="utf-8")
    pages_with_photos = 0
    for path, pages in jobs:
        doc = pymupdf.open(path)
        for page_number in pages:
            page = doc[page_number - 1]
            page_started = time.monotonic()
            try:
                photos = process_page(render_page(page, DETECT_DPI), render_page(page, CUT_DPI), models)
            except Exception as exc:  # one page that fails must not stop the pilot
                print(f"[photos] {path.name} page {page_number}: FAILED {type(exc).__name__}: {str(exc)[:120]}", flush=True)
                photos = []
            done += 1
            pages_with_photos += bool(photos)
            for photo in photos:
                name = f"crops/{path.stem[:12].replace(' ', '_')}_p{page_number:04d}_{photo.index}_{photo.kind}.jpg"
                (out / name).write_bytes(photo.jpeg)
                entry = {
                    "pdf": path.stem, "page": page_number, "index": photo.index, "kind": photo.kind, "kind_score": round(photo.kind_score, 3),
                    "faces": photo.faces, "caption": photo.caption, "whole_page": photo.whole_page, "box": [round(v, 4) for v in photo.fractions],
                    "size": [photo.width, photo.height], "file": name, "seconds": round(time.monotonic() - page_started, 1),
                }
                entries.append(entry)
                results.write(json.dumps(entry, ensure_ascii=False) + "\n")
            results.flush()
            rate = (time.monotonic() - started) / done
            print(f"[photos] {path.name[:24]} — page {page_number} — {done}/{total} pages — {len(entries)} photos — {rate:.1f} s/page — ETA {(total - done) * rate / 60:.0f}m", flush=True)
    results.close()

    sheets = contact_sheets(entries, out) if entries else []
    kinds: dict[str, int] = {}
    for e in entries:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    summary = {
        "pages": done, "pages_with_photos": pages_with_photos, "photos": len(entries), "kinds": kinds,
        "with_faces": sum(1 for e in entries if e["faces"]), "with_caption": sum(1 for e in entries if e["caption"]),
        "seconds_per_page": round((time.monotonic() - started) / max(done, 1), 2), "sheets": [s.name for s in sheets],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print("[photos] pilot finished:", json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
