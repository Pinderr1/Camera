"""A scripted ground station for ArduPilot SITL, standing in for two people:

- the commissioning engineer, who loads the fence polygon from a Sentinel
  `.plan` export onto the flight controller, and
- the pilot, who loads Sentinel's proposed inspection route into their ground
  station and presses "start mission".

Not part of the Sentinel product. It flies SIMULATORS ONLY: it refuses any
connection that is not loopback, and any vehicle without SIM_* parameters.

    python scripts/sitl/gcs_standin.py commission PLAN_FILE [--connection tcp:127.0.0.1:5763]
    python scripts/sitl/gcs_standin.py fly PLAN_FILE
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from sentinel.drone.mavlink import is_loopback_endpoint  # noqa: E402

FENCE, MISSION = 1, 0


class NotASimulator(RuntimeError):
    pass


class SitlGroundStation:
    def __init__(self, connection: str = "tcp:127.0.0.1:5763", system_id: int = 255) -> None:
        if not is_loopback_endpoint(connection):
            raise NotASimulator(f"{connection} is not a loopback endpoint: this tool only flies simulators")
        os.environ.setdefault("MAVLINK20", "1")
        from pymavlink import mavutil

        self.mavutil = mavutil
        self.link = mavutil.mavlink_connection(connection, source_system=system_id, source_component=190, autoreconnect=True)
        heartbeat = self.link.wait_heartbeat(timeout=30)
        if heartbeat is None:
            raise TimeoutError("no heartbeat from the simulator")
        self.target = (self.link.target_system, self.link.target_component or 1)
        if self.get_param("SIM_SPEEDUP") is None:
            raise NotASimulator("the vehicle has no SIM_* parameters: refusing to command what may be a real aircraft")
        self._heartbeats = threading.Event()

    # ---- what a real ground station does on connect ------------------------------

    def start_heartbeats(self) -> None:
        """Ground stations send a heartbeat every second; the flight controller's
        GCS failsafe watches the one whose id is MAV_GCS_SYSID."""
        def beat() -> None:
            while not self._heartbeats.wait(1.0):
                try:
                    self.link.mav.heartbeat_send(6, 8, 0, 0, 4)  # MAV_TYPE_GCS, MAV_AUTOPILOT_INVALID
                except OSError:
                    pass

        threading.Thread(target=beat, name="gcs-heartbeat", daemon=True).start()

    def download_parameters(self, timeout: float = 60.0) -> int:
        """Full parameter download, as QGroundControl does on connect."""
        self.link.mav.param_request_list_send(*self.target)
        seen: set[int] = set()
        count = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self.link.recv_match(type="PARAM_VALUE", blocking=True, timeout=1)
            if message is None:
                missing = [i for i in range(count) if i not in seen][:20]
                for index in missing:
                    self.link.mav.param_request_read_send(*self.target, b"", index)
                continue
            count = max(count, int(message.param_count))
            seen.add(int(message.param_index))
            if count and len(seen) >= count:
                return count
        raise TimeoutError(f"parameter download incomplete ({len(seen)}/{count})")

    def download_fence(self) -> int:
        """Read the fence back (mission protocol download, mission_type 1)."""
        self.link.mav.mission_request_list_send(*self.target, FENCE)
        message = self.link.recv_match(type="MISSION_COUNT", blocking=True, timeout=5)
        if message is None:
            raise TimeoutError("no fence count")
        for seq in range(int(message.count)):
            self.link.mav.mission_request_int_send(*self.target, seq, FENCE)
            if self.link.recv_match(type="MISSION_ITEM_INT", blocking=True, timeout=3) is None:
                raise TimeoutError(f"fence item {seq} not received")
        self.link.mav.mission_ack_send(*self.target, 0, FENCE)
        return int(message.count)

    def clear_fence(self) -> None:
        """Remove the fence polygon (a commissioning mistake to simulate)."""
        self.link.mav.mission_clear_all_send(*self.target, FENCE)
        self.link.recv_match(type="MISSION_ACK", blocking=True, timeout=3)

    # ---- parameters (simulator fault injection only) --------------------------

    def get_param(self, name: str, timeout: float = 3.0) -> float | None:
        self.link.mav.param_request_read_send(*self.target, name.encode(), -1)
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self.link.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
            if message is not None and message.param_id.strip("\x00") == name:
                return float(message.param_value)
        return None

    def set_param(self, name: str, value: float) -> None:
        self.link.mav.param_set_send(*self.target, name.encode(), float(value), 9)
        deadline = time.time() + 3
        while time.time() < deadline:
            message = self.link.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
            if message is not None and message.param_id.strip("\x00") == name:
                return
        raise TimeoutError(f"{name} not confirmed")

    # ---- mission protocol upload ----------------------------------------------

    def upload(self, items: list[dict[str, Any]], mission_type: int, attempts: int = 3) -> None:
        """Mission protocol upload. A slow round trip (WSL, a reboot, SIM_SPEEDUP)
        can make the flight controller re-request an item we already answered; the
        duplicate then arrives out of order and is refused with
        MAV_MISSION_INVALID_SEQUENCE (13). Like a ground station, start the upload
        again (bounded); any other refusal is final. Success still needs the FC's
        own MAV_MISSION_ACCEPTED."""
        requested: list[int] = []
        for attempt in range(attempts):
            while self.link.recv_match(type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"], blocking=False):
                pass  # stale replies from an earlier exchange would be answered out of order
            result = self._upload_once(items, mission_type, requested)
            if result == 0:
                return
            if result != 13:
                raise RuntimeError(f"upload rejected (MAV_MISSION_RESULT {result}; item requests {requested[-12:]})")
        raise RuntimeError(f"upload rejected (MAV_MISSION_RESULT 13 after {attempts} attempts; item requests {requested[-12:]})")

    def _upload_once(self, items: list[dict[str, Any]], mission_type: int, requested: list[int]) -> int:
        self.link.mav.mission_count_send(*self.target, len(items), mission_type)
        deadline = time.time() + 15
        while time.time() < deadline:
            message = self.link.recv_match(type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"], blocking=True, timeout=1)
            if message is None or getattr(message, "mission_type", 0) != mission_type:
                continue
            if message.get_type() == "MISSION_ACK":
                return int(message.type)
            requested.append(int(message.seq))
            item = items[message.seq]
            p = [0.0 if v is None or (isinstance(v, float) and math.isnan(v)) else float(v) for v in item["params"]]
            self.link.mav.mission_item_int_send(*self.target, message.seq, item["frame"], item["command"], 0, 1,
                                                p[0], p[1], p[2], p[3], int(p[4] * 1e7), int(p[5] * 1e7), p[6], mission_type)
        raise TimeoutError("mission upload timed out")

    def load_fence(self, plan: dict[str, Any]) -> int:
        items = []
        for polygon in plan["geoFence"]["polygons"]:
            command = 5001 if polygon["inclusion"] else 5002
            for lat, lon in polygon["polygon"]:
                items.append({"command": command, "frame": 0, "params": [len(polygon["polygon"]), 0, 0, 0, lat, lon, 0]})
        self.upload(items, FENCE)
        return len(items)

    def load_mission(self, plan: dict[str, Any]) -> int:
        home = plan["mission"]["plannedHomePosition"]
        items = [{"command": 16, "frame": 0, "params": [0, 0, 0, 0, home[0], home[1], home[2]]}]
        items += [{"command": item["command"], "frame": item["frame"], "params": item["params"]} for item in plan["mission"]["items"]]
        self.upload(items, MISSION)
        return len(items) - 1

    # ---- the pilot's "start mission" -----------------------------------------

    def command(self, command: int, *params: float, timeout: float = 5.0) -> int:
        values = list(params) + [0.0] * (7 - len(params))
        self.link.mav.command_long_send(*self.target, command, 0, *values)
        deadline = time.time() + timeout
        while time.time() < deadline:
            ack = self.link.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.5)
            if ack is not None and ack.command == command:
                return int(ack.result)
        raise TimeoutError(f"no acknowledgement for command {command}")

    def start_mission(self, ready_timeout: float = 90.0) -> None:
        """AUTO mode, arm (retrying until pre-arm checks pass), start."""
        if self.command(176, 1, 3) != 0:  # MAV_CMD_DO_SET_MODE AUTO
            raise RuntimeError("AUTO mode refused")
        deadline = time.time() + ready_timeout
        while self.command(400, 1) != 0:  # arm
            if time.time() > deadline:
                raise RuntimeError("arming refused (pre-arm checks)")
            time.sleep(2)
        if self.command(300, 0, 0) not in (0, 5):  # MAV_CMD_MISSION_START
            raise RuntimeError("mission start refused")

    # ---- what a pilot does by hand (fault scenarios) -------------------------

    def set_mode(self, name: str) -> int:
        from sentinel.drone.ardupilot import COPTER_MODE_IDS

        return self.command(176, 1, COPTER_MODE_IDS[name])

    def guided_takeoff(self, altitude_m: float, ready_timeout: float = 90.0) -> None:
        if self.set_mode("GUIDED") != 0:
            raise RuntimeError("GUIDED refused")
        deadline = time.time() + ready_timeout
        while self.command(400, 1) != 0:
            if time.time() > deadline:
                raise RuntimeError("arming refused (pre-arm checks)")
            time.sleep(2)
        if self.command(22, 0, 0, 0, 0, 0, 0, altitude_m) not in (0, 5):
            raise RuntimeError("take-off refused")

    def goto(self, lat: float, lon: float, altitude_m: float) -> None:
        """Fly in GUIDED to a point (relative altitude), as a pilot clicking on the map."""
        self.link.mav.set_position_target_global_int_send(0, *self.target, 6, 0b110111111000, int(lat * 1e7), int(lon * 1e7), altitude_m,
                                                         0, 0, 0, 0, 0, 0, 0, 0)

    def sticks(self, roll: int = 0, pitch: int = 0, throttle: int = 0, seconds: float = 1.0) -> None:
        """The pilot's sticks (RC_CHANNELS_OVERRIDE, PWM; 0 = release to the radio). Sent at 10 Hz for
        `seconds`, as ArduPilot drops an override that is not refreshed."""
        deadline = time.time() + seconds
        while True:
            self.link.mav.rc_channels_override_send(*self.target, roll, pitch, throttle, 0, 0, 0, 0, 0)
            if time.time() >= deadline:
                return
            time.sleep(0.1)

    def set_home(self, lat: float, lon: float, altitude_amsl_m: float) -> int:
        return self.command(179, 0, 0, 0, 0, lat, lon, altitude_amsl_m)  # MAV_CMD_DO_SET_HOME

    def reboot(self) -> int:
        """MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN. SITL restarts its process with the same arguments (-w wipes stored settings)."""
        return self.command(246, 1)

    def request_message(self, message_id: int) -> int:
        return self.command(512, message_id)

    def setup_signing(self, key: bytes | None) -> None:
        """MAVLink 2 signing, as the pilot's ground station sets it up (disarmed only). None removes it:
        an all-zero key AND timestamp, sent signed with the current key."""
        secret = key or bytes(32)
        # ArduPilot disables signing only when both stored fields are zero.
        stamp = int((time.time() - 1420070400) * 1e5) if key else 0
        self.link.mav.setup_signing_send(*self.target, secret, stamp)
        time.sleep(1.0)
        if key:
            self.link.setup_signing(key, sign_outgoing=True)
        else:
            self.link.disable_signing()

    def swap_battery(self) -> None:
        """A charged battery: MAV_CMD_BATTERY_RESET (all instances, 100%), as after a manual swap."""
        if self.command(42651, 255, 100) != 0:
            raise RuntimeError("battery reset refused")

    def mode(self) -> str:
        message = self.link.recv_match(type="HEARTBEAT", blocking=True, timeout=3)
        return self.mavutil.mode_string_v10(message) if message else "unknown"

    def close(self) -> None:
        self._heartbeats.set()
        self.link.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["commission", "fly"])
    parser.add_argument("plan")
    parser.add_argument("--connection", default="tcp:127.0.0.1:5763")
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    station = SitlGroundStation(args.connection)
    if args.action == "commission":
        print(f"fence loaded: {station.load_fence(plan)} vertices")
    else:
        print(f"mission loaded: {station.load_mission(plan)} items")
        station.start_mission()
        print("mission started")
    station.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
