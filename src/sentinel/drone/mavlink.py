"""ArduPilot MAVLink adapters.

`MavlinkTelemetryAdapter` is for physical (or unknown) aircraft and is
TELEMETRY ONLY: it reads state, parameters and the fence, sends the Hub's
ground-station heartbeat and telemetry-rate requests, and refuses every flight
command. Enabling commands for a real aircraft is a deliberate future milestone
gated on docs/drone-integration.md. Do not remove the refusal to "try it out".

`SitlMavlinkAdapter` flies ArduPilot SITL, and only SITL. Commands work only
when all three hold, re-checked on every command:
1. the drone is configured with `allow_flight_commands: true`;
2. the connection is a loopback endpoint (127.0.0.1 / localhost / ::1);
3. the vehicle proved it is a simulator: its parameters include SIM_* entries,
   which exist only in ArduPilot's software-in-the-loop builds.
Until then it behaves exactly like the telemetry-only adapter and reports
itself as physical.

pymavlink (LGPL-3.0) is used unmodified as an optional library."""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import queue
import threading
import time
from typing import Any, Callable

from .ardupilot import COPTER_MODE_IDS, COPTER_MODES, LANDED_STATES, MAV_STATES, MAV_SYS_STATUS_PREARM_CHECK, ekf_navigation_ok, is_sitl, version_text
from .base import DroneAdapter, DroneTelemetry, FlightCommandRejected, distance_m, valid_coordinates

log = logging.getLogger("sentinel.drone.mavlink")

MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_AUTOPILOT_ARDUPILOTMEGA = 3
MAV_AUTOPILOT_INVALID = 8
NON_VEHICLE_TYPES = {6, 18, 26, 27, 30, 31}  # GCS, onboard controller, gimbal, ADSB, camera, charging station
# Multicopter MAV_TYPEs. ArduCopter keeps REQUEST_DATA_STREAM rates in RAM; ArduPlane
# saves them as parameters (persist_streamrates), so the request is sent to copters only.
MULTICOPTER_TYPES = {2, 3, 4, 13, 14, 15, 29, 35}
REFUSAL = "physical flight commands are disabled in this build (telemetry-only MAVLink adapter)"
GIMBAL_YAW_IN_EARTH_FRAME = 2048
# EXTENDED_SYS_STATE, HOME_POSITION, WIND, GIMBAL_DEVICE_ATTITUDE_STATUS, FENCE_STATUS
TELEMETRY_INTERVALS_HZ = {245: 1.0, 242: 0.2, 168: 1.0, 285: 4.0, 162: 1.0}
MAX_MESSAGES = 20
MAVLINK_IFLAG_SIGNED = 0x01
MAX_PLAUSIBLE_SPEED_MPS = 40.0  # faster than any Sentinel aircraft flies: a bigger step is a position jump
JUMP_SLACK_M = 10.0
REBOOT_BACKSTEP_MS = 5000  # the boot clock going back by more than this means the flight controller restarted
TIMED_MESSAGES = ("ATTITUDE", "GLOBAL_POSITION_INT", "SYSTEM_TIME")  # from the autopilot component, carrying time_boot_ms
# The command-path probe: a read-only request (MAV_CMD_REQUEST_CAMERA_INFORMATION). Any
# COMMAND_ACK, even "unsupported", proves the Hub's commands reach the flight controller.
# The companion relay drops it (not a telemetry command) and the FC's MAV_OPTIONS lockout ignores it.
PROBE_COMMAND = 521


def endpoint_host(connection: str) -> tuple[str, str]:
    """('tcp'|'udpin'|'udpout'|'udp'|'serial', host)."""
    parts = connection.split(":")
    scheme = parts[0].lower()
    if scheme in ("tcp", "tcpin", "udp", "udpin", "udpout", "udpbcast") and len(parts) >= 3:
        return scheme, ":".join(parts[1:-1]).strip("[]")
    return "serial", ""


def is_loopback_endpoint(connection: str) -> bool:
    scheme, host = endpoint_host(connection)
    if scheme == "serial" or not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _quaternion_pitch_yaw(q: list[float]) -> tuple[float, float]:
    w, x, y, z = q
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))))
    yaw = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    return pitch, yaw


def _battery_pct_ok(value: Any) -> bool:
    return value is not None and 0 <= value <= 100


BATTERY_CHARGE_STATES = {1: "ok", 2: "low", 3: "critical", 4: "emergency", 5: "failed", 6: "unhealthy"}
FENCE_BREACH_TYPES = {0: "boundary", 1: "min_alt", 2: "max_alt", 3: "boundary"}  # FENCE_BREACH (0 NONE with breach_status 1: treat as boundary)


