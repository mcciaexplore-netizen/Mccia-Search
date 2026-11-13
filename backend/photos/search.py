"""Searching the photographs: by the caption printed next to them (names), by the article on the same page, and by what they look like (CLIP).

A photograph's score is   0.45 * caption + 0.30 * page + 0.25 * picture   where each part is 0 to 1:
  caption  the share of the request found in the printed caption, counting Marathi and English spellings of names (Gemini supplies the spellings)
  page     how well the text search finds the page the photograph is on (the same hybrid search the chat uses, so a name is found through the article)
  picture  how close CLIP says the picture is to the request, once translated to English (works for concepts, not for names: CLIP cannot read a caption)
A photograph is only shown when at least one signal is clearly there. A request that names a year is searched in the issues that cover it only, never in others.
"""

from dataclasses import dataclass, field
from typing import Callable

from pgvector import Vector

from db.connection import get_pooled_connection
from photos.query import photo_subject, spellings as default_spellings
from retrieval.search import keyword_terms, search as text_search, volume_label, year_scope

W_CAPTION, W_PAGE, W_PICTURE = 0.45, 0.30, 0.25
CAPTION_MIN = 0.5  # a photograph is shown if its caption holds at least half of the request ...
PAGE_MIN = 0.5  # ... or its page is a good text match (the text search's own floor is about 0.27, so 0.5 means a clear match) ...
PICTURE_MIN = 0.26  # ... or CLIP's cosine similarity to the request is at least this (typical matches 0.25-0.35, unrelated 0.15-0.20)
PICTURE_LOW, PICTURE_HIGH = 0.18, 0.32  # the similarities mapped to 0 and 1
PAGE_LOW, PAGE_HIGH = 0.40, 0.75  # the text search's scores mapped to 0 and 1
CLIP_CANDIDATES = 30
PAGE_RESULTS = 20  # chunks the text search is asked for, to find the pages
MIN_TERM_CHARS = 3  # a shorter word would match inside everything


@dataclass
class PhotoHit:
    id: int
    document_id: int
    page_number: int
    filename: str
    year: str | None
    caption: str
    kind: str
    contains_face: bool
    storage_path: str
    score: float
    signals: dict = field(default_factory=dict)


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def caption_score(caption: str, terms: list[str], names: tuple[str, ...]) -> float:
    """0 to 1: how much of the request the caption holds. Terms (the request's own words) count by their share; a spelling of a name counts by how many
    of the request's words it covers, so the full name scores 1 and a surname alone about half."""
    low = (caption or "").lower()
    if not low or not terms:
        return 0.0
    usable = [t for t in terms if len(t) >= MIN_TERM_CHARS]
    literal = sum(1 for t in usable if t.lower() in low) / len(terms) if usable else 0.0
    spelt = 0.0
    for name in names:
        words = name.split()
        if words and len(name) >= MIN_TERM_CHARS and name.lower() in low:
            spelt = max(spelt, min(1.0, len(words) / len(terms)))
    return clamp01(max(literal, spelt))


def page_score(raw: float | None) -> float:
    return 0.0 if raw is None else clamp01((raw - PAGE_LOW) / (PAGE_HIGH - PAGE_LOW))


def picture_score(similarity: float | None) -> float:
    return 0.0 if similarity is None else clamp01((similarity - PICTURE_LOW) / (PICTURE_HIGH - PICTURE_LOW))


def combine(caption: float, page: float, picture: float) -> float:
    return W_CAPTION * caption + W_PAGE * page + W_PICTURE * picture


def qualifies(caption: float, page_raw: float | None, similarity: float | None) -> bool:
    return caption >= CAPTION_MIN or (page_raw is not None and page_raw >= PAGE_MIN) or (similarity is not None and similarity >= PICTURE_MIN)


