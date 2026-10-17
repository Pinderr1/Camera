"""Species decisions from a crop classifier (stdlib only).

A classifier (SpeciesNet today) returns scores over its own taxonomy. This
module turns those scores into something Guardian may show, and refuses to
force a species:

- A species is `confirmed` only when its score clears `min_confidence`, beats
  the runner-up by `min_margin`, is allowed in the configured country
  (geofence), and, on live tracks, at least `min_agreeing` crops agree.
- Otherwise scores roll up the taxonomy (species -> genus -> family). A group
  that clears the threshold is shown as `uncertain` ("Bear detected — species
  uncertain"); below that the result is `unknown` and the detector label stays.
- The classifier's own taxonomy string, common name and scores are always kept.

Nothing here touches images or models; see species_onnx.py for inference."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

RANKS = ("class", "order", "family", "genus", "species")
NON_ANIMAL = {"blank", "vehicle", "human"}

# Classifier taxa Guardian has a profile for, keyed by "genus species" (or "genus"
# alone for every species of that genus). Anything else keeps the classifier's name.
GUARDIAN_SPECIES = {
    "ursus americanus": "black_bear",
    "ursus arctos horribilis": "grizzly_bear",
    "ursus arctos": "brown_bear",
    "puma concolor": "cougar",
    "canis lupus": "wolf",
    "canis latrans": "coyote",
    "canis familiaris": "domestic_dog",
    "alces alces": "moose",
    "cervus canadensis": "elk",
    "odocoileus": "deer",
}
# Group wording when only the genus or family is supported.
GROUP_DISPLAY = {
    "ursidae": "Bear", "ursus": "Bear",
    "canidae": "Dog-like animal", "canis": "Dog-like animal",
    "felidae": "Cat-like animal", "puma": "Cat-like animal",
    "cervidae": "Deer-family animal", "cervus": "Deer-family animal", "alces": "Deer-family animal",
}


@dataclass(frozen=True)
class Taxon:
    index: int
    uid: str
    ranks: tuple[str, str, str, str, str]  # class, order, family, genus, species (may be empty)
    common: str

    @property
    def level(self) -> str:
        if not any(self.ranks):
            return "non_animal" if self.common in NON_ANIMAL else "animal"
        for rank, value in zip(reversed(RANKS), reversed(self.ranks)):
            if value:
                return rank
        return "animal"

    @property
    def scientific(self) -> str:
        genus, species = self.ranks[3], self.ranks[4]
        return f"{genus} {species}".strip() if species else genus

    @property
    def taxonomy(self) -> str:
        return ";".join(self.ranks)

    def ancestor_key(self, rank: str) -> str | None:
        """Prefix key at `rank` (e.g. the family), None when this taxon is less specific."""
        depth = RANKS.index(rank) + 1
        if not self.ranks[depth - 1]:
            return None
        return ";".join(self.ranks[:depth])


def parse_labels(lines: Iterable[str]) -> list[Taxon]:
    """SpeciesNet label file: uid;class;order;family;genus;species;common name."""
    taxa = []
    for index, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if len(parts) != 7:
            raise ValueError(f"label line {index + 1} has {len(parts)} fields, expected 7")
        taxa.append(Taxon(index, parts[0], tuple(p.strip().lower() for p in parts[1:6]), parts[6].strip()))
    return taxa


def guardian_key(taxon: Taxon) -> str | None:
    if taxon.level != "species":
        return None
    name = taxon.scientific
    return GUARDIAN_SPECIES.get(name) or GUARDIAN_SPECIES.get(taxon.ranks[3])


class Geofence:
    """SpeciesNet geofence: taxonomy prefix -> allow/block lists by ISO-3166 alpha-3
    country (and optional first-level region). An empty fence allows everything."""

    def __init__(self, rules: dict[str, Any] | None = None) -> None:
        self.rules = rules or {}

    def allowed(self, taxon: Taxon, country: str | None, region: str | None = None) -> bool:
        if not country or not self.rules:
            return True
        country = country.upper()
        for depth in range(5, 0, -1):
            if not taxon.ranks[depth - 1]:
                continue
            rule = self.rules.get(";".join(taxon.ranks[:depth]))
            if not rule:
                continue
            allow, block = rule.get("allow"), rule.get("block") or {}
            if allow is not None:
                if country not in allow:
                    return False
                if region and allow[country] and region not in allow[country]:
                    return False
            if country in block and (not block[country] or (region and region in block[country])):
                return False
        return True


@dataclass
class SpeciesSettings:
    enabled: bool = False
    model_dir: str = "models/speciesnet/v4.0.3a"
    providers: Any = "auto"
    min_confidence: float = 0.8  # wrong confident calls on the first sample scored 0.70-0.76 (docs/guardian-datasets.md)
    min_margin: float = 0.2
    group_confidence: float = 0.6
    min_agreeing: int = 2
    reclassify_s: float = 4.0
    max_crops_per_track: int = 6
    min_box_px: int = 48
    pad: float = 0.1
    country: str | None = None
    admin1_region: str | None = None
    queue_size: int = 8
    cpu_threads: int = 2
    # A track seen only as a generic "animal" (camera-trap detector, no COCO label) is set
    # aside when at least `min_agreeing` crops agree the picture is blank at this score:
    # MegaDetector boxes rocks, sticks and moving water (docs/guardian-datasets.md).
    # 1 turns this off: SpeciesNet also called 1.9 % of single crops of real animals blank.
    blank_confidence: float = 0.8
    blank_recheck_s: float = 1.0  # a generic box whose first crop looks blank is re-checked this soon
    # A species confirmed at this score by `min_agreeing` crops is not re-classified:
    # each SpeciesNet crop holds the shared GPU for about 0.35 s (Iris Xe). 1 turns it off.
    settle_confidence: float = 0.95

    @classmethod
    def from_dict(cls, data: dict | None) -> "SpeciesSettings":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class SpeciesDecision:
    status: str  # confirmed | uncertain | unknown
    key: str | None  # Guardian species key when confirmed (or None)
    display: str
    confidence: float  # score of what is displayed (species or group)
    taxon: dict[str, Any] | None  # what the classifier's top label was
    group: str | None = None  # rolled-up group when uncertain
    reasons: list[str] = field(default_factory=list)
    top: list[dict[str, Any]] = field(default_factory=list)
    crops: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "key": self.key,
            "display": self.display,
            "confidence": round(self.confidence, 3),
            "taxon": self.taxon,
            "group": self.group,
            "reasons": list(self.reasons),
            "top": list(self.top),
            "crops": self.crops,
        }


class SpeciesPolicy:
    def __init__(self, taxa: list[Taxon], settings: SpeciesSettings, geofence: Geofence | None = None,
                 profile_display: dict[str, str] | None = None) -> None:
        self.taxa = taxa
        self.settings = settings
        self.geofence = geofence or Geofence()
        self.profile_display = profile_display or {}

    def taxon_view(self, taxon: Taxon, score: float) -> dict[str, Any]:
        return {"label": taxon.common, "scientific": taxon.scientific, "taxonomy": taxon.taxonomy,
                "level": taxon.level, "uid": taxon.uid, "score": round(score, 4)}

    def decide(self, scores: dict[int, float], crops: int = 1) -> SpeciesDecision:
        """`scores`: label index -> probability (top-k is enough; missing = 0)."""
        s = self.settings
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top = [self.taxon_view(self.taxa[i], p) for i, p in ranked[:5]]
        if not ranked:
            return SpeciesDecision("unknown", None, "", 0.0, None, reasons=["no scores"], crops=crops)
        best_index, best = ranked[0]
        taxon = self.taxa[best_index]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        reasons: list[str] = []
        if taxon.level == "non_animal":
            return SpeciesDecision("unknown", None, "", best, self.taxon_view(taxon, best),
                                   reasons=[f"classifier top label is '{taxon.common}'"], top=top, crops=crops)
        if taxon.level == "species":
            allowed = self.geofence.allowed(taxon, s.country, s.admin1_region)
            if best < s.min_confidence:
                reasons.append(f"top species score {best:.2f} below {s.min_confidence:.2f}")
            if best - second < s.min_margin:
                reasons.append(f"runner-up within {s.min_margin:.2f}")
            if not allowed:
                reasons.append(f"{taxon.common} not expected in {s.country} (geofence)")
            if crops < s.min_agreeing:
                reasons.append(f"{crops} of {s.min_agreeing} crops needed")
            if not reasons:
                key = guardian_key(taxon)
                name = self.profile_display.get(key or "", taxon.common.capitalize())
                return SpeciesDecision("confirmed", key, name, best, self.taxon_view(taxon, best),
                                       top=top, crops=crops)
        else:
            reasons.append(f"classifier top label is a group ('{taxon.common}')")
        for rank in ("genus", "family"):
            group = self._group(ranked, rank)
            if group and group[1] >= s.group_confidence:
                name = GROUP_DISPLAY.get(group[0].rsplit(";", 1)[-1], "Animal")
                return SpeciesDecision("uncertain", None, f"{name} detected — species uncertain", group[1],
                                       self.taxon_view(taxon, best), group=group[0], reasons=reasons, top=top, crops=crops)
        return SpeciesDecision("unknown", None, "", best, self.taxon_view(taxon, best),
                               reasons=reasons or ["no taxon clears the thresholds"], top=top, crops=crops)

    def _group(self, ranked: list[tuple[int, float]], rank: str) -> tuple[str, float] | None:
        totals: Counter = Counter()
        for index, p in ranked:
            key = self.taxa[index].ancestor_key(rank)
            if key:
                totals[key] += p
        if not totals:
            return None
        key, total = totals.most_common(1)[0]
        return key, min(1.0, total)


@dataclass
class TrackSpecies:
    """Per guardian track: accumulated scores from several crops, re-checked
    periodically rather than every frame."""

    scores: Counter = field(default_factory=Counter)
    votes: Counter = field(default_factory=Counter)
    crops: int = 0
    last_request_ms: int = -10**12
    pending: bool = False
    decision: SpeciesDecision | None = None

    def due(self, now_ms: int, settings: SpeciesSettings, interval_s: float | None = None) -> bool:
        interval = settings.reclassify_s if interval_s is None else interval_s
        return (not self.pending and self.crops < settings.max_crops_per_track and not self.settled(settings)
                and now_ms - self.last_request_ms >= interval * 1000)

    def settled(self, settings: SpeciesSettings) -> bool:
        """Confirmed confidently enough that more crops would not change the answer."""
        d = self.decision
        return (d is not None and settings.settle_confidence < 1 and d.status == "confirmed"
                and d.confidence >= settings.settle_confidence and d.crops >= settings.min_agreeing)

    def looks_blank(self) -> bool:
        """The averaged top label so far is "blank" (worth a quick second look)."""
        return ((self.decision.taxon or {}).get("label") == "blank") if self.decision is not None else False

    def add(self, scores: dict[int, float], policy: SpeciesPolicy) -> SpeciesDecision:
        self.pending = False
        self.crops += 1
        for index, p in scores.items():
            self.scores[index] += p
        if scores:
            self.votes[max(scores, key=scores.get)] += 1
        mean = {i: v / self.crops for i, v in self.scores.items()}
        # Only crops whose own top label matches the averaged top label count toward
        # confirmation, so a track that flips between species stays uncertain.
        top_index = max(mean, key=mean.get) if mean else None
        self.decision = policy.decide(mean, crops=self.votes.get(top_index, 0))
        return self.decision


def is_blank(decision: "SpeciesDecision | None", settings: SpeciesSettings) -> bool:
    """The classifier agrees, over enough crops, that there is no animal in the box."""
    taxon = (decision.taxon or {}) if decision is not None else {}
    return (decision is not None and settings.blank_confidence < 1 and taxon.get("label") == "blank"
            and decision.confidence >= settings.blank_confidence
            and decision.crops >= settings.min_agreeing)
