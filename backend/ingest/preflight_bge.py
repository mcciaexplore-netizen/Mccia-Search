"""Is everything in place for a backend that searches with bge-m3? Checks it, changes nothing.

    EMBEDDING_ENGINE=bge uv run python -m ingest.preflight_bge

Run it on the Mac after ingest.reembed_all, and again inside the new server image before switching the live backend to it
(deploy/bge-switch.sh does the second one). It prints one line per check and exits with 1 if any check failed:

  1. the bge column exists in the database, and every chunk has its vector (nothing would be missing from the answers)
  2. the search index on the column exists (without it every search reads all 39,000 vectors)
  3. the model loads here and embeds Marathi and English questions into unit-length 1024-dimension vectors
  4. real searches (the same code the chat uses) find the right issues, and the time they take
  5. how much memory this process now uses (the server has to hold the model next to its other work)
"""

import resource
import sys
import time

import embedding_engine
from db.connection import get_connection

# Each question must find something; the one that names a year must find only pages of the issues that cover that year.
CHECKS = (
    ("भारताचे पहिले राष्ट्रपती कोण होते?", None),
    ("Who was the first president of India?", None),
    ("What happened in 1965?", 1965),
)
INDEX_NAME = "chunks_embedding_bge_idx"
SLOW_SEARCH_MS = 3000  # a search slower than this would be felt in the chat


def main(argv: list[str] | None = None) -> int:
    if embedding_engine.engine() != "bge":
        print(f"EMBEDDING_ENGINE is {embedding_engine.engine()!r}, not 'bge'. Run this with EMBEDDING_ENGINE=bge.")
        return 2
    failures = 0
    for name, check in (
        ("database", _check_database),
        ("search index", _check_index),
        ("model", _check_model),
        ("searches", _check_searches),
        ("memory", _check_memory),
    ):
        try:
            ok, message = check()
        except Exception as exc:  # a check that crashes is a check that failed
            ok, message = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {message}", flush=True)
        failures += 0 if ok else 1
    print("\nReady for bge-m3." if not failures else f"\n{failures} check(s) failed: do not switch yet.")
    return 1 if failures else 0


def _check_database() -> tuple[bool, str]:
    embedding_engine.ensure_matches_database()  # raises with the fix when the column is missing
    have, total = embedding_engine.coverage()
    if have != total:
        return False, f"{total - have} of {total} chunks have no bge vector (run: uv run python -m ingest.reembed_all --yes)"
    return True, f"all {total} chunks have a bge-m3 vector"


def _check_index() -> tuple[bool, str]:
    with get_connection() as conn:
        found = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (INDEX_NAME,)).fetchone()[0]
        usable = conn.execute(
            "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(%s)", (INDEX_NAME,)
        ).fetchone()
    if not found or usable is None:
        return False, f"{INDEX_NAME} does not exist (ingest.reembed_all builds it when it finishes)"
    return (True, f"{INDEX_NAME} exists") if usable[0] else (False, f"{INDEX_NAME} exists but is not valid: drop it and run ingest.reembed_all --yes")


def _check_model() -> tuple[bool, str]:
    started = time.monotonic()
    vectors = embedding_engine.embed_local(["भारताचे पहिले राष्ट्रपती कोण होते?", "Who was the first president of India?"])
    seconds = time.monotonic() - started
    width = embedding_engine.dimension()
    if len(vectors) != 2 or any(len(v) != width for v in vectors):
        return False, f"expected two {width}-dimension vectors, got {[len(v) for v in vectors]}"
    norms = [sum(x * x for x in v) ** 0.5 for v in vectors]
    if any(abs(n - 1.0) > 0.01 for n in norms):
        return False, f"the vectors are not unit length ({norms})"
    started = time.monotonic()
    embedding_engine.embed_local(["Who was the first president of India?"])
    return True, f"{embedding_engine.model_id()}, {width} dimensions, loaded and first used in {seconds:.1f}s, then {1000 * (time.monotonic() - started):.0f} ms per question"


def _check_searches() -> tuple[bool, str]:
    from retrieval.search import search, volume_years

    notes, slowest = [], 0
    for question, year in CHECKS:
        started = time.monotonic()
        results = search(question, 5)
        elapsed = round(1000 * (time.monotonic() - started))
        slowest = max(slowest, elapsed)
        if not results:
            return False, f"no result for {question!r}"
        if year is not None:
            outside = [r["filename"] for r in results if not _covers(volume_years, r["filename"], year)]
            if outside:
                return False, f"{question!r} returned pages outside the issues that cover {year}: {sorted(set(outside))}"
        notes.append(f"{len(results)} results (top similarity {results[0]['similarity']:.2f})")
    if slowest > SLOW_SEARCH_MS:
        return False, f"slowest search took {slowest} ms (over {SLOW_SEARCH_MS} ms): the search index may not be used"
    return True, f"{len(CHECKS)} real searches ok, slowest {slowest} ms; " + "; ".join(notes)


def _covers(volume_years, filename: str, year: int) -> bool:
    span = volume_years(filename)
    return span is not None and span[0] <= year <= span[1]


def _check_memory() -> tuple[bool, str]:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    megabytes = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024  # macOS reports bytes, Linux kilobytes
    return True, f"this process peaked at {megabytes:.0f} MB"


if __name__ == "__main__":
    sys.exit(main())