def like_pattern(term: str) -> str:
    """A term as an ILIKE pattern that finds it anywhere, with the characters that mean something in LIKE (% _ and the escape \\) taken literally."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


_COLUMNS = "p.id, p.document_id, p.page_num, d.filename, p.kind, p.contains_face, p.storage_path, coalesce(p.description, '')"


def _rows(conn, sql: str, params: dict) -> list[tuple]:
    return conn.execute(sql, params).fetchall()


def search_photos(
    query: str,
    top_k: int = 5,
    *,
    clip_text=None,
    spellings: Callable = default_spellings,
    pages_for: Callable[[str, "object"], list[dict]] | None = None,
    say=print,
) -> list[PhotoHit]:
    """The photographs best matching a request, best first (at most top_k; fewer, or none, when nothing matches well).

    `clip_text` embeds an English phrase (photos.clip_text.ClipText); `pages_for(subject, scope)` returns text-search chunks (the chat's search by default).
    """
    subject = photo_subject(query)
    if not subject:
        return []  # "show me a photo": nothing says of what, so there is nothing to look for
    scope = year_scope(query)
    if scope.document_ids is not None and not scope.document_ids:
        return []  # a year no issue covers: nothing to search, and no fallback to other years
    spelt = spellings(subject)
    terms = keyword_terms(subject) or [w for w in subject.split() if len(w) >= MIN_TERM_CHARS]
    scoped = "AND p.document_id = ANY(%(docs)s)" if scope.document_ids is not None else ""
    base = {"docs": list(scope.document_ids) if scope.document_ids is not None else None}
    patterns = sorted({like_pattern(t) for t in (*terms, *spelt.names) if len(t) >= MIN_TERM_CHARS})

    page_raw: dict[tuple[int, int], float] = {}
    try:
        for chunk in (pages_for or _pages_for)(subject, scope):
            key = (chunk["document_id"], chunk["page_number"])
            page_raw[key] = max(page_raw.get(key, 0.0), float(chunk["score"]))
    except Exception as exc:  # the text search failing must not lose the other two signals
        say(f"[photos] page search failed ({type(exc).__name__}): {str(exc)[:100]}")

    vector = None
    if clip_text is not None:
        try:
            vector = Vector(clip_text.embed(spelt.english).tolist()).to_text()
        except Exception as exc:
            say(f"[photos] CLIP failed ({type(exc).__name__}): {str(exc)[:100]}")

    found: dict[int, dict] = {}

    def add(rows: list[tuple]) -> None:
        for pid, doc, page, filename, kind, face, path, caption, sim in rows:
            found.setdefault(pid, {"id": pid, "document_id": doc, "page": page, "filename": filename, "kind": kind, "face": face, "path": path,
                                   "caption": caption, "sim": float(sim) if sim is not None else None})

    sim_sql = "1 - (p.clip_embedding <=> %(vec)s::vector)" if vector else "NULL"
    with get_pooled_connection() as conn:
        params = {**base, "vec": vector}
        if patterns:  # by caption
            add(_rows(conn, f"SELECT {_COLUMNS}, {sim_sql} FROM photo_index p JOIN documents d ON d.id = p.document_id "
                            f"WHERE p.description ILIKE ANY(%(patterns)s) {scoped} LIMIT 200", {**params, "patterns": patterns}))
        if page_raw:  # by page
            pairs = sorted(page_raw)
            add(_rows(conn, f"SELECT {_COLUMNS}, {sim_sql} FROM photo_index p JOIN documents d ON d.id = p.document_id "
                            f"WHERE (p.document_id, p.page_num) IN (SELECT * FROM unnest(%(docs_p)s::bigint[], %(pages_p)s::int[])) {scoped}",
                      {**params, "docs_p": [d for d, _ in pairs], "pages_p": [n for _, n in pairs]}))
        if vector:  # by look
            add(_rows(conn, f"SELECT {_COLUMNS}, {sim_sql} FROM photo_index p JOIN documents d ON d.id = p.document_id "
                            f"WHERE p.clip_embedding IS NOT NULL {scoped} ORDER BY p.clip_embedding <=> %(vec)s::vector LIMIT {CLIP_CANDIDATES}", params))

    hits: list[PhotoHit] = []
    for item in found.values():
        raw = page_raw.get((item["document_id"], item["page"]))
        caption = caption_score(item["caption"], terms, spelt.names)
        if not qualifies(caption, raw, item["sim"]):
            continue
        parts = {"caption": caption, "page": page_score(raw), "picture": picture_score(item["sim"])}
        hits.append(PhotoHit(id=item["id"], document_id=item["document_id"], page_number=item["page"], filename=item["filename"], year=volume_label(item["filename"]),
                             caption=item["caption"], kind=item["kind"], contains_face=bool(item["face"]), storage_path=item["path"],
                             score=combine(**parts), signals=parts))
    hits.sort(key=lambda h: (-h.score, -h.signals["caption"], -h.signals["picture"], h.id))
    return hits[:top_k]


def _pages_for(subject: str, scope) -> list[dict]:
    return text_search(subject, PAGE_RESULTS, scope=scope)