REBOOT_CONFIRM_MESSAGES = 3  # consecutive timed messages on the new, lower clock before a restart is believed


class _ClockOffset:
    """The flight controller's boot clock as seen by one link.

    - Added delay: arrival time minus the boot clock. The smallest offset seen
      is the link's normal delay; anything above it is added delay (a
      congested radio, a buffering relay). Reset when the FC restarts.
    - Restart confirmation: one late packet from before a delay cleared (or a
      reordering link) must not look like a restart, because a restart
      discards the audited settings. A restart is believed after
      REBOOT_CONFIRM_MESSAGES consecutive messages on a clock that went back."""

    def __init__(self) -> None:
        self.minimum: float | None = None
        self.candidate: list[int] = []  # boot times seen on a lower clock, in a row

    def delay_ms(self, now_ms: int, boot_ms: int) -> int:
        offset = now_ms - boot_ms
        if self.minimum is None or offset < self.minimum:
            self.minimum = offset
        return int(offset - self.minimum)


def _restarted(telemetry: DroneTelemetry) -> None:
    telemetry.fc_reboots += 1
    telemetry.home_lat = telemetry.home_lon = None
    telemetry.messages = (telemetry.messages + ["Sentinel: flight controller restarted"])[-MAX_MESSAGES:]
    telemetry.message_count += 1


def _note_boot_time(telemetry: DroneTelemetry, message: Any, now_ms: int, clock: "_ClockOffset | None" = None) -> None:
    """A boot clock that runs backwards means the flight controller restarted:
    its home position is gone and it may come back with other settings."""
    boot = getattr(message, "time_boot_ms", None)
    if not isinstance(boot, (int, float)) or boot <= 0:
        return
    boot = int(boot)
    went_back = bool(telemetry.boot_ms) and boot < telemetry.boot_ms - REBOOT_BACKSTEP_MS
    if clock is None:  # no history to confirm with (single snapshots, tests)
        if went_back:
            _restarted(telemetry)
        telemetry.boot_ms = boot
        return
    if went_back:
        if clock.candidate and boot < clock.candidate[-1]:
            clock.candidate = []  # not one steady new clock either
        clock.candidate.append(boot)
        if len(clock.candidate) < REBOOT_CONFIRM_MESSAGES:
            return  # keep the known clock until the new one is confirmed; this sample's time is ignored
        clock.candidate, clock.minimum = [], None
        _restarted(telemetry)
        telemetry.boot_ms = boot
    else:
        clock.candidate = []  # on the known clock: anything lower before was a late packet, not a restart
        telemetry.boot_ms = max(telemetry.boot_ms, boot)
    telemetry.link_delay_ms = clock.delay_ms(now_ms, boot)


