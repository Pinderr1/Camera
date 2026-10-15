# Installation and hardware management

The dashboard's **Install** tab takes a homeowner, business or installer from an empty Sentinel to a working installation, whichever way they start. It sits on top of the existing camera wizard, network troubleshooter, zone and rule editors, drone manager and health monitor; it does not replace them.

## The four installation paths

| Path | Starts with | What the journey adds |
| --- | --- | --- |
| **Connect existing cameras** | IP cameras, a DVR/NVR, analog cameras through a recorder or capture card, USB cameras | Network finder and troubleshooter up front, recorder registration, channel search, per-camera verification |
| **Install new cameras** | Nothing yet | A camera-count suggestion from a few questions, the property planner (placement, aim, coverage, PoE and cabling), new-camera troubleshooting |
| **Drone-first** | A hub plus a drone, or plus sensors | Property location, drone dock and observation points, drone registration and geofence. **No fixed camera is required** |
| **Complete Sentinel package** | Any mix, grown over time | All of the above; cameras, drones, docks and sensors managed together |

The path only changes the guidance and the checks that block completion. Any hardware can be added later, from any path.

## The journey

Ten steps, each saved as it happens (`<storage>/installation.json`), so the installation can stop and resume, also on another browser:

1. **Installation type.**
2. **Property:** name, property type, the size of the area to plan, and (for drones) the location. The location is stored locally only and is used for the drone geofence.
3. **Plan or discover:** the property planner, the camera suggestion, and for existing systems the network finder.
4. **Register and connect hardware:** add cameras (camera wizard), find and troubleshoot cameras and recorders, register recorders, drones, sensors and docks. Each device shows its compatibility status and its connection levels.
5. **Verify devices:** live previews and the per-device checks.
6. **Zones and privacy:** opens the zone and mask editor per camera; drone inspection locations.
7. **AI monitoring and rules:** opens the rule builder (every rule is still tested before it is turned on).
8. **Alerts and notifications:** ntfy and MQTT channels, the arm-mode push levels, and a **test notification**.
9. **System health check:** storage, database, backups, and the pipeline components.
10. **Finish:** the readiness summary, open issues, and completion.

Steps can be marked done or skipped and revisited in any order. A step's issues are counted next to it. Completion is refused while a **blocking** check fails (no installation type; no device that fits the path; a drone without a property location). Open warnings and non-blocking failures must be acknowledged, and are recorded with the completion. "Start over" resets the journey only; cameras, devices and settings are kept. Reopening a completed installation is allowed at any time.

## Readiness: what Sentinel will and will not say

Readiness is computed from live data on every request. It keeps four things apart:

| Level | Earned by |
| --- | --- |
| **Registered** | The device is in the configuration. Nothing more |
| **Connected** | Sentinel receives data from it: frames from a camera, telemetry from a drone, reports from a sensor |
| **Video available** | New frames are arriving from the camera |
| **AI monitoring verified** | The camera's health state is `monitoring`: changing frames are reaching a working detector (see [reliability.md](./reliability.md)). A still picture that cannot be proven live is `unverified`, not verified |

The statement at the top of the Install tab is the strongest claim Sentinel makes: "AI monitoring verified on N of M camera(s)". It never says a property is protected or secure. Other cases:
- No camera has passed: "Not monitoring yet".
- No cameras at all: "No fixed cameras: there is no continuous video monitoring." Drones are described as "ready for operator-approved inspections", and sensors as reporting (logged only).
- Camera processing not running in this session (for example `serve` without cameras): monitoring "cannot be checked".
- Simulated devices are always called out: they demonstrate the system but watch nothing real.

## Device registry

Cameras stay in `cameras[]` (the pipeline depends on them). Other hardware is registered in `devices[]`:

| Category | Registration fields | What Sentinel does with it today |
| --- | --- | --- |
| `recorder` | profile, `host`, `channels`, `model` | Groups its channels: cameras linked with `recorder`, or streaming from the recorder's address. Channels are found with the troubleshooter's channel search. No password is stored on the recorder entry |
| `drone` | `drone_model` (`sentinel_lite`, `sentinel_pro`, `sentinel_thermal`, `generic_ardupilot`), `drone_options` (the model's options, e.g. `obstacle_sensor`), profile (how Sentinel connects: `simulated_drone`, `ardupilot_sitl`, `mavlink_drone`), `adapter`, `connection`, write-only `video_url`, `indoor`, `map` (home position; defaults to the drone setup's home or the middle of the property) | Simulated drones get the simulator with the model's speed, endurance and camera; MAVLink drones get the telemetry-only adapter. Readiness follows the model (`docs/drone-models.md`). **Planned drones get no adapter and never appear connected** |
| `dock` | profile, `drone_id` | A simulated dock reports the simulated drone's docked/charging state |
| `contact_sensor`, `motion_sensor`, `environmental_sensor` | profile, `zone_id` | Simulated sensors report check-ins and battery; their Test button records an `info` event (`sensor_test`). **Sensors never open incidents in this build.** Real sensors (MQTT/Home Assistant) are planned |

