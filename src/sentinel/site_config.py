"""Editable site configuration behind the dashboard's setup screens.

The service starts from a config file; the first edit writes a managed copy to
`<storage>/site.json`, which then takes precedence (the original file is never
modified). Every change is validated as a whole, saved atomically with a
rotating backup, and handed to listeners (the running service) to apply.

Credentials never enter the config: passwords go to the credential store and
the camera source keeps only a reference. Unsaved connection tests keep their
full source in a short-lived server-side draft, so the browser never needs, or
receives, a stream URL."""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .config import CATEGORIES, ZONE_TYPES, validate_config
from .credentials import SecretStore
from .models import CATEGORY_LABELS, DEFAULT_CATEGORIES
from .streams import BRAND_TEMPLATES, STREAM_TYPES, public_source

DRAFT_TTL_S = 900
BACKUPS = 10
SOURCE_FIELDS = {
    "type", "host", "port", "template", "channel", "username", "path", "sub_path", "url", "sub_url",
    "prefer_substream", "hw_decode", "transport", "index", "backend", "resolution", "mode", "loop", "scenario", "faults", "noise",
}
WRITE_ONLY_FIELDS = {"path", "sub_path", "url", "sub_url"}  # stream addresses: accepted, never returned
CAMERA_FIELDS = {"name", "enabled", "fps", "zones", "privacy_masks", "map", "categories", "detector", "conditions", "recorder"}
DEVICE_FIELDS = {"name", "profile", "simulated", "enabled", "map", "zone_id", "host", "http_port", "rtsp_port", "channels", "model",
                 "adapter", "connection", "indoor", "drone_id", "drone_model", "drone_options", "video_url"}
NTFY_TOPIC_CREDENTIAL = "alerts.ntfy_topic"
MAX_MAP_BYTES = 10 * 1024 * 1024


class SiteConfigError(ValueError):
    pass


