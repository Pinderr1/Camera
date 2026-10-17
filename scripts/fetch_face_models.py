"""Download the face models used by household-member recognition into models/face/
(git-ignored). Checks size and SHA-256 so a changed file is never used silently.

  python scripts/fetch_face_models.py

YuNet 2023mar (MIT) and SFace 2021dec (Apache-2.0), both from OpenCV Zoo.
SFace's training data is not documented by OpenCV Zoo: see sources.md before
any commercial use."""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://github.com/opencv/opencv_zoo/raw/main/models/"
MODELS = {
    "face_detection_yunet_2023mar.onnx": ("face_detection_yunet/", "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "face_recognition_sface_2021dec.onnx": ("face_recognition_sface/", "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    target = ROOT / "models" / "face"
    target.mkdir(parents=True, exist_ok=True)
    failed = False
    for name, (folder, expected) in MODELS.items():
        path = target / name
        if path.is_file() and sha256(path) == expected:
            print(f"ok       {name}")
            continue
        temporary = path.with_suffix(".part")
        print(f"download {name}")
        urllib.request.urlretrieve(BASE + folder + name, temporary)
        actual = sha256(temporary)
        if actual != expected:
            temporary.unlink()
            print(f"FAILED   {name}: SHA-256 {actual} does not match {expected}", file=sys.stderr)
            failed = True
            continue
        temporary.replace(path)
        print(f"ok       {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
