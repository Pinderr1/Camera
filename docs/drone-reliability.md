# Drone reliability: failure handling and the SITL fault scenarios

Status, 2026-10-14:
- Every result here comes from **ArduPilot SITL** (the official ArduCopter 4.7.2 prebuilt binary) or from offline unit tests.
- **No physical aircraft has been tested.**
- Physical aircraft remain telemetry-only. Sentinel proposes, the pilot flies, the flight controller's failsafes act.

## 1. What Sentinel checks during a flight (`drone/flight_health.py`)

The flight controller owns every response to a failure. Sentinel decides what it may **claim** and what it must **tell people**.

| Condition | Derived from | In flight |
| --- | --- | --- |
| `link_lost`, `link_unstable`, `link_delayed` | Heartbeat age; link drops per minute; added latency from the FC boot clock against arrival time | critical / warn / critical |
| `position_unknown`, `position_stale`, `position_jump` | (0,0) or out-of-range positions are rejected and counted; position age; a step larger than 40 m/s plus 10 m | critical |
| `gps_no_fix`, `gps_degraded`, `gps_stale` | GPS_RAW_INT fix, satellites, HDOP, report age | critical / warn / critical |
| `ekf_unhealthy`, `ekf_stale` | EKF_STATUS_REPORT flags and age | critical / warn |
| `home_mismatch`, `home_unknown` | FC home against the configured home (25 m) | warn |
| `battery_unknown`, `battery_stale`, `battery_frozen` | No reading or an implausible one (rejected and counted); report age; no change for 90 s in flight | critical / critical / warn |
| `battery_fast_drain`, `battery_return_now` | Observed %/min against the model's estimate; minutes to the reserve against the time home | warn / critical |
| `battery_failsafe` | The FC's own verdict (`BATTERY_STATUS.charge_state`), shown even when the percentage looks fine | critical |
| `fc_restarted`, `vehicle_emergency`, `other_vehicle` | Boot clock confirmed going back (three messages); MAV_STATE; a second autopilot on the link | critical / critical / warn |
| `gimbal_stale`, `wind_over_limit` | Gimbal report age; the FC's wind estimate against the model's limit | warn |

**Return-to-home is never assumed.** `return_home_possible()` is false without a 3D fix, with an unhealthy EKF, or with a stale position. In that state Sentinel never says "returning home": ArduPilot's EKF failsafe lands or holds altitude, and the pilot may have to fly manually.

## 2. MAVLink adapter hardening (`drone/mavlink.py`)

- **Invalid positions.** (0,0) and out-of-range coordinates are rejected; the last good position ages instead. Implausible jumps are flagged but still shown.
- **Implausible batteries.** Battery percentages above 100 % and voltages outside 3–62 V are rejected and counted.
- **Gimbal quaternions.** NaN or unnormalised quaternions are ignored. Every signal has its own timestamp (`gps_ms`, `battery_ms`, `gimbal_ms`, `ekf_ms`).
- **Flight-controller restarts.** A boot clock that goes back and stays back for three messages means a restart. Sentinel then discards the parameters and fence it audited, clears home, and asks again.
  - A single late packet (a reordering or catching-up link) is ignored. Found in SITL: the first version believed one stray packet.
- **Other autopilots on the link** are reported and never mixed into the aircraft's state.
- **Acknowledgements** addressed to the pilot's ground station on a shared radio are never taken as answers to the Hub.
- **Fence downloads** never overlap (they share one queue).
- **Stream rates.** `REQUEST_DATA_STREAM` goes to multicopters only. ArduCopter 4.5–4.7 keeps those rates in RAM; ArduPlane saves them as parameters (`persist_streamrates()`), and Sentinel never writes parameters.
- **MAVLink signing** is detected from the aircraft's frames (`link_signed`). The lockout check accepts signing as a lockout: the Hub holds no key.
- **Command-path probe** (`probe_command_path`): one read-only request (`MAV_CMD_REQUEST_CAMERA_INFORMATION`). Any acknowledgement proves the Hub's commands reach the flight controller. It is used by the bench test and the SITL scenarios.

## 3. Lockout findings (verified in ArduPilot source and SITL)

- **`MAV_OPTIONS` bit 0** makes the FC drop every packet from system ids other than `MAV_GCS_SYSID` (`accept_packet`).
  - **It trusts a system id.** A compromised device on the same network can claim id 255. The lockout check now says so.
  - **ArduPilot routes packets before that check** (`raw_packetReceived` → `routing.check_and_forward`). Hub packets addressed to *another MAVLink device on another FC port* would still be forwarded. The Lite has no such device: its SIYI gimbal speaks the SIYI serial protocol. **Adding a MAVLink gimbal or companion on another port changes this.**
