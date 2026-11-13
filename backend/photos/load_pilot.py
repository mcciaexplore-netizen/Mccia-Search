"""Write the pilot's photographs (the folder photos.pilot produced and you approved) into the bucket and the database.

    uv run python -m photos.load_pilot ~/Desktop/photo-pilot           # shows the plan and changes nothing
    uv run python -m photos.load_pilot ~/Desktop/photo-pilot --yes     # does it

The pilot did not keep the CLIP vectors, so they are made again from the saved crops (the same picture, as stored). Safe to run again: a page's rows are
replaced, and uploads overwrite. Only pages that had photographs are marked as looked at, so the full run still looks at the pages that had none.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from photos.boxes import Box
from photos.pipeline import Photo


def normalise(name: str) -> str:
    """A PDF's name without its extension, with underscores read as spaces and case ignored: "1952_April" and "1952 April.PDF" are the same."""
    return re.sub(r"\s+", " ", re.sub(r"\.pdf$", "", name.strip(), flags=re.IGNORECASE).replace("_", " ")).lower()


def match_documents(conn, stems: list[str]) -> dict[str, int | None]:
    """{pilot name: document id}, None where nothing (or more than one document) matches."""
    by_name: dict[str, list[int]] = {}
    for doc_id, filename in conn.execute("SELECT id, filename FROM documents").fetchall():
        by_name.setdefault(normalise(filename), []).append(doc_id)
    return {stem: (by_name[normalise(stem)][0] if len(by_name.get(normalise(stem), [])) == 1 else None) for stem in stems}


def read_entries(folder: Path) -> list[dict]:
    return [json.loads(line) for line in (folder / "results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def build_photos(entries: list[dict], folder: Path, clip) -> list[Photo]:
    """Photo objects for the entries, with their CLIP vectors made from the saved crops."""
    from PIL import Image

    images = []
    for entry in entries:
        with Image.open(folder / entry["file"]) as image:
            images.append(image.convert("RGB"))
    vectors = clip.image_vectors(images)
    photos = []
    for entry, vector in zip(entries, vectors):
        photos.append(
            Photo(index=entry["index"], box=Box(0, 0, 0, 0), fractions=tuple(entry["box"]), kind=entry["kind"], kind_score=entry["kind_score"],
                  jpeg=(folder / entry["file"]).read_bytes(), width=entry["size"][0], height=entry["size"][1], vector=vector, faces=entry["faces"],
                  caption=entry["caption"], whole_page=entry["whole_page"])
        )
    return photos


def load(conn, store, folder: Path, clip, write: bool, say=print) -> dict:
    """Plan (write=False) or do (write=True) the loading. Returns counts."""
    from photos.upload import store_page

    entries = read_entries(folder)
    matches = match_documents(conn, sorted({e["pdf"] for e in entries}))
    unmatched = [stem for stem, doc in matches.items() if doc is None]
    usable = [e for e in entries if matches[e["pdf"]] is not None]
    pages: dict[tuple[int, int], list[dict]] = {}
    for entry in usable:
        pages.setdefault((matches[entry["pdf"]], entry["page"]), []).append(entry)
    say(f"{len(entries)} photos on {len({(e['pdf'], e['page']) for e in entries})} pages from {len(matches)} PDFs; {len(usable)} photos match a document")
    for stem, doc in matches.items():
        say(f"  {stem}: {'document ' + str(doc) if doc else 'NO MATCHING DOCUMENT: skipped'}")
    if not write:
        return {"photos": len(usable), "pages": len(pages), "unmatched": unmatched, "written": 0}
    photos = build_photos(usable, folder, clip)
    by_entry = {id(e): p for e, p in zip(usable, photos)}
    for (doc_id, page), page_entries in sorted(pages.items()):
        store_page(conn, store, doc_id, page, [by_entry[id(e)] for e in sorted(page_entries, key=lambda e: e["index"])])
        conn.commit()
    return {"photos": len(usable), "pages": len(pages), "unmatched": unmatched, "written": len(usable)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder", help="the folder photos.pilot wrote (results.jsonl and crops/)")
    parser.add_argument("--yes", action="store_true", help="really write (without it, only show the plan)")
    args = parser.parse_args(argv)
    folder = Path(args.folder).expanduser()

    from db.connection import get_connection
    from photos.clip_model import ClipModel
    from photos.db import init_tables
    from photos.store import from_env

    store = from_env()
    if store is None:
        print("No Supabase service-role key found (SUPABASE_SERVICE_ROLE_KEY in .env.supabase.local).")
        return 2
    with get_connection() as conn:
        if args.yes:
            init_tables(conn)
            print("bucket created" if store.ensure_bucket() else "bucket already there")
        result = load(conn, store, folder, ClipModel(), write=args.yes)
    print(("Written: " if args.yes else "Plan: ") + json.dumps({k: v for k, v in result.items() if k != "unmatched"}) + (f"; unmatched PDFs: {result['unmatched']}" if result["unmatched"] else ""))
    if not args.yes:
        print("Run again with --yes to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
