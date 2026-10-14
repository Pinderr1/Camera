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

    def upload(self, items: list[dict[str, Any]], mission_type: int) -> None:
        self.link.mav.mission_count_send(*self.target, len(items), mission_type)
        deadline = time.time() + 15
        while time.time() < deadline:
            message = self.link.recv_match(type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"], blocking=True, timeout=1)
            if message is None or getattr(message, "mission_type", 0) != mission_type:
                continue
            if message.get_type() == "MISSION_ACK":
                if message.type != 0:
                    raise RuntimeError(f"upload rejected (MAV_MISSION_RESULT {message.type})")
                return
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

    def swap_battery(self) -> None:
        """A charged battery: MAV_CMD_BATTERY_RESET (all instances, 100%), as after a manual swap."""
        if self.command(42651, 255, 100) != 0:
            raise RuntimeError("battery reset refused")

    def mode(self) -> str:
        message = self.link.recv_match(type="HEARTBEAT", blocking=True, timeout=3)
        return self.mavutil.mode_string_v10(message) if message else "unknown"

    def close(self) -> None:
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
