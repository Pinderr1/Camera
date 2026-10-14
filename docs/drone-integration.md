# Drone integration

The drone is Sentinel's **mobile camera, not its brain**:
- Sentinel decides *where a look would help* and asks a person.
- A person approves.
- The pilot in command flies with a certified flight stack (ArduPilot), whose failsafes are authoritative.

Hardware choice, prototype plan, bill of materials and Canadian rules are in [`docs/drone-hardware-v1.md`](drone-hardware-v1.md).

## Current state (2026-10-14)

| Piece | Status |
| --- | --- |
| `DroneAdapter` (`drone/base.py`) | Built: telemetry, commands, `commands_allowed()`, `parameters()`, `fence_polygon()` |
| `SimulatedDrone` (`drone/mock.py`) | Built: kinematic stand-in (no physics) |
| `MavlinkTelemetryAdapter` (`drone/mavlink.py`) | Built and **tested against ArduPilot SITL** (ArduCopter 4.7.2): reconnect with backoff, Hub ground-station heartbeat, stream requests, full parameter download (1,369 in about 3 s), read-only fence download, gimbal attitude, STATUSTEXT. **Every flight command is refused** |
| `SitlMavlinkAdapter` | Built and SITL-tested: GUIDED take-off, go-to, hold, RTL, land, **only** with `allow_flight_commands`, a loopback endpoint, and SIM_* parameters proving a simulator |
| Parameter audit (`drone/ardupilot.py`) | Built: failsafes, fence, RTL altitude, arming checks, FC fence polygon inside Sentinel's geofence, changes since commissioning. Understands ArduCopter 4.7 renames |
| Routes and exports (`drone/mission.py`) | Built: QGroundControl `.plan` and `QGC WPL 110` `.waypoints`, fence items. Loaded into SITL by a ground station and flown |
| `DroneManager` (`drone/manager.py`) | Built: propose/command modes, 15 readiness checks, approval window, telemetry-driven phases, safety monitor (commands for simulators, advisories for real aircraft), flight log, maintenance and commissioning records, persistence and restart recovery |
| Companion (`sentinel/companion/`) | Built: MAVLink relay with a CRC-checked command filter, health agent, pairing CLI, Pi provisioning files. Relay and agent tested; **not yet run on a Pi** |
| Companion identity (`drone/pairing.py`) | Built: one-time pairing codes, per-device secret in the credential store, HMAC sessions with replay protection, revocation |
| Drone video and observations (`drone/video.py`, `drone/observation.py`) | Built: RTSP/file/simulated-aerial sources, detection on the Hub, tracking across frames, ground position with uncertainty, fingerprinted snapshots, incident explanation section. Tested with the simulated aerial camera posed by SITL telemetry. **No real aerial footage yet** |
| Dashboard Drone tab, API, health | Built: readiness, audit, conditions, approvals with route downloads, live drone camera, companion, flight log, maintenance. `drone_link` health component |
| Physical hardware | **None bought. No physical flight.** |

## Architecture

```text
 SENTINEL HUB (all AI)              DRONE COMPANION (relay)              FLIGHT CONTROLLER (flies)
 ┌───────────────────────────┐      ┌──────────────────────────────┐     ┌──────────────────────────┐
 │ incidents → inspection     │      │ sentinel.companion           │     │ ArduCopter (Pixhawk 6X)  │
 │ requests → operator        │ UDP  │  relay: FC ↔ Hub ↔ pilot GCS │UART │  stabilisation, nav      │
 │ approval → route export    │◄────►│  Hub→FC allowlist (telemetry │◄───►│  failsafes, fence, RTL   │
 │ MavlinkTelemetryAdapter    │      │  policy) — CRC-checked       │     │  RC override (ELRS)      │
 │ readiness + param audit    │ HTTP │  agent: pairing, HMAC        │     └──────────────────────────┘
 │ CompanionRegistry          │◄─────┤  sessions, health reports    │            ▲ pilot RC (independent)
 │ DroneVideoWorker (RTSP) ───┼──────┼─ MediaMTX / camera RTSP      │
 │ evidence → incident        │      └──────────────────────────────┘
 └───────────────────────────┘
```

