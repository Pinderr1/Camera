# Guardian wildlife datasets

What wildlife data Sentinel Guardian has, where it came from, what it may be used for, and what is still missing. Companion to [wilderness-guardian.md](wilderness-guardian.md) (Phase 1) and the licence audit in [`sources.md`](../sources.md).

Nothing here is a trained model or an accuracy measurement. The sample prepares evaluation and later fine-tuning of a species classifier. Every accuracy claim must still name the labelled footage behind it.

## Summary (2026-10-15)

- **Usable commercially:** ENA24, Caltech Camera Traps, NACTI and WCS Camera Traps, all CDLA-Permissive-1.0 on LILA BC.
- **Research only / not fetched:** Animal Kingdom (no licence published), Idaho Camera Traps (images may not be sold).
- **Species coverage from the metadata:** black bear, cougar, coyote, moose and elk are present (mainly NACTI); wolf only in WCS (822 images; which countries is not yet checked); **no grizzly bear in any registered dataset.**
- **Downloaded:** 240 images (100.9 MB) from ENA24 and Caltech: black bear 60, cougar 50, coyote 50, negatives 80. **No moose, elk or wolf images yet**: they are only in NACTI and WCS, whose metadata is too large to parse in memory on this machine (see [What is missing](#what-is-missing)).

## Licences, versions and attribution

Checked against each publisher's own page on 2026-10-15. "Commercial use" is our reading of the published licence, not legal advice; a lawyer should confirm before a product ships with a model trained on this data.

| Dataset | Version used | Licence | Commercial use | Attribution / citation |
| --- | --- | --- | --- | --- |
| [ENA24-detection](https://lila.science/datasets/ena24detection) | `ena24.json` (info.version 1); page updated 2024-08-28 | [CDLA-Permissive-1.0](https://cdla.io/permissive-1-0/) | Allowed | Yousif H, Kays R, He Z. *Dynamic Programming Selection of Object Proposals for Sequence-Level Animal Species Classification in the Wild.* IEEE TCSVT, 2019 |
| [Caltech Camera Traps](https://lila.science/datasets/caltech-camera-traps) | `caltech_camera_traps.json` version 20210113; boxes `caltech_bboxes_20200316.json` | CDLA-Permissive-1.0 | Allowed | Beery S, Van Horn G, Perona P. *Recognition in Terra Incognita.* ECCV 2018 |
| [NACTI](https://lila.science/datasets/nacti) | `nacti_metadata.1.14.json` (2019-02-27) | CDLA-Permissive-1.0 | Allowed | Tabak MA et al. *Machine learning to classify animal species in camera trap images: Applications in ecology.* Methods Ecol Evol 10(4):585-590, 2019 |
| [WCS Camera Traps](https://lila.science/datasets/wcscameratraps) | `wcs_camera_traps.json` (info.version 1.0, 2019) | CDLA-Permissive-1.0 | Allowed | Credit the Wildlife Conservation Society (no paper named on the page) |
| [LILA MegaDetector results](https://lila.science/megadetector-results-for-camera-trap-datasets/) | `*_mdv5a.0.0_results.json` (MDv5a, 2022) | **None stated** | Unclear | Machine-generated boxes; LILA notes most of these datasets were in MegaDetector's training data, so they cannot measure its accuracy |
| [Animal Kingdom](https://github.com/sutdcv/Animal-Kingdom) | Not obtained | **None published**; request form | Unknown: research only | Ng XL et al. *Animal Kingdom.* CVPR 2022 |
| [Idaho Camera Traps](https://lila.science/datasets/idaho-camera-traps/) | Not obtained | Custom: "Images may not be sold in any format" | **No** | |

CDLA-Permissive-1.0 lets us use, modify and share the data, including commercially. Anyone who redistributes the **data** must include the licence text; results of analysing the data (for example trained weights) are not restricted by it. Each run writes `ATTRIBUTION.md` next to its manifest with the licence and citation for every dataset it used.

### Models

| Model | Code | Weights | Commercial use | Notes |
| --- | --- | --- | --- | --- |
| [SpeciesNet](https://github.com/google/cameratrapai) v4.0.3a (crop) / v4.0.3b (whole image) | Apache-2.0 | Apache 2.0 (Kaggle `licenseName` on every instance v4.0.0a–v4.0.3b, [kaggle.com/models/google/speciesnet](https://www.kaggle.com/models/google/speciesnet)) | Allowed for the classifier | 2,000+ labels, geofencing by country and US state, taxonomic roll-up. The ensemble runs MegaDetector v5a first, whose weights are not cleared (below), so a shippable pipeline needs a permissive detector in front of it. Training data includes Wildlife Insights images; not independently reviewed. Cite Gadot et al. 2024, *To crop or not to crop*, IET Computer Vision |
| MegaDetector V6 ([Pytorch-Wildlife model zoo](https://microsoft.github.io/Pytorch-Wildlife/model_zoo/)) | MIT | `MDV6-yolov9-c/e`, `MDV6-yolov10-c/e`: **AGPL**. `MDV6-mit-yolov9-c/e`: **MIT**. `MDV6-apa-rtdetr-c/e`: **Apache 2.0** | MIT and Apache variants only | Classes: animal, person, vehicle. No species. Weight URLs are resolved by the Pytorch-Wildlife loader; record the exact file and hash before use |
| MegaDetector v5a/v5b, MDv1000 ([agentmorris/MegaDetector](https://github.com/agentmorris/MegaDetector)) | MIT (repository) | Not stated; v5 is YOLOv5-based (Ultralytics, AGPL-3.0) | **Not cleared** | Do not ship. Precomputed v5a boxes are kept only as unverified hints |
| Pytorch-Wildlife DFNE (northeastern North America) | MIT (repository) | Not stated | Unknown | Species list not published on the model zoo page |

## Species coverage in the source metadata

Counts are image-level labels in each dataset's own metadata (ENA24: images with a box of that class). They show what exists to sample from, not what we downloaded.

| Guardian target | ENA24 | Caltech | NACTI | WCS | Notes |
| --- | --- | --- | --- | --- | --- |
| Black bear | 959 boxes (`American Black Bear`) | none | 28,770 (`ursus americanus`) | none (`ursus thibetanus` is the Asiatic black bear, kept as a negative) | |
| Grizzly bear | none | none | none | none | **No source.** Needs owner footage or a new dataset |
| Cougar | none | 145 (`mountain_lion`) | 14,756 (`puma concolor`) | 6,823 (`puma concolor`, mostly Latin America) | |
| Wolf | none | none | 0 (category exists, no images) | 822 (`canis lupus`) | WCS spans 12 countries; where these wolves were photographed has not been checked |
| Coyote | 344 boxes | 16,601 | 20,968 | 40 | |
| Moose | none | none | 9,960 (`alces alces`) | none | |
| Elk | none | none | 22,143 (`cervus canadensis`) | none | NACTI also has 183,794 `cervus elaphus` (red deer). In US studies that may be elk under older taxonomy, but the source does not say so: kept as **`uncertain:elk`**, never as elk |

## The importer

`src/sentinel/guardian/datasets.py` (standard library only) with the registry `config/guardian/datasets.json`.

```powershell
$env:PYTHONPATH='src'
python -m sentinel.guardian.datasets sources                         # registry and licences
python -m sentinel.guardian.datasets plan                            # what the metadata offers and what would be picked
python -m sentinel.guardian.datasets fetch --out data/guardian/runs/initial --max-mb 900
python -m sentinel.guardian.datasets fetch --only nacti --quota moose=100 --out data/guardian/runs/moose
python -m unittest tests.test_sentinel_guardian_datasets
```

What it does:

1. **Metadata, not archives.** Downloads each dataset's metadata JSON (cached in `data/guardian/cache/<dataset>/`), then fetches individual images over HTTPS from the LILA Google Cloud bucket. Downloads above 200 MB and JSON above 512 MB uncompressed are refused, so NACTI (1.17 GB) and WCS (838 MB) are refused today (see below).
2. **Explicit label maps.** Each source label maps to a Guardian target with a status: `species` (the source names that species; not verified by Sentinel), `uncertain` (a possible target the label does not settle), `negative` (empty frames and non-target animals, including hard negatives such as dogs, deer and cattle) or `excluded` (people and vehicles, never downloaded). A label missing from the map means the image is not imported, so a new category can never fall silently into the negatives. Images with two different targets are skipped in this first sample. Unmapped labels are counted in `summary.json`.
3. **Licence gate.** Only sources with `commercial_use: allowed` are fetched; `--allow-research-only` opts in to others.
4. **Diverse selection.** Per-bucket quotas filled round-robin across datasets and camera locations (reservoir-sampled, deterministic seed), so one camera cannot dominate a class.
5. **Provenance.** `manifest.jsonl` has one record per image: dataset, source image id, file and URL, SHA-256, bytes, source labels, every source annotation (boxes, counts, age/sex where given), Guardian labels with status, MegaDetector v5a boxes marked "machine-generated, not verified", location, sequence, licence, attribution, source version and retrieval time.
6. **Duplicates.** Exact duplicates (same SHA-256) are dropped and listed. Near-duplicates (burst frames) are kept: they are real frames, and the location split already stops them crossing train/eval.
7. **Splits.** Train/eval (80/20) by hashing the camera location, else the sequence id. Images with neither (ENA24) are `unassigned`. `summary.json` lists any location found in both splits (`split_leaks`; must be empty).
8. **Budget and resume.** Stops at `--max-mb`; a rerun keeps files already on disk.
9. **Future datasets.** A new dataset in COCO Camera Traps format is a registry entry. Another format is one parser added to `FORMATS`.

Downloaded images, caches and manifests live under `data/guardian/` (git-ignored). Images are never committed.

## Downloaded sample

Run `data/guardian/runs/initial` on 2026-10-15 (`fetch --only ena24 caltech_camera_traps --max-mb 900`, seed 7). Storage: 100.9 MB of images (98 MB on disk) plus 139 MB of cached metadata, including the NACTI and WCS files, about 240 MB in total. No duplicates, no failed downloads, no location in both splits. Every file was checked to be a JPEG.

| Bucket | Images | From | Distinct camera locations | Split (train / eval / unassigned) | With source boxes |
| --- | --- | --- | --- | --- | --- |
| Black bear (`species`) | 60 | ENA24 | unknown (ENA24 has no location field) | 0 / 0 / 60 | 60 |
| Cougar (`species`) | 50 | Caltech `mountain_lion` | 10 | 42 / 8 / 0 | 18 |
| Coyote (`species`) | 50 | 25 ENA24, 25 Caltech | 25 (Caltech) + unknown (ENA24) | 22 / 3 / 25 | 34 |
| Negatives | 80 | 40 ENA24, 40 Caltech | 40 (Caltech) + unknown (ENA24) | 33 / 7 / 40 | 49 |
| Grizzly bear, wolf, moose, elk, `uncertain:elk` | 0 | | | | |

Negatives are 20 empty frames plus deer (17), dogs (11), bobcats (11), red fox (9), raccoon (5), domestic cat (5) and horse (2), all as labelled by the source. Every record also carries the MegaDetector v5a boxes LILA published for that image, marked unverified.

Available in the parsed metadata (eligible after the label rules): black bear 893 (ENA24), cougar 145 (Caltech), coyote 16,676, negatives 161,440. The Caltech cougars come from few cameras, so the 8 evaluation cougars cover very few locations.

Unmapped source labels (never imported) are listed per dataset in `summary.json`, for example ENA24 `American Crow`, `Virginia Opossum`, `Grey Fox` and Caltech `opossum`, `raccoon`, `fox`. Add them to the label map deliberately if they are wanted as negatives.

## What is missing

1. **Moose, elk and wolf images, and more black bear and cougar variety.** These are in NACTI and WCS, whose metadata (1.17 GB and 838 MB uncompressed) does not fit in memory on this machine, so the importer refuses them. Next step: a streaming parser registered in `FORMATS` (NACTI also publishes `nacti_metadata.csv.zip`, 31 MB, which can be read row by row), then rerun `fetch` for those sources. NACTI's MegaDetector results (125 MB zipped) are left out of the registry for the same reason.
2. **Grizzly bears.** None of the registered datasets has `ursus arctos`. Black bear vs grizzly cannot be trained or evaluated from this data; Sentinel keeps "Bear (species not determined)".
3. **North American wolves.** NACTI's wolf category is empty; the WCS wolves' countries are not yet checked.
4. **Security-camera viewpoint and video.** All four datasets are camera-trap stills at knee height. Movement intelligence (Phase 2) still needs tracked video from elevated house cameras.
5. **Boxes for NACTI and WCS species.** NACTI has image-level labels only; boxes would come from MegaDetector v5a results, which are unverified and from a model whose weights are not cleared for shipping.
6. **Expert verification.** `species` means the publisher's label. Nothing has been reviewed by a wildlife professional.
7. **Location metadata for ENA24.** No camera location or sequence, so ENA24 images (all 60 black bears) cannot be split by camera and stay `unassigned`.
8. **Behaviour labels.** Only Animal Kingdom has them, and it is not licensed for a product.

## Next steps (no training yet)

1. Run Sentinel's current detectors over the sample to measure how often each species is detected at all and which COCO label it gets. Report it per dataset and split, as camera-trap stills (not security-camera footage).
2. Evaluate SpeciesNet with a permissive detector (MegaDetector V6 Apache or MIT variant) in front of it on the `eval` split, without changing any Guardian wording until results are recorded.
3. Look for a commercially usable source of grizzly bear and North American wolf images; record owner footage with the bench tools.
