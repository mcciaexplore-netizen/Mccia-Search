"""Split extracted document text into chunks for embedding."""

import re
from bisect import bisect_right
from typing import NamedTuple

CHUNK_SIZE = 500  # target tokens per chunk (approx by word count)
CHUNK_OVERLAP = 50  # words shared between consecutive chunks
MIN_CHUNK_WORDS = 10

# Pages are joined with a single newline (not a paragraph break), so a sentence
# that runs over a page break stays in one piece.
_PAGE_SEPARATOR = "\n"

# A sentence ends at a danda, at . ! ? followed by whitespace (closing quotes and
# brackets may sit in between), or just before a blank line (paragraph break).
_SENTENCE = re.compile(
    r"\S.*?(?:[।॥]|[.!?][\"'”’)\]]*(?=\s|\Z)|(?=\n[ \t]*\n)|\Z)",
    re.DOTALL,
)


class _Sentence(NamedTuple):
    text: str
    words: int
    page: int  # page the sentence starts on


def split_into_chunks(
    pages: list[dict], document_id: int, filename: str | None = None
) -> list[dict]:
    """Split OCR pages into overlapping, sentence-aligned chunks.

    `pages` is a list of {page_number, text}. Returns dicts of
    {document_id, chunk_index, content, page_number}, where page_number is the
    page the chunk starts on and chunk_index counts the chunks kept (0-based).
    Chunks under MIN_CHUNK_WORDS words are dropped. `filename` is only used for
    the progress message.

    Chunks hold whole sentences, up to CHUNK_SIZE words, and repeat up to
    CHUNK_OVERLAP words (whole trailing sentences) from the previous chunk. A run
    longer than CHUNK_SIZE words with no sentence punctuation or paragraph break
    is cut at CHUNK_SIZE words, since it could not fit in any chunk.
    """
    groups = _group_sentences(_sentences(pages))

    chunks: list[dict] = []
    for group in groups:
        content = " ".join(sentence.text for sentence in group)
        if len(content.split()) < MIN_CHUNK_WORDS:
            continue
        chunks.append(
            {
                "document_id": document_id,
                "chunk_index": len(chunks),
                "content": content,
                "page_number": group[0].page,
            }
        )

    label = filename or f"document {document_id}"
    print(f"[chunk] {label} → {len(chunks)} chunks created")
    return chunks


def _sentences(pages: list[dict]) -> list[_Sentence]:
    """Flatten the pages into one text stream and split it into sentences."""
    offsets: list[int] = []
    numbers: list[int] = []
    parts: list[str] = []
    position = 0
    for page in sorted(pages, key=lambda p: p["page_number"]):
        text = page.get("text") or ""
        offsets.append(position)
        numbers.append(page["page_number"])
        parts.append(text)
        position += len(text) + len(_PAGE_SEPARATOR)
    stream = _PAGE_SEPARATOR.join(parts)

    sentences: list[_Sentence] = []
    for match in _SENTENCE.finditer(stream):
        page = numbers[bisect_right(offsets, match.start()) - 1]
        words = match.group().split()
        for start in range(0, len(words), CHUNK_SIZE):
            piece = words[start : start + CHUNK_SIZE]
            sentences.append(_Sentence(" ".join(piece), len(piece), page))
    return sentences


def _group_sentences(sentences: list[_Sentence]) -> list[list[_Sentence]]:
    """Pack sentences into groups of at most CHUNK_SIZE words, with overlap."""
    groups: list[list[_Sentence]] = []
    current: list[_Sentence] = []
    current_words = 0
    for sentence in sentences:
        if current and current_words + sentence.words > CHUNK_SIZE:
            groups.append(current)
            current = _overlap_tail(current, room=CHUNK_SIZE - sentence.words)
            current_words = sum(s.words for s in current)
        current.append(sentence)
        current_words += sentence.words
    if current:
        groups.append(current)
    return groups


def _overlap_tail(group: list[_Sentence], room: int) -> list[_Sentence]:
    """The last whole sentences of `group` totalling at most CHUNK_OVERLAP words."""
    limit = min(CHUNK_OVERLAP, room)
    tail: list[_Sentence] = []
    words = 0
    for sentence in reversed(group):
        if words + sentence.words > limit:
            break
        tail.insert(0, sentence)
        words += sentence.words
    return tail
