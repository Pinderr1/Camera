"""Face detection and face templates (I/O edge: OpenCV, numpy).

Models (OpenCV Zoo, downloaded by scripts/fetch_face_models.py into models/face/):
- YuNet 2023mar face detector, MIT licence;
- SFace 2021dec recognizer (MobileFaceNet with the SFace loss), Apache-2.0
  licence. Its training data is not documented by OpenCV Zoo; the SFace paper
  trained on CASIA-WebFace, VGGFace2 and MS-Celeb-1M, whose terms are research
  oriented. See sources.md: legal review is required before commercial sale.

Both run through OpenCV's DNN module on the CPU (cv2.FaceDetectorYN,
cv2.FaceRecognizerSF), which aligns the face on its five landmarks before
computing a 128-value template."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .gallery import EnrollmentError
from .settings import IdentitySettings

MAX_DETECT_SIDE = 1280  # enrollment photos are scaled down for detection; the face is aligned on the original


class FaceAnalyzer:
    def __init__(self, detector_path: str | Path, recognizer_path: str | Path, *, score_threshold: float = 0.6, cpu_threads: int = 1) -> None:
        import cv2

        for path in (detector_path, recognizer_path):
            if not Path(path).is_file():
                raise FileNotFoundError(f"{path} missing: run python scripts/fetch_face_models.py")
        cv2.setNumThreads(max(1, cpu_threads))
        self.detector = cv2.FaceDetectorYN.create(str(detector_path), "", (320, 320), score_threshold, 0.3, 20)
        self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")
        self._lock = threading.Lock()

    def detect(self, image_bgr: Any) -> list[Any]:
        """Face rows [x, y, w, h, 5 landmark pairs, score] in image pixels."""
        import cv2
        import numpy as np

        height, width = image_bgr.shape[:2]
        scale = min(1.0, MAX_DETECT_SIDE / max(height, width))
        work = image_bgr if scale == 1.0 else cv2.resize(image_bgr, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
        with self._lock:
            self.detector.setInputSize((work.shape[1], work.shape[0]))
            _, faces = self.detector.detect(work)
        if faces is None:
            return []
        rows = []
        for face in faces:
            row = np.array(face, dtype=np.float32)
            row[:14] /= scale
            rows.append(row)
        return rows

    def template(self, image_bgr: Any, face: Any) -> Any:
        """L2-normalised 128-value template of one detected face."""
        import numpy as np

        with self._lock:
            aligned = self.recognizer.alignCrop(image_bgr, face)
            feature = np.asarray(self.recognizer.feature(aligned), dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(feature))
        if norm == 0:
            raise ValueError("empty face template")
        return feature / norm

    def person_face(self, crop_bgr: Any, settings: IdentitySettings) -> tuple[str, Any, int]:
        """(outcome, template or None, face size in px) for the upper part of one person box.
        Two faces of similar size in one box are not compared: it is unclear whose face it is."""
        faces = [face for face in self.detect(crop_bgr) if float(face[14]) >= settings.min_face_score]
        if not faces:
            return "no_face", None, 0
        faces.sort(key=lambda face: float(face[2] * face[3]), reverse=True)
        if len(faces) > 1 and float(faces[1][2] * faces[1][3]) >= 0.5 * float(faces[0][2] * faces[0][3]):
            return "several_faces", None, 0
        size = int(min(faces[0][2], faces[0][3]))
        if size < settings.min_face_px:
            return "poor_face", None, size
        return "face", self.template(crop_bgr, faces[0]), size

    def enrollment_template(self, image_bgr: Any, settings: IdentitySettings) -> list[float]:
        """Template from one enrollment photo: exactly one clear, large face."""
        faces = [face for face in self.detect(image_bgr) if float(face[14]) >= 0.9]
        if not faces:
            raise EnrollmentError("no clear face found in the photo (face the camera, good light, no sunglasses)")
        if len(faces) > 1:
            raise EnrollmentError("more than one face in the photo: use a photo of the member alone")
        size = int(min(faces[0][2], faces[0][3]))
        if size < settings.enroll_min_face_px:
            raise EnrollmentError(f"the face is too small ({size} px; at least {settings.enroll_min_face_px} px): move closer")
        return [float(value) for value in self.template(image_bgr, faces[0])]


def decode_image(data: bytes) -> Any:
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise EnrollmentError("the file is not a readable image (JPEG or PNG)")
    return image


def person_crop(image_bgr: Any, bbox: tuple[float, float, float, float]) -> Any | None:
    """Copy of the upper part of a person box (where the face is), with a small margin."""
    height, width = image_bgr.shape[:2]
    x, y, w, h = bbox
    x0, x1 = max(0, int((x - 0.1 * w) * width)), min(width, int((x + 1.1 * w) * width))
    y0, y1 = max(0, int((y - 0.05 * h) * height)), min(height, int((y + 0.55 * h) * height))
    if x1 - x0 < 24 or y1 - y0 < 24:
        return None
    return image_bgr[y0:y1, x0:x1].copy()
