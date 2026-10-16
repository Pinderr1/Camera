"""Streaming metadata for very large camera-trap datasets (stdlib).

NACTI's JSON metadata is 1.17 GB uncompressed and does not fit in memory on a
12 GB machine. LILA also publishes it as CSV (`nacti_metadata.csv.zip`, 31 MB
zipped, about 600 MB uncompressed), which is read here row by row straight out
of the zip, so memory stays bounded however large the dataset is. The rows feed
the same label rules, selection, duplicate checks, splits and manifest as
`datasets.py`.

    python -m sentinel.guardian.dataset_stream --source nacti \\
        --quota moose=40 elk=40 uncertain:elk=15 black_bear=30 cougar=30 negative=20 \\
        --out data/guardian/runs/nacti_initial --max-mb 300

Location: the CSV's `location` is a study area (e.g. "San Juan Mntns, Colorado"),
coarser than the JSON's per-camera id. Splitting by it is stricter (a whole area
goes to one side), never leakier.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterator

from . import datasets as ds

CSV_FIELDS = {"id", "filename", "location", "name"}


def iter_lila_csv(source_id: str, path: Path) -> Iterator[ds.SourceImage]:
    """Yield one SourceImage per image from a LILA metadata CSV (optionally zipped).
    Consecutive rows with the same image id (several species in one image) merge."""
    with _open_text(path) as handle:
        reader = csv.DictReader(handle)
        missing = CSV_FIELDS - set(reader.fieldnames or [])
        if missing:
            raise ds.DatasetError(f"{path.name}: CSV lacks columns {sorted(missing)}")
        current: ds.SourceImage | None = None
        for row in reader:
            image_id = row["id"]
            annotation = {"category": row["name"], **{k: row[k] for k in ("category_id", "genus", "family", "common_name") if row.get(k)}}
            if current is not None and current.image_id == image_id:
                current.annotations.append(annotation)
                if row["name"] not in current.labels:
                    current.labels = sorted(current.labels + [row["name"]])
                continue
            if current is not None:
                yield current
            current = ds.SourceImage(
                dataset=source_id, image_id=image_id, file_name=row["filename"], labels=[row["name"]],
                annotations=[annotation], location=row.get("location") or None, seq_id=None,
                width=_int(row.get("width")), height=_int(row.get("height")),
                extra={k: row[k] for k in ("study",) if row.get(k)},
            )
        if current is not None:
            yield current


def plan_from_images(source: ds.Source, images: Iterator[ds.SourceImage], quotas: dict[str, int],
                     seed: int = 7, per_location: int = 40) -> ds.Plan:
    """Same selection as datasets.build_plan, over an iterator, with bounded memory."""
    reservoirs: dict[tuple[str, str], list[ds.Candidate]] = {}
    seen: Counter = Counter()
    unmapped: Counter = Counter()
    rng = random.Random(seed)
    for image in images:
        unmapped.update(label for label in image.labels if label not in source.label_map)
        cand = ds.classify(image, source)
        if cand is None:
            continue
        key = (cand.bucket, image.location or image.image_id)
        seen[key] += 1
        if cand.bucket not in quotas:
            continue
        pool = reservoirs.setdefault(key, [])
        if len(pool) < per_location:
            pool.append(cand)
        else:
            slot = rng.randrange(seen[key])
            if slot < per_location:
                pool[slot] = cand
    available: dict[str, Counter] = defaultdict(Counter)
    for (bucket, _), count in seen.items():
        available[bucket][source.id] += count
    candidates = [c for pool in reservoirs.values() for c in pool]
    return ds.Plan(candidates, ds.select(candidates, quotas, seed=seed), {source.id: dict(unmapped.most_common())}, {},
                   {b: dict(c) for b, c in sorted(available.items())})


def _open_text(path: Path):
    if path.suffix == ".zip":
        archive = zipfile.ZipFile(path)
        names = [n for n in archive.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ds.DatasetError(f"{path.name}: expected one CSV file, found {names}")
        return _ZipText(archive, names[0])
    return path.open("r", encoding="utf-8", newline="")


class _ZipText:
    def __init__(self, archive: zipfile.ZipFile, name: str) -> None:
        self.archive, self.name = archive, name

    def __enter__(self):
        self.raw = self.archive.open(self.name)
        self.text = io.TextIOWrapper(self.raw, encoding="utf-8", newline="")
        return self.text

    def __exit__(self, *exc):
        self.text.close()
        self.archive.close()


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.guardian.dataset_stream")
    parser.add_argument("--source", required=True)
    parser.add_argument("--registry", default=str(ds.DEFAULT_REGISTRY))
    parser.add_argument("--cache", default=str(ds.DEFAULT_CACHE))
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-mb", type=float, default=300.0)
    parser.add_argument("--quota", nargs="+", required=True, help="bucket=count")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    registry = ds.load_registry(args.registry)
    source = registry.get(args.source)
    url = getattr(source, "metadata_csv_url", None) or _registry_field(args.registry, args.source, "metadata_csv_url")
    if not url:
        raise SystemExit(f"{args.source} has no metadata_csv_url in the registry")
    if not source.commercial:
        raise SystemExit(f"{args.source}: licence commercial_use={source.licence.get('commercial_use')}; not fetched")
    cache = Path(args.cache) / source.id
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / url.rsplit("/", 1)[-1]
    if not path.exists():
        payload = ds.http_fetch(url, timeout=600)
        if len(payload) > ds.MAX_METADATA_BYTES:
            raise SystemExit(f"{url} is larger than {ds.MAX_METADATA_BYTES} bytes")
        path.write_bytes(payload)
    quotas = {}
    for item in args.quota:
        bucket, _, count = item.partition("=")
        quotas[bucket] = int(count)
    plan = plan_from_images(source, iter_lila_csv(source.id, path), quotas, seed=args.seed)
    summary = ds.fetch_images(plan, registry, Path(args.out), int(args.max_mb * 1024 * 1024), Path(args.cache))
    print(json.dumps({k: summary[k] for k in ("images", "bytes", "buckets", "split_leaks", "skipped_over_budget")}, indent=2))
    print(f"available: {json.dumps(plan.available)}")
    print(f"duplicates={len(summary['duplicates'])} failures={len(summary['failures'])}")
    return 0


def _registry_field(registry_path: str, source_id: str, key: str):
    raw = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    return next((s.get(key) for s in raw.get("sources", []) if s.get("id") == source_id), None)


if __name__ == "__main__":
    sys.exit(main())
