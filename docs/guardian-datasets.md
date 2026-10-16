# Guardian wildlife datasets and species recognition

What wildlife data Sentinel Guardian has, where it came from, what it may be used for, how the optional species classifier (Google SpeciesNet) is wired in, what it did on real images, and what is still missing. Companion to [wilderness-guardian.md](wilderness-guardian.md) (Phase 1) and the licence audit in [`sources.md`](../sources.md).

No model has been trained. Every number below names the images behind it. They are small camera-trap samples from public datasets that SpeciesNet may have been trained on, so they show that the pipeline works and where it fails. **They are not accuracy claims for Sentinel cameras.**

## Summary (2026-10-16)

- **Species recognition works end to end, off by default.** SpeciesNet v4.0.3a (Apache-2.0 code and weights) runs on ONNX Runtime. Crops come from Sentinel's own YOLOX animal boxes, so MegaDetector is never used. Results flow into guardian tracks, wording, events and incidents, notifications, the live overlay and `/api/v1/guardian`. A species is named only when confident; otherwise "Bear detected — species uncertain" or the detector's unconfirmed label.
- **On real images (435 downloaded):** with Sentinel's boxes, 160 of the 161 crops SpeciesNet confirmed were the species the dataset names. The one wrong call was an elk confirmed as domestic cattle at 0.98. **The bottleneck is the detector:** YOLOX found an animal in only 168 of the 395 images that contain one (many are night IR frames or partly visible animals).
- **Usable commercially:** ENA24, Caltech Camera Traps, NACTI, WCS and WSU Lynx (all CDLA-Permissive-1.0). **Research only / not fetched:** Animal Kingdom, Idaho Camera Traps.
- **Downloaded:** 435 images (194.5 MB): 240 from ENA24 and Caltech, and 195 from NACTI through the new streaming CSV importer (moose 40, elk 40, uncertain elk 15, black bear 30, cougar 30, coyote 20, negatives 20).
- **Still missing:** grizzly bears (no source in any LILA dataset) and North American wolves (best candidate: WSU Lynx, not yet fetched).

## Species recognition (SpeciesNet)

### Model, licences and dependencies

| Item | Version / file | Licence | Notes |
| --- | --- | --- | --- |
| SpeciesNet crop classifier | v4.0.3a, `always_crop_99710272_22x8_v12_epoch_00148.pt` (SHA-256 `d0996320…50ade`), labels `…labels.20260609.txt` (2,498 labels), `taxonomy_release.20260609.txt`, `geofence_release.20260609.json` | Apache-2.0: the archive's own README (`license: apache-2.0`), the Kaggle `licenseName` on every instance (v4.0.0a–v4.0.3b) and the repository | Downloaded anonymously from the Kaggle link in the [cameratrapai README](https://github.com/google/cameratrapai): `speciesnet-pytorch-v4.0.3a-v1.tar.gz`, 209 MB, SHA-256 `d15d0701…167b6ae`. Cite Gadot et al. 2024, *To crop or not to crop*, IET Computer Vision |
| ONNX export | `speciesnet.onnx` (SHA-256 `8090aa40…5568cb`), opset 17 | Same as the weights | `scripts/guardian/export_speciesnet_onnx.py`. Parity with PyTorch: max logit difference 1.2e-5, same top-1. Record in `onnx_export.json` |
| Export-only tools (not Sentinel dependencies) | torch 2.14.1+cpu, onnx2torch 1.5.15, onnx 1.23.2, onnxruntime 1.31.0 | BSD-3 (with Apache/MIT parts), Apache-2.0, Apache-2.0, MIT | Run once in a throwaway venv. The checkpoint is a pickle; checked to reference only `torch`, `onnx2torch` and `collections` before loading |
| Run time | onnxruntime-directml 1.24.4, OpenCV, numpy | MIT, Apache-2.0, BSD-3 | Already Sentinel dependencies |
| SpeciesNet's MegaDetector v5a | `md_v5a.0.1.pt` | Weights licence not stated; YOLOv5-based (AGPL-3.0) | **Not used.** Sentinel's YOLOX (Apache-2.0) supplies the boxes |

Files live in `models/speciesnet/v4.0.3a/` (git-ignored).

### How it works

`guardian.species_classifier` in the site config (off by default):

```json
"guardian": {"enabled": true, "species_classifier": {"enabled": true, "country": "CAN"}}
```

