# Reuse And License Audit

This project is reuse-first, but code is copied only when the upstream license clearly allows it.

## Direct Reuse

| Source | License / terms | Use in this prototype |
| --- | --- | --- |
| Hugging Face OmniFall dataset | Dataset card lists CC BY-NC 4.0; annotations/splits may also carry non-commercial/share-alike terms. Original videos keep original owners' terms. | Use as a research/evaluation dataset and label taxonomy reference. Do not train or ship commercial models from it without legal review. |
| Home Assistant Core | Apache-2.0 | Use as the local automation/runtime surface. Do not reimplement entity, dashboard, or notification plumbing. |
| ESPHome | Mixed; Python portions generally MIT, firmware/runtime pieces include GPLv3 components. | Use as external firmware/integration tooling. Do not copy runtime code into this repo. |
| MediaPipe | Apache-2.0 | Optional pose-only extraction baseline. Store derived landmarks/features by default. |
| MQTT | Protocol / broker-specific licenses | Use MQTT discovery or plain topics to pass local sensor events into Home Assistant and this state machine. |

## Research / Inspiration Only

| Source | Observed license risk | Useful idea | Do not copy |
| --- | --- | --- | --- |
| `simplexsigil/omnifall-experiments` | No visible license found during audit. | Benchmark organization, split discipline, feature-transformer comparison. | Training/evaluation code until license is clarified. |
| P2MFDS bathroom fall detection repo | No visible license found during audit. Dataset not yet generally available per README. | Bathroom privacy strategy: mmWave + vibration, object-drop hard negatives, sensor fusion. | Model code, preprocessing scripts, figures, dataset assumptions. |
| `radar-lab/mmfall` | No visible license found during audit. | Semi-supervised mmWave anomaly idea: anomaly spike plus centroid-height drop. | Notebook/code, Colab paths, radar preprocessing implementation. |
| YOLO + MediaPipe fall repos | Many are student/demo repos with unclear licenses. Ultralytics YOLO is AGPL unless separately licensed. | Quick visual-baseline patterns and posture thresholds. | Demo code and AGPL YOLO runtime in a closed product. |

## Local Policy

- Keep unlicensed repos as citations and implementation references only.
- Build the senior-night state machine, event normalizer, alert review flow, and household baselines ourselves.
- Prefer Home Assistant/ESPHome entity configuration over custom hardware drivers.
- Prefer pose-only and sensor-only records over raw video.
- Treat this as an assistive monitor, not a medical or emergency-service automation system.
