"""Download public, human-annotated surveillance footage for the Sentinel benchmark.

Everything lands in data/bench/ (git-ignored). Sources and terms:

- MOTChallenge MOT16 train sequences (CC BY-NC-SA 3.0): street scenes with
  per-frame pedestrian boxes. Only the needed sequences are pulled out of the
  1.9 GB archive using HTTP range requests, then re-encoded to H.264 MP4 with
  ffmpeg so they behave like a recorded camera clip.
- CAVIAR (EC Funded CAVIAR project/IST 2001 37540, research use with
  acknowledgement): INRIA lobby clips with per-frame boxes and behaviour labels.
- OpenCV sample vtest.avi (BSD sample data): unlabeled pedestrian footage used
  only for throughput and ordinary-activity alert counts.

Evaluation use only. None of this data may be used to train a shipped model.

Usage: python scripts/fetch_bench_footage.py [--only mot|caviar|opencv]
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path("data/bench")
MOT_URL = "https://motchallenge.net/data/MOT16.zip"
MOT_SEQUENCES = ["MOT16-02", "MOT16-04", "MOT16-09"]
CAVIAR_BASE = "https://homepages.inf.ed.ac.uk/rbf/CAVIARDATA1"
CAVIAR_CLIPS = {
    "Walk1": "wk1gt.xml",
    "Browse1": "br1gt.xml",
    "Meet_WalkTogether1": "mwt1gt.xml",
    "LeftBag": "lb1gt.xml",
    "Rest_InChair": "ricgt.xml",
}
OPENCV_VTEST = "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/vtest.avi"


class HttpRangeFile(io.RawIOBase):
    """Seekable read-only view of a remote file, so zipfile can read the
    central directory and individual members without fetching everything."""

    def __init__(self, url: str, block: int = 4 << 20) -> None:
        self.url = url
        self.block = block
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=60) as response:
            self.size = int(response.headers["Content-Length"])
        self.position = 0
        self._cache_start = -1
        self._cache = b""
        self._span: tuple[int, int, object] | None = None  # (start, end, local file) prefetched range

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.position, 2: self.size}[whence]
        self.position = max(0, min(self.size, base + offset))
        return self.position

    def prefetch(self, start: int, end: int, local) -> None:
        """Stream [start, end) into a local file with one request; later reads inside it stay local."""
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={start}-{end - 1}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            shutil.copyfileobj(response, local, length=1 << 20)
        local.flush()
        self._span = (start, end, local)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self.size - self.position
        if self._span and self._span[0] <= self.position and self.position + size <= self._span[1]:
            local = self._span[2]
            local.seek(self.position - self._span[0])
            data = local.read(size)
            self.position += len(data)
            return data
        out = bytearray()
        while size > 0 and self.position < self.size:
            if not (self._cache_start <= self.position < self._cache_start + len(self._cache)):
                self._fill(self.position, max(size, self.block))
            offset = self.position - self._cache_start
            chunk = self._cache[offset : offset + size]
            out += chunk
            self.position += len(chunk)
            size -= len(chunk)
        return bytes(out)

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def _fill(self, start: int, length: int) -> None:
        end = min(self.size, start + length) - 1
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            self._cache = response.read()
        self._cache_start = start


def ffmpeg() -> str:
    """ffmpeg on PATH, else $FFMPEG, else the binary bundled with the optional
    imageio-ffmpeg package (pip install imageio-ffmpeg; nothing system-wide)."""
    import os

    found = shutil.which("ffmpeg") or os.environ.get("FFMPEG")
    if not found:
        try:
            import imageio_ffmpeg

            found = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pass
    if not found:
        raise SystemExit("ffmpeg is needed to encode the MOT16 frames: install it, set FFMPEG=<path to ffmpeg.exe>, "
                         "or pip install imageio-ffmpeg")
    return found


def fetch_mot() -> None:
    encoder = ffmpeg()  # fail before downloading anything
    remote = HttpRangeFile(MOT_URL)
    with zipfile.ZipFile(remote) as archive:
        names = archive.namelist()
        for sequence in MOT_SEQUENCES:
            target = ROOT / "mot16" / sequence
            video = target / f"{sequence}.mp4"
            if video.exists():
                print(f"{sequence}: already present")
                continue
            prefix = f"train/{sequence}/"
            members = [name for name in names if name.startswith(prefix) and not name.endswith("/")]
            infos = sorted((archive.getinfo(name) for name in members), key=lambda info: info.header_offset)
            members = [info.filename for info in infos]
            frames = [info for info in infos if info.filename.startswith(prefix + "img1/")]
            span_start = frames[0].header_offset
            span_end = frames[-1].header_offset + 30 + len(frames[-1].filename.encode()) + 1024 + frames[-1].compress_size
            target.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryFile() as span_file:
                print(f"{sequence}: downloading {(span_end - span_start) / 1e6:.0f} MB")
                remote.prefetch(span_start, min(span_end, remote.size), span_file)
                for member in members:
                    relative = member[len(prefix) :]
                    if relative.startswith("img1/"):
                        destination = Path(temporary) / Path(relative).name
                    elif relative in {"gt/gt.txt", "seqinfo.ini"}:
                        destination = target / Path(relative).name
                    else:
                        continue
                    with archive.open(member) as source, destination.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
                frame_rate = _seqinfo(target / "seqinfo.ini").get("frameRate", "30")
                print(f"{sequence}: encoding {len(list(Path(temporary).glob('*.jpg')))} frames")
                subprocess.run(
                    [encoder, "-loglevel", "error", "-y", "-framerate", frame_rate, "-i", str(Path(temporary) / "%06d.jpg"),
                     "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(video)],
                    check=True,
                )


def make_substreams() -> None:
    """640x360 @ 10 fps copies, standing in for a camera's low-resolution sub-stream."""
    for sequence in MOT_SEQUENCES:
        source = ROOT / "mot16" / sequence / f"{sequence}.mp4"
        target = source.with_name("substream_640p10.mp4")
        if source.exists() and not target.exists():
            print(f"{sequence}: encoding 640p10 sub-stream copy")
            subprocess.run(
                [ffmpeg(), "-nostdin", "-loglevel", "error", "-y", "-i", str(source), "-vf", "scale=640:-2", "-r", "10",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-g", "20", str(target)],
                check=True,
            )


def _seqinfo(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def fetch_caviar() -> None:
    for clip, gt in CAVIAR_CLIPS.items():
        target = ROOT / "caviar" / clip
        target.mkdir(parents=True, exist_ok=True)
        for name in (f"{clip}.mpg", gt):
            destination = target / name
            if not destination.exists():
                print(f"CAVIAR {clip}: {name}")
                urllib.request.urlretrieve(f"{CAVIAR_BASE}/{clip}/{name}", destination)


def fetch_opencv() -> None:
    destination = ROOT / "unlabeled" / "vtest.avi"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        print("OpenCV vtest.avi")
        urllib.request.urlretrieve(OPENCV_VTEST, destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=["mot", "caviar", "opencv"])
    args = parser.parse_args()
    for name, fetch in (("caviar", fetch_caviar), ("opencv", fetch_opencv), ("mot", fetch_mot)):
        if args.only in (None, name):
            fetch()
    if args.only in (None, "mot"):
        make_substreams()


if __name__ == "__main__":
    main()
