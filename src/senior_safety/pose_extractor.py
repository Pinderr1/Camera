from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .pose_events import FrameFeatures, PoseEventEngine

CORE_LANDMARKS = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
FULL_BODY_LANDMARKS = (
    "left_shoulder",
    "right_shoulder",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
)
MOTION_LANDMARKS = ("nose", "left_shoulder", "right_shoulder", "left_hip", "right_hip", "left_ankle", "right_ankle")
VISIBILITY_MIN = 0.4
PRESENCE_MIN_CORE_LANDMARKS = 2
HEARTBEAT_INTERVAL_S = 30
DEFAULT_MODEL_PATH = "models/pose_landmarker_full.task"
DEFAULT_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_full/float16/latest/pose_landmarker_full.task"
)


LEGACY_EVENT_KEYS = {
    "motion_score_threshold": "motion_score_threshold_bl",
    "rapid_drop_min_vy": "rapid_drop_min_vy_bl",
    "fall_suppression_min_points": "fall_suppression_min_fraction",
}


def load_zones_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        config = json.load(handle)
    for legacy, replacement in LEGACY_EVENT_KEYS.items():
        if legacy in config.get("events", {}):
            print(f"Warning: zones config key events.{legacy} is no longer read; use events.{replacement}.")
    if "calibration" not in config:
        calibration_path = Path("config/calibration") / f"{config.get('camera_id', 'cam01')}.json"
        if calibration_path.exists():
            with calibration_path.open(encoding="utf-8") as handle:
                config["calibration"] = json.load(handle)
            print(f"Loaded camera calibration from {calibration_path}")
    return config


def extract_features(landmark_map: dict[str, tuple[float, float, float, float]], timestamp_ms: int) -> FrameFeatures:
    """Build FrameFeatures from a name -> (x, y, z, visibility) landmark map."""
    core = [landmark_map[name] for name in CORE_LANDMARKS if name in landmark_map]
    core_visible = [lm for lm in core if lm[3] >= VISIBILITY_MIN]
    if len(core_visible) < PRESENCE_MIN_CORE_LANDMARKS:
        return FrameFeatures(timestamp_ms=timestamp_ms, person_present=False)

    def midpoint(a: str, b: str) -> tuple[float, float] | None:
        if a in landmark_map and b in landmark_map:
            pa, pb = landmark_map[a], landmark_map[b]
            if pa[3] >= VISIBILITY_MIN and pb[3] >= VISIBILITY_MIN:
                return ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
        return None

    visible = [(lm[0], lm[1]) for lm in landmark_map.values() if lm[3] >= VISIBILITY_MIN]
    xs = [p[0] for p in visible]
    ys = [p[1] for p in visible]
    bbox = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)) if visible else None
    keypoints = {
        name: (landmark_map[name][0], landmark_map[name][1])
        for name in MOTION_LANDMARKS
        if name in landmark_map and landmark_map[name][3] >= VISIBILITY_MIN
    }
    confidence = sum(lm[3] for lm in core_visible) / len(core_visible)
    full_body_confidence = sum(_landmark_frame_confidence(landmark_map.get(name)) for name in FULL_BODY_LANDMARKS) / len(
        FULL_BODY_LANDMARKS
    )
    hip = midpoint("left_hip", "right_hip")
    shoulder_mid = midpoint("left_shoulder", "right_shoulder")
    torso_length = math.hypot(shoulder_mid[0] - hip[0], shoulder_mid[1] - hip[1]) if hip and shoulder_mid else None
    ankle_ys = [
        landmark_map[name][1]
        for name in ("left_ankle", "right_ankle")
        if name in landmark_map and landmark_map[name][3] >= VISIBILITY_MIN and 0.0 <= landmark_map[name][1] <= 1.0
    ]
    return FrameFeatures(
        timestamp_ms=timestamp_ms,
        person_present=True,
        hip=hip,
        shoulder_mid=shoulder_mid,
        bbox=bbox,
        keypoints=keypoints,
        confidence=confidence,
        full_body_confidence=full_body_confidence,
        torso_length=torso_length,
        ankle_y=max(ankle_ys) if ankle_ys else None,
    )


def _landmark_frame_confidence(landmark: tuple[float, float, float, float] | None) -> float:
    if landmark is None:
        return 0.0
    x, y, _, visibility = landmark
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return 0.0
    return visibility


