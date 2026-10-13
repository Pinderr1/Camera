"""Synthetic degradations applied to REAL footage to stress-test conditions we
have no labeled recordings for (heavy low light, IR-like night, rain).
Box labels stay valid because geometry is unchanged. Results from these runs
are always reported as synthetic and never substitute for real recordings."""

from __future__ import annotations

from typing import Any

DEGRADATIONS = ("low_light", "night_ir", "rain")


def apply(image: Any, kind: str | None, frame_index: int, seed: int = 7) -> Any:
    if not kind:
        return image
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed + frame_index)
    if kind == "low_light":
        dark = np.power(image.astype(np.float32) / 255.0, 1.8) * 255.0 * 0.45
        noisy = dark + rng.normal(0, 6, image.shape)
        return np.clip(noisy, 0, 255).astype(np.uint8)
    if kind == "night_ir":
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
        flat = gray * 0.55 + 40 + rng.normal(0, 9, gray.shape)
        flat = cv2.GaussianBlur(np.clip(flat, 0, 255).astype(np.uint8), (3, 3), 0)
        return cv2.cvtColor(flat, cv2.COLOR_GRAY2BGR)
    if kind == "rain":
        height, width = image.shape[:2]
        streaks = np.zeros((height, width), np.uint8)
        layer = np.random.default_rng(seed)  # fixed drop field, scrolled per frame
        count = int(width * height / 900)
        xs = layer.integers(0, width, count)
        ys = (layer.integers(0, height, count) + frame_index * max(4, height // 30)) % height
        length = max(6, height // 25)
        for x, y in zip(xs, ys):
            cv2.line(streaks, (int(x), int(y)), (int(x) - length // 4, int(y) + length), 200, 1)
        streaks = cv2.GaussianBlur(streaks, (3, 3), 0)
        hazy = cv2.addWeighted(image, 0.75, np.full_like(image, 120), 0.25, -10)
        hazy = cv2.GaussianBlur(hazy, (3, 3), 0)
        return cv2.add(hazy, cv2.cvtColor(streaks, cv2.COLOR_GRAY2BGR) // 2)
    raise ValueError(f"unknown degradation {kind!r}; choose from {DEGRADATIONS}")
