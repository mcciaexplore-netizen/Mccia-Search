"""CLIP (OpenAI ViT-B/16, 512-dimension vectors, free): what a picture shows, searchable in words. It cannot read names: those come from captions."""

import threading

import numpy as np

CLIP_REPO = "openai/clip-vit-base-patch16"
DIMENSION = 512
BATCH = 16


class ClipModel:
    def __init__(self, repo: str = CLIP_REPO):
        self.repo = repo
        self._lock = threading.Lock()
        self._model = self._processor = self._device = None

    def _load(self):
        with self._lock:
            if self._model is not None:
                return
            import torch
            from transformers import CLIPModel, CLIPProcessor

            self._device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._model = CLIPModel.from_pretrained(self.repo).to(self._device).eval()
            self._processor = CLIPProcessor.from_pretrained(self.repo)

    @staticmethod
    def _unit(features) -> np.ndarray:
        import torch

        tensor = features if torch.is_tensor(features) else features.pooler_output
        return torch.nn.functional.normalize(tensor, dim=-1).cpu().numpy().astype(np.float32)

    def image_vectors(self, images: list) -> np.ndarray:
        """Unit-length vectors (n x 512) of PIL images."""
        import torch

        if not images:
            return np.zeros((0, DIMENSION), dtype=np.float32)
        self._load()
        out = []
        for start in range(0, len(images), BATCH):
            batch = [im.convert("RGB") for im in images[start : start + BATCH]]
            with torch.inference_mode():
                out.append(self._unit(self._model.get_image_features(**self._processor(images=batch, return_tensors="pt").to(self._device))))
        return np.concatenate(out)

    def text_vectors(self, texts: list[str]) -> np.ndarray:
        """Unit-length vectors (n x 512) of texts (English works; Marathi does not)."""
        import torch

        self._load()
        with torch.inference_mode():
            return self._unit(self._model.get_text_features(**self._processor(text=texts, return_tensors="pt", padding=True, truncation=True).to(self._device)))
