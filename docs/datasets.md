# Dataset library

Every dataset Sentinel uses, its licence, what it may be used for, and its split. All media is git-ignored and fetched by a script; nothing here is redistributed.

**Splits.** `validation`: Sentinel was tuned on it (detector choice, thresholds, schedules), so it measures progress but not independent accuracy. `test`: nobody tuned on it; `python -m sentinel.training compare` uses it to accept or reject a model, and it is never exported for training. `train`: nothing yet. Training sets are built from reviewed Test Lab frames (docs/training.md). `private`: the owner's own footage, local only.

| Dataset | Content | Labels | Licence | Commercial use | Split | On disk / fetch |
| --- | --- | --- | --- | --- | --- | --- |
| MOT16 train 02, 04, 09 | Pedestrians, day/night street and storefront | Per-frame person boxes + identities | CC BY-NC-SA 3.0 | Evaluation only | validation | `data/bench/mot16`, `python scripts/fetch_bench_footage.py` |
| CAVIAR (5 INRIA lobby clips) | Indoor overhead, people walking, meeting, leaving a bag | Per-frame boxes | Research use, acknowledgement required | Evaluation only | validation | `data/bench/caviar`, same script |
| Guardian wildlife stills (ENA24, Caltech Camera Traps, NACTI via LILA) | 435 camera-trap images: black bear, cougar, coyote, moose, elk, negatives | Image-level species | CDLA-Permissive-1.0 | Yes (keep attribution) | validation | `data/guardian/cache`, `python -m sentinel.guardian.datasets` (docs/guardian-datasets.md) |
| Guardian trail-camera videos (13 clips) | Bear, cougar, wolf, coyote, elk, lynx, weasel, fox, moose; day and infrared night; one empty river | Presence ranges by eye | Public domain / CC BY-SA 2.0 (per clip, `config/guardian/videos.json`) | Yes per licence | validation | `data/guardian/videos`, `python scripts/guardian/fetch_wildlife_videos.py` |
| **Test Lab public clips (new, 14 registered)** | Night IR yard, retail store, porch parcel removals, mailbox/street fisheye, parking lot (PTZ), driveway (mostly empty), night street, cyclists, motorway, storms (doorbell, apartment lot), bear near a home, cat | None yet (label in the Test Lab) | Public domain / CC BY 3.0 / CC BY 4.0 (per clip, `config/lab/videos.json`) | Evaluation; training only after the licence is confirmed (7 PD claims flagged for verification) | **test** | `data/lab/videos`, `python scripts/guardian/fetch_wildlife_videos.py --registry config/lab/videos.json --out data/lab/videos` |
| Owner footage | Whatever the owner drops in `<storage>/lab/videos/` | Optional `NAME.labels.json` + Test Lab marks | Owner's | Local personalization only, with consent | private | `data/sentinel/lab/videos` |

The new clips were chosen from Wikimedia Commons because licence and SHA-1 can be checked automatically at download. Clips showing real incidents involving harm to people (shootings, crashes, attacks) were deliberately left out, even where public domain. As of 2026-10-17, 9 of the 14 are downloaded (139 MB). Commons rate-limited the rest (`motorway_bridge`, `doorbell_tornado`, `apartments_tornado`, `bear_near_home`, `cat_at_camera`); run the fetch command again later. It skips files already present.

**Gaps that remain.** No labelled, commercially usable security footage with per-frame boxes for people *and* vehicles at night; no rain/fog/snow security clips with people; no dogs or deer on a home camera with labels; no wolf or grizzly footage beyond one wolf clip. No accuracy claim may be made for these conditions.

## Candidate sources not downloaded

Licences below are as the sources state them; verify the primary terms before use.

- **Open Images V7** (Google): boxes CC BY 4.0; images listed as CC BY 2.0 by their uploaders (not verified by Google). Person, car, dog, deer, bear classes, with night and low-light images. Best route to a commercially usable labelled *training* set; download per class with the official downloader.
- **LILA BC camera-trap datasets** (more Caltech/NACTI/WCS/Snapshot Serengeti species): mostly CDLA-Permissive-1.0 or Community Data License. Each is registered or checked in `config/guardian/datasets.json`.
- **COCO 2017**: annotations CC BY 4.0, images under individual Flickr licences (mixed, some non-commercial): evaluation only unless filtered per image licence.
- Excluded: MOT17/20, UA-DETRAC, VisDrone, VIRAT and most academic surveillance sets (non-commercial or research-only terms), OmniFall (CC BY-NC).