class PoseJsonlWriter:
    def __init__(self, directory: Path, camera_id: str, pose_model: str):
        directory.mkdir(parents=True, exist_ok=True)
        started = datetime.now()
        self.clip_id = f"{camera_id}_{started:%Y%m%dT%H%M%S}_live"
        self.pose_model = pose_model
        self.path = directory / f"{self.clip_id}.jsonl"
        self.frame_index = 0
        self._handle = self.path.open("a", encoding="utf-8")

    def write(self, timestamp_ms: int, landmark_map: dict[str, tuple[float, float, float, float]], features: FrameFeatures) -> None:
        bbox = None
        if features.bbox:
            bbox = {"x": features.bbox[0], "y": features.bbox[1], "w": features.bbox[2], "h": features.bbox[3]}
        row = {
            "clip_id": self.clip_id,
            "frame_index": self.frame_index,
            "timestamp_ms": timestamp_ms,
            "person_id": 0,
            "pose_model": self.pose_model,
            "pose_confidence": round(features.confidence, 3),
            "full_body_pose_confidence": round(features.full_body_confidence or 0.0, 3),
            "landmarks": [
                {"name": name, "x": round(lm[0], 4), "y": round(lm[1], 4), "z": round(lm[2], 4), "visibility": round(lm[3], 3)}
                for name, lm in landmark_map.items()
            ],
            "bbox": bbox,
            "quality_flags": _quality_flags(features),
        }
        self._handle.write(json.dumps(row) + "\n")
        self._handle.flush()
        self.frame_index += 1

    def close(self) -> None:
        self._handle.close()


def _quality_flags(features: FrameFeatures) -> list[str]:
    flags = []
    if not features.person_present:
        flags.append("no_person_detected")
    if features.person_present and (features.full_body_confidence or 0.0) < VISIBILITY_MIN:
        flags.append("partial_body_pose")
    return flags


def ensure_model(pose_config: dict[str, Any]) -> Path:
    path = Path(pose_config.get("model_path", DEFAULT_MODEL_PATH))
    if not path.exists():
        url = pose_config.get("model_url", DEFAULT_MODEL_URL)
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading pose model to {path} ...")
        urllib.request.urlretrieve(url, path)
        print("Model downloaded.")
    return path


def create_landmarker(pose_config: dict[str, Any]):
    from mediapipe.tasks.python import vision
    from mediapipe.tasks.python.core.base_options import BaseOptions

    options = vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(ensure_model(pose_config))),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=int(pose_config.get("max_poses", 1)),
        min_pose_detection_confidence=float(pose_config.get("min_detection_confidence", 0.3)),
        min_pose_presence_confidence=float(pose_config.get("min_presence_confidence", 0.3)),
        min_tracking_confidence=float(pose_config.get("min_tracking_confidence", 0.5)),
    )
    landmark_names = [name.name.lower() for name in vision.PoseLandmark]
    return vision.PoseLandmarker.create_from_options(options), landmark_names


def select_pose(poses, landmark_names, last_hip: tuple[float, float] | None):
    """Pick the pose whose hip midpoint is nearest the previously tracked hip,
    so a second person in frame does not steal the track."""
    if len(poses) == 1 or last_hip is None:
        return poses[0]
    hip_indices = [landmark_names.index("left_hip"), landmark_names.index("right_hip")]

    def hip_distance(pose) -> float:
        hip_x = sum(pose[i].x for i in hip_indices) / 2
        hip_y = sum(pose[i].y for i in hip_indices) / 2
        return math.hypot(hip_x - last_hip[0], hip_y - last_hip[1])

    return min(poses, key=hip_distance)


def health_payload(config: dict[str, Any], event_name: str, timestamp_ms: int) -> dict[str, Any]:
    return {
        "sensor_id": f"pose_{config.get('camera_id', 'cam01')}",
        "sensor_type": "camera_pose",
        "room": config.get("room", ""),
        "zone_id": "",
        "event_name": event_name,
        "value": True,
        "timestamp_ms": timestamp_ms,
        "confidence": 1.0,
    }


