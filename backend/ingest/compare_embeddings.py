"""Search the same questions with Gemini's vectors and with bge-m3's, side by side, before deciding to switch.

    uv run python -m ingest.compare_embeddings --audit 40            # the 40 most recent real questions from the audit log
    uv run python -m ingest.compare_embeddings questions.txt         # or your own, one per line
    uv run python -m ingest.compare_embeddings --audit 40 --out report.md

Read-only. Each question runs through the real search (hybrid: meaning plus keywords, the year filter, each engine's own minimum
similarity) once per engine and the top results are printed next to each other as "file p.page score". It needs both vector sets (so
ingest.reembed_all must have finished) and a GEMINI_API_KEY for the Gemini side. The questions are searched as typed: the follow-up
rewriting of the chat is not part of this comparison.

It cannot say which engine is right (nobody has marked the right pages), so read the table: "same top page" and the overlap tell you
how much the two agree, "only gemini / only bge" are the questions to look at by hand, and the speed column is the time of the
whole search, including embedding the question.
"""

import argparse
import sys
import time

import embedding_engine
from db.connection import get_connection
from retrieval.search import TOP_K, search

MIN_QUESTION_CHARS = 8  # shorter audit entries are greetings and smoke tests, not real questions
SHOWN = 3  # results printed per engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("questions", nargs="?", help="a text file with one question per line")
    parser.add_argument("--audit", type=int, metavar="N", help="use the N most recent distinct real questions from the audit log")
    parser.add_argument("--out", help="also write the report to this markdown file")
    args = parser.parse_args(argv)
    if bool(args.questions) == bool(args.audit):
        parser.error("give either a questions file or --audit N")

    questions = _read_file(args.questions) if args.questions else _from_audit(args.audit)
    if not questions:
        print("No questions to compare.")
        return 1

    lines = [f"# Gemini vs bge-m3 on {len(questions)} questions\n"]
    rows = []
    for number, question in enumerate(questions, start=1):
        gemini, g_ms = _timed("gemini", question)
        bge, b_ms = _timed("bge", question)
        rows.append((question, gemini, bge, g_ms, b_ms))
        lines.append(_describe(number, question, gemini, bge, g_ms, b_ms))
        print(lines[-1], flush=True)
    lines.append(_summary(rows))
    print(lines[-1])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        print(f"(report written to {args.out})")
    return 0


def _timed(engine_name: str, question: str) -> tuple[list[dict], int]:
    with embedding_engine.using(engine_name):
        started = time.monotonic()
        results = search(question, TOP_K)
        return results, round((time.monotonic() - started) * 1000)


def _pages(results: list[dict]) -> list[tuple[str, int | None]]:
    return [(r["filename"], r["page_number"]) for r in results]


def _agreement(gemini: list[dict], bge: list[dict]) -> dict:
    """How the two result lists relate: whether both found anything, whether the top page is the same, and the overlap of the pages."""
    g, b = _pages(gemini), _pages(bge)
    shared = len(set(g) & set(b))
    return {
        "both": bool(g) and bool(b),
        "only_gemini": bool(g) and not b,
        "only_bge": bool(b) and not g,
        "neither": not g and not b,
        "same_top": bool(g) and bool(b) and g[0] == b[0],
        "overlap": shared / max(len(set(g) | set(b)), 1) if (g or b) else 1.0,
    }


def _label(result: dict) -> str:
    name = result["filename"].rsplit(".", 1)[0]
    return f"{name} p.{result['page_number']} {result['score']:.2f}"


def _describe(number: int, question: str, gemini: list[dict], bge: list[dict], g_ms: int, b_ms: int) -> str:
    a = _agreement(gemini, bge)
    verdict = (
        "neither found anything" if a["neither"]
        else "ONLY GEMINI FOUND" if a["only_gemini"]
        else "ONLY BGE FOUND" if a["only_bge"]
        else "same top page" if a["same_top"]
        else "different top page"
    )
    out = [f"\n{number}. {question}\n   {verdict}; the pages overlap {a['overlap']:.0%}; gemini {g_ms} ms, bge {b_ms} ms"]
    for name, results in (("gemini", gemini), ("bge   ", bge)):
        shown = " | ".join(_label(r) for r in results[:SHOWN]) or "(nothing)"
        out.append(f"   {name}: {shown}")
    return "\n".join(out)


def _summary(rows: list[tuple]) -> str:
    agreements = [_agreement(g, b) for _, g, b, _, _ in rows]
    n = len(rows)
    count = lambda key: sum(1 for a in agreements if a[key])
    gemini_ms = sorted(r[3] for r in rows)
    bge_ms = sorted(r[4] for r in rows)
    median = lambda values: values[len(values) // 2]
    return (
        f"\nSummary of {n} questions\n"
        f"  both found something:   {count('both')}\n"
        f"  only gemini found:      {count('only_gemini')}\n"
        f"  only bge found:         {count('only_bge')}\n"
        f"  neither found:          {count('neither')}\n"
        f"  same top page:          {count('same_top')} (of the {count('both')} where both found)\n"
        f"  average page overlap:   {sum(a['overlap'] for a in agreements) / n:.0%}\n"
        f"  median search time:     gemini {median(gemini_ms)} ms, bge {median(bge_ms)} ms\n"
    )


def _read_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if len(line.strip()) >= MIN_QUESTION_CHARS]


def _from_audit(limit: int) -> list[str]:
    """The most recent distinct real questions people asked (chat entries that were answered; greetings and smoke tests left out)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT query FROM audit_log
            WHERE event = 'chat' AND status = 200 AND length(btrim(COALESCE(query, ''))) >= %s
              AND COALESCE(user_id, '') NOT LIKE 'smoke%%' AND COALESCE(user_id, '') NOT LIKE 'test%%'
            ORDER BY occurred_at DESC LIMIT %s
            """,
            (MIN_QUESTION_CHARS, limit * 4),
        ).fetchall()
    questions: list[str] = []
    for (query,) in rows:
        query = query.strip()
        if query.lower() not in (q.lower() for q in questions):
            questions.append(query)
        if len(questions) == limit:
            break
    return questions


if __name__ == "__main__":
    sys.exit(main())
