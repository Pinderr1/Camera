"""ONNX Runtime object detectors (YOLOX and DETR-family exports).

Every registry entry records where the weights come from, their licence and
their training data, because the weights' licence is not implied by the code's.
Only entries with `commercial_ok: True` may be the shipped default; others are
kept for benchmarking and need legal review first.

Providers: "dml" (DirectML: any DirectX 12 GPU on Windows), "cuda", "openvino",
"cpu". "auto" tries GPU providers that are installed, then CPU."""

from __future__ import annotations

import logging
import threading
import urllib.request
from pathlib import Path
from typing import Any

import onnxruntime  # noqa: F401  (ImportError here triggers the EfficientDet fallback)

from .detectors import crop_region, detect_with_tiles, non_max_suppression
from .models import Detection
from .vision import Frame

log = logging.getLogger("sentinel.detectors")

COCO80 = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]
# COCO category ids (1..90 with gaps) -> the 80 names, for models that predict raw category ids.
COCO91_IDS = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 27, 28, 31, 32, 33, 34,
    35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63,
    64, 65, 67, 70, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 84, 85, 86, 87, 88, 89, 90,
]
COCO91 = {category_id: name for category_id, name in zip(COCO91_IDS, COCO80)}
# MegaDetector's three classes; "animal" and "vehicle" are Sentinel category names already.
MEGADETECTOR = ["animal", "person", "vehicle"]
_LABEL_SETS = {"coco80": COCO80, "megadetector": MEGADETECTOR}

_YOLOX = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/{}.onnx"
_HF = "https://huggingface.co/onnx-community/{}/resolve/main/onnx/model.onnx"
_YOLOX_META = {
    "family": "yolox",
    "license": "Apache-2.0 (code and weights), Megvii YOLOX 0.1.1rc0 release",
    "training_data": "COCO 2017 (annotations CC BY 4.0; images under Flickr licences)",
    "commercial_ok": True,
}
_DETR_META = {"family": "detr", "labels": "coco80", "normalize": False}

MODELS: dict[str, dict[str, Any]] = {
    "yolox_nano": _YOLOX_META | {"url": _YOLOX.format("yolox_nano"), "path": "models/onnx/yolox_nano.onnx", "input_size": [416, 416]},
    "yolox_tiny": _YOLOX_META | {"url": _YOLOX.format("yolox_tiny"), "path": "models/onnx/yolox_tiny.onnx", "input_size": [416, 416]},
    "yolox_s": _YOLOX_META | {"url": _YOLOX.format("yolox_s"), "path": "models/onnx/yolox_s.onnx", "input_size": [640, 640]},
    "yolox_m": _YOLOX_META | {"url": _YOLOX.format("yolox_m"), "path": "models/onnx/yolox_m.onnx", "input_size": [640, 640]},
    "dfine_n": _DETR_META | {
        "url": _HF.format("dfine_n_coco-ONNX"), "dml_ok": False, "path": "models/onnx/dfine_n_coco-ONNX/model.onnx", "input_size": [640, 640],
        "license": "Apache-2.0 (code: Peterande/D-FINE; weights: ustc-community/dfine-nano-coco); ONNX conversion by onnx-community",
        "training_data": "COCO 2017 only (the *_coco variants; *_obj2coco variants add Objects365 and are excluded)",
        "commercial_ok": True,
    },
    "dfine_s": _DETR_META | {
        "url": _HF.format("dfine_s_coco-ONNX"), "dml_ok": False, "path": "models/onnx/dfine_s_coco-ONNX/model.onnx", "input_size": [640, 640],
        "license": "Apache-2.0 (code: Peterande/D-FINE; weights: ustc-community/dfine-small-coco); ONNX conversion by onnx-community",
        "training_data": "COCO 2017 only", "commercial_ok": True,
    },
    "dfine_m": _DETR_META | {
        "url": _HF.format("dfine_m_coco-ONNX"), "dml_ok": False, "path": "models/onnx/dfine_m_coco-ONNX/model.onnx", "input_size": [640, 640],
        "license": "Apache-2.0 (code: Peterande/D-FINE; weights: ustc-community/dfine-medium-coco); ONNX conversion by onnx-community",
        "training_data": "COCO 2017 only", "commercial_ok": True,
    },
    "rtdetrv2_r18": _DETR_META | {
        "url": _HF.format("rtdetr_v2_r18vd-ONNX"), "path": "models/onnx/rtdetr_v2_r18vd-ONNX/model.onnx", "input_size": [640, 640],
        "license": "Apache-2.0 (code: lyuwenyu/RT-DETR; weights: PekingU/rtdetr_v2_r18vd); ONNX conversion by onnx-community",
        "training_data": "COCO 2017 (ImageNet-pretrained ResNet-18 backbone)", "commercial_ok": True,
    },
    # MegaDetector V6, Apache variant (camera-trap detector: animal / person / vehicle).
    # No public ONNX: exported once by scripts/guardian/export_megadetector_onnx.py.
    "mdv6_apa_rtdetr_c": {
        "family": "detr", "labels": "megadetector", "normalize": False, "keep_aspect": False, "resize": "area",
        "path": "models/megadetector/mdv6_apa_rtdetr_c/model.onnx", "input_size": [640, 640],
        "export": "scripts/guardian/export_megadetector_onnx.py",
        "license": "Apache-2.0 weights (Pytorch-Wildlife model zoo: MDV6-apa-rtdetr-c; Zenodo record 15398270, "
                   "MD5 3992888e26f627ee5a4ff9f116be97c3); code Apache-2.0 (lyuwenyu/RT-DETR) and MIT (Pytorch-Wildlife)",
        "training_data": "MegaDetector camera-trap training set (Microsoft AI for Good; includes LILA public datasets such as "
                         "Caltech, ENA24, NACTI); ImageNet-pretrained ResNet-18-vd backbone",
        "commercial_ok": True,
    },
    "rfdetr_base": {
        "family": "detr", "labels": "coco91", "normalize": True,
        "url": _HF.format("rfdetr_base-ONNX"), "path": "models/onnx/rfdetr_base-ONNX/model.onnx", "input_size": [560, 560],
        "license": "Apache-2.0 (code and weights: roboflow/rf-detr); ONNX conversion by onnx-community",
        "training_data": "Objects365 pre-training then COCO; DINOv2 backbone. Objects365 terms are non-commercial: legal review needed",
        "commercial_ok": False,
    },
}