def run_camera_loop(
    config: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
    show: bool = False,
    jsonl_writer: PoseJsonlWriter | None = None,
    on_key: Callable[[str], None] | None = None,
) -> None:
    """Capture frames, run pose landmarking, and emit derived sensor events.

    Runs until KeyboardInterrupt or the q key in the preview window.
    """
    import cv2
    import mediapipe as mp

    engine = PoseEventEngine(config)
    fps_limit = float(config.get("fps_limit", 10))
    landmarker, landmark_names = create_landmarker(config.get("pose", {}))
    source = config.get("source", 0)
    capture = cv2.VideoCapture(source)
    if not capture.isOpened() and isinstance(source, int):
        capture = cv2.VideoCapture(source, cv2.CAP_DSHOW)
    if not capture.isOpened():
        raise SystemExit(
            f"Could not open camera source {source!r}. "
            "Use a USB webcam index (0, 1, ...) or an RTSP/HTTP URL for an IP camera in the zones config."
        )

    print(f"Pose extractor running on source {source!r} as {config.get('camera_id', 'cam01')}. Ctrl+C to stop.")
    last_heartbeat = 0.0
    last_hip: tuple[float, float] | None = None
    detector_start = time.monotonic()
    frame_interval = 1.0 / fps_limit if fps_limit > 0 else 0.0
    try:
        while True:
            started = time.time()
            ok, frame_bgr = capture.read()
            if not ok:
                emit(health_payload(config, "sensor_offline", int(time.time() * 1000)))
                time.sleep(2)
                continue

            timestamp_ms = int(time.time() * 1000)
            detector_ms = int((time.monotonic() - detector_start) * 1000)
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), detector_ms)

            landmark_map: dict[str, tuple[float, float, float, float]] = {}
            if result.pose_landmarks:
                pose = select_pose(result.pose_landmarks, landmark_names, last_hip)
                for name, landmark in zip(landmark_names, pose):
                    landmark_map[name] = (landmark.x, landmark.y, landmark.z, landmark.visibility or 0.0)

            features = extract_features(landmark_map, timestamp_ms)
            if features.hip:
                last_hip = features.hip
            for payload in engine.update(features):
                emit(payload)

            if jsonl_writer:
                # No-person frames are written too so dropouts replay faithfully.
                jsonl_writer.write(timestamp_ms, landmark_map, features)

            if started - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                last_heartbeat = started
                emit(health_payload(config, "sensor_online", timestamp_ms))

            if show:
                height, width = frame_bgr.shape[:2]
                for zone in config.get("zones", []):
                    points = [(int(x * width), int(y * height)) for x, y in zone["polygon"]]
                    for i in range(len(points)):
                        cv2.line(frame_bgr, points[i], points[(i + 1) % len(points)], (0, 200, 255), 2)
                    cv2.putText(frame_bgr, zone["id"], points[0], cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
                if features.hip:
                    cv2.circle(frame_bgr, (int(features.hip[0] * width), int(features.hip[1] * height)), 6, (0, 0, 255), -1)
                cv2.imshow("senior-night pose", frame_bgr)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key != 255 and on_key:
                    on_key(chr(key))

            elapsed = time.time() - started
            if frame_interval > elapsed:
                time.sleep(frame_interval - elapsed)
    except KeyboardInterrupt:
        pass
    finally:
        capture.release()
        landmarker.close()
        if jsonl_writer:
            jsonl_writer.close()
        if show:
            cv2.destroyAllWindows()


def calibrate(config: dict[str, Any], seconds: float) -> Path:
    """Capture a standing reference so thresholds adapt to this camera's view.

    The person should stand fully visible on the monitored path, then walk it
    once, while this runs. Writes config/calibration/<camera_id>.json which
    load_zones_config picks up automatically."""
    import cv2
    import mediapipe as mp

    from .pose_events import torso_angle_deg

    landmarker, landmark_names = create_landmarker(config.get("pose", {}))
    source = config.get("source", 0)
    capture = cv2.VideoCapture(source)
    if not capture.isOpened() and isinstance(source, int):
        capture = cv2.VideoCapture(source, cv2.CAP_DSHOW)
    if not capture.isOpened():
        raise SystemExit(f"Could not open camera source {source!r} for calibration.")

    print(f"Calibrating for {seconds:.0f}s: stand fully visible, then walk the monitored path once.")
    torso_lengths: list[float] = []
    standing_heights: list[float] = []
    ankle_ys: list[float] = []
    detector_start = time.monotonic()
    try:
        while time.monotonic() - detector_start < seconds:
            ok, frame_bgr = capture.read()
            if not ok:
                continue
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            detector_ms = int((time.monotonic() - detector_start) * 1000)
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), detector_ms)
            landmark_map = {}
            if result.pose_landmarks:
                for name, landmark in zip(landmark_names, result.pose_landmarks[0]):
                    landmark_map[name] = (landmark.x, landmark.y, landmark.z, landmark.visibility or 0.0)
            features = extract_features(landmark_map, detector_ms)
            if not features.person_present or features.hip is None or features.shoulder_mid is None:
                continue
            if not (0.0 <= features.hip[1] <= 1.0):
                continue
            if torso_angle_deg(features.shoulder_mid, features.hip) >= 30:
                continue
            if features.torso_length:
                torso_lengths.append(features.torso_length)
            if features.bbox:
                standing_heights.append(features.bbox[3])
            if features.ankle_y is not None:
                ankle_ys.append(features.ankle_y)
    finally:
        capture.release()
        landmarker.close()

    if len(torso_lengths) < 20:
        raise SystemExit(
            f"Only {len(torso_lengths)} usable standing frames captured; stand fully visible and re-run calibration."
        )

    def percentile(values: list[float], fraction: float) -> float:
        ordered = sorted(values)
        return ordered[round(fraction * (len(ordered) - 1))]

    calibration = {
        "camera_id": config.get("camera_id", "cam01"),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "frames_used": len(torso_lengths),
        "torso_length_norm": round(percentile(torso_lengths, 0.5), 4),
        "standing_bbox_h": round(percentile(standing_heights, 0.9), 4) if standing_heights else None,
        "floor_line_y": round(min(1.0, percentile(ankle_ys, 0.95) - 0.02), 4) if ankle_ys else None,
    }
    calibration = {key: value for key, value in calibration.items() if value is not None}
    target = Path("config/calibration") / f"{calibration['camera_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(calibration, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Calibration written to {target}: {calibration}")
    return target


