"""Sentinel: multi-camera property security brain with a drone-ready interface."""

import os

__version__ = "0.1.0"

# OpenCV/FFmpeg warnings can echo stream URLs (with credentials); keep them out of logs.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
