# Environmental intelligence

Sentinel separates three kinds of things that move in front of a camera:

1. **Objects the detector recognises:** COCO classes from YOLOX (or EfficientDet as the fallback).
2. **Objects it cannot name:** coherent movers found by motion analysis (`unknown_motion`).
3. **Environmental effects:** light, weather, shadows, vegetation, insects. These are classified by motion analysis and **never** raise incidents.

## What the pretrained detectors recognise (audit, October 2026)

YOLOX and EfficientDet are trained on COCO's 80 classes. Sentinel folds the security-relevant ones into categories (`models.CATEGORY_LABELS`):

| Category | COCO labels used | Notes |
| --- | --- | --- |
| `person` | person | Benchmarked (see ROADMAP). |
| `vehicle` | car, truck, bus, motorcycle | In COCO but **not yet benchmarked**: the public clips have no vehicle annotations. |
| `bicycle` | bicycle | Riders are detected as person plus bicycle. |
| `animal` | bird, cat, dog, horse, sheep, cow, bear, elephant, zebra, giraffe | Small birds at a distance are unreliable. Foxes, raccoons, deer, and coyotes are **not** COCO classes: they may appear as dog/cat/sheep/horse or be missed. |
| `bag` | backpack, handbag, suitcase | Carried bags only. |
| `unknown_motion` | (motion analysis) | Off by default. |

Events show the specific label, e.g. "Animal (dog) entered Backyard".

**Not in COCO, so a different model or data would be needed:**

| Need | Status | Possible data (licence check required before training) |
| --- | --- | --- |
| Packages / parcels / delivery boxes | Missing. A box on a porch is not detected. | Open Images V7 boxable classes include *Box* and *Envelope* (annotations CC BY 4.0; images listed as CC BY 2.0, but Google states it makes no warranty per image, so per-image verification is needed). Several Roboflow Universe "parcel detection" sets are labelled CC BY 4.0 (small, varied quality; verify each). Best long-term: images from our own installations, with consent. |
| Insects, spiders, webs near the lens | Missing as objects; handled as an environmental condition (`near_camera_object`). | Open Images V7 has *Insect*, *Spider*, *Bee*. A dedicated "lens obstruction" classifier would need our own data. |
| Wildlife (fox, raccoon, deer, coyote) | Missing. | Camera-trap datasets (e.g. LILA BC collections) mostly carry research/CDLA-style licences that differ per collection; review each. |
| Tools/weapons (ladder, crowbar, firearm) | Missing. | Open Images V7 has *Ladder*, *Hammer*, *Knife*, *Handgun*, *Rifle*, *Weapon*. **Weapon detection has serious false-positive and ethical risks**; it is out of scope unless a product decision is recorded in CLAUDE.md. |
| Vehicle type, colour, licence plates | Not planned. | Plate recognition is identification and is excluded by CLAUDE.md boundary 2. |

Datasets to **avoid** for shipped models: Objects365 (used by RF-DETR; licence restricts commercial use), CrowdHuman (non-commercial), MOT/CAVIAR (evaluation only), and anything scraped without clear terms. Nothing has been downloaded or trained yet. That is a future, separately approved step (ROADMAP Phase 4).

## Category settings

`categories` in the site config (and per camera) set `detect` and `alerts` for each category:

```json
"categories": {"animal": {"detect": true, "alerts": false}, "unknown_motion": {"detect": false, "alerts": false}}
```

- `detect: false` removes the category's labels from the detector allowlist and drops any remaining boxes before tracking.
- `alerts: false` keeps the category tracked and logged, but its events are recorded as `info` (with `original_severity` and the reason `category_alerts_off`), so they never open incidents or send notifications.
- Defaults: person and vehicle alert; bicycle, animal, and bag are logged only; unknown motion is off.
- Per-camera overrides let you, for example, alert on animals only in a chicken-coop camera.
- The dashboard edits these under Setup → Categories.

## Environmental motion analysis (`environment.py`)

This runs on every frame of every camera: MOG2 background subtraction plus frame differencing on a 320x180 copy, about 1–2 ms. Results appear as `scene` in camera status and as rate-limited `scene_condition` info events (at most once per condition per camera per 10 minutes). **These never open incidents.**

| Condition | Signal | Typical cause |
| --- | --- | --- |
| `lighting_change` | Mean brightness jump > 18 grey levels, or > 45% of the frame changing at once | Lights switching, headlights, clouds, auto-exposure |
| `ir_mode` | Mean saturation < 6 (monochrome image) | Camera in night/IR mode |
| `precipitation_or_noise` | ≥ 80 tiny blobs with < 15% of the frame changing (counted before morphology, which would erase 1–2 px streaks) | Rain, snow, sensor noise at night |
| `moving_shadows` | MOG2 shadow pixels dominate the change | Clouds, trees casting shadows |
| `recurring_motion` | Pixels changing in > 8% of frames over about 20 s | Swaying plants, flags, water, blinking lights |
| `near_camera_object` | One blob covering > 25% of the view, not a lighting change | Insect or spider on the lens, a web, debris |

**Unknown moving objects:** a blob between 0.2% and 15% of the frame becomes `unknown_motion` when all of these hold:
- it persists for 8 or more analysed frames;
- it travels at least 5% of the frame width;
- no detection box explains it;
- it is not in a habitual-motion area;
- it does not happen during a lighting change, precipitation, or near-camera condition.

These boxes go to the tracker only when the `unknown_motion` category is enabled.

**Validation status:** unit tests cover synthetic sequences: static scene, lighting jump, synthetic rain, IR switch, an unexplained mover, a mover covered by a detection, and swaying vegetation. Notably, MOG2 learns periodic motion into its background, which is why habitual motion is measured by frame differencing. **None of these thresholds have been validated on labeled real-world footage.** Real rain, snow, insects at night, and headlight sweeps need recordings from real cameras (ROADMAP Phase 1).