def apply_mavlink_message(telemetry: DroneTelemetry, message: Any, now_ms: int, clock: _ClockOffset | None = None) -> None:
    kind = message.get_type()
    if kind in TIMED_MESSAGES:
        _note_boot_time(telemetry, message, now_ms, clock)
    if kind == "HEARTBEAT":
        if getattr(message, "autopilot", MAV_AUTOPILOT_ARDUPILOTMEGA) == MAV_AUTOPILOT_INVALID or getattr(message, "type", 2) in NON_VEHICLE_TYPES:
            return  # another ground station, the companion, a gimbal: not the aircraft
        header = getattr(message, "_header", None)
        if header is not None and getattr(message, "_msgbuf", b"")[:1] == b"\xfd":  # MAVLink 2 frame: signed or not
            telemetry.link_signed = bool(header.incompat_flags & MAVLINK_IFLAG_SIGNED)
        telemetry.armed = bool(message.base_mode & MAV_MODE_FLAG_SAFETY_ARMED)
        if getattr(message, "autopilot", MAV_AUTOPILOT_ARDUPILOTMEGA) == MAV_AUTOPILOT_ARDUPILOTMEGA:
            telemetry.flight_mode = COPTER_MODES.get(int(message.custom_mode), f"custom_{message.custom_mode}")
        else:
            telemetry.flight_mode = f"custom_{message.custom_mode}"
        if hasattr(message, "system_status"):
            telemetry.vehicle_state = MAV_STATES.get(int(message.system_status), str(message.system_status))
        telemetry.last_heartbeat_ms = now_ms
        telemetry.link_ok = True
    elif kind == "SYS_STATUS":
        # -1 is "not reported"; anything above 100 % is a broken monitor, not a full battery.
        remaining = message.battery_remaining
        if remaining >= 0:
            if _battery_pct_ok(remaining):
                telemetry.battery_pct = float(remaining)
                telemetry.battery_ms = now_ms
            else:
                telemetry.battery_rejected += 1
        if message.voltage_battery not in (0, 65535):
            volts = message.voltage_battery / 1000
            if 3.0 <= volts <= 62.0:  # 1S to 14S
                telemetry.battery_voltage = volts
                telemetry.battery_ms = now_ms
            else:
                telemetry.battery_rejected += 1
        current = getattr(message, "current_battery", -1)
        telemetry.battery_current_a = None if current is None or current < 0 else round(current / 100, 2)
        present = getattr(message, "onboard_control_sensors_present", 0)
        if present & MAV_SYS_STATUS_PREARM_CHECK:
            telemetry.prearm_ok = bool(message.onboard_control_sensors_health & MAV_SYS_STATUS_PREARM_CHECK)
    elif kind == "BATTERY_STATUS":
        if message.battery_remaining >= 0:
            if _battery_pct_ok(message.battery_remaining):
                telemetry.battery_pct = float(message.battery_remaining)
                telemetry.battery_ms = now_ms
            else:
                telemetry.battery_rejected += 1
        if message.temperature != 32767:
            telemetry.temperature_c = message.temperature / 100
        state = getattr(message, "charge_state", 0)
        if state in BATTERY_CHARGE_STATES:
            telemetry.battery_state = BATTERY_CHARGE_STATES[state]
    elif kind == "GLOBAL_POSITION_INT":
        lat, lon = message.lat / 1e7, message.lon / 1e7
        telemetry.alt_m = round(message.relative_alt / 1000, 2)  # barometric: valid without a position
        if message.hdg != 65535:
            telemetry.heading_deg = message.hdg / 100
        telemetry.ground_speed_mps = round(math.hypot(message.vx, message.vy) / 100, 2)
        if not valid_coordinates(lat, lon):
            telemetry.position_rejected += 1  # no position yet (0, 0) or garbage: keep the last good one, let it age
        else:
            if telemetry.lat is not None and telemetry.lon is not None and telemetry.telemetry_ms:
                elapsed_s = max(0.05, (now_ms - telemetry.telemetry_ms) / 1000)
                if distance_m((telemetry.lat, telemetry.lon), (lat, lon)) > MAX_PLAUSIBLE_SPEED_MPS * elapsed_s + JUMP_SLACK_M:
                    telemetry.position_jumps += 1
                    telemetry.last_jump_ms = now_ms
            telemetry.lat, telemetry.lon = lat, lon
            telemetry.telemetry_ms = now_ms
    elif kind == "GPS_RAW_INT":
        telemetry.gps_fix = int(message.fix_type)
        telemetry.satellites = int(message.satellites_visible)
        eph = getattr(message, "eph", 65535)
        telemetry.hdop = None if eph in (65535, None) else round(eph / 100, 2)
        telemetry.gps_ms = now_ms
    elif kind == "EXTENDED_SYS_STATE":
        telemetry.landed_state = LANDED_STATES.get(int(message.landed_state), "")
    elif kind == "ATTITUDE":
        telemetry.roll_deg = round(math.degrees(message.roll), 1)
        telemetry.pitch_deg = round(math.degrees(message.pitch), 1)
    elif kind == "HOME_POSITION":
        if valid_coordinates(message.latitude / 1e7, message.longitude / 1e7):
            telemetry.home_lat = message.latitude / 1e7
            telemetry.home_lon = message.longitude / 1e7
    elif kind == "EKF_STATUS_REPORT":
        telemetry.ekf_ok = ekf_navigation_ok(int(message.flags))
        telemetry.ekf_ms = now_ms
    elif kind == "WIND":
        # A multicopter's EKF estimates wind only with drag fusion (EK3_DRAG_*) or an
        # airspeed sensor; otherwise ArduCopter streams an exact zero vector. That is
        # no estimate, not calm air.
        estimating = float(message.speed) != 0.0 or float(getattr(message, "speed_z", 0.0)) != 0.0
        telemetry.wind_mps = round(float(message.speed), 1) if estimating else None
    elif kind == "FENCE_STATUS":
        breached = int(getattr(message, "breach_status", 0)) == 1
        telemetry.fence_breach = FENCE_BREACH_TYPES.get(int(getattr(message, "breach_type", 3)), "boundary") if breached else ""
        telemetry.fence_breaches = int(getattr(message, "breach_count", 0))
    elif kind == "MOUNT_STATUS":
        telemetry.gimbal_pitch_deg = message.pointing_a / 100
        telemetry.gimbal_yaw_deg = message.pointing_c / 100
        telemetry.gimbal_ms = now_ms
    elif kind == "GIMBAL_DEVICE_ATTITUDE_STATUS":
        q = list(message.q)
        if not all(math.isfinite(c) for c in q) or abs(sum(c * c for c in q) - 1.0) > 0.1:
            return  # NaN or an unnormalised quaternion: the gimbal did not report a usable angle
        pitch, yaw = _quaternion_pitch_yaw(q)
        if int(getattr(message, "flags", 0)) & GIMBAL_YAW_IN_EARTH_FRAME:
            yaw -= telemetry.heading_deg
        telemetry.gimbal_pitch_deg, telemetry.gimbal_yaw_deg = round(pitch, 1), round(yaw, 1)
        telemetry.gimbal_ms = now_ms
    elif kind == "STATUSTEXT":
        text = message.text if isinstance(message.text, str) else bytes(message.text).decode(errors="replace")
        telemetry.messages = (telemetry.messages + [text.strip("\x00").strip()])[-MAX_MESSAGES:]
        telemetry.message_count += 1
    elif kind == "AUTOPILOT_VERSION":
        telemetry.autopilot_version = version_text(int(message.flight_sw_version))
    if telemetry.landed_state:
        telemetry.in_air = telemetry.landed_state in ("in_air", "takeoff", "landing")
    else:
        telemetry.in_air = telemetry.alt_m > 0.5


