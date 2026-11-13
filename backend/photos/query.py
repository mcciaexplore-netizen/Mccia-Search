"""Understanding a request for photographs: is it one, what is it a photo of, and how might the name be spelt in the captions?

Pure text handling, except spellings(): that asks Gemini once for the Marathi and English spellings of the names, because a caption is printed in
Devanagari and a question may be typed in Latin letters ("Kirloskar" and "किर्लोस्कर" share no characters), and for an English rendering to give CLIP.
"""

import json
import re
import string
from dataclasses import dataclass, field
from functools import lru_cache

# Words that make a question a request for pictures. English words must match exactly; the Devanagari ones are matched on their stem (Marathi
# attaches endings: "फोटोंचे", "छायाचित्रांचा"). "चित्र" alone is NOT a stem: it would also catch "चित्रपट" (film).
ENGLISH_INTENT = frozenset({"photo", "photos", "photograph", "photographs", "picture", "pictures", "pic", "pics", "image", "images", "portrait", "portraits"})
DEVANAGARI_STEMS = ("फोटो", "फ़ोटो", "छायाचित्र")
DEVANAGARI_EXACT = frozenset({"चित्र", "चित्रे", "चित्रांचे", "चित्रांचा", "तस्वीर", "तस्वीरें", "तस्वीरों", "प्रतिमा", "प्रतिमाचित्र"})
# Words around the subject that say nothing about it ("show me a photo of ...").
FILLER = frozenset(
    {"show", "me", "us", "a", "an", "the", "of", "from", "with", "please", "give", "find", "get", "display", "see", "look", "at", "for", "and", "in", "any", "some",
     "दाखवा", "दाखव", "दाखवू", "द्या", "दे", "पाहा", "पहा", "हवा", "हवे", "मला", "आम्हाला", "दिखाओ", "दिखाइए", "दीजिए", "दिखाएं", "मुझे", "का", "की", "के", "चा", "ची", "चे", "चे."}
)
MAX_SUBJECT_CHARS = 200
_EDGE_PUNCTUATION = string.punctuation + "।॥“”‘’…«»"


def _tokens(text: str) -> list[str]:
    """The words of a text. Split on spaces, not with a regex: Python's \\w does not match Devanagari vowel signs and would cut Marathi words in pieces."""
    return [word for word in (w.strip(_EDGE_PUNCTUATION) for w in (text or "").split()) if word]


def _is_intent(token: str) -> bool:
    low = token.lower()
    return low in ENGLISH_INTENT or low in DEVANAGARI_EXACT or any(low.startswith(stem) for stem in DEVANAGARI_STEMS)


def wants_photos(query: str) -> bool:
    """Whether the question asks for photographs, pictures or images of something."""
    return any(_is_intent(token) for token in _tokens(query))


def photo_subject(query: str) -> str:
    """What the photographs are to show: the question without the request words ("show me a photo of Kirloskar" is "Kirloskar"). "" if nothing is left."""
    kept = [t for t in _tokens(query) if not _is_intent(t) and t.lower() not in FILLER]
    return " ".join(kept)[:MAX_SUBJECT_CHARS].strip()


@dataclass(frozen=True)
class Spellings:
    """How a subject may be written: `english` is for CLIP (an English phrase), `names` are spellings (Devanagari and Latin) to look for in captions."""

    english: str
    names: tuple[str, ...] = field(default_factory=tuple)


SPELLING_PROMPT = (
    "You help search a Marathi and English magazine archive for photographs. The user asked for photographs of the subject below. Reply with JSON only: "
    '{"english": "<the subject as a short English phrase, for an image-search model>", '
    '"spellings": ["<up to 6 ways the names and key words may be printed in a caption: Marathi (Devanagari) spellings first, then English>"]}. '
    "Keep person and company names as names (do not translate them). No commentary."
)


def spellings(subject: str, generate=None) -> Spellings:
    """The English phrase and the caption spellings for a subject, from Gemini (asked once per subject, remembered). If that fails, the subject itself."""
    return _spellings_cached(subject, generate) if generate is not None else _spellings_default(subject)


@lru_cache(maxsize=512)
def _spellings_default(subject: str) -> Spellings:
    from generation.answer import _generate

    return _spellings_cached(subject, _generate)


def _spellings_cached(subject: str, generate) -> Spellings:
    fallback = Spellings(english=subject, names=(subject,) if subject else ())
    if not subject:
        return fallback
    try:
        raw = generate(f"Subject: {subject}", system_instruction=SPELLING_PROMPT, max_output_tokens=300, temperature=0.0, json_output=True)
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.IGNORECASE))
        english = data.get("english")
        names = data.get("spellings")
        if not isinstance(english, str) or not english.strip() or not isinstance(names, list):
            return fallback
        cleaned = tuple(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and 2 <= len(n.strip()) <= 80))[:8]
        return Spellings(english=english.strip()[:200], names=tuple(dict.fromkeys((*cleaned, subject))))
    except Exception as exc:  # no answer, bad JSON, quota: the subject as typed still searches
        print(f"[photos] spellings failed ({type(exc).__name__}): {str(exc)[:100]}")
        return fallback
