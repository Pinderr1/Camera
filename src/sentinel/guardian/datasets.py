"""Guardian wildlife dataset ingestion (stdlib).

Downloads a small, licence-checked sample of individual camera-trap images
instead of whole archives, and writes a manifest that keeps every source label
and annotation next to the Guardian label derived from it.

Rules this module enforces:
- Sources come from a registry (`config/guardian/datasets.json`). A new
  dataset is a registry entry; a new metadata format is one parser in FORMATS.
- Label maps are explicit. A source label that is not in the map is never
  imported, and an uncertain label stays `uncertain`; nothing is promoted to a
  species the source did not name.
- Only sources whose licence allows commercial use are fetched, unless the
  caller opts in to research-only sources.
- Images whose source labels include an excluded class (people, vehicles) are
  never downloaded.
- Train/eval splits are by camera location (or sequence when there is no
  location), so one camera never appears on both sides; images with neither
  stay `unassigned`.
- Exact duplicates (same SHA-256) are dropped; the download stops at a byte
  budget.

    python -m sentinel.guardian.datasets plan  --out data/guardian/runs/initial
    python -m sentinel.guardian.datasets fetch --out data/guardian/runs/initial --max-mb 900
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import re
import sys
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator

DEFAULT_REGISTRY = Path("config/guardian/datasets.json")
DEFAULT_CACHE = Path("data/guardian/cache")
STATUSES = ("species", "uncertain", "negative", "excluded")
COMMERCIAL = "allowed"
NEGATIVE = "negative"
MAX_METADATA_BYTES = 200 * 1024 * 1024
MAX_JSON_BYTES = 512 * 1024 * 1024  # uncompressed; NACTI (1.17 GB) and WCS (838 MB) need a streaming parser
USER_AGENT = "Sentinel-Guardian-dataset-importer/1"

# Per-bucket image quotas for the first sample (about 450 images).
DEFAULT_QUOTAS = {
    "black_bear": 60, "grizzly_bear": 50, "cougar": 50, "wolf": 40, "coyote": 50,
    "moose": 50, "elk": 50, "uncertain:elk": 25, NEGATIVE: 80,
}


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class LabelRule:
    status: str
    target: str | None = None
    note: str = ""


@dataclass
class Source:
    id: str
    name: str
    page: str
    version: str
    format: str | None
    licence: dict[str, Any]
    label_map: dict[str, LabelRule]
    metadata_url: str | None = None
    image_base_url: str | None = None
    bbox_url: str | None = None
    md_results_url: str | None = None

    @property
    def commercial(self) -> bool:
        return self.licence.get("commercial_use") == COMMERCIAL

    @property
    def fetchable(self) -> bool:
        return bool(self.format and self.metadata_url and self.image_base_url)


@dataclass
class SourceImage:
    """One image as the source describes it (format-independent)."""

    dataset: str
    image_id: str
    file_name: str
    labels: list[str]
    annotations: list[dict[str, Any]]
    location: str | None = None
    seq_id: str | None = None
    width: int | None = None
    height: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    image: SourceImage
    bucket: str
    guardian_labels: list[dict[str, Any]]


@dataclass
class Registry:
    targets: list[str]
    sources: list[Source]

    def get(self, source_id: str) -> Source:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise DatasetError(f"unknown dataset {source_id!r}")


# ---------------------------------------------------------------- registry

def load_registry(path: str | Path = DEFAULT_REGISTRY) -> Registry:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_registry(raw)


def parse_registry(raw: dict[str, Any]) -> Registry:
    targets = raw.get("targets")
    if not isinstance(targets, list) or not targets:
        raise DatasetError("registry needs a non-empty 'targets' list")
    seen: set[str] = set()
    sources = []
    for entry in raw.get("sources", []):
        sid = entry.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9_]+", sid):
            raise DatasetError(f"bad dataset id {sid!r}")
        if sid in seen:
            raise DatasetError(f"duplicate dataset id {sid!r}")
        seen.add(sid)
        licence = entry.get("licence") or {}
        for key in ("id", "commercial_use", "conditions", "attribution"):
            if key not in licence:
                raise DatasetError(f"{sid}: licence.{key} is required")
        fmt = entry.get("format")
        if fmt is not None and fmt not in FORMATS:
            raise DatasetError(f"{sid}: unknown format {fmt!r}")
        rules = {}
        for label, rule in (entry.get("label_map") or {}).items():
            status = rule.get("status")
            if status not in STATUSES:
                raise DatasetError(f"{sid}: label {label!r} has bad status {status!r}")
            target = rule.get("target")
            if status in ("species", "uncertain") and target not in targets:
                raise DatasetError(f"{sid}: label {label!r} needs a target from {targets}")
            if status in ("negative", "excluded") and target:
                raise DatasetError(f"{sid}: {status} label {label!r} must not name a target")
            rules[label] = LabelRule(status=status, target=target, note=rule.get("note", ""))
        sources.append(Source(
            id=sid, name=entry.get("name", sid), page=entry.get("page", ""),
            version=entry.get("version", ""), format=fmt, licence=licence, label_map=rules,
            metadata_url=entry.get("metadata_url"), image_base_url=entry.get("image_base_url"),
            bbox_url=entry.get("bbox_url"), md_results_url=entry.get("md_results_url"),
        ))
    return Registry(targets=list(targets), sources=sources)


# ---------------------------------------------------------------- formats

def parse_coco_camera_traps(source_id: str, data: dict[str, Any],
                            bboxes: dict[str, Any] | None = None) -> Iterator[SourceImage]:
    """COCO Camera Traps (LILA): images, annotations with category_id, categories.
    Yields lazily: NACTI and WCS have millions of images."""
    categories = {c["id"]: c["name"] for c in data.get("categories", [])}
    per_image: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ann in data.get("annotations", []):
        per_image[str(ann["image_id"])].append(ann)
    box_images: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if bboxes:
        box_categories = {c["id"]: c["name"] for c in bboxes.get("categories", [])}
        for ann in bboxes.get("annotations", []):
            if "bbox" in ann:
                box_images[str(ann["image_id"])].append(
                    {**ann, "category": box_categories.get(ann.get("category_id"))})
    for img in data.get("images", []):
        image_id = str(img["id"])
        anns = per_image.get(image_id, [])
        annotations = [{**a, "category": categories.get(a.get("category_id"))} for a in anns]
        annotations += [dict(a, from_bbox_file=True) for a in box_images.get(image_id, [])
                        if not any(a.get("id") == b.get("id") for b in anns)]
        labels = sorted({a["category"] for a in annotations if a.get("category") and not a.get("from_bbox_file")})
        extra = {k: img[k] for k in ("date_captured", "datetime", "country_code", "study",
                                     "rights_holder", "frame_num", "corrupt") if k in img}
        yield SourceImage(
            dataset=source_id, image_id=image_id, file_name=str(img["file_name"]), labels=labels,
            annotations=annotations, location=_opt_str(img.get("location")),
            seq_id=_opt_str(img.get("seq_id")), width=img.get("width"), height=img.get("height"),
            extra=extra,
        )


FORMATS: dict[str, Callable[..., Iterable[SourceImage]]] = {
    "coco_camera_traps": parse_coco_camera_traps,
}


def parse_md_results(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """MegaDetector batch output: file -> detections (normalised xywh), detector name."""
    categories = data.get("detection_categories", {})
    detector = (data.get("info") or {}).get("detector", "unknown")
    out = {}
    for item in data.get("images", []):
        dets = [{"category": categories.get(d.get("category"), d.get("category")),
                 "conf": d.get("conf"), "bbox_relative": d.get("bbox")}
                for d in item.get("detections") or []]
        out[item["file"]] = {"detector": detector, "detections": dets}
    return out


def _opt_str(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


# ---------------------------------------------------------------- labels and selection

def classify(image: SourceImage, source: Source) -> Candidate | None:
    """Decide which bucket an image can fill. None means: do not import."""
    if not image.labels or image.extra.get("corrupt"):
        return None
    rules = []
    for label in image.labels:
        rule = source.label_map.get(label)
        if rule is None:
            return None  # an unmapped label could be anything; never guess
        rules.append((label, rule))
    if any(rule.status == "excluded" for _, rule in rules):
        return None
    labels = [{"source_label": label, "status": rule.status, "target": rule.target,
               **({"note": rule.note} if rule.note else {})} for label, rule in rules]
    species = sorted({rule.target for _, rule in rules if rule.status == "species"})
    uncertain = sorted({rule.target for _, rule in rules if rule.status == "uncertain"})
    if len(species) == 1 and not uncertain:
        bucket = species[0]
    elif species or uncertain:
        if len(species) > 1 or (species and uncertain):
            return None  # mixed species in one image: not a clean example for the first sample
        bucket = f"uncertain:{uncertain[0]}"
    else:
        bucket = NEGATIVE
    return Candidate(image=image, bucket=bucket, guardian_labels=labels)


def split_for(image: SourceImage, eval_percent: int = 20) -> tuple[str, str]:
    """Deterministic split by camera location, else by sequence, else unassigned."""
    if image.location:
        basis, key = "location", image.location
    elif image.seq_id:
        basis, key = "sequence", image.seq_id
    else:
        return "unassigned", "none (no location or sequence metadata)"
    digest = hashlib.sha256(f"{image.dataset}:{basis}:{key}".encode()).digest()
    return ("eval" if digest[0] * 100 // 256 < eval_percent else "train"), basis


def select(candidates: Iterable[Candidate], quotas: dict[str, int], seed: int = 7) -> list[Candidate]:
    """Fill each bucket round-robin across datasets, and within a dataset
    round-robin across locations, so one camera or dataset cannot dominate."""
    grouped: dict[str, dict[str, dict[str, list[Candidate]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for cand in candidates:
        if cand.bucket in quotas:
            loc = cand.image.location or cand.image.seq_id or cand.image.image_id
            grouped[cand.bucket][cand.image.dataset][loc].append(cand)
    rng = random.Random(seed)
    chosen = []
    for bucket in sorted(grouped):
        rotations = []
        for dataset in sorted(grouped[bucket]):
            locations = grouped[bucket][dataset]
            keys = sorted(locations)
            rng.shuffle(keys)
            for key in keys:
                locations[key].sort(key=lambda c: c.image.image_id)
                rng.shuffle(locations[key])
            rotations.append(deque((key, locations[key]) for key in keys))
        taken = 0
        while taken < quotas[bucket] and rotations:
            for rotation in rotations:
                if taken >= quotas[bucket]:
                    break
                key, items = rotation.popleft()
                chosen.append(items.pop())
                taken += 1
                if items:
                    rotation.append((key, items))
            rotations = [r for r in rotations if r]
    return chosen


# ---------------------------------------------------------------- downloading

Fetcher = Callable[[str], bytes]


def http_fetch(url: str, timeout: float = 60.0, max_bytes: int = MAX_METADATA_BYTES) -> bytes:
    """HTTPS only, and stops reading at `max_bytes` instead of loading an
    oversized file into memory first."""
    if not url.startswith("https://"):
        raise DatasetError(f"only https downloads are allowed: {url!r}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https registry URLs)
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise DatasetError(f"{url} is {int(declared)} bytes, over the {max_bytes} byte limit")
        chunks, total = [], 0
        while chunk := response.read(1 << 20):
            total += len(chunk)
            if total > max_bytes:
                raise DatasetError(f"{url} is larger than {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)


def safe_relative(file_name: str) -> PurePosixPath:
    path = PurePosixPath(file_name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise DatasetError(f"unsafe file name in metadata: {file_name!r}")
    return path


def image_url(source: Source, image: SourceImage) -> str:
    return f"{source.image_base_url.rstrip('/')}/{safe_relative(image.file_name).as_posix()}"


def load_json_resource(url: str, cache: Path, fetch: Fetcher = http_fetch,
                       record: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Fetch (or reuse from cache) a JSON or zipped-JSON metadata file. `record`
    collects the file's SHA-256, so a run states exactly which metadata it used
    (a cached file may be older than the publisher's current one)."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / PurePosixPath(url.split("?")[0]).name
    if not target.exists():
        payload = fetch(url)
        if len(payload) > MAX_METADATA_BYTES:
            raise DatasetError(f"metadata {url} is larger than {MAX_METADATA_BYTES} bytes")
        target.write_bytes(payload)
    raw = target.read_bytes()
    if record is not None:
        record[url] = {"file": target.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                       "cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(target.stat().st_mtime))}
    if target.suffix == ".zip":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = [i for i in archive.infolist() if i.filename.endswith(".json")]
            if len(infos) != 1:
                raise DatasetError(f"{target.name}: expected one JSON file, found {[i.filename for i in infos]}")
            if infos[0].file_size > MAX_JSON_BYTES:
                raise DatasetError(f"{target.name}: {infos[0].file_size} bytes uncompressed is over the limit")
            raw = archive.read(infos[0])
    return json.loads(raw)


def load_source_images(source: Source, cache_root: Path, fetch: Fetcher = http_fetch,
                       record: dict[str, dict[str, Any]] | None = None) -> Iterable[SourceImage]:
    cache = cache_root / source.id
    data = load_json_resource(source.metadata_url, cache, fetch, record)
    bboxes = load_json_resource(source.bbox_url, cache, fetch, record) if source.bbox_url else None
    return FORMATS[source.format](source.id, data, bboxes)


@dataclass
class Plan:
    candidates: list[Candidate]  # bounded reservoir sample, not every eligible image
    chosen: list[Candidate]
    unmapped_labels: dict[str, dict[str, int]]
    skipped_sources: dict[str, str]
    available: dict[str, dict[str, int]] = field(default_factory=dict)  # every eligible image
    metadata_files: dict[str, dict[str, Any]] = field(default_factory=dict)  # url -> file, sha256, bytes


def build_plan(registry: Registry, quotas: dict[str, int], cache_root: Path = DEFAULT_CACHE,
               fetch: Fetcher = http_fetch, allow_research_only: bool = False,
               only: set[str] | None = None, seed: int = 7, per_location: int = 8) -> Plan:
    # Keep at most `per_location` candidates per (bucket, dataset, location) by
    # reservoir sampling, so memory stays bounded while every camera can be picked.
    reservoirs: dict[tuple[str, str, str], list[Candidate]] = {}
    seen: Counter = Counter()
    rng = random.Random(seed)
    unmapped: dict[str, Counter] = {}
    skipped: dict[str, str] = {}
    metadata_files: dict[str, dict[str, Any]] = {}
    for source in registry.sources:
        if only and source.id not in only:
            continue
        if not source.fetchable:
            skipped[source.id] = "no downloadable metadata/images in the registry"
            continue
        if not source.commercial and not allow_research_only:
            skipped[source.id] = f"licence commercial_use={source.licence.get('commercial_use')}"
            continue
        counts: Counter = Counter()
        for image in load_source_images(source, cache_root, fetch, metadata_files):
            counts.update(label for label in image.labels if label not in source.label_map)
            cand = classify(image, source)
            if cand is None:
                continue
            key = (cand.bucket, source.id, image.location or image.seq_id or image.image_id)
            seen[key] += 1
            pool = reservoirs.setdefault(key, [])
            if len(pool) < per_location:
                pool.append(cand)
            else:
                slot = rng.randrange(seen[key])
                if slot < per_location:
                    pool[slot] = cand
        unmapped[source.id] = dict(counts.most_common())
    candidates = [c for pool in reservoirs.values() for c in pool]
    available: dict[str, Counter] = defaultdict(Counter)
    for (bucket, dataset, _), count in seen.items():
        available[bucket][dataset] += count
    chosen = select(candidates, quotas, seed=seed)
    return Plan(candidates, chosen, unmapped, skipped,
                {b: dict(c) for b, c in sorted(available.items())}, metadata_files)


def fetch_images(plan: Plan, registry: Registry, out: Path, max_bytes: int,
                 cache_root: Path = DEFAULT_CACHE, fetch: Fetcher = http_fetch,
                 eval_percent: int = 20, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Download the chosen images, write manifest.jsonl and summary.json."""
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.jsonl"
    records: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():  # resumable: keep what is already on disk
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if (out / rec["path"]).exists():
                records[rec["key"]] = rec
    by_sha = {rec["sha256"]: rec["key"] for rec in records.values()}
    used = sum(rec["bytes"] for rec in records.values())
    md_cache: dict[str, dict[str, dict[str, Any]]] = {}
    duplicates, failures, over_budget = [], [], 0
    for cand in plan.chosen:
        image, source = cand.image, registry.get(cand.image.dataset)
        key = f"{image.dataset}:{image.image_id}"
        if key in records:
            continue
        if used >= max_bytes:
            over_budget += 1
            continue
        url = image_url(source, image)
        try:
            payload = fetch(url)
        except Exception as exc:  # network errors are reported, never fatal
            failures.append({"key": key, "url": url, "error": str(exc)[:200]})
            continue
        sha = hashlib.sha256(payload).hexdigest()
        if sha in by_sha:
            duplicates.append({"key": key, "duplicate_of": by_sha[sha]})
            continue
        if used + len(payload) > max_bytes:
            over_budget += 1
            continue
        rel = PurePosixPath("images") / image.dataset / "__".join(safe_relative(image.file_name).parts)
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(payload)
        used += len(payload)
        split, basis = split_for(image, eval_percent)
        if source.md_results_url and source.id not in md_cache:
            try:
                md_cache[source.id] = parse_md_results(
                    load_json_resource(source.md_results_url, cache_root / source.id, fetch))
            except Exception as exc:
                log(f"{source.id}: MegaDetector results unavailable ({exc})")
                md_cache[source.id] = {}
        md = md_cache.get(source.id, {}).get(image.file_name)
        rec = {
            "key": key, "dataset": image.dataset, "source_image_id": image.image_id,
            "source_file": image.file_name, "source_url": url, "path": rel.as_posix(),
            "sha256": sha, "bytes": len(payload),
            "bucket": cand.bucket, "guardian_labels": cand.guardian_labels,
            "source_labels": image.labels, "source_annotations": image.annotations,
            "megadetector": ({**md, "status": "machine-generated, not verified"} if md else None),
            "location": image.location, "seq_id": image.seq_id, "split": split, "split_basis": basis,
            "width": image.width, "height": image.height, "source_extra": image.extra,
            "licence": source.licence["id"], "commercial_use": source.licence["commercial_use"],
            "attribution": source.licence["attribution"], "source_version": source.version,
            "retrieved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        records[key] = rec
        by_sha[sha] = key
        if len(records) % 50 == 0:
            log(f"{len(records)} images, {used / 1e6:.1f} MB")
            _write_manifest(manifest_path, records)
    _write_manifest(manifest_path, records)
    leaks = location_leaks(records.values())
    summary = summarise(records.values(), plan, registry)
    summary.update(duplicates=duplicates, failures=failures, skipped_over_budget=over_budget,
                   split_leaks=leaks, max_bytes=max_bytes)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _write_attribution(out / "ATTRIBUTION.md", records.values(), registry)
    return summary


def _write_manifest(path: Path, records: dict[str, dict[str, Any]]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records.values()), encoding="utf-8")
    tmp.replace(path)


