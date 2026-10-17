# Guardian wildlife datasets and species recognition

What wildlife data Sentinel Guardian has, where it came from, what it may be used for, how the optional species classifier (Google SpeciesNet) is wired in, what it did on real images, and what is still missing. Companion to [wilderness-guardian.md](wilderness-guardian.md) (Phase 1) and the licence audit in [`sources.md`](../sources.md).

No model has been trained. Every number below names the images behind it. They are small camera-trap samples from public datasets that SpeciesNet may have been trained on, so they show that the pipeline works and where it fails. **They are not accuracy claims for Sentinel cameras.**

## Summary (2026-10-16)

- **Species recognition works end to end, off by default.** SpeciesNet v4.0.3a (Apache-2.0 code and weights) runs on ONNX Runtime. Crops come from Sentinel's own detector boxes (YOLOX, plus the optional MegaDetector V6 Apache animal detector below); SpeciesNet's own MegaDetector v5a step is never used. Results flow into guardian tracks, wording, events and incidents, notifications, the live overlay and `/api/v1/guardian`. A species is named only when confident; otherwise "Bear detected — species uncertain" or the detector's unconfirmed label.
- **On real images (435 downloaded):** with Sentinel's boxes, 160 of the 161 crops SpeciesNet confirmed were the species the dataset names. The one wrong call was an elk confirmed as domestic cattle at 0.98. **The bottleneck is the detector:** YOLOX found an animal in only 168 of the 395 images that contain one (many are night IR frames or partly visible animals).
- **Usable commercially:** ENA24, Caltech Camera Traps, NACTI, WCS and WSU Lynx (all CDLA-Permissive-1.0). **Research only / not fetched:** Animal Kingdom, Idaho Camera Traps.
- **Detector gap closed on camera-trap stills (2026-10-16).** The optional animal detector (MegaDetector V6, Apache variant `MDV6-apa-rtdetr-c`, next to YOLOX) finds an animal in 368 of 395 animal images (YOLOX alone: 168), and SpeciesNet then confirms the right species on 321–324 of 395 (YOLOX: 157). Cost: boxes on 9 of 40 empty frames (rocks, sticks; mostly called `blank` by SpeciesNet, which Guardian now uses to set such tracks aside) and about twice the detection time. MegaDetector's training data probably includes these datasets, so these figures are optimistic. See [Animal detector](#animal-detector-megadetector-v6).
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

