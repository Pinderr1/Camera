#!/bin/bash
# Start a SEPARATE ArduPilot SITL (ArduCopter) instance for fault-injection
# runs, without touching any other SITL on this machine. Linux or WSL2.
#
#   bash scripts/sitl/start_sitl_instance.sh [INSTANCE] [SPEEDUP] [PARAMS]
#   bash scripts/sitl/start_sitl_instance.sh 2 3 config/drone/sentinel-lite-sitl.parm
#
# Instance N listens on tcp 5760+10N (SERIAL0, held by a local drain client),
# 5762+10N (SERIAL1: Sentinel or a relay) and 5763+10N (SERIAL2: the scripted
# ground station). Instance 2: 5780 / 5782 / 5783.
#
# Unlike start_sitl.sh this never runs `pkill`: it only stops the process it
# started. Its working directory ($SITL_DIR/instance-N) keeps its own eeprom.
# The binary is the official prebuilt ArduCopter SITL (GPLv3, a separate
# program, never linked into Sentinel), shared with start_sitl.sh.
set -euo pipefail
INSTANCE="${1:-2}"
SPEEDUP="${2:-3}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# One or more repo-relative parameter files, comma-separated, applied in order.
PARAMS=""
IFS=',' read -ra FILES <<< "${3:-config/drone/sentinel-lite-sitl.parm}"
for file in "${FILES[@]}"; do PARAMS="${PARAMS:+$PARAMS,}$REPO/$file"; done
SITL_DIR="${SITL_DIR:-$HOME/sentinel-sitl}"
HOME_POS="${SITL_HOME:-44.999964067553,-74.99992377576919,80,0}"
WORK="$SITL_DIR/instance-$INSTANCE"
BASE_PORT=$((5760 + 10 * INSTANCE))
mkdir -p "$SITL_DIR/bin" "$SITL_DIR/params" "$WORK"
if [ ! -x "$SITL_DIR/bin/arducopter" ]; then
  echo "downloading ArduCopter SITL (stable) from firmware.ardupilot.org"
  curl -sSfL -o "$SITL_DIR/bin/arducopter" https://firmware.ardupilot.org/Copter/stable/SITL_x86_64_linux_gnu/arducopter
  chmod +x "$SITL_DIR/bin/arducopter"
fi
if [ ! -f "$SITL_DIR/params/copter.parm" ]; then
  curl -sSfL -o "$SITL_DIR/params/copter.parm" https://raw.githubusercontent.com/ArduPilot/ardupilot/master/Tools/autotest/default_params/copter.parm
fi
if ss -ltn 2>/dev/null | grep -q ":$BASE_PORT "; then
  echo "port $BASE_PORT is in use: SITL instance $INSTANCE (or something else) is already running" >&2
  exit 2
fi
cd "$WORK"
LOG="$WORK/sitl.$(date +%Y%m%dT%H%M%S).log"
"$SITL_DIR/bin/arducopter" --model + --speedup "$SPEEDUP" -I"$INSTANCE" \
  --defaults "$SITL_DIR/params/copter.parm,$PARAMS" \
  --home "$HOME_POS" -w ${SITL_EXTRA_ARGS:-} < /dev/null > "$LOG" 2>&1 &
PID=$!
DRAIN_PID=""
cleanup() {
  kill "$PID" ${DRAIN_PID:+"$DRAIN_PID"} 2>/dev/null || true
  wait "$PID" ${DRAIN_PID:+"$DRAIN_PID"} 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# SERIAL0 drain client. It reconnects: a reboot command makes SITL re-execute
# itself, and the new process waits for a SERIAL0 client again.
BASE_PORT=$BASE_PORT SITL_PID=$PID python3 - <<'EOF' &
import os, socket, time
port, pid = int(os.environ["BASE_PORT"]), int(os.environ["SITL_PID"])
def alive():
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
while alive():
    try:
        s = socket.create_connection(("127.0.0.1", port))
    except OSError:
        time.sleep(0.1)
        continue
    while True:
        try:
            if not s.recv(65536):
                break
        except OSError:
            break
    s.close()
EOF
DRAIN_PID=$!
sleep 2
kill -0 "$PID" 2>/dev/null || { echo "SITL failed to start; see $LOG" >&2; exit 1; }
echo "ArduCopter SITL instance $INSTANCE running (pid $PID, speedup $SPEEDUP, parameters ${3:-config/drone/sentinel-lite-sitl.parm}). Sentinel: tcp:127.0.0.1:$((BASE_PORT + 2))  Ground station: tcp:127.0.0.1:$((BASE_PORT + 3))"
wait $PID