- **MAVLink signing.** Once the pilot's ground station sets a key, ArduPilot signs every link and rejects unsigned packets, except on channel 0 (USB) and RADIO/RADIO_STATUS (`GCS_Signing.cpp`).
  - This is a cryptographic lockout the Hub cannot imitate.
  - **Run the bench lockout check over a radio port, never USB.**
- **Companion relay.**
  - Previously, **any device on the network could send unfiltered packets through the relay's ground-station socket.** It now accepts datagrams only from the configured Hub and ground-station addresses (IP and port), and refuses wildcard peers.
  - The provisioning default `udp:0.0.0.0:14550` sent the pilot's telemetry to the companion itself. It was removed: ground stations must be named (`--gcs`).

## 4. Drone video (`drone/video.py`, `drone/observation.py`)

- **A frozen or blank picture is never coverage.**
  - Every frame is judged by `VideoQualityMonitor`. Sensor noise or change proves a frame live. An identical picture for 1.5 s is frozen. A uniform frame is blank.
  - Frozen and blank frames are not analysed. Proven-live frames over the inspection point make up `on_station_s`.
  - The summary states the coverage (`none` / `partial` / `full`). It never says "did not detect anything" without live video of the point.
- **Before a flight**, the camera stream is checked while the aircraft is powered: 3 proven-live frames, every 60 s, frames discarded. The result is `ready`, `unreachable` or `unverified`.
- **The gimbal angle** is used only while fresh (2 s) and not slewing (>20°/s). No camera pose without a 3D GPS fix.
- **Contained failures.**
  - Evidence writes that fail (disk, database) are counted in the summary, never as empty evidence paths.
  - A worker crash mid-inspection continues the same observation record instead of starting over.

## 5. The fault-injection framework (`drone/faults.py`, `scripts/sitl/fault_scenarios.py`)

- **Each scenario states up front:** initial conditions, injected failure, expected aircraft behaviour, expected Sentinel behaviour.
- **The runner records a timeline** of state transitions from real telemetry (flight state, mode, safety state, request status, health conditions, advisories, restarts), evaluates explicit checks, and writes `report.md` and `results.json`.
- **Verdicts:**
  - **pass** only when every check passed;
  - a scenario that makes no checks is an **error**, because finishing proves nothing;
  - **not modelled** where SITL cannot represent the fault faithfully (with the reason).
- **Where faults are injected:**
  - SITL `SIM_*` parameters: GPS, satellites, glitch, battery voltage, wind;
  - `FaultLink`: the emulated radio, with loss, delay (first in, first out), duplicates, filtered message types, a cut on either side, injected frames;
  - `FaultyVideoSource`: freeze, blank, disconnect, slow;
  - the pilot's ground station: modes, home, reboot, signing, fence edits.
- **Between scenarios**, SITL is restored. A vehicle away from home, or after a sensor fault, is reset by a SITL reboot to `--home`.
- **Run it** on a separate instance so another session's simulator is untouched:

```bash
bash scripts/sitl/start_sitl_instance.sh 2 3 config/drone/sentinel-lite-sitl.parm          # WSL; ports 5780-5783, never kills other SITLs
python scripts/sitl/fault_scenarios.py --fc tcp:127.0.0.1:5782 --out bench_runs/faults_lite
# with the simulated SIYI gimbal (ArduPilot's real SIYI driver):
SITL_EXTRA_ARGS=--serial5=sim:siyi_zt30 bash scripts/sitl/start_sitl_instance.sh 2 3 \
    config/drone/sentinel-lite-sitl.parm,config/drone/sentinel-lite-gimbal-sitl.parm
```

### Things learned about SITL while building the scenarios

- **SITL keeps `SIM_*` parameters across a reboot.**
  - A 10.3 V battery survived one and left the low-voltage failsafe latched ("PreArm: Battery 1 low voltage failsafe").
  - Restores therefore put every changed parameter back first, then reboot to clear latched failsafes.
- **ArduCopter ignores GUIDED position targets while its GUIDED take-off is still running.** A ground station must wait for the take-off to finish, or re-send the target.
- **Simulator speed-up breaks wall-clock latency measurement.** At speed-up 3 the boot clock runs three times faster than the Hub's, so the added-delay estimate only works at speed-up 1 (the `telemetry_delayed` scenario sets it).
  - This is also how the false-restart bug was found: frames queued behind a delay arrived after fresh ones, and their 7.5 s older boot times looked like a restart.
- **A host that sleeps** (the laptop at night) stops SITL's clock and every timer. Runs that span a sleep are discarded.
- **SITL's battery voltage barely sags with use.** With voltage-only thresholds the flight controller kept flying while its capacity count, which Sentinel shows as %, was nearly empty. Hence the new `BATT_LOW_MAH` audit recommendation.

