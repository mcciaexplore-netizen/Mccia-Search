"""Boxes on a page and the rules that decide which of Florence-2's answers are real photographs. Pure: no model, no image library."""

from dataclasses import dataclass

# Florence-2's open-vocabulary "photograph" on a page with no distinct photo answers with one box that is the whole page (95-100% of it on every one of
# 13 text-only pages tried), so a box this large is "nothing found" or a full-page picture, never an ordinary photo.
PAGE_SIZED = 0.88
MIN_SIDE_FRACTION = 0.04  # a box narrower or shorter than this share of the page is a speck, not a photo
DUPLICATE_IOU = 0.6


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return max(self.x1 - self.x0, 0.0)

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.0)

    @property
    def area(self) -> float:
        return self.width * self.height

    def intersection(self, other: "Box") -> float:
        return max(min(self.x1, other.x1) - max(self.x0, other.x0), 0.0) * max(min(self.y1, other.y1) - max(self.y0, other.y0), 0.0)

    def iou(self, other: "Box") -> float:
        inter = self.intersection(other)
        union = self.area + other.area - inter
        return inter / union if union else 0.0

    def clipped(self, width: float, height: float) -> "Box":
        return Box(max(self.x0, 0.0), max(self.y0, 0.0), min(self.x1, width), min(self.y1, height))

    def scaled(self, sx: float, sy: float) -> "Box":
        return Box(self.x0 * sx, self.y0 * sy, self.x1 * sx, self.y1 * sy)

    def fractions(self, width: float, height: float) -> tuple[float, float, float, float]:
        """The box as shares of the page (0-1), which stay true however large the page is drawn."""
        return (self.x0 / width, self.y0 / height, self.x1 / width, self.y1 / height)


def box_from(values) -> Box:
    x0, y0, x1, y1 = (float(v) for v in values)
    return Box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def page_share(box: Box, width: float, height: float) -> float:
    """How much of the page the box covers (0-1)."""
    return box.area / (width * height) if width > 0 and height > 0 else 0.0


def is_page_sized(box: Box, width: float, height: float) -> bool:
    return page_share(box.clipped(width, height), width, height) >= PAGE_SIZED


def is_speck(box: Box, width: float, height: float) -> bool:
    return box.width < MIN_SIDE_FRACTION * width or box.height < MIN_SIDE_FRACTION * height


def distinct(boxes: list[Box], known: list[Box] | None = None) -> list[Box]:
    """The boxes that are not (nearly) another box in the list or in `known`, in order."""
    kept: list[Box] = []
    seen = list(known or [])
    for box in boxes:
        if any(box.iou(other) >= DUPLICATE_IOU for other in seen):
            continue
        kept.append(box)
        seen.append(box)
    return kept


def split_answer(boxes: list[Box], width: float, height: float) -> tuple[list[Box], bool]:
    """Florence's boxes for one page as (the real photographs, whether it also answered with a page-sized box).

    Specks go, boxes are clipped to the page, a page-sized box is set apart (it is either "no photo here" or a picture that fills the page, which the
    caller tells apart by looking at it), and near-duplicates are dropped.
    """
    photos, whole_page = [], False
    for raw in boxes:
        box = raw.clipped(width, height)
        if box.area <= 0 or is_speck(box, width, height):
            continue
        if is_page_sized(box, width, height):
            whole_page = True
        else:
            photos.append(box)
    return distinct(photos), whole_page
