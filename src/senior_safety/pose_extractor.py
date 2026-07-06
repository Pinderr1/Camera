from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .pose_events import FrameFeatures, PoseEventEngine

CORE_LANDMARKS = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
MOTION_LANDMARKS = ("nose", "left_shoulder", "right_shoulder", "left_hip", "right_hip", "left_ankle", "right_ankle")
VISIBILITY_MIN = 0.5
HEARTBEAT_INTERVAL_S = 30


def load_zones_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def extract_features(landmark_map: dict[str, tuple[float, float, float, float]], timestamp_ms: int) -> FrameFeatures:
    """Build FrameFeatures from a name -> (x, y, z, visibility) landmark map."""
    core = [landmark_map[name] for name in CORE_LANDMARKS if name in landmark_map]
    core_visible = [lm for lm in core if lm[3] >= VISIBILITY_MIN]
    if len(core_visible) < 3:
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
    keypoints = [
        (landmark_map[name][0], landmark_map[name][1])
        for name in MOTION_LANDMARKS
        if name in landmark_map and landmark_map[name][3] >= VISIBILITY_MIN
    ]
    confidence = sum(lm[3] for lm in core_visible) / len(core_visible)
    return FrameFeatures(
        timestamp_ms=timestamp_ms,
        person_present=True,
        hip=midpoint("left_hip", "right_hip"),
        shoulder_mid=midpoint("left_shoulder", "right_shoulder"),
        bbox=bbox,
        keypoints=keypoints,
        confidence=confidence,
    )


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
            "landmarks": [
                {"name": name, "x": round(lm[0], 4), "y": round(lm[1], 4), "z": round(lm[2], 4), "visibility": round(lm[3], 3)}
                for name, lm in landmark_map.items()
            ],
            "bbox": bbox,
            "quality_flags": [] if features.person_present else ["no_person_detected"],
        }
        self._handle.write(json.dumps(row) + "\n")
        self._handle.flush()
        self.frame_index += 1

    def close(self) -> None:
        self._handle.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Camera pose extractor: publishes derived sensor events over MQTT.")
    parser.add_argument("--zones", default="config/zones.example.json")
    parser.add_argument("--dry-run", action="store_true", help="Print events instead of publishing to MQTT.")
    parser.add_argument("--show", action="store_true", help="Preview window with zones and landmarks (q quits).")
    parser.add_argument("--no-jsonl", action="store_true", help="Skip writing pose JSONL records.")
    args = parser.parse_args()

    import cv2
    import mediapipe as mp

    config = load_zones_config(args.zones)
    engine = PoseEventEngine(config)
    camera_id = config.get("camera_id", "cam01")
    pose_config = config.get("pose", {})
    fps_limit = float(config.get("fps_limit", 10))

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

    writer = None
    if not args.no_jsonl:
        writer = PoseJsonlWriter(Path(config.get("pose_jsonl_dir", f"data/processed/pose/{camera_id}")), camera_id, "mediapipe_pose")

    landmark_names = [name.name.lower() for name in mp.solutions.pose.PoseLandmark]
    pose = mp.solutions.pose.Pose(
        model_complexity=int(pose_config.get("model_complexity", 1)),
        min_detection_confidence=float(pose_config.get("min_detection_confidence", 0.5)),
        min_tracking_confidence=float(pose_config.get("min_tracking_confidence", 0.5)),
    )
    source = config.get("source", 0)
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        raise SystemExit(f"Could not open camera source {source!r}")

    print(f"Pose extractor running on source {source!r} as {camera_id}. Ctrl+C to stop.")
    last_heartbeat = 0.0
    frame_interval = 1.0 / fps_limit if fps_limit > 0 else 0.0
    try:
        while True:
            started = time.time()
            ok, frame_bgr = capture.read()
            if not ok:
                emit_offline = {
                    "sensor_id": f"pose_{camera_id}",
                    "sensor_type": "camera_pose",
                    "room": config.get("room", ""),
                    "zone_id": "",
                    "event_name": "sensor_offline",
                    "value": True,
                    "timestamp_ms": int(time.time() * 1000),
                    "confidence": 1.0,
                }
                emit(emit_offline)
                time.sleep(2)
                continue

            timestamp_ms = int(time.time() * 1000)
            results = pose.process(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
            landmark_map: dict[str, tuple[float, float, float, float]] = {}
            if results.pose_landmarks:
                for name, landmark in zip(landmark_names, results.pose_landmarks.landmark):
                    landmark_map[name] = (landmark.x, landmark.y, landmark.z, landmark.visibility)

            features = extract_features(landmark_map, timestamp_ms)
            for payload in engine.update(features):
                emit(payload)

            if writer and landmark_map:
                writer.write(timestamp_ms, landmark_map, features)

            if started - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                last_heartbeat = started
                emit(
                    {
                        "sensor_id": f"pose_{camera_id}",
                        "sensor_type": "camera_pose",
                        "room": config.get("room", ""),
                        "zone_id": "",
                        "event_name": "sensor_online",
                        "value": True,
                        "timestamp_ms": timestamp_ms,
                        "confidence": 1.0,
                    }
                )

            if args.show:
                height, width = frame_bgr.shape[:2]
                for zone in config.get("zones", []):
                    points = [(int(x * width), int(y * height)) for x, y in zone["polygon"]]
                    for i in range(len(points)):
                        cv2.line(frame_bgr, points[i], points[(i + 1) % len(points)], (0, 200, 255), 2)
                    cv2.putText(frame_bgr, zone["id"], points[0], cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
                if features.hip:
                    cv2.circle(frame_bgr, (int(features.hip[0] * width), int(features.hip[1] * height)), 6, (0, 0, 255), -1)
                cv2.imshow("senior-night pose", frame_bgr)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            elapsed = time.time() - started
            if frame_interval > elapsed:
                time.sleep(frame_interval - elapsed)
    except KeyboardInterrupt:
        pass
    finally:
        capture.release()
        pose.close()
        if writer:
            writer.close()
        if client:
            client.loop_stop()
            client.disconnect()
        if args.show:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
