"""One page in, its photographs out: detect (Florence-2), cut out, say what each is (CLIP), count faces, read the caption. Models are passed in."""

import io
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from photos.boxes import Box
from photos.caption import read_caption
from photos.classify import KINDS, PROMPTS, classify
from photos.detect import find_photos

DETECT_DPI = 100  # Florence-2 looks at 768 x 768 pixels whatever it is given; 100 DPI is plenty
CUT_DPI = 200  # the photographs are cut from a sharper drawing of the page
TEXT_DROP = 0.6  # a detected box that CLIP says is text with at least this probability is not a photo
MAX_JPEG_SIDE = 1600
JPEG_QUALITY = 85
MAX_FACE_SIDE = 1400


@dataclass
class Photo:
    index: int  # which photo on the page, 0, 1, ...
    box: Box  # in pixels of the sharp drawing
    fractions: tuple[float, float, float, float]  # the box as shares of the page, 0-1
    kind: str  # photo | advertisement | illustration
    kind_score: float
    jpeg: bytes
    width: int
    height: int
    vector: np.ndarray  # 512 numbers, unit length
    faces: int
    caption: str
    whole_page: bool  # the picture fills the page (a cover, a full-page advertisement)
    scores: dict[str, float] = field(default_factory=dict)


@dataclass
class Models:
    detect: Callable  # image -> list[Box]
    clip: object  # .image_vectors(list[image]) -> n x 512
    kind_vectors: dict[str, np.ndarray]
    faces: object  # .count(image) -> int
    ocr_png: Callable[[bytes], str]


def default_models() -> Models:
    """The real models. Loading is lazy: nothing heavy happens until a page is processed."""
    from ingest import ocr
    from photos.clip_model import ClipModel
    from photos.detect import FlorenceDetector
    from photos.faces import FaceDetector

    ocr._check_tesseract()
    clip = ClipModel()
    return Models(
        detect=FlorenceDetector(),
        clip=clip,
        kind_vectors={kind: clip.text_vectors(PROMPTS[kind]) for kind in KINDS},
        faces=FaceDetector(),
        ocr_png=lambda png: _tesseract(png),
    )


def _tesseract(png: bytes) -> str:
    from ingest import ocr

    try:
        return ocr._ocr_page_tesseract(png, 0)
    except ocr.OCRPageError:
        return ""


def process_page(small, big, models: Models) -> list[Photo]:
    """The photographs of one page. `small` is the page drawn for detection, `big` the same page drawn sharper (same shape, more pixels)."""
    boxes, whole_page = find_photos(small, models.detect)
    candidates = [(box, False) for box in boxes]
    if whole_page:
        candidates.append((Box(0, 0, small.width, small.height), True))
    if not candidates:
        return []

    sx, sy = big.width / small.width, big.height / small.height
    cuts = []
    for box, _ in candidates:
        big_box = box.scaled(sx, sy).clipped(big.width, big.height)
        cuts.append((big_box, big.crop((int(big_box.x0), int(big_box.y0), int(big_box.x1), int(big_box.y1)))))
    vectors = models.clip.image_vectors([crop for _, crop in cuts])
    verdicts = classify(vectors, models.kind_vectors)

    photos: list[Photo] = []
    for (box, is_whole), (big_box, crop), vector, (kind, probability, scores) in zip(candidates, cuts, vectors, verdicts):
        if kind == "text" and (is_whole or probability >= TEXT_DROP):
            continue  # a text page (every one answers with a page-sized box) or a text block
        if kind == "text":
            kind, probability = max(((k, scores[k]) for k in scores if k != "text"), key=lambda kv: kv[1])
        photos.append(
            Photo(
                index=len(photos), box=big_box, fractions=big_box.fractions(big.width, big.height), kind=kind, kind_score=probability,
                jpeg=_jpeg(crop), width=crop.width, height=crop.height, vector=vector, faces=_faces(models, crop),
                caption="" if is_whole else read_caption(big, big_box, models.ocr_png), whole_page=is_whole, scores=scores,
            )
        )
    return photos


def _faces(models: Models, crop) -> int:
    scale = MAX_FACE_SIDE / max(crop.size)
    if scale < 1:
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)))
    return models.faces.count(crop)


def _jpeg(crop) -> bytes:
    scale = MAX_JPEG_SIDE / max(crop.size)
    if scale < 1:
        crop = crop.resize((max(int(crop.width * scale), 1), max(int(crop.height * scale), 1)))
    buffer = io.BytesIO()
    crop.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()