_PROVIDERS = {"dml": "DmlExecutionProvider", "cuda": "CUDAExecutionProvider", "openvino": "OpenVINOExecutionProvider", "cpu": "CPUExecutionProvider"}
_SESSIONS: dict[tuple, Any] = {}
_SESSIONS_LOCK = threading.Lock()


def resolve_providers(requested: Any) -> list[str]:
    import onnxruntime as ort

    available = ort.get_available_providers()
    if requested in (None, "auto"):
        names = [_PROVIDERS[key] for key in ("cuda", "dml", "openvino") if _PROVIDERS[key] in available]
    else:
        names = [_PROVIDERS.get(item, item) for item in ([requested] if isinstance(requested, str) else requested)]
        missing = [name for name in names if name not in available]
        if missing:
            log.warning("execution provider(s) %s not installed; using CPU", missing)
            names = [name for name in names if name in available]
    return names + (["CPUExecutionProvider"] if "CPUExecutionProvider" not in names else [])


def gpu_available(requested: Any = "auto") -> bool:
    return resolve_providers(requested)[0] != "CPUExecutionProvider"


def _session(path: Path, providers: list[str], cpu_threads: int):
    """Sessions are shared between cameras running the same model: ONNX Runtime's
    run() is thread-safe, and one GPU copy of the weights is enough."""
    import onnxruntime as ort

    key = (str(path), tuple(providers), cpu_threads)
    with _SESSIONS_LOCK:
        if key not in _SESSIONS:
            options = ort.SessionOptions()
            if cpu_threads:
                options.intra_op_num_threads = cpu_threads
            if "DmlExecutionProvider" in providers:
                options.enable_mem_pattern = False  # required by DirectML
                options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            session = ort.InferenceSession(str(path), sess_options=options, providers=providers)
            # One GPU runs one inference at a time anyway; serialising DirectML calls
            # from several camera threads avoids relying on concurrent-run support.
            lock = threading.Lock() if session.get_providers()[0] == "DmlExecutionProvider" else None
            _SESSIONS[key] = _SharedSession(session, lock)
            log.info("loaded %s on %s", path.name, session.get_providers()[0])
        return _SESSIONS[key]


class _SharedSession:
    def __init__(self, session, lock: threading.Lock | None) -> None:
        self.session = session
        self.lock = lock

    def run(self, names, feed):
        if self.lock is None:
            return self.session.run(names, feed)
        with self.lock:
            return self.session.run(names, feed)

    def get_inputs(self):
        return self.session.get_inputs()

    def get_outputs(self):
        return self.session.get_outputs()

    def get_providers(self):
        return self.session.get_providers()