class MavlinkTelemetryAdapter(DroneAdapter):
    kind = "mavlink"
    flight_controller = "ardupilot"

    def __init__(self, connection: str, *, heartbeat_timeout_s: float = 3.0, gcs_system_id: int = 245, gcs_component_id: int = 190,
                 stream_rate_hz: int = 4, reconnect_max_s: float = 30.0, silence_reconnect_s: float = 10.0,
                 link_factory: Callable[[str, int, int], Any] | None = None) -> None:
        self.connection_string = connection
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.gcs_system_id = gcs_system_id
        self.gcs_component_id = gcs_component_id
        self.stream_rate_hz = stream_rate_hz
        self.reconnect_max_s = reconnect_max_s
        self.silence_reconnect_s = silence_reconnect_s
        self.link_factory = link_factory or _pymavlink_link
        self._telemetry = DroneTelemetry()
        self._params: dict[str, float] = {}
        self._param_index: dict[int, str] = {}
        self._param_count = 0
        self._fence: list[list[float]] | None = None
        self._fence_exclusions: list[list[list[float]]] = []
        self._overheard_fence: dict[str, Any] = {"count": None, "items": {}}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._link: Any = None
        self._vehicle: tuple[int, int] | None = None
        self._acks: queue.Queue = queue.Queue()
        self._mission_rx: queue.Queue = queue.Queue()
        self._clock = _ClockOffset()
        self._fence_busy = threading.Lock()  # one fence download at a time (they share the mission queue)
        self._ack_lock = threading.Lock()  # one command waiting for its acknowledgement at a time
        self._vehicle_type: int | None = None
        self._eof = False
        self._refresh = threading.Event()
        self.state = "idle"
        self.last_error = ""
        self.reconnects = 0
        self.messages_received = 0
        self.message_counts: dict[str, int] = {}  # from the aircraft, by message type (rates for diagnostics)
        self.autopilot_info: dict[str, Any] = {}  # firmware version, git hash, board (no serial number / UID)

    @property
    def is_physical(self) -> bool:  # type: ignore[override]
        return True

    # ---- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"mavlink-{self.kind}", daemon=True)
        self._thread.start()
        self._telemetry.connected = True

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._close_link()
        self._telemetry.connected = False

    def telemetry(self) -> DroneTelemetry:
        with self._lock:
            snapshot = DroneTelemetry(**self._telemetry.as_dict())
            sitl = is_sitl(self._params) if self.params_complete() else None
        if not snapshot.last_heartbeat_ms or time.time() * 1000 - snapshot.last_heartbeat_ms > self.heartbeat_timeout_s * 1000:
            snapshot.link_ok = False
        snapshot.sitl = sitl
        return snapshot

    def parameters(self) -> dict[str, float]:
        with self._lock:
            return dict(self._params) if self.params_complete() else {}

    def params_complete(self) -> bool:
        return bool(self._param_count) and len(self._param_index) >= self._param_count

    def fence_polygon(self) -> list[list[float]] | None:
        with self._lock:
            return None if self._fence is None else [list(point) for point in self._fence]

    def refresh(self) -> None:
        """Re-read the fence soon (after commissioning changed it). Parameter
        changes arrive by themselves: ArduPilot broadcasts PARAM_VALUE on change."""
        self._refresh.set()

    def link_status(self) -> dict[str, Any]:
        with self._lock:
            telemetry = self._telemetry
            return {"state": self.state, "connection": self.connection_string, "reconnects": self.reconnects, "last_error": self.last_error,
                    "messages": self.messages_received, "parameters": f"{len(self._param_index)}/{self._param_count or '?'}",
                    "fence_vertices": None if self._fence is None else len(self._fence),
                    "vehicle": None if self._vehicle is None else f"{self._vehicle[0]}/{self._vehicle[1]}",
                    "fc_reboots": telemetry.fc_reboots, "signed": telemetry.link_signed, "other_vehicles": list(telemetry.other_vehicles),
                    "position_rejected": telemetry.position_rejected, "position_jumps": telemetry.position_jumps,
                    "battery_rejected": telemetry.battery_rejected}

    # ---- reader thread -------------------------------------------------------

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.state = "connecting"
                self._eof = False
                self._link = self.link_factory(self.connection_string, self.gcs_system_id, self.gcs_component_id)
                self._patch_eof(self._link)
                self.state = "connected"
                backoff = 1.0
                self._session(self._link)
            except Exception as error:  # reconnect with backoff; never let the thread die
                self.last_error = f"{type(error).__name__}: {error}"[:200]
                log.warning("MAVLink link %s: %s", self.connection_string, self.last_error)
            finally:
                self._close_link()
                with self._lock:
                    self._vehicle = None
            if self._stop.is_set():
                break
            self.state = "reconnecting"
            self.reconnects += 1
            self._stop.wait(backoff)
            backoff = min(self.reconnect_max_s, backoff * 2)

    def _patch_eof(self, link: Any) -> None:
        def closed() -> None:
            self._eof = True

        for name in ("handle_eof", "handle_disconnect"):
            if hasattr(link, name):
                setattr(link, name, closed)

    def _close_link(self) -> None:
        link, self._link = self._link, None
        if link is not None:
            try:
                link.close()
            except Exception:
                pass

    def _session(self, link: Any) -> None:
        last_rx = time.monotonic()
        next_heartbeat = 0.0
        next_housekeeping = 0.0
        param_requested_at = 0.0
        param_last_rx = 0.0
        fence_next = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_heartbeat:
                next_heartbeat = now + 1.0
                self._send(lambda mav: mav.heartbeat_send(6, MAV_AUTOPILOT_INVALID, 0, 0, 4))  # we are a ground station
            message = link.recv_match(blocking=True, timeout=0.2)
            if self._eof:
                raise ConnectionError("link closed by the other end")
            if message is None:
                scheme, _ = endpoint_host(self.connection_string)
                if scheme.startswith("tcp") and now - last_rx > self.silence_reconnect_s:
                    raise TimeoutError(f"no data for {self.silence_reconnect_s:g} s")
                continue
            kind = message.get_type()
            if kind == "BAD_DATA":
                continue
            source = (message.get_srcSystem(), message.get_srcComponent())
            vehicle_heartbeat = kind == "HEARTBEAT" and message.autopilot != MAV_AUTOPILOT_INVALID and message.type not in NON_VEHICLE_TYPES
            if self._vehicle is None:
                if not vehicle_heartbeat:
                    continue
                with self._lock:
                    self._vehicle = source
                    self._vehicle_type = int(message.type)
                self._request_telemetry()
                param_requested_at = now
                self._send(lambda mav: mav.param_request_list_send(source[0], source[1]))
            if vehicle_heartbeat and source != self._vehicle:
                # A second autopilot on the link (another aircraft, or two with the same system id):
                # its telemetry is never mixed in, but people must know it is there.
                tag = f"{source[0]}/{source[1]}"
                with self._lock:
                    if tag not in self._telemetry.other_vehicles:
                        self._telemetry.other_vehicles = (self._telemetry.other_vehicles + [tag])[-5:]
                continue
            if source[0] != self._vehicle[0]:
                continue  # other systems (ground stations) on a shared link
            if source[1] != self._vehicle[1] and kind not in ("GIMBAL_DEVICE_ATTITUDE_STATUS", "MOUNT_STATUS", "STATUSTEXT"):
                continue
            last_rx = now
            self.messages_received += 1
            self.message_counts[kind] = self.message_counts.get(kind, 0) + 1
            with self._lock:
                reboots, boot_before = self._telemetry.fc_reboots, self._telemetry.boot_ms
                apply_mavlink_message(self._telemetry, message, int(time.time() * 1000), self._clock)
                rebooted = self._telemetry.fc_reboots != reboots
                if rebooted:
                    # It may have restarted with other settings: audit nothing until they are read again.
                    self._params, self._param_index, self._param_count = {}, {}, 0
                    self._fence, self._fence_exclusions = None, []
                if kind == "PARAM_VALUE":
                    name = message.param_id if isinstance(message.param_id, str) else bytes(message.param_id).decode(errors="replace")
                    name = name.strip("\x00")
                    self._params[name] = float(message.param_value)
                    if message.param_index not in (65535, -1):
                        self._param_index[int(message.param_index)] = name
                    self._param_count = max(self._param_count, int(message.param_count))
                    param_last_rx = now
            if rebooted:
                log.warning("MAVLink %s: the flight controller restarted (%s boot time %d ms after %d ms); re-reading its settings",
                            self.connection_string, kind, self._telemetry.boot_ms, boot_before)
                self._request_telemetry()  # stream rates requested over MAVLink live in RAM and are gone
                param_requested_at = now
                self._send(lambda mav: mav.param_request_list_send(*self._vehicle))
                self._refresh.set()
            if kind == "AUTOPILOT_VERSION":
                custom = getattr(message, "flight_custom_version", b"") or b""
                git = bytes(custom).rstrip(b"\x00")[:8] if isinstance(custom, (bytes, bytearray, list)) else b""
                self.autopilot_info = {"version": version_text(int(message.flight_sw_version)), "git": git.decode(errors="replace"),
                                       "board_version": int(getattr(message, "board_version", 0)),
                                       "vendor_id": int(getattr(message, "vendor_id", 0)), "product_id": int(getattr(message, "product_id", 0))}
            if kind == "COMMAND_ACK" and getattr(message, "target_system", 0) in (0, self.gcs_system_id):
                self._acks.put(message)  # acknowledgements addressed to the pilot's ground station are not ours
            elif kind in ("MISSION_COUNT", "MISSION_ITEM_INT", "MISSION_ACK"):
                self._mission_rx.put(message)
                self._overhear_fence(message)
            if now >= next_housekeeping:
                next_housekeeping = now + 2.0
                if not self.params_complete() and param_requested_at and now - max(param_last_rx, param_requested_at) > 2.0:
                    self._request_missing_params()
                    param_last_rx = now
                if self.params_complete() and (now >= fence_next or self._refresh.is_set()):
                    self._refresh.clear()
                    with self._lock:
                        airborne = self._telemetry.in_air
                    fence_next = now + (120.0 if airborne else 15.0)
                    threading.Thread(target=self._download_fence, name="mavlink-fence", daemon=True).start()
                with self._lock:
                    home_missing = self._telemetry.home_lat is None and self._telemetry.gps_fix >= 3
                if home_missing:
                    self._request_message(242)

    def _send(self, call: Callable[[Any], None]) -> None:
        link = self._link
        if link is None:
            raise FlightCommandRejected("no MAVLink connection")
        with self._send_lock:
            call(link.mav)

    def _request_telemetry(self) -> None:
        system, component = self._vehicle
        if self._vehicle_type in MULTICOPTER_TYPES:  # never on a vehicle that would save the rates as parameters
            self._send(lambda mav: mav.request_data_stream_send(system, component, 0, self.stream_rate_hz, 1))
        for message_id, rate in TELEMETRY_INTERVALS_HZ.items():
            self._send(lambda mav, m=message_id, r=rate: mav.command_long_send(system, component, 511, 0, m, 1e6 / r, 0, 0, 0, 0, 0))
        self._request_message(148)  # AUTOPILOT_VERSION
        self._request_message(242)  # HOME_POSITION

    def _request_message(self, message_id: int) -> None:
        if self._vehicle is None:
            return
        system, component = self._vehicle
        self._send(lambda mav: mav.command_long_send(system, component, 512, 0, message_id, 0, 0, 0, 0, 0, 0))

    def _request_missing_params(self) -> None:
        system, component = self._vehicle
        with self._lock:
            missing = [index for index in range(self._param_count) if index not in self._param_index][:40]
        if not self._param_count:
            self._send(lambda mav: mav.param_request_list_send(system, component))
            return
        for index in missing:
            self._send(lambda mav, i=index: mav.param_request_read_send(system, component, b"", i))

    def _download_fence(self) -> None:
        """Read-only: MAVLink mission protocol download of the fence (mission_type 1)."""
        if self._vehicle is None or not self._fence_busy.acquire(blocking=False):
            return  # a slow download (packet loss) is still running: never two at once
        try:
            self._download_fence_locked()
        finally:
            self._fence_busy.release()

    def _download_fence_locked(self) -> None:
        if self._vehicle is None:
            return
        system, component = self._vehicle
        while not self._mission_rx.empty():
            self._mission_rx.get_nowait()
        try:
            self._send(lambda mav: mav.mission_request_list_send(system, component, 1))
            count_msg = self._await_mission("MISSION_COUNT", None)
            if count_msg is None:
                return
            items = []
            for seq in range(int(count_msg.count)):
                for _ in range(3):
                    self._send(lambda mav, s=seq: mav.mission_request_int_send(system, component, s, 1))
                    item = self._await_mission("MISSION_ITEM_INT", seq)
                    if item is not None:
                        items.append(item)
                        break
                else:
                    return
            self._send(lambda mav: mav.mission_ack_send(system, component, 0, 1))
        except FlightCommandRejected:
            return
        self._store_fence(items)

    def _overhear_fence(self, message: Any) -> None:
        """On a link shared with the pilot's ground station (one radio), the
        fence items the FC sends to *that* ground station pass the Hub too. When
        the FC ignores the Hub's own requests (MAV_OPTIONS command lockout), this
        is how Sentinel still reads the fence it must audit. Read-only."""
        if getattr(message, "mission_type", 0) != 1:
            return
        heard = self._overheard_fence
        if message.get_type() == "MISSION_COUNT":
            heard["count"], heard["items"] = int(message.count), {}
            if heard["count"] == 0:
                self._store_fence([])
                heard["count"] = None
        elif message.get_type() == "MISSION_ITEM_INT" and heard["count"] is not None:
            heard["items"][int(message.seq)] = message
            if len(heard["items"]) >= heard["count"]:
                self._store_fence([heard["items"][seq] for seq in sorted(heard["items"])])
                heard["count"], heard["items"] = None, {}

    def _store_fence(self, items: list[Any]) -> None:
        inclusion: list[list[float]] = []
        exclusions: list[list[list[float]]] = []
        current: list[list[float]] = []
        expected = 0
        for item in items:
            if item.command not in (5001, 5002):
                continue
            if not current:
                expected = int(item.param1)
            current.append([item.x / 1e7, item.y / 1e7])
            if len(current) >= expected:
                if item.command == 5001 and not inclusion:
                    inclusion = current
                elif item.command == 5002:
                    exclusions.append(current)
                current = []
        with self._lock:
            self._fence = inclusion
            self._fence_exclusions = exclusions

    def _await_mission(self, kind: str, seq: int | None, timeout_s: float = 2.0) -> Any:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                message = self._mission_rx.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if getattr(message, "mission_type", 0) != 1:
                continue
            if message.get_type() == kind and (seq is None or int(message.seq) == seq):
                return message
        return None

    # ---- command-path probe (read-only) ---------------------------------------

    def probe_command_path(self, timeout_s: float = 3.0) -> dict[str, Any]:
        """Do the Hub's commands reach the flight controller? Sends one read-only
        request (PROBE_COMMAND, camera information) and listens for any
        acknowledgement. The Hub command lockout (companion relay or the FC's
        MAV_OPTIONS) must make this `blocked`. Never sends anything that moves,
        arms or reconfigures the aircraft; the command id is fixed here."""
        telemetry = self.telemetry()
        if self._vehicle is None or not telemetry.link_ok:
            return {"state": "unknown", "detail": "no telemetry from the aircraft: the probe needs a live link"}
        system, component = self._vehicle
        with self._ack_lock:
            while not self._acks.empty():
                self._acks.get_nowait()
            try:
                self._send(lambda mav: mav.command_long_send(system, component, PROBE_COMMAND, 0, 1, 0, 0, 0, 0, 0, 0))
            except FlightCommandRejected as error:
                return {"state": "unknown", "detail": str(error)}
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    ack = self._acks.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if int(ack.command) == PROBE_COMMAND:
                    return {"state": "open", "result": int(ack.result),
                            "detail": f"the flight controller acknowledged a read-only request from the Hub (result {int(ack.result)}): "
                                      "Hub commands reach it, so nothing but Sentinel's own refusal stops a flight command"}
        return {"state": "blocked", "detail": f"no acknowledgement within {timeout_s:g} s while telemetry flows: Hub commands do not reach "
                                              "the flight controller (command lockout, relay filter, or a receive-only link)"}

    # ---- flight commands: refused for physical aircraft ---------------------

    def takeoff(self, altitude_m: float) -> None:
        raise FlightCommandRejected(REFUSAL)

    def goto(self, lat: float, lon: float, altitude_m: float) -> None:
        raise FlightCommandRejected(REFUSAL)

    def hold(self) -> None:
        raise FlightCommandRejected(REFUSAL)

    def return_to_home(self) -> None:
        raise FlightCommandRejected(REFUSAL)

    def land(self) -> None:
        raise FlightCommandRejected(REFUSAL)

    def commands_allowed(self) -> tuple[bool, str]:
        return False, "real or unverified aircraft: telemetry only in this build"