def make_jsonl_writer(config: dict[str, Any]) -> PoseJsonlWriter:
    camera_id = config.get("camera_id", "cam01")
    return PoseJsonlWriter(
        Path(config.get("pose_jsonl_dir", f"data/processed/pose/{camera_id}")),
        camera_id,
        "mediapipe_pose_landmarker_full",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Camera pose extractor: publishes derived sensor events over MQTT.")
    parser.add_argument("--zones", default="config/zones.example.json")
    parser.add_argument("--dry-run", action="store_true", help="Print events instead of publishing to MQTT.")
    parser.add_argument("--show", action="store_true", help="Preview window with zones and landmarks (q quits).")
    parser.add_argument("--no-jsonl", action="store_true", help="Skip writing pose JSONL records.")
    parser.add_argument(
        "--calibrate",
        type=float,
        metavar="SECONDS",
        help="Capture a standing reference for this camera instead of monitoring, then exit.",
    )
    args = parser.parse_args()

    config = load_zones_config(args.zones)

    if args.calibrate:
        calibrate(config, args.calibrate)
        return

    client = None
    topic = None
    if not args.dry_run:
        import paho.mqtt.client as mqtt

        mqtt_config = config.get("mqtt", {})
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        username = os.environ.get("MQTT_USERNAME")
        if username:
            client.username_pw_set(username, os.environ.get("MQTT_PASSWORD"))
        client.connect(mqtt_config.get("host", "localhost"), int(mqtt_config.get("port", 1883)))
        client.loop_start()
        topic = f"{mqtt_config.get('topic_prefix', 'senior-night')}/events"

    def emit(payload: dict[str, Any]) -> None:
        payload.setdefault("event_time_local", datetime.now().isoformat(timespec="seconds"))
        if client and topic:
            client.publish(topic, json.dumps(payload), qos=1)
        print(f"{payload['event_name']}={payload['value']} zone={payload.get('zone_id', '')}")

    writer = None if args.no_jsonl else make_jsonl_writer(config)
    try:
        run_camera_loop(config, emit, show=args.show, jsonl_writer=writer)
    finally:
        if client:
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    main()
