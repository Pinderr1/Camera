# Reuse And License Audit

This project is reuse-first, but code is copied only when the upstream license clearly allows it. Sentinel is intended to become a commercial product, so every shipped dependency and model must allow closed-source commercial distribution.

## Sentinel dependencies (current product)

| Component | License | Use | Notes |
| --- | --- | --- | --- |
| MediaPipe Tasks + EfficientDet-Lite0/2 models | Apache-2.0 | Object detection | Chosen specifically because it is commercially safe. |
| OpenCV (`opencv-contrib-python`) | Apache-2.0 (4.5+) | Capture, recording, image ops | FFmpeg inside the wheels is LGPL; ship the wheels unmodified. Bundled codecs (H.264) can carry patent-licensing obligations in commercial distribution, so review before shipping hardware. |
| FastAPI / Starlette / Pydantic | MIT | API | |
| Uvicorn | BSD-3 | ASGI server | |
| httpx | BSD-3 | Tests only | |
| Playwright for Python | Apache-2.0 | Browser UI tests only | Drives the installed Microsoft Edge; not shipped. |
| Hikvision SADP discovery | Own implementation | Troubleshooter discovery | No Hikvision code or SDK is used; Sentinel sends the multicast inquiry and parses the XML reply. |
| paho-mqtt | EPL-2.0 / EDL-1.0 | MQTT mirror | Used unmodified as a library. |
| pymavlink (optional) | LGPL-3.0 | Drone telemetry | Use unmodified as a dynamically imported library. Alternative: MAVSDK-Python (BSD-3). |
| ArduPilot / PX4 | GPL-3.0 / BSD-3 | Flight stacks (run on the drone, not linked) | Sentinel talks MAVLink over a link; it does not link or distribute firmware. |
| ntfy | Apache-2.0 / GPL-2.0 dual | Push delivery (external service or self-hosted) | Not linked. |
| psutil | BSD-3 | Benchmark resource sampling | |

### Benchmark footage and tools (evaluation only, never shipped or trained on)

| Source | Terms | Use |
| --- | --- | --- |
| MOTChallenge MOT16 (train sequences 02, 04, 09) | CC BY-NC-SA 3.0 | Annotated pedestrian boxes for detection, tracking and derived-event metrics. Non-commercial: evaluation only. |
| CAVIAR (INRIA lobby clips) | Research use; acknowledge "EC Funded CAVIAR project/IST 2001 37540" when publishing | Annotated indoor overhead footage. |
| OpenCV `vtest.avi` | OpenCV sample data | Unlabeled ordinary-activity footage. |
| MediaMTX | MIT | Local RTSP server for live-stream benchmarking (dev tool, not shipped). |
| ffmpeg | LGPL/GPL builds | Re-encoding benchmark footage and publishing test RTSP streams (dev tool, not shipped). |

### Detector models (verified 2026-10-13)

The weights' licence is checked separately from the code's, and the training data is recorded, because a permissive code licence does not license the weights.

| Model (registry name) | Code | Weights | Training data | Source of ONNX | Status |
| --- | --- | --- | --- | --- | --- |
| EfficientDet-Lite0/2 (MediaPipe) | Apache-2.0 | Apache-2.0 (MediaPipe model card) | COCO | MediaPipe model storage | Shippable (fallback) |
| YOLOX nano/tiny/s/m (`yolox_*`) | Apache-2.0 (Megvii-BaseDetection/YOLOX) | Apache-2.0, official 0.1.1rc0 release | COCO 2017 | Official release assets (`*.onnx`) | **Shippable, default** |
| D-FINE n/s/m (`dfine_*`) | Apache-2.0 (Peterande/D-FINE) | Apache-2.0 (ustc-community/dfine-*-coco model cards) | COCO only (`*_coco`; the `*_obj2coco` variants add Objects365 and are excluded) | onnx-community conversion | Shippable; CPU only (wrong output on DirectML) |
| RT-DETRv2-R18 (`rtdetrv2_r18`) | Apache-2.0 (lyuwenyu/RT-DETR) | Apache-2.0 (PekingU/rtdetr_v2_r18vd) | COCO 2017; ImageNet-pretrained backbone | onnx-community conversion | Shippable |
| RF-DETR base (`rfdetr_base`) | Apache-2.0 (roboflow/rf-detr) | Apache-2.0 | Objects365 pre-training (non-commercial dataset terms) and a DINOv2 backbone | onnx-community conversion | **Benchmark only until legal review** |
| RTMDet | Apache-2.0 in mmdetection, GPL-3.0 in mmyolo | Varies by repo | COCO | Requires export | Not evaluated (licence ambiguity) |
| Ultralytics YOLOv5/v8/v11, YOLOv10 | AGPL-3.0 | AGPL-3.0 | | | Excluded |
| YOLOv6, YOLOv7, YOLOv9 | GPL-3.0 | GPL-3.0 | | | Excluded |
| YOLO-NAS | Apache-2.0 code | Proprietary weights licence | | | Excluded |