1. Each processed frame, the camera worker asks the Guardian monitor which animal tracks are due for classification. Each track gets one crop every `reclassify_s` (4 s), at most `max_crops_per_track` (6), with one request in flight at a time, and boxes smaller than `min_box_px` (48 px) are skipped. The camera thread only copies the box.
2. One background `SpeciesWorker` thread resizes the crop to 480×480 and runs SpeciesNet. Its queue is bounded: when full, the crop is skipped, never waited for. The ONNX session (about 300 MB) loads on the first animal, not at start-up, so it never competes with the detector's start-up and costs nothing where no animal appears.
3. Scores accumulate per **guardian track**, so they survive tracker re-acquisition. `guardian/species.py` decides:
   - **confirmed**: top species score ≥ `min_confidence` (0.8), beats the runner-up by `min_margin` (0.2), is allowed by SpeciesNet's geofence for `country` (and `admin1_region`), and at least `min_agreeing` (2) crops had it as their own top label.
   - **uncertain**: otherwise, when the genus or family clears `group_confidence` (0.6). Wording: "Bear detected — species uncertain", "Dog-like animal detected — species uncertain".
   - **unknown**: neither, or the classifier saw "blank". The detector's unconfirmed wording stays.
4. Wording: "Black bear — 91% species confidence — moving toward Driveway (Elevated Concern)". The classifier's own label, taxonomy string (`mammalia;carnivora;ursidae;ursus;americanus`), score, top five, reasons and crop count travel with every event and API track (`species.classifier`).
5. **Safety rules (tested):**
   - A classifier result never lowers concern below what the detector label gives (a "domestic dog" call about a detector "bear" cannot quieten Guardian).
   - `species_specific_guidance_allowed` is true only for a confirmed species. Uncertain predictions never unlock species-specific instructions, and Guardian has no such instructions today.
   - A missing model, missing library or inference error only sets the `guardian_species` health component to degraded. Movement intelligence, alerts and security detection carry on with detector labels.

Guardian species keys with their own profile: `black_bear`, `grizzly_bear` (`ursus arctos horribilis`), `brown_bear` (`ursus arctos`), `cougar` (`puma concolor`), `wolf` (`canis lupus`), `coyote`, `domestic_dog`, `moose`, `elk` (`cervus canadensis`), `deer` (`odocoileus`). Any other confirmed species keeps SpeciesNet's common name with the detector's concern.

### Results on real images

`scripts/guardian/speciesnet_eval.py` runs Sentinel's detector (YOLOX-s at 960×544, `model: auto`, score 0.5, the ten COCO animal classes) on each image and classifies the most confident animal box ("Sentinel boxes"). It also classifies the dataset's own box, or LILA's unverified MegaDetector v5a box when the dataset has none ("reference boxes"), to separate detector misses from classifier mistakes. One crop per image, so `min_agreeing` is 1; live tracks are stricter. Counted at `min_confidence` 0.7 (the setting at measurement time); the effect of the new 0.8 default is shown separately. Reports: `bench_runs/guardian_species/initial/` and `bench_runs/guardian_species/nacti_initial/` (`report.md`, `results.json`, git-ignored).

**Run `initial` (240 images: ENA24 + Caltech):**

| Group (split) | Images | Detector found an animal | Sentinel boxes: confirmed right / wrong | Reference boxes: confirmed right / wrong |
| --- | --- | --- | --- | --- |
| Black bear (all `unassigned`, ENA24) | 60 | 40 | 37 / 0 (1 uncertain, 2 "blank") | 60 / 0 |
| Cougar (eval) | 8 | 6 | 4 / 0 (2 "blank" on boxes the detector called `bird`) | 6 / 0 |
| Cougar (train) | 42 | 17 | 17 / 0 | 37 / 1 (white-tailed deer 0.71) |
| Coyote (all splits) | 50 | 15 | 15 / 0 | 44 / 2 (white-tailed deer 0.76, raccoon 0.74) |
| Negatives: dogs, deer, foxes, bobcats, cats, raccoons, horses | 60 | 20 | 19 / 0 | 52 / 1 (red fox as black-fronted duiker 0.75) |
| Empty frames | 20 | 0 (no false boxes) | | |

