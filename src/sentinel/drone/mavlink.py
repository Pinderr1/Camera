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
from .base import DroneAdapter, DroneTelemetry, FlightCommandRejected

log = logging.getLogger("sentinel.drone.mavlink")

MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_AUTOPILOT_ARDUPILOTMEGA = 3
MAV_AUTOPILOT_INVALID = 8
NON_VEHICLE_TYPES = {6, 18, 26, 27, 30, 31}  # GCS, onboard controller, gimbal, ADSB, camera, charging station
REFUSAL = "physical flight commands are disabled in this build (telemetry-only MAVLink adapter)"
GIMBAL_YAW_IN_EARTH_FRAME = 2048
TELEMETRY_INTERVALS_HZ = {245: 1.0, 242: 0.2, 168: 1.0, 285: 4.0}  # EXTENDED_SYS_STATE, HOME_POSITION, WIND, GIMBAL_DEVICE_ATTITUDE_STATUS
MAX_MESSAGES = 20


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


def apply_mavlink_message(telemetry: DroneTelemetry, message: Any, now_ms: int) -> None:
    kind = message.get_type()
    if kind == "HEARTBEAT":
        if getattr(message, "autopilot", MAV_AUTOPILOT_ARDUPILOTMEGA) == MAV_AUTOPILOT_INVALID or getattr(message, "type", 2) in NON_VEHICLE_TYPES:
            return  # another ground station, the companion, a gimbal: not the aircraft
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
        if message.battery_remaining >= 0:
            telemetry.battery_pct = float(message.battery_remaining)
        if message.voltage_battery not in (0, 65535):
            telemetry.battery_voltage = message.voltage_battery / 1000
        present = getattr(message, "onboard_control_sensors_present", 0)
        if present & MAV_SYS_STATUS_PREARM_CHECK:
            telemetry.prearm_ok = bool(message.onboard_control_sensors_health & MAV_SYS_STATUS_PREARM_CHECK)
    elif kind == "BATTERY_STATUS":
        if message.battery_remaining >= 0:
            telemetry.battery_pct = float(message.battery_remaining)
        if message.temperature != 32767:
            telemetry.temperature_c = message.temperature / 100
    elif kind == "GLOBAL_POSITION_INT":
        telemetry.lat = message.lat / 1e7
        telemetry.lon = message.lon / 1e7
        telemetry.alt_m = round(message.relative_alt / 1000, 2)
        if message.hdg != 65535:
            telemetry.heading_deg = message.hdg / 100
        telemetry.ground_speed_mps = round(math.hypot(message.vx, message.vy) / 100, 2)
        telemetry.telemetry_ms = now_ms
    elif kind == "GPS_RAW_INT":
        telemetry.gps_fix = int(message.fix_type)
        telemetry.satellites = int(message.satellites_visible)
        eph = getattr(message, "eph", 65535)
        telemetry.hdop = None if eph in (65535, None) else round(eph / 100, 2)
    elif kind == "EXTENDED_SYS_STATE":
        telemetry.landed_state = LANDED_STATES.get(int(message.landed_state), "")
    elif kind == "ATTITUDE":
        telemetry.roll_deg = round(math.degrees(message.roll), 1)
        telemetry.pitch_deg = round(math.degrees(message.pitch), 1)
    elif kind == "HOME_POSITION":
        telemetry.home_lat = message.latitude / 1e7
        telemetry.home_lon = message.longitude / 1e7
    elif kind == "EKF_STATUS_REPORT":
        telemetry.ekf_ok = ekf_navigation_ok(int(message.flags))
    elif kind == "WIND":
        telemetry.wind_mps = round(float(message.speed), 1)
    elif kind == "MOUNT_STATUS":
        telemetry.gimbal_pitch_deg = message.pointing_a / 100
        telemetry.gimbal_yaw_deg = message.pointing_c / 100
    elif kind == "GIMBAL_DEVICE_ATTITUDE_STATUS":
        pitch, yaw = _quaternion_pitch_yaw(list(message.q))
        if int(getattr(message, "flags", 0)) & GIMBAL_YAW_IN_EARTH_FRAME:
            yaw -= telemetry.heading_deg
        telemetry.gimbal_pitch_deg, telemetry.gimbal_yaw_deg = round(pitch, 1), round(yaw, 1)
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
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._link: Any = None
        self._vehicle: tuple[int, int] | None = None
        self._acks: queue.Queue = queue.Queue()
        self._mission_rx: queue.Queue = queue.Queue()
        self._eof = False
        self._refresh = threading.Event()
        self.state = "idle"
        self.last_error = ""
        self.reconnects = 0
        self.messages_received = 0

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
            return {"state": self.state, "connection": self.connection_string, "reconnects": self.reconnects, "last_error": self.last_error,
                    "messages": self.messages_received, "parameters": f"{len(self._param_index)}/{self._param_count or '?'}",
                    "fence_vertices": None if self._fence is None else len(self._fence)}

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
            if self._vehicle is None:
                if kind != "HEARTBEAT" or message.autopilot == MAV_AUTOPILOT_INVALID or message.type in NON_VEHICLE_TYPES:
                    continue
                with self._lock:
                    self._vehicle = source
                self._request_telemetry()
                param_requested_at = now
                self._send(lambda mav: mav.param_request_list_send(source[0], source[1]))
            if source[0] != self._vehicle[0]:
                continue  # other systems (ground stations) on a shared link
            if source[1] != self._vehicle[1] and kind not in ("GIMBAL_DEVICE_ATTITUDE_STATUS", "MOUNT_STATUS", "STATUSTEXT"):
                continue
            last_rx = now
            self.messages_received += 1
            with self._lock:
                apply_mavlink_message(self._telemetry, message, int(time.time() * 1000))
                if kind == "PARAM_VALUE":
                    name = message.param_id if isinstance(message.param_id, str) else bytes(message.param_id).decode(errors="replace")
                    name = name.strip("\x00")
                    self._params[name] = float(message.param_value)
                    if message.param_index not in (65535, -1):
                        self._param_index[int(message.param_index)] = name
                    self._param_count = max(self._param_count, int(message.param_count))
                    param_last_rx = now
            if kind == "COMMAND_ACK":
                self._acks.put(message)
            elif kind in ("MISSION_COUNT", "MISSION_ITEM_INT", "MISSION_ACK"):
                self._mission_rx.put(message)
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