Each layer can be replaced without rewriting the others:
- **Flight controllers** sit behind `DroneAdapter`: telemetry, `commands_allowed`, parameters and fence. PX4 would be another adapter; its parameter audit would need its own rules.
- **Companion computers** run plain Python with no hardware dependency. The relay can be swapped for mavlink-router with the same policy.
- **Cameras** sit behind the video source (`rtsp`, `http`, `file`, `simulated_aerial`) and `drone.video.camera` (field of view, default gimbal pitch).
- **Radios** are transparent: Sentinel only sees UDP and RTSP.

### Responsibilities

| Layer | Does | Never does |
| --- | --- | --- |
| Hub | Requests inspections from incidents; shows readiness; audits the FC; records approvals; exports routes; follows flights from telemetry; analyses drone video; attaches observations and evidence to incidents; logs flights and maintenance; notifies on lost links in flight | Fly a real aircraft; write FC parameters; merge drone observations with fixed-camera identities; feed drone video into zone rules |
| Companion | Relays MAVLink and video; drops Hub traffic that could change flight; reports its health; keeps relaying to the pilot when the Hub is unreachable | Run detection; act on the aircraft on its own |
| Flight controller | Stabilises and navigates; enforces fence and failsafes; obeys the pilot's RC | Depend on the Hub to stay safe |

## Operating modes

Each aircraft gets a mode, fixed at approval:

| Mode | When | What approval does |
| --- | --- | --- |
| `propose` | Any real or unverified aircraft (`mavlink` adapter, or `sitl` not yet verified) | Plans the route (vertical climb at home to the safe transit altitude, straight transit, hover, RTL), checks every leg against the geofence, and opens a window (`approval_window_s`, default 15 min) for the pilot to fly it. The pilot downloads the `.plan`/`.waypoints`, reviews it and starts it in their own ground station. Sentinel follows the flight from telemetry: started → en route → inspecting → returning → completed/aborted. A flight not started in time becomes `not_flown` |
| `command` | Simulators only: the built-in `SimulatedDrone`, or `SitlMavlinkAdapter` that passed all three checks | Sentinel flies the same route (GUIDED take-off, go-to, hover, RTL) |

Safety monitor (every 0.5 s):
- **Command mode:** returns home on low battery or a geofence breach, and lands on critical battery.
- **Propose mode:** shows an operator advisory instead ("Battery low: the pilot should return home now").
- **Link loss:** reported, recorded in the flight log and audit, and raised as the `drone_link` health component, notified only when it happens in flight. The FC's GCS failsafe acts on its own (verified in SITL).
- **Returns started by the FC** (failsafe) or by the pilot are recognised in either mode.

## Inspection workflow

