"""Comparing Marathi and Hindi names that are spelt differently: पारखे and पार्खे, किर्लोस्कर and the OCR slip किर्लास्कर.

The consonant skeleton of a word is its consonants and nothing else: no vowel signs, no halant (the mark that joins consonants), no nasal dots, no joiners, no independent
vowels. Words that differ only in those marks have the same skeleton, which is exactly how a name's spelling varies between writers and how an OCR slip changes it.
"""

import re

# Devanagari consonants (क to ह, and the nukta forms), the only characters a skeleton keeps.
CONSONANTS = "क-हक़-य़"
_NOT_CONSONANT = re.compile(f"[^{CONSONANTS}]")
MIN_SKELETON = 3  # a skeleton shorter than this (कर, चा) would match inside almost anything and is not used


def skeleton(text: str) -> str:
    """The Devanagari consonants of a text, in order; "" for text that has none (Latin letters do not count)."""
    return _NOT_CONSONANT.sub("", text or "")


def significant_skeletons(spelling: str) -> list[str]:
    """The skeletons of the words of a spelling that are long enough to mean something."""
    return [s for s in (skeleton(word) for word in spelling.split()) if len(s) >= MIN_SKELETON]


def matches(spelling: str, caption_skeleton: str) -> int:
    """How many significant words a spelling has if ALL of them are found in the caption's skeleton, else 0.

    All of them must be found: a spelling such as "पार्खे उद्योग समूह" (Parkhe industrial group) must not match every caption that has the common word "उद्योग" (industry).
    """
    parts = significant_skeletons(spelling)
    return len(parts) if parts and all(part in caption_skeleton for part in parts) else 0