This computer always appears as the hub (`hub_local`), with its CPU and GPU.

Removing a device unlinks it everywhere: cameras keep working without their recorder link, docks lose their drone, a removed primary drone is replaced by the next registered one, and removing the last drone turns drone features off (Sentinel never falls back to a drone nobody registered). Plan items keep their position and become "proposed" again. Removing a camera deletes its stored password, zones in rules (rules left without zones are disabled), and connections; recorded history is kept. A site may have no cameras.

### Hardware compatibility profiles

`devices.PROFILES` lists every kind of hardware with its protocols, codecs, power options, capabilities, notes and a **support level**:

| Support | Meaning |
| --- | --- |
| `verified` | Tested on physical hardware. Today only "this computer running Sentinel" |
| `simulated` | Works with Sentinel's simulator (no hardware) |
| `expected` | Should work through a protocol Sentinel implements (RTSP, ONVIF, HTTP snapshot, USB); not yet tested on that hardware |
| `limited` | Partly supported: for example Reolink (RTSP off by default; battery models cannot stream continuously) and MAVLink drones (telemetry only) |
| `planned` | Future products (Sentinel camera, hub, drones, dock; MQTT sensors). Registration only |
| `unsupported` | Cloud-only cameras with no local stream. They cannot be registered |

No prices and no unverified model specifications are recorded. Power entries name the standard (PoE 802.3af) or the typical option.

## Property planner

The planner draws on the property map, in metres (x east, y south), at the size set in step 2, over the uploaded map image if there is one.

- **Property features:** buildings, driveways, garages, yards, parking, fences, obstructions (trees, walls, hedges); doors, gates and garage doors; public areas (street, sidewalk) and privacy-sensitive areas; indoor rooms.
- **Equipment:** cameras (aim, field of view, optional range, indoor/outdoor, power: PoE, Wi-Fi, wired with adapter, battery; optional datasheet wattage; proposed or installed), sensors, network equipment (router, PoE switch, recorder, hub, access point, UPS), drone docks and drone observation points.
- Cameras already configured with a map position (Setup → Property map) are included in coverage and shown in grey.

### Coverage analysis

`planner.analyse_coverage` rasterises the map (at most 80 cells per side) and, for every cell, checks each camera's range, field of view and line of sight. Drawn buildings and obstructions block the view; indoor cameras cover drawn rooms only. It reports:
- the share of the open property area in view, and the share seen by two or more cameras;
- each door, gate and garage door, judged by its approach (about 2 m outside the wall, which is what a door camera sees), and the share of each driveway, garage, parking area and room in view;
- gaps (priority spots out of view, areas under 50% in view);
- privacy warnings where a camera's view reaches a public or privacy-sensitive area.

**Detection range.** When no range is entered, it is estimated as the distance at which a standing person fills about 10% of a 16:9 picture's height: about 15 m for a 90° lens, 11 m for 110°, 26 m for 60°. The 10% comes from Sentinel's benchmark, where smaller people were mostly missed (ROADMAP detector findings). It is not a camera specification.

Every analysis carries its assumptions: approximate positions, only drawn obstacles, no lighting, terrain or IR range, and "visual coverage is not detection, recording or alerting". The planner never presents map geometry as verified physical coverage.

### Camera suggestions

`planner.recommend` turns a few answers (doors, garage doors, driveway, gates, sides of the building, loading areas, indoor areas, wiring, sensors, drone) into lines such as "Exterior doors: 2, essential, wide lens", each with the reason, an approximate range, PoE ports and budget. Bathrooms, changing rooms and bedrooms are excluded by name; corner lots get a sidewalk-privacy note. It is labelled as a starting point, not a guarantee, and the suggested cameras can be dropped onto the plan to check placement.

### Equipment estimate