def location_leaks(records: Iterable[dict[str, Any]]) -> list[str]:
    """Locations that ended up in both train and eval (should always be empty)."""
    sides: dict[tuple[str, str], set[str]] = defaultdict(set)
    for rec in records:
        if rec["split_basis"] == "location":
            sides[(rec["dataset"], rec["location"])].add(rec["split"])
    return [f"{d}:{loc}" for (d, loc), s in sides.items() if {"train", "eval"} <= s]


def summarise(records: Iterable[dict[str, Any]], plan: Plan, registry: Registry) -> dict[str, Any]:
    records = list(records)
    per_bucket: dict[str, Counter] = defaultdict(Counter)
    splits: dict[str, Counter] = defaultdict(Counter)
    locations: dict[str, set] = defaultdict(set)
    for rec in records:
        per_bucket[rec["bucket"]][rec["dataset"]] += 1
        splits[rec["bucket"]][rec["split"]] += 1
        if rec["location"]:
            locations[rec["bucket"]].add((rec["dataset"], rec["location"]))
    buckets = sorted(set(per_bucket) | set(registry.targets) | {NEGATIVE})
    return {
        "images": len(records),
        "bytes": sum(r["bytes"] for r in records),
        "buckets": {b: {"images": sum(per_bucket[b].values()), "by_dataset": dict(per_bucket[b]),
                        "splits": dict(splits[b]), "distinct_locations": len(locations[b]),
                        "without_location": sum(1 for r in records if r["bucket"] == b and not r["location"])}
                    for b in buckets},
        "available_in_metadata": plan.available,
        "unmapped_labels": plan.unmapped_labels,
        "skipped_sources": plan.skipped_sources,
        "metadata_files": plan.metadata_files,
    }


