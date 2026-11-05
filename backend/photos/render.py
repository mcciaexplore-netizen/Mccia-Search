"""Drawing a page for the photo pipeline: at a chosen resolution, never more than MAX_SIDE pixels on the longest side (see MAX_RENDER_SIDE in ingest/ocr.py:
a page with a wrong box can otherwise need gigapixels)."""

import io

import pymupdf
from PIL import Image

MAX_SIDE = 3500


def render_page(page, dpi: float) -> Image.Image:
    """A pymupdf page as an RGB PIL image at `dpi`, or smaller if the page box would make it larger than MAX_SIDE pixels."""
    longest = max(page.rect.width, page.rect.height)
    if longest > 0:
        dpi = min(dpi, 72 * MAX_SIDE / longest)
    scale = dpi / 72
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
    return Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
