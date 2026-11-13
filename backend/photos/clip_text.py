"""CLIP's text side, for searching photographs by what they show. Only the text tower is loaded (about 250 MB), on the CPU; the picture side ran when the photos were
extracted. It is the same model and the same 512-number space as photos.clip_model, so a sentence and a picture can be compared."""

import threading

import numpy as np

from photos.clip_model import CLIP_REPO, DIMENSION

PROMPT = "a photograph of {}"  # CLIP matches pictures to sentences like this better than to a bare word


class ClipText:
    def __init__(self, repo: str = CLIP_REPO):
        self.repo = repo
        self._lock = threading.Lock()
        self._model = self._tokenizer = None

    def _load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            from transformers import AutoTokenizer, CLIPTextModelWithProjection

            self._tokenizer = AutoTokenizer.from_pretrained(self.repo)
            self._model = CLIPTextModelWithProjection.from_pretrained(self.repo).eval()

    def warm_up(self) -> None:
        self.embed("warm up")

    def embed(self, text: str) -> np.ndarray:
        """The unit-length 512-number vector of a short English phrase."""
        import torch

        self._load()
        tokens = self._tokenizer([PROMPT.format(text.strip())], return_tensors="pt", padding=True, truncation=True, max_length=77)
        with torch.inference_mode():
            vector = self._model(**tokens).text_embeds[0]
        vector = torch.nn.functional.normalize(vector, dim=-1).numpy().astype(np.float32)
        if vector.shape != (DIMENSION,):
            raise RuntimeError(f"CLIP made a {vector.shape} vector, expected {DIMENSION} numbers")
        return vector