class SitlMavlinkAdapter(MavlinkTelemetryAdapter):
    kind = "sitl"

    def __init__(self, connection: str, *, allow_flight_commands: bool = False, command_timeout_s: float = 4.0, **kwargs: Any) -> None:
        super().__init__(connection, **kwargs)
        self.allow_flight_commands = allow_flight_commands
        self.command_timeout_s = command_timeout_s

    @property
    def is_physical(self) -> bool:  # type: ignore[override]
        return not self.verified_simulator()

    def verified_simulator(self) -> bool:
        with self._lock:
            return self.params_complete() and is_sitl(self._params)

    def commands_allowed(self) -> tuple[bool, str]:
        if not self.allow_flight_commands:
            return False, "flight commands are not enabled for this simulator (allow_flight_commands)"
        if not is_loopback_endpoint(self.connection_string):
            return False, "simulator commands need a loopback connection (127.0.0.1)"
        if not self.verified_simulator():
            return False, "the vehicle has not proved it is a simulator (no SIM_* parameters read yet)"
        return True, "ArduPilot SITL (verified simulator, loopback link)"

    def _require(self) -> tuple[int, int]:
        allowed, why = self.commands_allowed()
        if not allowed:
            raise FlightCommandRejected(f"{REFUSAL}: {why}")
        if self._vehicle is None or not self.telemetry().link_ok:
            raise FlightCommandRejected("no command link")
        return self._vehicle

    def _command(self, command: int, *params: float, accept_in_progress: bool = True) -> None:
        system, component = self._require()
        with self._ack_lock:
            while not self._acks.empty():
                self._acks.get_nowait()
            values = list(params) + [0.0] * (7 - len(params))
            self._send(lambda mav: mav.command_long_send(system, component, command, 0, *values))
            deadline = time.monotonic() + self.command_timeout_s
            while time.monotonic() < deadline:
                try:
                    ack = self._acks.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if int(ack.command) != command:
                    continue
                if int(ack.result) == 0 or (accept_in_progress and int(ack.result) == 5):
                    return
                latest = next((m for m in reversed(self.telemetry().messages) if m.lower().startswith(("prearm", "arm"))), "")
                raise FlightCommandRejected(f"flight controller refused command {command} (result {ack.result})" + (f": {latest}" if latest else ""))
        raise FlightCommandRejected(f"no acknowledgement for command {command}")

    def _set_mode(self, mode: str) -> None:
        self._command(176, 1, COPTER_MODE_IDS[mode])  # MAV_CMD_DO_SET_MODE, custom mode enabled

    def takeoff(self, altitude_m: float) -> None:
        if self.telemetry().in_air:
            raise FlightCommandRejected("already airborne")
        self._set_mode("GUIDED")
        self._command(400, 1)  # MAV_CMD_COMPONENT_ARM_DISARM
        self._command(22, 0, 0, 0, 0, 0, 0, altitude_m)  # MAV_CMD_NAV_TAKEOFF

    def goto(self, lat: float, lon: float, altitude_m: float) -> None:
        system, component = self._require()
        if self.telemetry().flight_mode != "GUIDED":
            self._set_mode("GUIDED")
        self._send(lambda mav: mav.set_position_target_global_int_send(
            0, system, component, 6, 0b110111111000, int(lat * 1e7), int(lon * 1e7), altitude_m, 0, 0, 0, 0, 0, 0, 0, 0))

    def hold(self) -> None:
        telemetry = self.telemetry()
        if telemetry.lat is None:
            raise FlightCommandRejected("no position to hold")
        self.goto(telemetry.lat, telemetry.lon, telemetry.alt_m)

    def return_to_home(self) -> None:
        self._set_mode("RTL")

    def land(self) -> None:
        self._set_mode("LAND")


def _pymavlink_link(connection: str, system_id: int, component_id: int) -> Any:
    os.environ.setdefault("MAVLINK20", "1")
    from pymavlink import mavutil  # optional dependency: pip install pymavlink

    return mavutil.mavlink_connection(connection, source_system=system_id, source_component=component_id, autoreconnect=False)