- **Detector recall is the main limitation.** YOLOX found an animal in 168 of 395 animal images (43 %): 56 of 90 black bears, 27 of 80 cougars, 22 of 70 coyotes, 21 of 40 moose and 13 of 40 elk. It also gives the wrong COCO label (cougars as `dog`, `cat` or `bird`). The optional MegaDetector V6 Apache add-on now closes most of this gap (see [Animal detector](#animal-detector-megadetector-v6)).
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

## Animal detector (MegaDetector V6)

Added 2026-10-16 to close the detector gap above. Optional and off by default: `detector.animal_detector` (see [architecture.md](architecture.md#3-detection-detectorspy-detectors_onnxpy)). YOLOX stays the main detector for people and vehicles. MegaDetector only adds `animal` boxes that YOLOX missed.

### Which MegaDetector, and why

| Variant | Licence (Pytorch-Wildlife model zoo) | Published animal mAR / mAP50 | Decision |
| --- | --- | --- | --- |
| MDv5a/b, MDV6-yolov9/yolov10/rtdetr (Ultralytics) | AGPL-3.0 | 76.8–82.8 / 87.2–92.8 | Excluded (AGPL) |
| **MDV6-apa-rtdetr-c** (RT-DETRv2-R18, 20 M parameters) | **Apache-2.0** | 81.1 / 91.0 | **Integrated** (`mdv6_apa_rtdetr_c`) |
| MDV6-apa-rtdetr-e (R101, 76 M) | Apache-2.0 | 82.9 / 94.1 | Not tried: 1.2 GB checkpoint, about 4× the compute |
| MDV6-mit-yolov9-c (9.7 M) | MIT | 74.8 / 87.6 | Downloaded, not exported: lower published recall, and its export needs the MultimediaTechLab YOLO stack (omegaconf, lightning, a separate config file). Worth revisiting if CPU-only speed matters |
| MDV6-mit-yolov9-e | MIT | 76.1 / 71.5 | Not tried |

The weights come from Zenodo record 15398270 (`MDV6-apa-rtdetr-c.pth`, MD5 `3992888e…be97c3`, SHA-256 `4814cdbc…a6fce`). The record carries a blanket CC-BY-4.0 tag; the per-model Apache licence in the model zoo is the one applied (see `sources.md`). `scripts/guardian/export_megadetector_onnx.py` (throwaway torch environment) refuses any other MD5 before unpickling, exports the model without its post-processor (`logits`, `pred_boxes`; opset 17; ONNX SHA-256 `2aed208d…ac7db4`) and checks parity on a real image: max logit difference 1.2e-4, max box difference 1.2e-5. DirectML and CPU give identical boxes in Sentinel. Preprocessing matches MegaDetector's training: plain 640×640 resize (area interpolation when shrinking), RGB 0–1, sigmoid scores.

**Caveat on every number below:** MegaDetector's training data very likely includes these LILA datasets (ENA24, Caltech, NACTI). YOLOX saw COCO only. MegaDetector's still-image figures are therefore optimistic. The video clips are not from LILA.

### Still images: 435 camera-trap frames

`python scripts/guardian/animal_detector_eval.py run --out bench_runs/guardian_detectors/NAME` (each detector in its own process; threshold sweep; then SpeciesNet on each configuration's best box). 395 images contain an animal (the `negative` bucket holds deer, foxes, raccoons and other non-target animals); 40 are labelled empty. Box metrics use the dataset's own boxes (178 boxes on ENA24 and part of Caltech; NACTI has none). Night = infrared (equal colour channels).

| Detector (threshold) | Animal images found | Day | Night | Empty frames with an animal box | Box recall @IoU 0.5 | Box precision | Mean IoU |
| --- | --- | --- | --- | --- | --- | --- | --- |
| YOLOX-s 960×544 (0.5, shipped) | 168/395 (42 %) | 92/195 | 76/200 | 0/40 | 76/178 | 92 % | 0.86 |
| YOLOX-s (0.1) | 294/395 (74 %) | 145/195 | 149/200 | 6/40 | 128/178 | 48 % | 0.86 |
| MegaDetector V6 Apache (0.4) | 385/395 (98 %) | 188/195 | 197/200 | 32/40 | 173/178 | 94 % | 0.87 |
| **MegaDetector V6 Apache (0.6, default)** | **367/395 (93 %)** | 175/195 | **192/200** | **9/40** | 170/178 | 98 % | 0.87 |
| YOLOX-s 0.5 + MegaDetector 0.6 (add-on) | 368/395 (93 %) | 176/195 | 192/200 | 9/40 | 170/178 | 94 % | 0.86 |

Images found per target, YOLOX → add-on at 0.6: black bear 56 → 83 of 90, cougar 27 → 66 of 80, coyote 22 → 67 of 70, elk 13 → 39 of 40, moose 21 → 40 of 40, other animals 20 → 59 of 60. Lowering YOLOX's threshold is not a fix: at 0.1 it still misses a quarter of the animals and halves box precision.

**False positives.** The 0.4 boxes on "empty" frames were checked by eye: a stick, graffiti on a culvert, rocks and a shadow, scored 0.6–0.8. At 0.6, 9 remain; SpeciesNet calls 7 of those 9 `blank` (6 at ≥ 0.8), and the other two are probably real animals in frames labelled empty (an opossum at the frame edge that MDv5a also boxed, and a raccoon in a dark frame). So 0.6 is the default: compared with 0.4 it keeps the same species results (below) with a quarter of the empty-frame boxes.

**With SpeciesNet** (one crop per image, `min_agreeing=1`; images where the detector found the animal and SpeciesNet confirmed the dataset's species, over all 395):

| Configuration | Confirmed right | Confirmed wrong | Uncertain | Unknown | No box | End to end |
| --- | --- | --- | --- | --- | --- | --- |
| YOLOX-s | 157 | 1 | 5 | 5 | 227 | **157/395 (40 %)** |
| MegaDetector 0.6 | 324 | 11 | 15 | 17 | 28 | **324/395 (82 %)** |
| YOLOX-s + MegaDetector 0.6 | 321 | 12 | 18 | 17 | 27 | 321/395 (81 %) |

Black bear 76/90 confirmed (YOLOX 52), cougar 58/80 (25), coyote 61/70 (21), elk 29/40 (12), moose 35/40 (20). Of the 11 wrong confirmations, 8 are one NACTI camera in Florida (FL-02) labelled elk and called domestic cattle: near-black night frames with only a rump or flank in view, and Florida has no wild elk, so the label itself is doubtful. The others: a bear called wild boar, a coyote called red fox, a coyote called white-tailed deer. These are single-crop calls; live tracks need two agreeing crops.

**SpeciesNet `blank` on real animals.** Of the 367 MegaDetector boxes on animal images, SpeciesNet called 7 (1.9 %) blank at ≥ 0.8 on a single crop (mostly night or partial views). This matters for the rule below.

### Video: 13 trail-camera clips

`python scripts/guardian/fetch_wildlife_videos.py` then `python scripts/guardian/video_eval.py --detectors yolox_s,mdv6,yolox_s+mdv6 --out bench_runs/guardian_video/NAME`. Clips and labels: `config/guardian/videos.json` (Wikimedia Commons: 11 US public domain, 2 CC BY-SA 2.0; licence and SHA-1 checked on download). 8 day clips, 3 night infrared, 1 tiny weasel, 1 empty river. Each clip runs at 5 fps through the detector, IoUTracker, SentinelRuntime, Guardian and SpeciesNet (on its real worker thread). Presence ranges were labelled by eye from contact sheets.

| Clip | Light | Animal-present frames found: YOLOX / MD / YOLOX+MD | Empty ranges, frames with a shown track (YOLOX / YOLOX+MD) | Expected species confirmed: YOLOX / YOLOX+MD |
| --- | --- | --- | --- | --- |
| black_bear_day | day | 78% / 84% / 98% | - / - | yes / yes |
| cougar_day | day | 40% / 64% / 65% | 0/130 / 3/130 | yes / yes |
| wolf_day | day | 64% / 100% / 100% | 0/7 / 1/7 | no / no |
| coyote_day | day | 58% / 100% / 100% | - / - | yes / yes |
| elk_cow_calf_day | day | 85% / 100% / 100% | 0/8 / 0/8 | WRONG: Red deer / WRONG: Red deer |
| elk_herd_day | day | 100% / 100% / 100% | - / - | no / no |
| elk_bulls_snow | day | 94% / 100% / 100% | - / - | yes / yes (also wrong: Red deer, Red deer) |
| lynx_snow_day | day | 40% / 69% / 73% | 0/30 / 0/30 | yes / yes |
| weasel_day | day | 0% / 0% / 0% | 0/35 / 0/35 | - / - |
| fox_family_night | night | 98% / 100% / 100% | - / - | yes / yes |
| moose_night | night | 69% / 100% / 100% | - / - | yes / yes |
| coyote_night | night | 9% / 100% / 100% | - / - | no / yes |
| river_empty | day | - / - / - | 0/150 / 17/150 | - / - |

| Configuration | Animal-present frames found (day / night / all) | Empty-range frames with a box | ... with a shown Guardian track | Clips with expected species confirmed | Wrong species confirmations |
| --- | --- | --- | --- | --- | --- |
| YOLOX | 490/763 (64%) / 175/276 (63%) / 665/1039 (64%) | 1/360 | 0/360 | 7/11 | 1 |
| MD | 628/763 (82%) / 276/276 (100%) / 904/1039 (87%) | 75/360 | 21/360 | 8/11 | 1 |
| YOLOX+MD | 668/763 (88%) / 276/276 (100%) / 944/1039 (91%) | 75/360 | 21/360 | 8/11 | 3 |

What it shows:

- **Recall:** the add-on finds animals on 91 % of animal-present frames (YOLOX 64 %); night infrared goes from 63 % to 100 %. The night coyote YOLOX almost never saw (9 %) is found on every frame and confirmed as a coyote. Detection time per frame roughly doubles (YOLOX 180–260 ms, add-on 460–750 ms on the Iris Xe).
- **Species:** the expected species is confirmed on 8 of 11 clips (YOLOX 7). Still not confirmed: the black wolf (SpeciesNet stays at "Dog-like animal — species uncertain", correctly not naming it), the elk herd (stays "Deer-family animal — species uncertain") and the elk cow and calf (confirmed as **red deer**, wrong). The add-on also confirmed red deer on two short tracks in the bull-elk clip (YOLOX-labelled `cow`/`horse` boxes), so it has 3 wrong confirmations to YOLOX's 1. SpeciesNet's geofence allows red deer in Canada (farmed and feral herds), so setting `country: CAN` does not prevent it; it does in the USA.
- **False positives:** MegaDetector boxes the empty river on 71 of 150 frames (YOLOX: 0) and opens four tracks. With the blank rule (below) all four are set aside; a false track is still on screen for about a second before its second crop (17 of 150 frames). The cougar clip keeps 3 shown frames after the cougar leaves.
- **Still missed by everything:** the weasel (tiny, under two seconds), the cougar once it is small and half hidden in vegetation (about 35 % of its frames), and the lynx at distance on bright snow (about 30 %).
- **More tracks per animal:** the add-on splits animals into more Guardian tracks (black bear 4 vs 2, elk bulls 5 vs 3), because YOLOX and MegaDetector boxes alternate on the same animal. Guardian links them by re-acquisition only when unambiguous.

### Generic boxes the classifier calls blank (Guardian)

MegaDetector boxes moving water, rocks and sticks. On the river clip it opened four "Animal (species unknown)" tracks, and SpeciesNet's agreed top label on every one was `blank` (0.90–0.94). Guardian now **sets aside** a track when (1) only the generic `animal` label ever described it (no COCO label such as `bear`, `dog` or `cat` from the main detector), and (2) at least `min_agreeing` (2) crops agree the box is `blank` with an averaged score of at least `guardian.species_classifier.blank_confidence` (0.8). A set-aside track gets no level, no event and no overlay; it stays in `/api/v1/guardian` with `set_aside` explaining why, and a later crop that finds an animal brings it back. To shorten the time a false box is on screen, a generic box whose first crop looks blank is re-checked after `blank_recheck_s` (1 s) instead of the usual 4 s. A COCO-labelled track is never set aside, so the classifier still never quietens the main detector. `blank_confidence: 1` turns the rule off. The rule only works with the species classifier on, so **use the animal detector together with the species classifier.** Residual risk: an animal seen only by MegaDetector and called blank on two crops would be set aside (single-crop rate on real animals above: 1.9 %).

### Speed and memory (i5-1135G7, Iris Xe, DirectML)

| Detector | Input | Detection p50 / p95 | CPU p50 / p95 | Process RSS added |
| --- | --- | --- | --- | --- |
| YOLOX-s | 960×544 | 147 / 207 ms | 86 / 99 ms (CPU profile: YOLOX-tiny 416) | 384 MB |
| MegaDetector V6 Apache | 640×640 | 220 / 447 ms | 804 / 1,003 ms (2048×1536 stills) | 635 MB |

With the add-on, each processed frame runs both detectors in sequence. In the video test that was about 600–750 ms per frame on this laptop (YOLOX alone 190–270 ms), so a 5 fps camera is processed at roughly 1.5 fps: enough for slow animal movement, too slow for several cameras on this machine. The CPU path (0.8 s per frame) is too slow for continuous use. No NVIDIA figures yet.

### Real-time optimisation (2026-10-16)

Same 13 clips, same i5-1135G7 / Iris Xe laptop, one run per configuration (`bench_runs/guardian_video/opt1`). "Followed" = animal-present frames with a live Guardian track (seen within 1 s): what a person sees as "the animal is being tracked". Duplicate-track frames = two live tracks overlapping (IoU > 0.3), a proxy for one animal tracked twice (identity switches need per-frame identity labels, which these clips do not have). Detection means are per processed frame; this laptop's GPU timing varies by about ±10 % between runs.

| Configuration | Animal frames with a box | Followed (night) | Empty frames: box / shown track | Duplicate-track frames | Guardian tracks / animals | Expected species / wrong | Detection mean |
| --- | --- | --- | --- | --- | --- | --- | --- |
| YOLOX only | 64 % | 68 % (61 %) | 1 / 0 of 360 | 111 | 25 / 21 | 7/11 / 1 | about 180 ms |
| First add-on (`merge: add_missing`, MegaDetector every frame) | 91 % | 88 % (98 %) | 75 / 21 | 157 | 36 / 21 | 8/11 / 3 | 482 ms |
| Paired boxes (`merge: animal_box`), every frame | 91 % | 88 % (98 %) | 75 / 21 | 117 | 33 / 21 | 8/11 / **1** | 471 ms |
| **Paired + schedule (shipped defaults: scan 1 s, follow every frame for 3 s)** | 91 % | 88 % (98 %) | 74 / 18 | 117 | 33 / 21 | 8/11 / 1 | 417 ms |
| Paired + schedule, half rate while following (`follow_interval_s: 0.4`) | 77 % | 87 % (96 %) | 32 / 8 | 122 | 29 / 21 | 8/11 / 1 | **298 ms** |

What changed and why:

- **Paired boxes** fixed the main cause of split tracks: on frames where YOLOX missed the animal, MegaDetector's differently sized box stood in, so one animal alternated between two geometries. Now each animal gets MegaDetector's box and keeps YOLOX's specific label. Wrong species confirmations fell from 3 to 1 and duplicate-track frames from 157 to 117, with the same recall.
- **Schedule.** These are animal-heavy trail-camera clips, so the saving here is modest (−11 %). It is large on empty scenes, which is most of a security camera's day: on the weasel clip (animal for under 2 s) MegaDetector ran on 10 of 50 frames and detection p50 fell from 447 to 184 ms, YOLOX's own cost. On the river clip its false boxes on water kept it following (147 of 151 frames); boxes on tracks Guardian has set aside no longer keep it following (unit-tested, not re-measured on the clip).
- **Half rate while following** cuts detection time by about 35 %, but a few animals are followed less (night coyote 97 → 92 %, day coyote 88 → 83 %). That is an accuracy trade, so it stays opt-in, e.g. for several cameras on one small GPU.
- **SpeciesNet re-use:** a species confirmed at ≥ 0.95 by two crops is no longer re-classified (`settle_confidence`), because every crop now holds the shared GPU (next item). Not separately measured.
- **Crash fixed (found while profiling):** two different DirectML sessions running at the same time crash the process (exit code 139, reproduced every time: YOLOX with SpeciesNet, YOLOX with MegaDetector). Live Sentinel could hit this whenever the species worker classified while a camera ran YOLOX, or with two cameras on different models. All DirectML calls now share one lock (both pairs then ran 30 concurrent rounds without a crash), and a caller waiting more than 10 s for the GPU gets an error, so a hung GPU call ends in the existing CPU fallback instead of stalling every camera.
- **fp16:** YOLOX in fp16 keeps people-detection accuracy within 0.3 points and is about 30 % faster on this GPU ([benchmarking.md](benchmarking.md#yolox-s-in-fp16-2026-10-16)); available as `detector.precision: "fp16"`. MegaDetector does not load in fp16 under DirectML. Lower input resolution and parallel model execution were not adopted: MegaDetector's export has a fixed 640×640 input, and parallel DirectML execution crashes (above).
- **Not measured** (stopped for time): the combined fp16 + schedule configuration on the clips, the set-aside schedule fix on the river clip, and the multi-camera load test (`scripts/guardian/multicam_load.py` is written but was not run; this laptop had about 1 GB of free RAM while another project's Android emulator ran, and the last full run failed with out-of-memory errors).

### Recommended configuration

For a camera where wildlife matters, keep YOLOX and add both optional models:

```json
"detector": {"type": "onnx", "model": "auto", "animal_detector": {"model": "mdv6_apa_rtdetr_c", "score_threshold": 0.6}},
"guardian": {"enabled": true, "species_classifier": {"enabled": true}}
```

`animal_detector` can also be set per camera (`cameras[].detector`). Why the add-on and not MegaDetector alone: MegaDetector as the only detector (`"model": "mdv6_apa_rtdetr_c"`, which also reports `person` and `vehicle`) was nearly as good on animals in the video test (87 % of frames, 1 wrong species, fewer split tracks, half the detection time), but its person and vehicle detection has never been measured on Sentinel's people benchmark, so it must not replace YOLOX on a security camera. On a wildlife-only camera (no people rules) it is a reasonable, cheaper choice. Without the exported model the camera runs YOLOX alone and the `animal_detector:<camera>` health component says why. Everything else (people, vehicles, V1 rules) is unchanged.

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
| MegaDetector V6 ([Pytorch-Wildlife model zoo](https://microsoft.github.io/Pytorch-Wildlife/model_zoo/)) | MIT | `MDV6-yolov9-c/e`, `MDV6-yolov10-c/e`: **AGPL**. `MDV6-mit-yolov9-c/e`: **MIT**. `MDV6-apa-rtdetr-c/e`: **Apache 2.0** | MIT and Apache variants only | `MDV6-apa-rtdetr-c` **integrated** as the optional animal detector (hashes in [Animal detector](#animal-detector-megadetector-v6)); `MDV6-mit-yolov9-c` downloaded, not exported |
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
3. **Detector false positives and cost.** MegaDetector raised recall on stills from 42 % to 93 %, but it boxes rocks, sticks and moving water, and doubles detection time. Its numbers on LILA images are optimistic (likely training data). A tiny animal (weasel clip) is still missed by every detector.
4. **Security-camera viewpoint and video.** Everything here is knee-height camera-trap stills. The species path still needs testing on the owner's camera footage.
5. **Independent evaluation.** SpeciesNet was trained partly on public camera-trap data that may include these datasets, ENA24 has no locations, and NACTI's sample has no eval split.
6. **Expert verification** of source labels, and **behaviour labels** (Animal Kingdom only, not licensed).
7. **NVIDIA measurements.** The GTX 1070 machine has not been measured.

## Next steps (no training yet)

1. Run `scripts/guardian/video_eval.py` on the owner's own recorded footage (add entries with `"path"` to a copy of `config/guardian/videos.json`), day and night, with the animal detector and species classifier on; record false tracks per day.
2. Inspect WSU Lynx metadata, add its label map (wolf, moose, elk, black bear) and fetch a location-split sample.
3. Run the evaluation on the GTX 1070 machine (copy `models/speciesnet/v4.0.3a/`) and record speed.
4. Run the species path on the owner's recorded camera footage (see [wilderness-guardian.md](wilderness-guardian.md#real-camera-validation-plan)) before enabling it on a live site.