class OnnxDetector:
    def __init__(self, settings: dict[str, Any], score_threshold: float) -> None:
        name = settings.get("model", "yolox_s")
        if name not in MODELS and "model_path" not in settings:
            raise ValueError(f"unknown ONNX model {name!r}; known: {', '.join(MODELS)}")
        spec = MODELS.get(name, {}) | {key: settings[key] for key in ("family", "labels", "normalize", "keep_aspect", "resize") if key in settings}
        self.name = name
        self.family = spec["family"]
        path = Path(settings.get("model_path", spec.get("path", "")))
        if not path.exists():
            if not settings.get("model_url", spec.get("url")):
                raise FileNotFoundError(f"{path} missing: export it with {spec.get('export', 'the model export script')}")
            path.parent.mkdir(parents=True, exist_ok=True)
            log.info("downloading %s to %s", name, path)
            urllib.request.urlretrieve(settings.get("model_url", spec["url"]), path)
        self.providers = resolve_providers(settings.get("providers", "auto"))
        if spec.get("dml_ok", True) is False and "DmlExecutionProvider" in self.providers:
            # Measured: this export returns corrupted scores/boxes under DirectML (ORT 1.24); CPU output is correct.
            log.warning("%s is not reliable on DirectML; running on CPU", name)
            self.providers = [provider for provider in self.providers if provider != "DmlExecutionProvider"]
        self.session = _session(path, self.providers, int(settings.get("cpu_threads", 0)))
        self.input_name = self.session.get_inputs()[0].name
        fixed = self.session.get_inputs()[0].shape
        static = all(isinstance(dim, int) for dim in fixed[2:])
        size = settings.get("input_size") or spec.get("input_size", [640, 640])
        if static and list(size) != [fixed[3], fixed[2]]:
            if self.family == "yolox" and size[0] % 32 == 0 and size[1] % 32 == 0:
                # YOLOX is fully convolutional and its export has no baked-in sizes, so
                # only the declared input shape needs relaxing (weights untouched).
                self.session = _session(_dynamic_copy(path), self.providers, int(settings.get("cpu_threads", 0)))
            else:
                log.warning("%s has a fixed %sx%s input; ignoring input_size %s", name, fixed[3], fixed[2], size)
                size = [fixed[3], fixed[2]]
        self.input_w, self.input_h = int(size[0]), int(size[1])
        self.keep_aspect = bool(spec.get("keep_aspect", self.family == "yolox"))
        self.normalize = bool(spec.get("normalize", False))
        self.labels = _LABEL_SETS.get(spec.get("labels", "coco80"))  # None: raw COCO category ids
        # "area" averages pixels when shrinking, as the PIL resize MegaDetector was trained with does;
        # plain bilinear drops detail on 2048 px camera-trap frames shrunk to 640.
        self.interpolation = spec.get("resize", "linear")
        self.score_threshold = score_threshold
        self.nms_iou = float(settings.get("nms_iou", 0.45 if self.family == "yolox" else 0.7))
        self.allowed = set(settings.get("classes", ["person", "car", "truck", "bus", "motorcycle", "bicycle", "dog", "cat"]))
        self.tiles = tuple(settings.get("tiles") or (1, 1))
        self.tile_overlap = float(settings.get("tile_overlap", 0.15))
        if self.family == "yolox":
            self._grids = _yolox_grids(self.input_w, self.input_h)

    def describe(self) -> dict[str, Any]:
        spec = MODELS.get(self.name, {})
        return {
            "model": self.name,
            "provider": self.session.get_providers()[0],
            "input": [self.input_w, self.input_h],
            "license": spec.get("license", "custom model: verify licence"),
            "training_data": spec.get("training_data", "unknown"),
            "commercial_ok": spec.get("commercial_ok", False),
        }

    def detect(self, frame: Frame) -> list[Detection]:
        return detect_with_tiles(frame.image, self._detect_region, self.tiles, self.tile_overlap)

    def close(self) -> None:
        pass  # sessions are shared and live for the process

    def _detect_region(self, bgr, region: tuple[float, float, float, float]) -> list[Detection]:
        height, width = bgr.shape[:2]
        crop, x0, y0 = crop_region(bgr, region)
        if self.family == "yolox":
            raw = self._run_yolox(crop)
        else:
            raw = self._run_detr(crop)
        ch, cw = crop.shape[:2]
        detections = []
        for label, score, (bx, by, bw, bh) in raw:
            box = ((x0 + bx * cw) / width, (y0 + by * ch) / height, bw * cw / width, bh * ch / height)
            detections.append(Detection(label, float(score), tuple(float(value) for value in box)))  # plain floats: JSON/SQLite safe
        return non_max_suppression(detections, self.nms_iou)

    # ---- YOLOX: letterbox, BGR 0-255, grid decode -------------------------

    def _run_yolox(self, bgr) -> list[tuple[str, float, tuple[float, float, float, float]]]:
        import cv2
        import numpy as np

        h, w = bgr.shape[:2]
        ratio = min(self.input_w / w, self.input_h / h)
        resized = cv2.resize(bgr, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.input_h, self.input_w, 3), 114, dtype=np.uint8)
        canvas[: resized.shape[0], : resized.shape[1]] = resized
        blob = canvas.transpose(2, 0, 1)[None].astype(np.float32)
        out = self.session.run(None, {self.input_name: blob})[0][0]
        grids, strides = self._grids
        xy = (out[:, :2] + grids) * strides
        wh = np.exp(out[:, 2:4]) * strides
        scores = out[:, 4:5] * out[:, 5:]
        classes = scores.argmax(1)
        best = scores[np.arange(len(scores)), classes]
        keep = best >= self.score_threshold
        results = []
        for (cx, cy), (bw, bh), cls, score in zip(xy[keep], wh[keep], classes[keep], best[keep]):
            name = COCO80[int(cls)]
            if name not in self.allowed:
                continue
            x, y = (cx - bw / 2) / ratio, (cy - bh / 2) / ratio
            results.append((name, float(score), (x / w, y / h, bw / ratio / w, bh / ratio / h)))
        return results

    # ---- DETR family (RT-DETR, D-FINE, RF-DETR): resize, RGB 0-1 -----------

    def _run_detr(self, bgr) -> list[tuple[str, float, tuple[float, float, float, float]]]:
        import cv2
        import numpy as np

        h, w = bgr.shape[:2]
        if self.keep_aspect:  # dynamic-shape models: keep the frame's aspect, longest side = input_w
            scale = self.input_w / max(w, h)
            target = (max(32, int(round(w * scale / 32)) * 32), max(32, int(round(h * scale / 32)) * 32))
        else:
            target = (self.input_w, self.input_h)
        shrink = self.interpolation == "area" and (target[0] < w or target[1] < h)
        resized = cv2.resize(bgr, target, interpolation=cv2.INTER_AREA if shrink else cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        if self.normalize:
            rgb = (rgb - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        outputs = dict(zip([o.name for o in self.session.get_outputs()], self.session.run(None, {self.input_name: rgb.transpose(2, 0, 1)[None]})))
        probs = 1 / (1 + np.exp(-outputs["logits"][0]))
        boxes = outputs["pred_boxes"][0]
        classes = probs.argmax(1)
        best = probs[np.arange(len(probs)), classes]
        results = []
        for (cx, cy, bw, bh), cls, score in zip(boxes, classes, best):
            if score < self.score_threshold:
                continue
            name = self.labels[int(cls)] if self.labels else COCO91.get(int(cls))
            if name is None or name not in self.allowed:
                continue
            results.append((name, float(score), (float(cx - bw / 2), float(cy - bh / 2), float(bw), float(bh))))
        return results


def _dynamic_copy(path: Path) -> Path:
    """Copy of a static-shape ONNX model with symbolic height/width (needs the
    `onnx` package once; the result is cached next to the original)."""
    target = path.with_name(f"{path.stem}.dynamic.onnx")
    if not target.exists():
        import onnx

        model = onnx.load(str(path))
        for dim, name in zip(model.graph.input[0].type.tensor_type.shape.dim[2:], ("height", "width")):
            dim.dim_param = name
        for output in model.graph.output:
            output.type.tensor_type.shape.dim[1].dim_param = "anchors"
        del model.graph.value_info[:]
        onnx.checker.check_model(model)
        onnx.save(model, str(target))
        log.info("wrote resolution-flexible copy %s", target)
    return target


def _yolox_grids(width: int, height: int):
    import numpy as np

    grids, strides = [], []
    for stride in (8, 16, 32):
        gw, gh = width // stride, height // stride
        xv, yv = np.meshgrid(np.arange(gw), np.arange(gh))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        strides.append(np.full((gw * gh, 1), stride))
    return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)
