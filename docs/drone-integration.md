# Drone integration

The drone is Sentinel's **mobile camera, not its brain**. Sentinel decides *where a look would help* and asks a person. A certified flight stack decides *how to fly*.

## Current state

| Piece | Status |
| --- | --- |
| `DroneAdapter` interface (`drone/base.py`) | Built: connect, telemetry, takeoff, goto, hold, return_to_home, land, step |
| `SimulatedDrone` (`drone/mock.py`) | Built: kinematic model, battery drain and charging, dock, RTL on link loss |
| `Geofence` (`drone/geofence.py`) | Built: inclusion polygon, exclusion polygons, min/max altitude |
| `DroneManager` (`drone/manager.py`) | Built: inspection requests, operator approval or denial, request expiry, preflight checks, manual commands, safety monitor, audit trail |
| `MavlinkTelemetryAdapter` (`drone/mavlink.py`) | Telemetry only (heartbeat, battery, GPS, position). **All flight commands raise `FlightCommandRejected`.** |
| Dashboard Drone tab and API | Built |
| Live drone video | Interface field only (`telemetry.video_url`). Not ingested yet |

## Registration (installation)

Drones are registered as devices (`devices[]`, category `drone`) from the Install tab or `POST /api/v1/devices` ([installation.md](./installation.md)):
- `simulated_drone` gets the simulator; `mavlink_drone` gets the telemetry-only adapter (needs `connection`); planned Sentinel drones get **no adapter**, so they can never appear connected or ready.
- Several drones may be registered. The primary (`drone.device`, else the first operable drone) takes inspection requests and drives the Drone tab; the others report telemetry and readiness only.
- A drone's `map` position is its home (dock). The installer creates a geofence over the property rectangle (editable), and `drone.inspection_points` lists named places an operator can request an inspection of (`point_id`).
- Drone settings changed while a drone is airborne or on a mission are applied only after landing.
- A config without registered drones keeps working with the single legacy `drone` section.
- Removing the last registered drone turns drone features off.

## Inspection workflow

1. A request is created by an operator (dashboard or API), or automatically by Sentinel when an incident reaches `drone.auto_request_min_severity`. The target is the centroid of the incident zone's `map_polygon`.
2. The target is checked against the geofence and `inspection_altitude_m`, and rejected if it violates either.
3. The request waits in `pending_approval`. It expires after `request_ttl_s`. **Sentinel never approves its own request.**
4. The operator approves it. Preflight requires: link up, battery ≥ `min_launch_battery_pct`, 3D GPS fix, not already flying, safety state `ok`.
5. The mission runs: takeoff → fly to target → hover `hover_s` → return to home → land and dock. Each phase is audited.
6. The operator can hold, return home, or land at any time. Return home or land aborts the mission.

## Safety monitor (every 0.5 s)

| Condition | Sentinel action | Flight-controller responsibility |
| --- | --- | --- |
| Battery ≤ `rth_battery_pct` | Command return-to-home, abort mission | FC battery failsafe (ArduPilot `BATT_FS_*`) |
| Battery ≤ `land_battery_pct` | Command land | FC critical battery failsafe |
| Position outside geofence | Command return-to-home | FC fence (`FENCE_ENABLE`, `FENCE_TYPE`, `FENCE_ACTION`) is authoritative |
| Link lost | Report `link_lost`, block new commands | FC GCS-loss failsafe (`FS_GCS_ENABLE`), RTL |

Sentinel's checks are a second layer. The flight controller's own failsafes must be configured and tested independently, because Sentinel can crash, lose network, or be wrong.

## Path to real hardware (gates, in order)

Do not skip gates. Record completion of each in `ROADMAP.md`.

1. **ArduPilot SITL.** Run `sim_vehicle.py -v ArduCopter` and point `drone.adapter: "mavlink"`, `drone.connection: "udp:127.0.0.1:14550"` at it. Verify the telemetry tiles and map position. Commands stay disabled.
2. **Command implementation against SITL only.** Implement commands with `pymavlink` (LGPL, used unmodified as a library) or MAVSDK-Python (BSD-3): `MAV_CMD_NAV_TAKEOFF`, `SET_POSITION_TARGET_GLOBAL_INT` in GUIDED mode, `MAV_CMD_NAV_RETURN_TO_LAUNCH`, `MAV_CMD_NAV_LAND`. Gate them behind an explicit `allow_flight_commands` config flag, plus a SITL-only connection check, plus tests against SITL.
3. **Failsafe validation in SITL.** Inject link loss, battery depletion, and fence breaches. Confirm FC-side behaviour without Sentinel's help.
4. **Bench and tethered testing** of the physical aircraft with props off, then tethered, with a certified pilot holding RC override.
5. **Regulatory review.** Check local rules (for example Transport Canada RPAS, FAA Part 107, EASA) for flights over people and property, beyond visual line of sight, night operations, and automated docking. Most jurisdictions require visual line of sight and a certified pilot. Unattended dock-launched operations usually need special authorization.
6. **Supervised field trials:** a pilot in command present for every flight, with geofence and altitude limits configured on the FC.

## Hardware notes (future)

- Flight controller: any ArduPilot or PX4 board (Pixhawk-class). Companion link over MAVLink (serial, UDP, or MAVLink router).
- Video: an RTSP/WebRTC stream from the companion computer or camera gimbal. Ingest it as an ordinary Sentinel camera source (`type: "rtsp"`) tagged as mobile, so detections from the air flow through the same engine.
- Dock: report `docked`/`charging` through the adapter. Off-the-shelf docks expose state through vendor SDKs, so wrap them in a separate adapter rather than coupling them to the flight adapter.

## What not to build

- Custom flight controllers, attitude or position control, or landing algorithms.
- Autonomous launch without a person approving the specific flight.
- Following, pursuing, or targeting people. Inspections are of places (zones), not of individuals.
