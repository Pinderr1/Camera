#!/bin/bash
# Start ArduPilot SITL (ArduCopter) for Sentinel development. Linux or WSL2.
#
#   bash scripts/sitl/start_sitl.sh [SPEEDUP]
#   SITL_PARAMS=config/drone/sentinel-lite-sitl.parm bash scripts/sitl/start_sitl.sh 3   # Sentinel Lite commissioning
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
SITL_DIR="${SITL_DIR:-$HOME/sentinel-sitl}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOME_POS="${SITL_HOME:-44.999964067553,-74.99992377576919,80,0}"
PARAMS="$REPO/${SITL_PARAMS:-config/drone/sentinel-v1-sitl.parm}"
mkdir -p "$SITL_DIR/bin" "$SITL_DIR/params"
if [ ! -x "$SITL_DIR/bin/arducopter" ]; then
  echo "downloading ArduCopter SITL (stable) from firmware.ardupilot.org"
  curl -sSfL -o "$SITL_DIR/bin/arducopter" https://firmware.ardupilot.org/Copter/stable/SITL_x86_64_linux_gnu/arducopter
  chmod +x "$SITL_DIR/bin/arducopter"
fi
if [ ! -f "$SITL_DIR/params/copter.parm" ]; then
  curl -sSfL -o "$SITL_DIR/params/copter.parm" https://raw.githubusercontent.com/ArduPilot/ardupilot/master/Tools/autotest/default_params/copter.parm
fi
cd "$SITL_DIR"
pkill -f "$SITL_DIR/bin/arducopter" 2>/dev/null || true
"$SITL_DIR/bin/arducopter" --model + --speedup "$SPEEDUP" \
  --defaults "params/copter.parm,$PARAMS" \
  --home "$HOME_POS" -I0 -w < /dev/null > "$SITL_DIR/sitl.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT INT TERM
python3 - <<'EOF' &
import socket, time
for _ in range(100):
    try:
        s = socket.create_connection(("127.0.0.1", 5760)); break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("SITL did not open port 5760")
while True:
    try:
        if not s.recv(65536):
            break
    except OSError:
        break
EOF
sleep 2
echo "ArduCopter SITL running (pid $PID, speedup $SPEEDUP, parameters $(basename "$PARAMS")). Sentinel: tcp:127.0.0.1:5762  Ground station: tcp:127.0.0.1:5763"
wait $PID