1. **Request.** An incident at or above `drone.auto_request_min_severity` creates a request for the incident zone's map centroid, with the observed reason ("Person entered Shed (incident severity high)"). Operators can request zones, inspection points or coordinates. Targets outside the geofence are refused.
2. **Readiness**, all shown on the Drone tab:
   - link, battery, GPS (fix, HDOP, satellites), EKF, pre-arm checks (with the FC's own PreArm message);
   - on the ground with no other inspection open, safety state, home inside the fence, launch point;
   - FC settings audit, weather and conditions confirmed by the operator within the hour, companion reporting (if paired);
   - video, firmware tested, maintenance due.

   Failing checks block approval; warnings do not.
3. **Approval** by an operator. **Sentinel never approves its own requests.** Denials and expiries are recorded.
4. **Flight** in the aircraft's mode (above). Drone video is analysed only while the inspection is in progress.
5. **Result.** The request is stored with route, phases, flight id, observations and evidence paths. The incident's explanation gains a "Drone inspection" section beside, not merged into, the fixed cameras' story.
6. **Restart recovery:**
   - pending requests come back;
   - an approved-but-unflown request keeps its window;
   - an inspection that was flying is marked **interrupted** and never resumed;
   - an open flight log is closed as interrupted.

## Drone video and observations

- **Sources:** the companion's or camera's RTSP stream (credentials in the credential store), a recorded file, or `simulated_aerial`. The simulated camera renders scripted ground objects from the drone's live telemetry, from SITL or the built-in simulator.
- **Detection:** the site detector (scripted for the simulated camera). Tracking uses the IoU tracker in image space. While the camera moves, one object can split into several tracks, so summaries report **the most objects visible at the same time** (a lower bound) next to the track count.
- **Camera pose:** aircraft position, height and heading, plus the gimbal angle when the gimbal reports it (`GIMBAL_DEVICE_ATTITUDE_STATUS`, `MOUNT_STATUS`), otherwise the configured `default_pitch_deg`. Uncertainty combines GPS error (from HDOP), angle error (2° reported, 6° assumed), the barometric height error and flat-ground geometry. **No estimate** is made when:
  - the pose is older than 1 s;
  - the aircraft is below 2 m;
  - the ray is within 12° of the horizon;
  - the point is farther than 5× the height.

  The reason is stated instead.
- **Evidence:** a JPEG when each object is first confirmed, under `media/drone/<request>/`, SHA-256 fingerprinted. It is protected from purges while its incident is protected.
- **Language** (`observation.describe`):
  - behaviour only ("Person: up to 1 visible at the same time near Shed…");
  - an empty result "does not prove the area was empty";
  - every summary states that drone observations are not linked to people seen by the fixed cameras;
  - every summary ends with the standard disclaimer.

## Companion identity and the relay

- **Pairing:**
  1. The operator presses "Show pairing code" (one-time, 10 minutes).
  2. On the companion, run `python -m sentinel.companion pair --hub http://HUB:8080 --code ABCD-EFGH`.
  3. The companion receives a random 256-bit secret. The Hub keeps its copy in the credential store; the companion keeps its copy in a 0600 file (a secure element in the product).
  4. Ten failed attempts pause pairing for 10 minutes.
  5. Pairing a replacement revokes the previous companion.
- **Sessions:** HMAC over companion id, timestamp (±120 s) and a fresh nonce gives a 30-minute token for health reports. The agent re-opens its session after a Hub restart.
- **Relay policy `telemetry`** (physical aircraft): from the Hub, only CRC-valid frames pass:
  - HEARTBEAT, SYSTEM_TIME, TIMESYNC;
  - parameter and mission *reads*;
  - REQUEST_DATA_STREAM;
  - COMMAND_LONG with telemetry commands (message interval, request message).

  Arming, modes, take-off, position targets, parameter writes and mission uploads are dropped and counted. Pilot ground-station traffic passes unchanged.
- **Relay policy `sitl`:** everything passes. Allowed only with a loopback flight-controller endpoint.
- **MAVLink 2 signing:** `pairing.mavlink_signing_key(secret)` derives the key. **Not enforced yet**; it is a product requirement.

## Configuration

`drone` section (validated in `config.validate_config`; all optional except the existing fields):

| Key | Meaning |
| --- | --- |
| `adapter` | `simulated`, `mavlink` (telemetry only) or `sitl`; legacy single-drone sections use `connection` and `allow_flight_commands` too |
| `gcs_system_id` | The Hub's MAVLink system id (default 245; the pilot's ground station is usually 255) |
| `firmware_tested` | Version prefixes, for example `["4.6", "4.7"]` (a warning when the FC reports another) |
| `safety` | Existing keys, plus `safe_transit_altitude_m`, `approval_window_s`, `arrival_radius_m`, `max_hdop`, `min_satellites`, `inspect_every_flights` |
| `weather` | `require_attestation` (default: all but the built-in simulator), `max_wind_mps` 8, `min_temperature_c` −5, `max_temperature_c` 40, `conditions_ttl_s` 3600 |
| `video` | `source` (`rtsp`/`http`/`file`/`simulated_aerial` with `objects`), `camera` (`hfov_deg`, `vfov_deg`, `default_pitch_deg`), `fps`, `detector` |

Registered drone devices (`devices[]`) accept the same `adapter`, `connection`, `allow_flight_commands` (**rejected unless `adapter` is `sitl`**) and `video`. The `ardupilot_sitl` profile registers a SITL vehicle.

## Registration (installation)

Drones are registered as devices (`devices[]`, category `drone`) from the Install tab or `POST /api/v1/devices` ([installation.md](./installation.md)):
- `simulated_drone` gets the built-in simulator, `mavlink_drone` the telemetry-only adapter, and `ardupilot_sitl` the SITL adapter. Planned Sentinel drones get **no adapter**, so they can never appear connected or ready.
- Several drones may be registered. The primary (`drone.device`, else the first operable drone) takes inspection requests and drives the Drone tab; the others report telemetry and readiness only.
- A drone's `map` position is its home (dock). The installer creates a geofence over the property rectangle (editable). `drone.inspection_points` lists named places.
- Drone settings changed while a drone is airborne or on a mission are applied only after landing.
- A config without registered drones keeps working with the single legacy `drone` section. Removing the last registered drone turns drone features off.

## Running against ArduPilot SITL

