"""Finding the photographs on a scanned page with Florence-2 (free, runs on this machine)."""

import glob
import os
import threading
from typing import Callable

from photos.boxes import Box, box_from, distinct, split_answer

FLORENCE_REPO = "florence-community/Florence-2-large"  # the base model was tried and is not reliable: it returns the whole page for some photos
MAX_PHOTOS_PER_PAGE = 6
TASK = "<OPEN_VOCABULARY_DETECTION>"
LABEL = "photograph"
# Florence answers with one box per question, so after each answer the box is painted over and the page is asked again, until nothing new is found.
MASK_MARGIN = 4  # pixels painted beyond the box, so its frame does not look like a new photo


class FlorenceDetector:
    """Florence-2-large through transformers, loaded on first use: the boxes of one page image. Greedy decoding (identical boxes to beam search, faster)."""

    def __init__(self, repo: str = FLORENCE_REPO):
        self.repo = repo
        self._lock = threading.Lock()
        self._model = self._processor = self._device = None

    def _load(self):
        with self._lock:
            if self._model is not None:
                return
            import torch
            from huggingface_hub import snapshot_download
            from transformers import AutoProcessor, Florence2ForConditionalGeneration

            path = snapshot_download(self.repo)
            self._device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._processor = AutoProcessor.from_pretrained(path)
            self._model = Florence2ForConditionalGeneration.from_pretrained(path, torch_dtype=torch.float32).to(self._device).eval()
            print(f"[photos] Florence-2 loaded on {self._device}")

    def __call__(self, image, label: str = LABEL) -> list[Box]:
        import torch

        self._load()
        inputs = self._processor(text=TASK + label, images=image, return_tensors="pt").to(self._device)
        with torch.inference_mode():
            ids = self._model.generate(**inputs, max_new_tokens=256, num_beams=1, do_sample=False)
        raw = self._processor.batch_decode(ids, skip_special_tokens=False)[0]
        parsed = self._processor.post_process_generation(raw, task=TASK, image_size=image.size)[TASK]
        return [box_from(b) for b in parsed.get("bboxes", [])]


def find_photos(image, detect: Callable, max_photos: int = MAX_PHOTOS_PER_PAGE) -> tuple[list[Box], bool]:
    """(the photographs on a page image, whether Florence only answered with a page-sized box).

    `detect(image)` returns Florence's boxes. A page-sized answer on the first question, with no photo found, means either "no photo here" (every text
    page) or a picture that fills the page (a cover): the caller looks at the picture to tell which.
    """
    from PIL import ImageDraw

    width, height = image.size
    work = image.copy()
    found: list[Box] = []
    whole_page = False
    for _ in range(max_photos):
        photos, page_sized = split_answer(detect(work), width, height)
        fresh = distinct(photos, known=found)
        if not fresh:
            whole_page = page_sized and not found
            break
        found.extend(fresh)
        painter = ImageDraw.Draw(work)
        for box in fresh:
            painter.rectangle((box.x0 - MASK_MARGIN, box.y0 - MASK_MARGIN, box.x1 + MASK_MARGIN, box.y1 + MASK_MARGIN), fill=(255, 255, 255))
    return found, whole_page
