"""The printed caption of a photograph, read with Tesseract from the strip of page next to it. This is what names are searched in."""

import io
import re

from photos.boxes import Box

BELOW_HEIGHT = 0.075  # share of the page height: about three lines of a caption
ABOVE_HEIGHT = 0.06
SIDE_MARGIN = 0.03  # share of the page width added to each side of the strip
MIN_LETTERS = 6


def caption_strips(box: Box, width: float, height: float) -> list[Box]:
    """Where a caption may be, best first: just below the photo, then just above it."""
    left, right = max(box.x0 - SIDE_MARGIN * width, 0), min(box.x1 + SIDE_MARGIN * width, width)
    below = Box(left, min(box.y1 + 2, height), right, min(box.y1 + BELOW_HEIGHT * height, height))
    above = Box(left, max(box.y0 - ABOVE_HEIGHT * height, 0), right, max(box.y0 - 2, 0))
    return [s for s in (below, above) if s.width > 20 and s.height > 10]


def clean(text: str) -> str:
    """One line of text: words kept, runs of spaces and stray line breaks gone."""
    return re.sub(r"\s+", " ", text).strip(" \t\n|_-~")


def has_words(text: str) -> bool:
    return sum(ch.isalpha() for ch in text) >= MIN_LETTERS


def read_caption(page_image, box: Box, ocr_png) -> str:
    """The caption of the photo at `box` (in pixels of `page_image`), or "" if there is none.

    `ocr_png(png_bytes)` returns the text of an image; the first strip that holds words wins.
    """
    width, height = page_image.size
    for strip in caption_strips(box, width, height):
        crop = page_image.crop((int(strip.x0), int(strip.y0), int(strip.x1), int(strip.y1))).convert("L")
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        text = clean(ocr_png(buffer.getvalue()))
        if has_words(text):
            return text[:500]
    return ""
