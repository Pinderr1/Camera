"""Download the Guardian test clips listed in config/guardian/videos.json.

    python scripts/guardian/fetch_wildlife_videos.py [--only black_bear_day,...]

For each clip the Wikimedia Commons API is asked for the file's current licence
and SHA-1. A clip whose licence differs from the registry, or whose downloaded
bytes do not match Commons' SHA-1, is refused. Files go to data/guardian/videos/
(git-ignored) with an ATTRIBUTION.md; they are for local testing only and are
never committed or redistributed."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "SentinelGuardian/1.0 (wildlife detector evaluation; local testing only)"


def commons_info(titles: list[str]) -> dict[str, dict]:
    """Current licence, author, SHA-1 and URL of each file, in one API call (Commons
    rate-limits per-file queries), retried with backoff when it answers 429."""
    query = urllib.parse.urlencode({"action": "query", "format": "json", "titles": "|".join(f"File:{t}" for t in titles),
                                    "prop": "imageinfo", "iiprop": "url|size|sha1|extmetadata"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{API}?{query}", headers={"User-Agent": USER_AGENT}), timeout=30) as response:
                data = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 4:
                raise
            time.sleep(15 * (attempt + 1))
    normalized = {item["to"]: item["from"] for item in data["query"].get("normalized", [])}
    out = {}
    for page in data["query"]["pages"].values():
        if "imageinfo" not in page:
            continue
        info = page["imageinfo"][0]
        meta = info.get("extmetadata", {})
        title = normalized.get(page["title"], page["title"]).removeprefix("File:")
        out[title] = {"url": info["url"].split("?")[0], "sha1": info["sha1"], "bytes": info["size"], "page": info["descriptionurl"],
                      "licence": (meta.get("LicenseShortName") or {}).get("value", ""),
                      "artist": re.sub("<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()}
    return out


def sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=ROOT / "config/guardian/videos.json")
    parser.add_argument("--out", type=Path, default=ROOT / "data/guardian/videos")
    parser.add_argument("--only", default="", help="comma-separated clip ids")
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    wanted = set(filter(None, args.only.split(",")))
    args.out.mkdir(parents=True, exist_ok=True)
    clips = [clip for clip in registry["clips"] if not wanted or clip["id"] in wanted]
    infos = commons_info([clip["title"] for clip in clips])
    attribution, failures = [], 0
    for clip in clips:
        target = args.out / f"{clip['id']}.webm"
        info = infos.get(clip["title"])
        if info is None:
            print(f"REFUSED {clip['id']}: {clip['title']!r} is no longer on Commons")
            failures += 1
            continue
        if info["licence"] != clip["licence"]:
            print(f"REFUSED {clip['id']}: Commons now says {info['licence']!r}, registry says {clip['licence']!r}")
            failures += 1
            continue
        if info["sha1"] != clip["sha1"]:
            print(f"REFUSED {clip['id']}: the file on Commons changed (SHA-1 {info['sha1']}); review it and update the registry")
            failures += 1
            continue
        if not (target.exists() and sha1(target) == clip["sha1"]):
            partial = target.with_suffix(".part")
            with urllib.request.urlopen(urllib.request.Request(info["url"], headers={"User-Agent": USER_AGENT}), timeout=120) as response, \
                    partial.open("wb") as handle:
                while block := response.read(1 << 20):
                    handle.write(block)
            if sha1(partial) != clip["sha1"]:
                partial.unlink()
                print(f"REFUSED {clip['id']}: downloaded bytes do not match SHA-1 {clip['sha1']}")
                failures += 1
                continue
            partial.replace(target)
            print(f"downloaded {clip['id']} ({info['bytes'] // 1_000_000} MB)")
        else:
            print(f"present {clip['id']}")
        attribution.append(f"- `{clip['id']}.webm`: \"{clip['title']}\" by {info['artist'] or clip['author']}, {clip['licence']}, {info['page']}")
    (args.out / "ATTRIBUTION.md").write_text(
        "# Guardian test clips\n\nLocal testing only; not redistributed. Source: Wikimedia Commons.\n\n" + "\n".join(attribution) + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