**Run `nacti_initial` (195 images, NACTI, all one split because NACTI's CSV has only three study areas):**

| Group | Images | Detector found an animal | Sentinel boxes: confirmed right / wrong |
| --- | --- | --- | --- |
| Moose | 40 | 21 | 21 / 0 |
| Elk (`cervus canadensis`) | 40 | 13 | 12 / **1 (domestic cattle, 0.98)** |
| Source label "red deer" (`uncertain:elk`) | 15 | 9 | 9 as "red deer" (the source's own label; still not counted as elk) |
| Black bear | 30 | 16 | 15 / 0 (1 uncertain) |
| Cougar | 30 | 4 | 4 / 0 |
| Coyote | 20 | 7 | 7 / 0 |
| Empty frames | 20 | 0 | |

NACTI has no boxes of its own, and its MegaDetector results are not loaded, so there are no reference crops.

What this shows:

- **Detector recall is the main limitation.** YOLOX found an animal in 168 of 395 animal images (43 %): 56 of 90 black bears, 27 of 80 cougars, 22 of 70 coyotes, 21 of 40 moose and 13 of 40 elk. It also gives the wrong COCO label (cougars as `dog`, `cat` or `bird`). A permissively licensed camera-trap detector (MegaDetector V6 MIT/Apache variants) or fine-tuning YOLOX on these datasets is the next step for recall.
- **Dog, coyote and wolf:** SpeciesNet never predicted a wolf on any image. Coyotes were called coyote in 22/22 crops from Sentinel boxes (both runs) and 44/47 reference crops (the other 3 were not canids); dogs were called domestic dog in 7/7 and 10/11. No wolf images exist yet, so wolf recognition is untested.
- **Bears:** every confident bear call was "American black bear" (112/112 across both runs and box types). No black bear was ever called brown or grizzly. There are no grizzly images, so the reverse confusion is untested. The 60 ENA24 bears are `unassigned` (no camera location) and probably in SpeciesNet's training data: they are **not** an independent held-out set.
- **Confidence:** correct top labels had median 0.99 (10th percentile 0.95 on Sentinel boxes, 0.90 on reference boxes). The four wrong confirmations on reference boxes scored 0.70–0.76. At the new 0.8 default they would all have become uncertain, at the cost of 7 correct reference and 3 correct Sentinel-box calls also becoming uncertain. The NACTI elk-as-cattle error at 0.98 shows that a threshold alone is not enough, which is why live tracks also need two agreeing crops and a confirmed species never lowers concern.
- **Eval split only (Caltech, location-split):** 8 cougars, 3 coyotes, 7 negatives. Too few to say anything beyond "no wrong confirmations".

### Speed and memory on this machine

Measured 2026-10-16 on the development laptop: **Intel Core i5-1135G7, Intel Iris Xe graphics (shared memory), 12 GB RAM**. The GTX 1070 mentioned for the target machine is **not present on this computer** (Windows lists only the Iris Xe), so no NVIDIA figures exist yet.

| Step | DirectML (Iris Xe) | CPU (2 threads) |
| --- | --- | --- |
| SpeciesNet, one crop | p50 342 ms, p95 498 ms (`nacti_initial`, nothing else running); p50 549 ms when another heavy job ran (`initial`) | p50 680 ms, p95 1,068 ms |
| Crop resize | about 12 ms | |
| YOLOX-s 960×544 detection, one frame | p50 144 ms | |
| Classifier session load | 1.2–1.6 s (once, on the first animal) | |
| Memory | Process RSS +300 MB for the classifier (detector + classifier about 740 MB; peak sampled 960 MB). About 650 MB of shared GPU memory | |

With the defaults, one animal costs at most 6 crops × about 0.35 s of GPU time, spread over at least 20 s. Camera threads never wait for it. On the GTX 1070 (DirectML or CUDA) both models should be several times faster, but that has not been measured.

## Licences, versions and attribution (datasets)

Checked against each publisher's own page on 2026-10-15 and 2026-10-16. "Commercial use" is our reading of the published licence, not legal advice; a lawyer should confirm before a product ships with a model trained on this data.

| Dataset | Version used | Licence | Commercial use | Attribution / citation |
| --- | --- | --- | --- | --- |
| [ENA24-detection](https://lila.science/datasets/ena24detection) | `ena24.json` (info.version 1); page updated 2024-08-28 | [CDLA-Permissive-1.0](https://cdla.io/permissive-1-0/) | Allowed | Yousif H, Kays R, He Z. *Dynamic Programming Selection of Object Proposals for Sequence-Level Animal Species Classification in the Wild.* IEEE TCSVT, 2019 |
| [Caltech Camera Traps](https://lila.science/datasets/caltech-camera-traps) | `caltech_camera_traps.json` version 20210113; boxes `caltech_bboxes_20200316.json` | CDLA-Permissive-1.0 | Allowed | Beery S, Van Horn G, Perona P. *Recognition in Terra Incognita.* ECCV 2018 |
| [NACTI](https://lila.science/datasets/nacti) | `nacti_metadata.csv.zip` (same records as `nacti_metadata.1.14.json`) | CDLA-Permissive-1.0 | Allowed | Tabak MA et al. *Machine learning to classify animal species in camera trap images: Applications in ecology.* Methods Ecol Evol 10(4):585-590, 2019 |
| [WCS Camera Traps](https://lila.science/datasets/wcscameratraps) | `wcs_camera_traps.json` (info.version 1.0, 2019) | CDLA-Permissive-1.0 | Allowed | Credit the Wildlife Conservation Society (no paper named on the page) |
| [WSU Lynx](https://lila.science/datasets/wsu-lynx/) | `wsu-lynx.26.02.13.1705` (not yet fetched) | CDLA-Permissive-1.0 | Allowed | Thornton D et al. *Identification of camera trap images by artificial intelligence and human experts.* J Appl Ecol, 2026 |
| [LILA MegaDetector results](https://lila.science/megadetector-results-for-camera-trap-datasets/) | `*_mdv5a.0.0_results.json` (MDv5a, 2022) | **None stated** | Unclear | Machine-generated boxes; LILA notes most of these datasets were in MegaDetector's training data |
| [Animal Kingdom](https://github.com/sutdcv/Animal-Kingdom) | Not obtained | **None published**; request form | Unknown: research only | Ng XL et al. *Animal Kingdom.* CVPR 2022 |
| [Idaho Camera Traps](https://lila.science/datasets/idaho-camera-traps/) | Not obtained | Custom: "Images may not be sold in any format" | **No** | |

CDLA-Permissive-1.0 lets us use, modify and share the data, including commercially. Anyone who redistributes the **data** must include the licence text; results of analysing the data (for example trained weights) are not restricted by it. Each run writes `ATTRIBUTION.md` next to its manifest.

### Other models

| Model | Code | Weights | Commercial use | Notes |
| --- | --- | --- | --- | --- |
| MegaDetector V6 ([Pytorch-Wildlife model zoo](https://microsoft.github.io/Pytorch-Wildlife/model_zoo/)) | MIT | `MDV6-yolov9-c/e`, `MDV6-yolov10-c/e`: **AGPL**. `MDV6-mit-yolov9-c/e`: **MIT**. `MDV6-apa-rtdetr-c/e`: **Apache 2.0** | MIT and Apache variants only | Not downloaded. Best candidate to raise animal recall in front of SpeciesNet; record the exact file and hash before use |
| MegaDetector v5a/v5b, MDv1000 ([agentmorris/MegaDetector](https://github.com/agentmorris/MegaDetector)) | MIT (repository) | Not stated; v5 is YOLOv5-based (Ultralytics, AGPL-3.0) | **Not cleared** | Do not ship. Precomputed v5a boxes are kept only as unverified hints |
| Pytorch-Wildlife DFNE (northeastern North America) | MIT (repository) | Not stated | Unknown | Species list not published |

## Species coverage in the source metadata

Counts are image-level labels in each dataset's own metadata (ENA24: boxes). They show what exists to sample from, not what we downloaded.

| Guardian target | ENA24 | Caltech | NACTI | WCS | Notes |
| --- | --- | --- | --- | --- | --- |
| Black bear | 959 boxes | none | 28,770 | none (`ursus thibetanus` is the Asiatic black bear, a negative) | |
| Grizzly bear | none | none | none | none | **No LILA dataset has `ursus arctos`** (LILA taxonomy mapping, 2026-10-16) |
| Cougar | none | 145 | 14,756 | 6,823 (mostly Latin America) | |
| Wolf | none | none | 0 | 822 (countries not checked) | WSU Lynx and Idaho list `canis lupus` (Washington, Idaho); Idaho is not for sale |
| Coyote | 344 boxes | 16,601 | 20,968 | 40 | |
| Moose | none | none | 9,960 | none | Also in WSU Lynx and AMMonitor |
| Elk | none | none | 22,143 | none | NACTI also has 183,794 `cervus elaphus` ("red deer"), many from San Juan Mountains, Colorado, where *Cervus* is elk. Still kept as **`uncertain:elk`** because the source label says red deer |

## The importers

`src/sentinel/guardian/datasets.py` (JSON metadata) and `src/sentinel/guardian/dataset_stream.py` (CSV metadata streamed from the zip), both standard library only, with the registry `config/guardian/datasets.json`.

```powershell
$env:PYTHONPATH='src'
python -m sentinel.guardian.datasets sources
python -m sentinel.guardian.datasets fetch --only ena24 caltech_camera_traps --out data/guardian/runs/initial --max-mb 900
python -m sentinel.guardian.dataset_stream --source nacti --quota moose=40 elk=40 uncertain:elk=15 black_bear=30 cougar=30 coyote=20 negative=20 --out data/guardian/runs/nacti_initial --max-mb 300
python scripts/guardian/speciesnet_eval.py --run data/guardian/runs/nacti_initial --out bench_runs/guardian_species/nacti_initial
python -m unittest tests.test_sentinel_guardian_datasets tests.test_sentinel_guardian_dataset_stream
```

1. **Metadata, not archives.** Metadata is cached in `data/guardian/cache/<dataset>/`; images are fetched one by one over HTTPS. JSON metadata above 512 MB uncompressed is refused (NACTI 1.17 GB, WCS 838 MB). NACTI's 593 MB CSV is streamed row by row instead, with memory bounded by reservoir sampling (at most 40 candidates per bucket and study area).
2. **Explicit label maps.** `species`, `uncertain`, `negative` or `excluded`; an unmapped label is never imported, and an image with two targets is skipped.
3. **Licence gate.** Only `commercial_use: allowed` sources are fetched.
4. **Diverse selection, provenance, duplicates (SHA-256), budget and resume**, as before. Manifests keep every source label and annotation.
5. **Splits** by camera location (else sequence); images with neither stay `unassigned`. NACTI's CSV location is a study area (coarser than the JSON's camera id), so a whole area lands on one side. In this sample all three areas hashed to `train`, so NACTI has no eval split yet.
6. **Future datasets:** a COCO Camera Traps JSON is a registry entry; a LILA CSV needs a `metadata_csv_url`; another format is one parser.

## Downloaded samples

| Run | Images | Size | Contents | Splits |
| --- | --- | --- | --- | --- |
| `data/guardian/runs/initial` (2026-10-15) | 240 | 100.9 MB | Black bear 60 (ENA24), cougar 50 (Caltech), coyote 50, negatives 80 (20 empty) | Caltech by camera (10 cougar cameras); ENA24 `unassigned` |
| `data/guardian/runs/nacti_initial` (2026-10-16) | 195 | 93.6 MB | Moose 40, elk 40, `uncertain:elk` 15, black bear 30, cougar 30, coyote 20, empty 20 | All `train` (three study areas); empty frames have no location |

Storage on disk: 194.5 MB of images (188 MB on disk), 170 MB of cached metadata (including the unused NACTI/WCS JSON, the NACTI CSV and LILA's taxonomy mapping), 435 MB for the SpeciesNet checkpoint plus its ONNX export, the 200 MB downloaded archive (can be deleted), and 69 MB for Sentinel's YOLOX-s. No duplicates, no failed downloads, no location in both splits.

## What is missing

1. **Grizzly bears.** No LILA dataset has `ursus arctos`. The best lead is iNaturalist research-grade observations with CC0 or CC-BY photos (per-photo licences; CC-BY-NC excluded). That would be close-up photos rather than camera-trap frames. Until then, a "grizzly bear" or "brown bear" call from SpeciesNet is unvalidated, and Sentinel's wording falls back to "Bear detected — species uncertain" whenever black and brown bear scores are close.
2. **North American wolves.** NACTI has none; WCS's are unchecked. WSU Lynx (Washington, 637 locations, CDLA-Permissive) is registered as a candidate; its metadata still needs inspecting and a label map.
3. **Detector recall on camera-trap stills** (43 % here) is the weakest link. See next steps.
4. **Security-camera viewpoint and video.** Everything here is knee-height camera-trap stills. The species path still needs testing on the owner's camera footage.
5. **Independent evaluation.** SpeciesNet was trained partly on public camera-trap data that may include these datasets, ENA24 has no locations, and NACTI's sample has no eval split.
6. **Expert verification** of source labels, and **behaviour labels** (Animal Kingdom only, not licensed).
7. **NVIDIA measurements.** The GTX 1070 machine has not been measured.

## Next steps (no training yet)

1. Measure a permissive camera-trap detector in front of SpeciesNet (MegaDetector V6 `MDV6-apa-rtdetr-c` or `MDV6-mit-yolov9-c`) on the same samples with `speciesnet_eval.py`.
2. Inspect WSU Lynx metadata, add its label map (wolf, moose, elk, black bear) and fetch a location-split sample.
3. Run the evaluation on the GTX 1070 machine (copy `models/speciesnet/v4.0.3a/`) and record speed.
4. Run the species path on the owner's recorded camera footage (see [wilderness-guardian.md](wilderness-guardian.md#real-camera-validation-plan)) before enabling it on a live site.
