#!/bin/bash
# Start ArduPilot SITL (ArduCopter) for Sentinel development. Linux or WSL2.
#
#   bash scripts/sitl/start_sitl.sh [SPEEDUP]
#   SITL_PARAMS=config/drone/sentinel-lite-sitl.parm bash scripts/sitl/start_sitl.sh 3   # Sentinel Lite commissioning
#   SITL_PARAMS=config/drone/sentinel-thermal-sitl.parm SITL_EXTRA_ARGS=--serial5=sim:sf45b bash scripts/sitl/start_sitl.sh 3   # Thermal (simulated lidar)
#
# Uses the official prebuilt SITL binary from firmware.ardupilot.org (GPLv3;
# run as a separate program, never linked into Sentinel), downloaded once into
# $SITL_DIR. Home is the drone home of config/sentinel.example.json, and the
# Sentinel V1 commissioning parameters are applied on top of ArduPilot's
# copter defaults.
#
# Ports: SERIAL0 tcp 5760 is held by a local drain client (SITL waits for it);
# SERIAL1 tcp 5762 is for Sentinel or the companion relay; SERIAL2 tcp 5763 is
# free for a ground station (QGroundControl / Mission Planner). From Windows,
# connect to 127.0.0.1:5762 (WSL forwards localhost; SERIAL0 does not work
# through the forwarder).
set -euo pipefail
SPEEDUP="${1:-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Share the owned-process lifecycle and reconnecting SERIAL0 drain. Refuse an
# occupied instance rather than killing another session's simulator.
exec bash "$SCRIPT_DIR/start_sitl_instance.sh" 0 "$SPEEDUP" "${SITL_PARAMS:-config/drone/sentinel-v1-sitl.parm}"