## 6. Results

Sentinel Lite parameters on ArduCopter 4.7.2 SITL. The latest run of each scenario, from `bench_runs/recovery_20261015/<group>/results.json` and, for the last three, `bench_runs/v1freeze_20261015/` (both ignored by git). 48 scenarios are defined.

| Outcome | Count | Scenarios |
| --- | --- | --- |
| pass | 45 | GPS and home (6), communications (9: delay, radio loss, intermittent link, packet loss, duplicates, Hub restart, pilot GCS lost, FC restart on the ground, second autopilot), geofence (8), battery (6, incl. `return_interrupted_by_battery`), security (2: unauthorized Hub commands, signing lockout), video (7, incl. gimbal angle and evidence storage), operations (7: pilot cancels, approval expires, drone unavailable, overlapping requests, strong wind, emergency landing midway, database interrupted in flight) |
| not modelled (by design) | 3 | `companion_power_loss`: needs Pro/Thermal commissioning, and is the Hub's radio-loss case. `fc_restart_in_flight`: ArduCopter refuses a reboot while armed, and SITL cannot fly across a process restart. `camera_latency`: the RTSP reader exposes no capture time. All three are hardware bench items |

The last three were each run alone on 2026-10-15 with a per-scenario deadline (`--scenario-timeout`) and an outer process kill:

- **`strong_wind`.** Earlier runs never reached a verdict. Investigation found three problems:
  - The Lite commissioning has no drag fusion, so ArduCopter streams an exact-zero WIND vector. Sentinel showed that as "0 m/s", a false calm reading; it now treats it as no estimate (`mavlink.apply_mavlink_message`).
  - SITL's default wind profile (`SIM_WIND_T 0`, `SIM_WIND_T_ALT 60`) scales the wind by the square root of height: 11 m/s becomes about 4.9 m/s at 12 m. The FC's estimate of 5.3 m/s was right, and under the 6 m/s limit.
  - Restoration landed before removing the wind. Constant 11 m/s gusts blew the aircraft out of the fence, and on the ground it never disarmed.

  The scenario now has two flights:
  - Lite commissioning in 4 m/s: no estimate is shown, and no warning is invented.
  - ArduPilot's own SITL drag coefficients (`EK3_DRAG_BCOEF_X/Y 9.5`, `EK3_DRAG_MCOEF 0.082`), set on the ground before a restart, then constant 8 m/s: the estimate rose past 6.4 m/s and Sentinel warned, wording it as an estimate.

  **Hardware requirement:** a real Lite reports no in-flight wind until drag coefficients are measured for its airframe. Until then the operator's attested wind is the only wind check.
- **`emergency_landing_midway`.** LAND is now selected by position, 16 m short of the shed. A wall-clock pause at speed-up 3 had reached the shed first. Sentinel's verdict: aborted, "landed before reaching the inspection point"; the flight was logged and closed.
- **`database_interrupted_in_flight`.** With the store closed en route, the safety loop raised no errors, the flight was followed, and the inspection completed.
  - Record recovery was then checked separately: a transient outage (writes refused, then working again) left a stale `in_progress` inspection and an open flight in the database. A restart would have mislabelled that inspection as interrupted.
  - Two causes:
    - `EventStore._write` reports a failed write by returning `False`, but the drone upserts dropped that result, so the manager never counted the failure.
    - Nothing rewrote the row once the database recovered.
  - Now the drone upserts return the result. The manager keeps the latest unwritten version of each flight, inspection or maintenance row (at most 200) and retries every 5 s; readiness shows how many rows are waiting.
  - Audit entries are append-only and timestamped at write time, so they are not replayed. A failed audit write stays disclosed as "records may be incomplete" (`tests/test_sentinel_drone_manager_safety.py`).
  - **Known limits of the recovery buffer:**
    - It lives in memory only. If Sentinel restarts or crashes before the database recovers, the waiting rows are lost, and startup recovery then sees the last row that was actually written. A stale `in_progress` inspection would be marked interrupted, and the flight closed at the restart time.
    - It holds at most 200 records. Updates to records already waiting are always kept; new records beyond 200 are dropped and only counted.
    - It retries at most every 5 s, only while the drone loop runs.
    - It was tested with simulated outages (refused statements, closed store), not with a real full disk or a locked file on the target hardware.

Harness fix: mission uploads restart (bounded) after `MAV_MISSION_INVALID_SEQUENCE` (13) from duplicated item requests after a reboot; any other refusal stays final.

Earlier failing attempts (for example `critical-battery-before`, `radio`, `video-primary`) are kept next to the passing reruns. A pass here is evidence about Sentinel's handling of simulated firmware behaviour, not about a physical aircraft.