From the plan: PoE ports needed and a switch size that leaves a spare port; PoE budget (datasheet wattage where entered, otherwise 15.4 W per camera, the 802.3af per-port maximum); approximate cable runs from the nearest PoE switch or recorder (straight line × 1.25 + 5 m), with a warning above 90 m of fixed cable; power outlets for Wi-Fi cameras and docks; warnings for Wi-Fi cameras far from a router or access point, battery cameras, and too few PoE ports.

### Installation plan report

**Installation plan** (`GET /api/v1/installation/report.html`) is a self-contained, printable page: the plan with coverage, priority spots, gaps, privacy warnings, assumptions, suggested cameras, planned equipment, PoE and cabling, the device inventory with support levels, an installation checklist for the chosen path, and a power and network checklist. It is served with a restrictive Content-Security-Policy and escapes every name. `GET /api/v1/installation/report` returns the same data as JSON.

## Notifications from the installer

Step 8 edits the ntfy and MQTT channels. A typed ntfy topic goes to the credential store (`alerts.ntfy_topic`); the config keeps `topic_credential`, and the API only reports whether a topic is set. Channel changes apply without a restart. **Send test notification** sends a clearly labelled test through every enabled channel, bypassing arm mode and rate limits, and reports what each channel did. Sentinel can only see that the push service accepted the message, so the step asks the person to confirm it arrived on the phone.

## Troubleshooting during installation

- Existing systems: the network troubleshooter ([camera-setup.md](./camera-setup.md#network-troubleshooter)) opens from step 3 and step 4, from a camera that is not verified ("Troubleshoot" runs the diagnosis on its address), and from a recorder ("Find channels").
- New cameras: "Camera not showing up?" lists common PoE problems (no power, PoE budget, PoE+ needs, passive injectors, cable length, factory addresses on another range, cameras behind a recorder's PoE ports, activation), Wi-Fi problems (pairing through the maker's app, 2.4 GHz, guest networks and client isolation, signal) and login/codec problems. `GET /api/v1/onboarding/guidance/new-cameras`.
- Everything stays read-only: Sentinel explains what to change and never changes router, camera, computer or firewall settings.

## Drone-first installations

- A site may have zero cameras. The header then shows "No cameras", the Live tab says there is no continuous video monitoring, and health coverage is `no_cameras` (not an alarm).
- Registering the first drone turns drone features on with a geofence covering the property rectangle (5–25 m altitude, editable), using the property location from step 2. Planned observation points become inspection locations (`drone.inspection_points`); the Drone tab lists them with **Request inspection**. Every request still needs an operator's approval, and the preflight checks and safety monitor are unchanged.
- The drone form chooses a **model** (Sentinel Lite, Pro, Thermal, or another ArduPilot multicopter) and how Sentinel connects. **Compare drone models** shows what each can do, what is shared, and their limits. All three Sentinel models are reference designs: none has been built or flown.
- Several drones can be registered. The primary (`drone.device`, or the first operable one) takes inspection requests and drives the Drone tab; the others report telemetry and readiness.
- Drone settings changed during a flight are applied only after every drone has landed.
- Real flight commands stay disabled; see [drone-integration.md](./drone-integration.md). This work covers installation and registration only.

## Data and backups

- `<storage>/installation.json`: path, property answers, camera suggestion, plan, step marks, completion and open issues, last notification test. Damaged files are moved to `recovery/` and the journey starts fresh; the original is never overwritten.
- Backups include `installation.json`; restoring the `config` part restores it too.
- Audit log: installation path chosen, plan saved, test notification, completion, reopen, reset, sensor tests, device registration and removal (as config changes).

## API

See [api.md](./api.md#devices-and-installation).

## Verified versus simulated

| Capability | Status |
| --- | --- |
| All four paths, end to end in the browser | Verified in Edge (Playwright) on simulated cameras, a simulated drone and simulated sensors |
| Simulated cameras earning "AI monitoring verified" from real frames | Verified (scripted detector on rendered frames) |
| Drone-first site with no cameras | Verified with the simulated drone |
| Test notification | Verified against a local fake ntfy server; **not yet drilled to a real phone** |
| Planner coverage, recommendations and estimates | Unit-tested geometry; **never compared with a real installation** |
| Physical cameras, recorders, PoE switches, Wi-Fi cameras | **Not tested yet** |
| MAVLink drones | Telemetry adapter only; not tested against SITL or hardware |
| Real sensors | Planned (MQTT / Home Assistant); registration only |