```bash
# Linux or WSL2 (downloads the official ArduCopter SITL build once)
bash scripts/sitl/start_sitl.sh 3          # speed-up 3; Sentinel on tcp 5762, ground station on tcp 5763
```

```powershell
$env:PYTHONPATH='src'
$env:SENTINEL_SITL='1'; python -m unittest tests.test_sentinel_drone_sitl   # 6 tests, about 5 min
python scripts/drone_sitl_demo.py --out bench_runs/drone_demo              # end-to-end demo with a report
```

- `scripts/sitl/gcs_standin.py` stands in for the commissioning engineer and the pilot (loads Sentinel's fence and route, presses "start mission"). It refuses non-loopback links and vehicles without SIM_* parameters.
- From Windows, WSL forwards `127.0.0.1:5762`. SITL's SERIAL0 port does not work through that forwarder, which is why the launcher holds it locally.

### What was demonstrated (2026-10-14, ArduCopter 4.7.2 SITL)

`scripts/drone_sitl_demo.py` through the real service and API:
1. A simulated backyard camera saw a person enter the Shed; the incident opened at high severity.
2. Sentinel requested an inspection ("Person entered Shed").
3. A companion paired and reported. The commissioning stand-in loaded the fence from Sentinel's export; the audit passed and commissioning was recorded.
4. All 15 readiness checks passed. The operator approved in propose mode.
5. The pilot stand-in loaded the `.plan` and flew it in SITL: 25 s, 60 m, 15 m RTL altitude.
6. The drone video worker analysed 125 frames (all with a pose). It found a person in 23 of them, estimated 1.4 m from the inspection point (±3.3 m; scripted truth about 1.1–1.7 m), and saved a fingerprinted snapshot.
7. The incident explanation showed the drone section and evidence. The relay forwarded 55 Hub frames and dropped none.

SITL integration tests (`tests/test_sentinel_drone_sitl.py`, all passing):
- telemetry, parameters and audit;
- **the relay drops Hub flight commands** (a take-off from an adapter that believed it could command was refused, and the vehicle stayed disarmed);
- a pilot-flown proposed inspection;
- a Sentinel-commanded inspection in SITL;
- **link loss → the FC's own GCS failsafe returned home**;
- low battery → return home.

### What is still simulated

- The aircraft physics: SITL is real firmware with simulated sensors and a simulated battery.
- Both cameras, and the scripted detector on the drone camera.
- The pilot.
- The companion: run on the Hub, not on a Pi.
- The radio link: loopback UDP.

Nothing here has flown, and no real gimbal attitude, video latency, link range or wind has been measured.

## Path to real hardware (gates, in order)

Do not skip gates. Record completion of each in `ROADMAP.md`.

1. **ArduPilot SITL.** ✅ Done 2026-10-14:
   - telemetry, audit, fence reading, mission export flown by a ground station;
   - companion relay and pairing;
   - simulated drone video.
2. **Commands against SITL only.** ✅ Done: `SitlMavlinkAdapter` behind `allow_flight_commands`, a loopback check and SIM_* proof, with tests.
3. **Failsafe validation in SITL.** ✅ Link loss (FC GCS failsafe) and low battery done. Still to do: fence breach and EKF failure injection.
4. **Bench rig, then the full airframe, props off:** `docs/drone-hardware-v1.md` sections 8 and 14. Needs the owner's purchase approval.
5. **Regulatory review:** Transport Canada scenarios, Standard 922 declaration, insurance, privacy (hardware doc section 13).
6. **Tethered, then supervised field trials,** pilot-flown, with Sentinel observing and proposing.
7. **Sentinel-commanded flight on a real aircraft:** a separate decision after 4–6, never by changing the telemetry-only adapter in place.

## Dock

Manual charging and placement are fine for V1. Report `docked`/`charging` through the adapter when a pad reports state; wrap vendor docks in their own adapter. Unattended dock launches near people need an SFOC in Canada.

## What not to build

- Custom flight controllers, attitude or position control, or landing algorithms.
- Autonomous launch without a person approving the specific flight.
- Following, pursuing, or targeting people. Inspections are of places (zones), not of individuals.
- Our own obstacle avoidance. Use ArduPilot's avoidance with supported sensors after SITL tests, and avoid obstacles by design with a surveyed safe transit altitude.
- AI on the aircraft. The companion relays; detection runs on the Hub.
- Zone events from drone video, or identity links between drone and fixed-camera observations.