COCO itself has CC BY 4.0 annotations, and its images remain under their Flickr licences. That is standard industry practice for pre-trained detectors, but it should be noted in a commercial licence review. Every registry entry in `src/sentinel/detectors_onnx.py` carries `license`, `training_data`, and `commercial_ok`, and the tests enforce that.

ONNX Runtime and `onnxruntime-directml` are MIT. `onnx` (Apache-2.0) is only used once, to relax YOLOX's declared input shape.

**Do not ship:** Ultralytics YOLOv5/v8/v11 (AGPL-3.0 unless an Ultralytics enterprise licence is purchased), models trained on non-commercial datasets (e.g. OmniFall CC BY-NC), and face or licence-plate recognition models, which need a separate product and privacy decision first (see `CLAUDE.md`).

Candidate future detectors with permissive licences: YOLOX (Apache-2.0), RT-DETR (Apache-2.0), RTMDet / MMDetection (Apache-2.0). Verify the licence of the **weights** and the training data, not only the code.

## Legacy CameraAI audit (senior night safety, frozen)

### Direct Reuse

| Source | License / terms | Use in this prototype |
| --- | --- | --- |
| Hugging Face OmniFall dataset | Dataset card lists CC BY-NC 4.0; annotations/splits may also carry non-commercial/share-alike terms. Original videos keep original owners' terms. | Use as a research/evaluation dataset and label taxonomy reference. Do not train or ship commercial models from it without legal review. |
| Home Assistant Core | Apache-2.0 | Use as the local automation/runtime surface. Do not reimplement entity, dashboard, or notification plumbing. |
| ESPHome | Mixed; Python portions generally MIT, firmware/runtime pieces include GPLv3 components. | Use as external firmware/integration tooling. Do not copy runtime code into this repo. |
| MediaPipe | Apache-2.0 | Optional pose-only extraction baseline. Store derived landmarks/features by default. |
| MQTT | Protocol / broker-specific licenses | Use MQTT discovery or plain topics to pass local sensor events into Home Assistant and this state machine. |

### Research / Inspiration Only

| Source | Observed license risk | Useful idea | Do not copy |
| --- | --- | --- | --- |
| `simplexsigil/omnifall-experiments` | No visible license found during audit. | Benchmark organization, split discipline, feature-transformer comparison. | Training/evaluation code until license is clarified. |
| P2MFDS bathroom fall detection repo | No visible license found during audit. Dataset not yet generally available per README. | Bathroom privacy strategy: mmWave + vibration, object-drop hard negatives, sensor fusion. | Model code, preprocessing scripts, figures, dataset assumptions. |
| `radar-lab/mmfall` | No visible license found during audit. | Semi-supervised mmWave anomaly idea: anomaly spike plus centroid-height drop. | Notebook/code, Colab paths, radar preprocessing implementation. |
| YOLO + MediaPipe fall repos | Many are student/demo repos with unclear licenses. Ultralytics YOLO is AGPL unless separately licensed. | Quick visual-baseline patterns and posture thresholds. | Demo code and AGPL YOLO runtime in a closed product. |

### Local Policy

- Keep unlicensed repos as citations and implementation references only.
- Build the senior-night state machine, event normalizer, alert review flow, and household baselines ourselves.
- Prefer Home Assistant/ESPHome entity configuration over custom hardware drivers.
- Prefer pose-only and sensor-only records over raw video.
- Treat this as an assistive monitor, not a medical or emergency-service automation system.