def _write_attribution(path: Path, records: Iterable[dict[str, Any]], registry: Registry) -> None:
    used = Counter(r["dataset"] for r in records)
    lines = ["# Attribution for downloaded Guardian sample", ""]
    for sid, count in sorted(used.items()):
        source = registry.get(sid)
        lic = source.licence
        lines += [f"## {source.name} ({count} images)", "", f"- Source: {source.page}",
                  f"- Version: {source.version}", f"- Licence: {lic['id']} ({lic.get('url')})",
                  f"- Conditions: {lic['conditions']}", f"- Cite: {lic['attribution']}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.guardian.datasets")
    parser.add_argument("command", choices=["sources", "plan", "fetch"])
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default="data/guardian/runs/initial")
    parser.add_argument("--max-mb", type=float, default=900.0)
    parser.add_argument("--only", nargs="*", help="dataset ids to use")
    parser.add_argument("--quota", nargs="*", default=[], help="bucket=count overrides")
    parser.add_argument("--allow-research-only", action="store_true",
                        help="also fetch sources whose licence does not allow commercial use")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    registry = load_registry(args.registry)
    if args.command == "sources":
        for s in registry.sources:
            print(f"{s.id:22} {s.licence['id']:20} commercial={s.licence['commercial_use']:12} "
                  f"fetchable={s.fetchable}  {s.page}")
        return 0
    quotas = dict(DEFAULT_QUOTAS)
    for item in args.quota:
        bucket, _, count = item.partition("=")
        quotas[bucket] = int(count)
    plan = build_plan(registry, quotas, Path(args.cache), allow_research_only=args.allow_research_only,
                      only=set(args.only) if args.only else None, seed=args.seed)
    if args.command == "plan":
        print(json.dumps({"available_in_metadata": plan.available,
                          "chosen": dict(Counter(c.bucket for c in plan.chosen)),
                          "skipped_sources": plan.skipped_sources}, indent=2))
        return 0
    summary = fetch_images(plan, registry, Path(args.out), int(args.max_mb * 1024 * 1024),
                           Path(args.cache))
    print(json.dumps({k: summary[k] for k in ("images", "bytes", "buckets", "skipped_sources",
                                               "split_leaks", "skipped_over_budget")}, indent=2))
    print(f"duplicates={len(summary['duplicates'])} failures={len(summary['failures'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
