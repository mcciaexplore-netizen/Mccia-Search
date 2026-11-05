"""Is there a face in a photograph? OpenCV's YuNet (MIT licence, 232 KB, runs on the CPU). Detection only: nobody is identified."""

import os
import urllib.request
from pathlib import Path

import numpy as np

MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
MODEL_PATH = Path(os.environ.get("YUNET_MODEL") or Path.home() / ".cache" / "mccia" / "face_detection_yunet_2023mar.onnx")
SCORE = 0.5  # lower than OpenCV's usual 0.9: halftone faces score low (a 0.61 portrait and 0.84 / 0.90 faces were found at 0.6, a group photo of about 20 was not)
SMALL_SIDE = 700  # a crop smaller than this is also looked at twice as large, which finds small faces


def ensure_model() -> Path:
    if not MODEL_PATH.exists():
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = MODEL_PATH.with_suffix(".tmp")
        urllib.request.urlretrieve(MODEL_URL, tmp)
        tmp.replace(MODEL_PATH)
    return MODEL_PATH


class FaceDetector:
    def __init__(self):
        self._detector = None

    def count(self, image) -> int:
        """How many faces are in a PIL image (the larger of the counts at its own size and, for a small crop, at twice the size)."""
        import cv2

        if self._detector is None:
            self._detector = cv2.FaceDetectorYN.create(str(ensure_model()), "", (320, 320), SCORE, 0.3, 5000)
        best = 0
        sizes = [1.0] + ([2.0] if min(image.size) < SMALL_SIDE else [])
        for scale in sizes:
            arr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
            if scale != 1.0:
                arr = cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            self._detector.setInputSize((arr.shape[1], arr.shape[0]))
            _, faces = self._detector.detect(arr)
            best = max(best, 0 if faces is None else len(faces))
        return best