class SiteConfigManager:
    def __init__(self, config: dict[str, Any], storage_dir: str | Path, secrets: SecretStore) -> None:
        self.storage_dir = Path(storage_dir)
        self.managed_path = self.storage_dir / "site.json"
        self.assets_dir = self.storage_dir / "assets"
        self.secrets = secrets
        self._config = {key: value for key, value in copy.deepcopy(config).items() if not key.startswith("_")}
        self._private = {key: value for key, value in config.items() if key.startswith("_")}
        self._drafts: dict[str, tuple[float, dict[str, Any], str]] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.RLock()

    @staticmethod
    def load_effective(config: dict[str, Any], storage_dir: str | Path) -> dict[str, Any]:
        """The managed copy wins over the file the service was started with. A
        damaged managed copy is moved to `recovery/` and the newest valid copy
        from `config_history/` is used instead (or the start config); the
        private `_config_recovery` key tells the service what happened."""
        storage_dir = Path(storage_dir)
        managed = storage_dir / "site.json"
        private = {key: value for key, value in config.items() if key.startswith("_")}
        if not managed.exists():
            return config
        try:
            loaded = json.loads(managed.read_text(encoding="utf-8"))
            validate_config(loaded)
            return loaded | private
        except (OSError, ValueError) as error:  # JSONDecodeError and ConfigValidationError are ValueErrors
            problem = str(error).splitlines()[0][:200]
        recovery = storage_dir / "recovery"
        recovery.mkdir(parents=True, exist_ok=True)
        quarantined = recovery / f"site.{int(time.time() * 1000)}.damaged.json"
        managed.replace(quarantined)
        for candidate in sorted((storage_dir / "config_history").glob("site.*.json"), reverse=True):
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
                validate_config(loaded)
            except (OSError, ValueError):
                continue
            temporary = managed.with_suffix(".tmp")
            temporary.write_text(json.dumps(loaded, indent=1), encoding="utf-8")
            temporary.replace(managed)
            return loaded | private | {"_config_recovery": {"problem": problem, "restored_from": candidate.name, "quarantined": quarantined.name}}
        return config | {"_config_recovery": {"problem": problem, "restored_from": "", "quarantined": quarantined.name}}

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    @property
    def config(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._config) | self._private

    # ---- read ----------------------------------------------------------------

    def public_view(self) -> dict[str, Any]:
        with self._lock:
            view = copy.deepcopy(self._config)
        for camera in view.get("cameras", []):
            camera["source"] = public_source(camera.get("source", {}))
        view.pop("storage", None)
        for holder in view.get("devices", []) + [view.get("drone") or {}]:
            _hide_video_address(holder)
        for channel in view.get("alerts", {}).get("channels", []):
            # An ntfy topic works like a password on public servers: report only whether one is set.
            channel["topic_set"] = bool(channel.pop("topic", None) or channel.pop("topic_credential", None))
        site_map = view.get("site", {}).get("map", {})
        if site_map.get("image"):
            site_map["image_url"] = "/api/v1/config/map/image"
        return view | {
            "catalog": {
                "zone_types": sorted(ZONE_TYPES),
                "categories": list(CATEGORIES),
                "category_defaults": DEFAULT_CATEGORIES,
                "category_labels": {name: list(labels) for name, labels in CATEGORY_LABELS.items()},
                "source_types": sorted(STREAM_TYPES),
                "templates": {key: value["label"] for key, value in BRAND_TEMPLATES.items()},
            },
            "credential_backend": self.secrets.backend,
        }

    def config_source(self, camera_id: str) -> dict[str, Any]:
        """Stored (unresolved) source of a camera, for server-side tests only."""
        with self._lock:
            camera = next((c for c in self._config.get("cameras", []) if c["id"] == camera_id), None)
            if camera is None:
                raise KeyError(camera_id)
            return copy.deepcopy(camera["source"])

    # ---- drafts (tested-but-unsaved sources) --------------------------------

    def make_draft(self, source: dict[str, Any], password: str | None = None) -> tuple[str, dict[str, Any], str | None]:
        """Returns (draft id, source with any URL password lifted out, password)."""
        source, lifted = _lift_url_password(_clean_source(source))
        password = password if password is not None else lifted
        draft_id = uuid.uuid4().hex
        with self._lock:
            self._expire_drafts()
            self._drafts[draft_id] = (time.time(), source, password or "")
        return draft_id, source, password

    def draft(self, draft_id: str) -> tuple[dict[str, Any], str]:
        with self._lock:
            self._expire_drafts()
            if draft_id not in self._drafts:
                raise SiteConfigError("connection test expired; test the camera again")
            _, source, password = self._drafts[draft_id]
            return copy.deepcopy(source), password

    def _expire_drafts(self) -> None:
        cutoff = time.time() - DRAFT_TTL_S
        for key in [key for key, (created, _, _) in self._drafts.items() if created < cutoff]:
            del self._drafts[key]

    def lookup_for(self, extra: dict[str, str] | None = None) -> Callable[[str], str | None]:
        """Credential lookup that also sees passwords not yet saved (drafts under test)."""
        return lambda name: (extra or {}).get(name) or self.secrets.get(name)

    # ---- cameras ---------------------------------------------------------------

    def save_camera(self, camera_id: str | None, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with self._lock:
            config = copy.deepcopy(self._config)
            cameras = config.setdefault("cameras", [])
            existing = next((camera for camera in cameras if camera["id"] == camera_id), None) if camera_id else None
            if camera_id and existing is None:
                raise KeyError(camera_id)
            camera = existing or {"id": _new_id(payload.get("name") or "camera", {c["id"] for c in cameras}), "zones": []}
            for key in CAMERA_FIELDS & payload.keys():
                camera[key] = payload[key]
            camera.setdefault("name", camera["id"])
            camera.setdefault("fps", 5)
            password = None
            if payload.get("draft_id"):
                source, password = self.draft(payload["draft_id"])
                previous = (existing or {}).get("source", {})
                if not password and previous.get("credential") and source.get("username") == previous.get("username"):
                    source["credential"] = previous["credential"]  # re-tested without retyping the password
                camera["source"] = source
            elif "source" in payload:
                incoming, lifted = _lift_url_password(_clean_source(payload["source"]))
                previous = (existing or {}).get("source", {})
                if previous.get("type") == incoming.get("type"):
                    # Editing keeps write-only addresses and the stored credential unless replaced.
                    incoming = {key: previous[key] for key in WRITE_ONLY_FIELDS | {"credential"} if key in previous} | incoming
                camera["source"] = incoming
                password = payload["source"].get("password") or lifted
            if "source" not in camera:
                raise SiteConfigError("a new camera needs a source (run a connection test first)")
            if password:
                name = f"camera.{camera['id']}"
                camera["source"]["credential"] = name
                pending = (name, password)
            else:
                pending = None
            if existing is None:
                cameras.append(camera)
            self._commit(config, actor, f"camera {camera['id']} saved", before_save=lambda: pending and self.secrets.set(*pending))
            return {"id": camera["id"]}

    def delete_camera(self, camera_id: str, actor: str) -> dict[str, Any]:
        with self._lock:
            config = copy.deepcopy(self._config)
            camera = next((c for c in config.get("cameras", []) if c["id"] == camera_id), None)
            if camera is None:
                raise KeyError(camera_id)
            config["cameras"].remove(camera)  # a site may have no cameras (drone- or sensor-first installations)
            notes = _forget_zones(config, {zone["id"] for zone in camera.get("zones", [])})
            links = config.get("topology", {}).get("links", [])
            config.setdefault("topology", {})["links"] = [l for l in links if camera_id not in (l["from"], l["to"])]
            credential = camera.get("source", {}).get("credential")
            self._commit(config, actor, f"camera {camera_id} deleted", after_save=lambda: credential and self.secrets.delete(credential))
            return {"deleted": camera_id, "notes": notes}

    def reorder(self, order: list[str], actor: str) -> None:
        with self._lock:
            config = copy.deepcopy(self._config)
            by_id = {camera["id"]: camera for camera in config["cameras"]}
            if sorted(order) != sorted(by_id):
                raise SiteConfigError("order must list every camera exactly once")
            config["cameras"] = [by_id[camera_id] for camera_id in order]
            self._commit(config, actor, "camera order changed")

    def set_zones(self, camera_id: str, zones: list[dict[str, Any]], privacy_masks: list, actor: str) -> dict[str, Any]:
        with self._lock:
            config = copy.deepcopy(self._config)
            camera = next((c for c in config["cameras"] if c["id"] == camera_id), None)
            if camera is None:
                raise KeyError(camera_id)
            other_ids = {z["id"] for c in config["cameras"] if c is not camera for z in c.get("zones", [])}
            previous = {zone["id"]: zone for zone in camera.get("zones", [])}
            cleaned = []
            for zone in zones:
                zone_id = zone.get("id") or _new_id(f"{camera_id}_{zone.get('name') or zone.get('type', 'zone')}", other_ids | {z.get("id") for z in cleaned})
                if zone_id in other_ids:
                    raise SiteConfigError(f"zone id {zone_id!r} is used by another camera")
                entry = {
                    "id": zone_id,
                    "name": str(zone.get("name") or zone_id)[:60],
                    "type": zone.get("type", "general"),
                    "polygon": [[round(float(x), 4), round(float(y), 4)] for x, y in zone.get("polygon", [])],
                }
                kept_map = zone.get("map_polygon", previous.get(zone_id, {}).get("map_polygon"))
                if kept_map:  # property-map footprint is separate from frame geometry and kept unless replaced
                    entry["map_polygon"] = kept_map
                cleaned.append(entry)
            notes = _forget_zones(config, set(previous) - {zone["id"] for zone in cleaned})
            camera["zones"] = cleaned
            camera["privacy_masks"] = [[[round(float(x), 4), round(float(y), 4)] for x, y in mask] for mask in privacy_masks]
            self._commit(config, actor, f"zones of {camera_id} saved")
            return {"zones": [zone["id"] for zone in cleaned], "notes": notes}

    # ---- property map, topology, categories ---------------------------------

    def set_map(self, payload: dict[str, Any], actor: str) -> None:
        with self._lock:
            config = copy.deepcopy(self._config)
            site_map = config.setdefault("site", {}).setdefault("map", {})
            for key in ("width_m", "height_m"):
                if key in payload:
                    value = float(payload[key])
                    if not 1 <= value <= 5000:
                        raise SiteConfigError(f"{key} must be between 1 and 5000 metres")
                    site_map[key] = value
            if "origin_geo" in payload:
                site_map["origin_geo"] = [float(v) for v in payload["origin_geo"]]
            for placement in payload.get("cameras", []):
                camera = next((c for c in config["cameras"] if c["id"] == placement["id"]), None)
                if camera is None:
                    raise KeyError(placement["id"])
                camera["map"] = {key: round(float(placement[key]), 2) for key in ("x", "y", "heading_deg", "fov_deg") if key in placement}
            for footprint in payload.get("zones", []):
                zone = next((z for c in config["cameras"] for z in c.get("zones", []) if z["id"] == footprint["id"]), None)
                if zone is None:
                    raise KeyError(footprint["id"])
                if footprint.get("map_polygon"):
                    zone["map_polygon"] = [[round(float(x), 2), round(float(y), 2)] for x, y in footprint["map_polygon"]]
                else:
                    zone.pop("map_polygon", None)
            self._commit(config, actor, "property map saved")

    def set_map_image(self, data: bytes, actor: str) -> None:
        if len(data) > MAX_MAP_BYTES:
            raise SiteConfigError("map image must be under 10 MB")
        extension = "png" if data.startswith(b"\x89PNG") else "jpg" if data.startswith(b"\xff\xd8") else None
        if extension is None:
            raise SiteConfigError("map image must be a PNG or JPEG")
        with self._lock:
            self.assets_dir.mkdir(parents=True, exist_ok=True)
            for old in self.assets_dir.glob("site_map.*"):
                old.unlink()
            (self.assets_dir / f"site_map.{extension}").write_bytes(data)
            config = copy.deepcopy(self._config)
            config.setdefault("site", {}).setdefault("map", {})["image"] = f"site_map.{extension}"
            self._commit(config, actor, "property map image uploaded")

    def map_image_path(self) -> Path | None:
        name = self._config.get("site", {}).get("map", {}).get("image")
        path = (self.assets_dir / name) if name else None
        return path if path and path.is_file() else None

    def set_topology(self, links: list[dict[str, Any]], actor: str) -> None:
        with self._lock:
            config = copy.deepcopy(self._config)
            cleaned = []
            for link in links:
                entry = {
                    "from": link["from"], "to": link["to"],
                    "min_transit_s": float(link.get("min_transit_s", 0)), "max_transit_s": float(link["max_transit_s"]),
                    "bidirectional": bool(link.get("bidirectional", True)), "overlap": bool(link.get("overlap", False)),
                }
                for key in ("from_zones", "to_zones"):
                    if link.get(key):
                        entry[key] = list(link[key])
                if entry["from"] == entry["to"]:
                    raise SiteConfigError("a travel connection needs two different cameras")
                cleaned.append(entry)
            config.setdefault("topology", {})["links"] = cleaned
            self._commit(config, actor, "camera connections saved")

    def set_categories(self, categories: dict[str, Any], actor: str, camera_id: str | None = None) -> None:
        with self._lock:
            config = copy.deepcopy(self._config)
            target = config if camera_id is None else next((c for c in config["cameras"] if c["id"] == camera_id), None)
            if target is None:
                raise KeyError(camera_id)
            target["categories"] = {name: {key: bool(value) for key, value in settings.items()} for name, settings in categories.items()}
            self._commit(config, actor, "category settings saved")

    # ---- devices (recorders, drones, sensors, docks) ----------------------------

    def save_device(self, device_id: str | None, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """Register (device_id None) or edit a non-camera device. Registration
        records what is installed; it never claims the device is paired."""
        from .devices import PROFILE_INDEX, REGISTRABLE, default_profile

        with self._lock:
            config = copy.deepcopy(self._config)
            devices = config.setdefault("devices", [])
            existing = next((d for d in devices if d["id"] == device_id), None) if device_id else None
            if device_id and existing is None:
                raise KeyError(device_id)
            category = existing["category"] if existing else payload.get("category")
            if category not in REGISTRABLE:
                raise SiteConfigError(f"category must be one of {list(REGISTRABLE)} (cameras are added with the camera wizard)")
            taken = {d["id"] for d in devices} | {c["id"] for c in config.get("cameras", [])}
            device = existing or {"id": _new_id(payload.get("name") or category, taken), "category": category}
            for key in DEVICE_FIELDS & payload.keys():
                value = payload[key]
                if value in (None, ""):
                    device.pop("video" if key == "video_url" else key, None)
                elif key in ("name", "model"):
                    device[key] = str(value).strip()[:60]
                elif key == "map":
                    device[key] = {axis: round(float(value[axis]), 2) for axis in ("x", "y")}
                elif key in ("http_port", "rtsp_port", "channels"):
                    device[key] = int(value)
                elif key == "video_url":
                    # Write-only like camera stream addresses: stored as the drone's video source, never returned.
                    device["video"] = (device.get("video") or {}) | {"source": {"type": "rtsp", "url": str(value).strip()}}
                elif key == "drone_options":
                    device[key] = {str(name): bool(flag) for name, flag in dict(value).items()}
                else:
                    device[key] = value
            if "profile" not in device:
                device["profile"] = default_profile(category, bool(device.get("simulated")))
            profile = PROFILE_INDEX.get(device["profile"], {})
            device.setdefault("name", profile.get("name", category)[:60])
            if category == "drone" and profile.get("adapter"):
                device["adapter"] = profile["adapter"]
            if category == "drone" and "map" not in device:
                # A home on the fence edge (the map corner) fails readiness; start inside the property until
                # the installer sets the real launch point.
                site_map = config.get("site", {}).get("map", {})
                home = config.get("drone", {}).get("home_map") or [float(site_map.get("width_m", 40)) / 2, float(site_map.get("height_m", 30)) / 2]
                device["map"] = {"x": round(float(home[0]), 2), "y": round(float(home[1]), 2)}
            if existing is None:
                devices.append(device)
            self._commit(config, actor, f"device {device['id']} {'saved' if existing else 'registered'}")
            return _hide_video_address(copy.deepcopy(device))

    def delete_device(self, device_id: str, actor: str) -> dict[str, Any]:
        with self._lock:
            config = copy.deepcopy(self._config)
            devices = config.get("devices", [])
            device = next((d for d in devices if d["id"] == device_id), None)
            if device is None:
                raise KeyError(device_id)
            devices.remove(device)
            notes = []
            for camera in config.get("cameras", []):
                if camera.get("recorder") == device_id:
                    camera.pop("recorder")
                    notes.append(f"camera {camera['id']} kept; it is no longer listed under the removed recorder")
            for other in devices:
                if other.get("drone_id") == device_id:
                    other.pop("drone_id")
            drone = config.get("drone", {})
            if drone.get("device") == device_id:
                drone.pop("device")
            from .devices import drone_devices

            if device["category"] == "drone" and drone.get("enabled") and not drone_devices(config):
                drone["enabled"] = False  # never fall back to a drone the owner did not register
                notes.append("drone features turned off: no drone is registered")
            self._commit(config, actor, f"device {device_id} removed")
            return {"deleted": device_id, "notes": notes}

    def set_site_name(self, name: str, actor: str) -> None:
        name = str(name).strip()[:80]
        if not name:
            raise SiteConfigError("the property needs a name")
        with self._lock:
            config = copy.deepcopy(self._config)
            config.setdefault("site", {})["name"] = name
            self._commit(config, actor, "property name saved")

    def set_drone(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """Drone setup from the installer: primary aircraft, home position,
        geofence (default: the property rectangle) and inspection points.
        Flight commands for real aircraft stay disabled regardless."""
        with self._lock:
            config = copy.deepcopy(self._config)
            drone = config.setdefault("drone", {})
            site_map = config.get("site", {}).get("map", {})
            if "enabled" in payload:
                drone["enabled"] = bool(payload["enabled"])
            if payload.get("device"):
                drone["device"] = str(payload["device"])
            if payload.get("home_map"):
                drone["home_map"] = [round(float(v), 2) for v in payload["home_map"]]
            fence = payload.get("geofence")
            if fence or (drone.get("enabled") and not drone.get("geofence")):
                fence = fence or {"property": True}
                width, height = float(site_map.get("width_m", 40)), float(site_map.get("height_m", 30))
                polygon = [[0, 0], [width, 0], [width, height], [0, height]] if fence.get("property") else fence.get("map_polygon")
                previous = drone.get("geofence", {})
                drone["geofence"] = {
                    "map_polygon": [[round(float(x), 2), round(float(y), 2)] for x, y in polygon or []],
                    "min_altitude_m": float(fence.get("min_altitude_m", previous.get("min_altitude_m", 5))),
                    "max_altitude_m": float(fence.get("max_altitude_m", previous.get("max_altitude_m", 25))),
                }
            if "inspection_points" in payload:
                points, taken = [], set()
                for point in payload["inspection_points"]:
                    point_id = point.get("id") or _new_id(point.get("name") or "point", taken)
                    taken.add(point_id)
                    points.append({"id": point_id, "name": str(point.get("name") or point_id)[:60],
                                   "map": [round(float(point["map"][0]), 2), round(float(point["map"][1]), 2)]})
                drone["inspection_points"] = points
            self._commit(config, actor, "drone settings saved")
            return copy.deepcopy(drone)

    def set_notifications(self, payload: dict[str, Any], actor: str) -> None:
        """Push channels from the installer. A typed ntfy topic goes to the
        credential store; the config keeps only a reference to it."""
        with self._lock:
            config = copy.deepcopy(self._config)
            channels = config.setdefault("alerts", {}).setdefault("channels", [])
            pending = None
            if "ntfy" in payload:
                body = payload["ntfy"] or {}
                channel = next((c for c in channels if c.get("type") == "ntfy"), None)
                if channel is None:
                    channel = {"type": "ntfy", "server": "https://ntfy.sh"}
                    channels.append(channel)
                channel["enabled"] = bool(body.get("enabled", True))
                if body.get("server"):
                    channel["server"] = str(body["server"]).strip()
                if "attach_snapshot" in body:
                    channel["attach_snapshot"] = bool(body["attach_snapshot"])
                topic = str(body.get("topic") or "").strip()
                if topic:
                    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", topic):
                        raise SiteConfigError("the ntfy topic must be 6-64 letters, digits, - or _ (use a long, hard-to-guess name)")
                    channel.pop("topic", None)
                    channel["topic_credential"] = NTFY_TOPIC_CREDENTIAL
                    pending = (NTFY_TOPIC_CREDENTIAL, topic)
                elif channel["enabled"] and not channel.get("topic") and not (
                        channel.get("topic_credential") and self.secrets.get(channel["topic_credential"])):
                    raise SiteConfigError("enter an ntfy topic to turn on push notifications")
            if "mqtt" in payload:
                channel = next((c for c in channels if c.get("type") == "mqtt"), None)
                if channel is None:
                    channel = {"type": "mqtt"}
                    channels.append(channel)
                channel["enabled"] = bool((payload["mqtt"] or {}).get("enabled", False))
            self._commit(config, actor, "notification channels saved", before_save=lambda: pending and self.secrets.set(*pending))

    # ---- security rules ------------------------------------------------------

    def save_rule(self, rule_id: str | None, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """Create (rule_id None) or replace a rule. The whole config is validated,
        so a rule that references a missing zone or rule is rejected, not saved."""
        from .rules import clean_rule

        with self._lock:
            config = copy.deepcopy(self._config)
            rules = config.setdefault("rules", [])
            rule = clean_rule(payload)
            if rule_id is None:
                rule["id"] = _new_id(rule.get("name") or rule.get("type", "rule"), {existing["id"] for existing in rules})
                rules.append(rule)
                summary = f"rule {rule['id']} added"
            else:
                index = next((i for i, existing in enumerate(rules) if existing["id"] == rule_id), None)
                if index is None:
                    raise KeyError(rule_id)
                rule["id"] = rule_id
                rules[index] = rule
                summary = f"rule {rule_id} saved"
            rule.setdefault("enabled", True)
            self._commit(config, actor, summary)
            return copy.deepcopy(rule)

    def delete_rule(self, rule_id: str, actor: str) -> None:
        with self._lock:
            config = copy.deepcopy(self._config)
            rules = config.get("rules", [])
            if not any(rule["id"] == rule_id for rule in rules):
                raise KeyError(rule_id)
            users = [rule["id"] for rule in rules if rule_id in rule.get("rules", [])]
            if users:
                raise SiteConfigError(f"rule {rule_id} is part of {', '.join(users)}; remove it there first")
            config["rules"] = [rule for rule in rules if rule["id"] != rule_id]
            self._commit(config, actor, f"rule {rule_id} deleted")

    def set_rule_enabled(self, rule_id: str, enabled: bool, actor: str) -> None:
        with self._lock:
            config = copy.deepcopy(self._config)
            rule = next((rule for rule in config.get("rules", []) if rule["id"] == rule_id), None)
            if rule is None:
                raise KeyError(rule_id)
            rule["enabled"] = bool(enabled)
            self._commit(config, actor, f"rule {rule_id} {'enabled' if enabled else 'disabled'}")

    def replace(self, config: dict[str, Any], actor: str, summary: str) -> None:
        """Whole-config replacement (backup restore). Validated like any edit; the
        running storage location is kept, so a backup from elsewhere cannot move it."""
        with self._lock:
            clean = {key: value for key, value in copy.deepcopy(config).items() if not key.startswith("_")}
            if "storage" in self._config:
                clean["storage"] = copy.deepcopy(self._config["storage"])
            else:
                clean.pop("storage", None)
            self._commit(clean, actor, summary)

    # ---- commit --------------------------------------------------------------

    def _commit(self, config: dict[str, Any], actor: str, summary: str, before_save=None, after_save=None) -> None:
        try:
            validate_config(config)
        except ValueError as error:
            raise SiteConfigError(str(error)) from None
        if before_save:
            before_save()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        history = self.storage_dir / "config_history"
        if self.managed_path.exists():
            history.mkdir(exist_ok=True)
            self.managed_path.replace(history / f"site.{int(time.time() * 1000)}.json")
            for old in sorted(history.glob("site.*.json"))[:-BACKUPS]:
                old.unlink()
        temporary = self.managed_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(config, indent=1), encoding="utf-8")
        temporary.replace(self.managed_path)
        self._config = config
        if after_save:
            after_save()
        snapshot = self.config
        for listener in self._listeners:
            listener(snapshot | {"_change": {"actor": actor, "summary": summary}})


def _clean_source(source: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in source.items() if key in SOURCE_FIELDS and value not in (None, "")}
    if cleaned.get("type") not in STREAM_TYPES:
        raise SiteConfigError(f"source type must be one of {sorted(STREAM_TYPES)}")
    for key in ("port", "channel", "index"):
        if key in cleaned:
            cleaned[key] = int(cleaned[key])
    return cleaned


def _lift_url_password(source: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """A pasted rtsp://user:pass@host URL keeps its user but loses its password,
    which moves to the credential store; build_url puts it back at runtime.
    Placeholders (${...}) are left alone."""
    lifted = None
    for key in ("url", "sub_url"):
        url = source.get(key)
        if not url or "${" in url:
            continue
        parts = urlsplit(url)
        if parts.password is None:
            continue
        lifted = lifted or unquote(parts.password)
        netloc = f"{parts.username}@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        source = dict(source, **{key: parts._replace(netloc=netloc).geturl()})
    return source, lifted


def _forget_zones(config: dict[str, Any], removed: set[str]) -> list[str]:
    """Drop removed zones from rules and links. A rule left with no zones is
    disabled rather than silently widened to every zone."""
    notes = []
    if not removed:
        return notes
    for rule in config.get("rules", []):
        if set(rule.get("zones", [])) & removed:
            rule["zones"] = [zone for zone in rule["zones"] if zone not in removed]
            if not rule["zones"] or rule["type"] == "sequence":  # a sequence missing a step means something else
                rule["enabled"] = False
                notes.append(f"rule {rule['id']} disabled: the zones it watched were removed")
            else:
                notes.append(f"rule {rule['id']} no longer covers {sorted(removed)}")
    for device in config.get("devices", []):
        if device.get("zone_id") in removed:
            device.pop("zone_id")
            notes.append(f"device {device['id']} is no longer assigned to a removed zone")
    for link in config.get("topology", {}).get("links", []):
        for key in ("from_zones", "to_zones"):
            if set(link.get(key, [])) & removed:
                link[key] = [zone for zone in link[key] if zone not in removed]
    return notes


def _hide_video_address(holder: dict[str, Any]) -> dict[str, Any]:
    """Drone video stream addresses are write-only, like camera stream addresses."""
    source = (holder.get("video") or {}).get("source")
    if isinstance(source, dict):
        for key in WRITE_ONLY_FIELDS & source.keys():
            source[f"{key}_set"] = bool(source.pop(key))
    return holder


def _new_id(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "item"
    if not base[0].isalnum():
        base = "x" + base
    candidate, counter = base, 2
    while candidate in taken:
        candidate, counter = f"{base}_{counter}", counter + 1
    return candidate
