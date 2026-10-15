# Sentinel Drone V1: hardware architecture and prototype plan

Status, 2026-10-14:
- The software integration is built and tested against ArduPilot SITL (real ArduCopter 4.7.2 firmware). See `docs/drone-integration.md`.
- **No hardware has been bought. No physical flight has happened. Physical flight commands stay disabled.**
- Specifications below come from manufacturer or distributor listings, cited at the end. Figures that are estimates are marked **est.**; measure them on the bench before relying on them.

**The product range is now three models (`docs/drone-models.md`):** "V1 full" below is **Sentinel Thermal**; the X650 platform with a zoom camera is **Sentinel Pro**; and the new cheapest design, **Sentinel Lite** (X500 V2 dev kit, Pixhawk 6C, A8 mini, HM30, no companion), is the engineering priority. This document remains the detailed reference for the X650 platform, wiring, bench procedure, product readiness and Canadian rules, which apply to all three.

This replaces the earlier $300 feasibility study. Its conclusions still hold:
- consumer drones are closed;
- assembling on ArduPilot beats modifying a locked aircraft;
- the companion is a relay;
- regulation is the main market constraint.

This version drops the fixed price target and picks components for capability, reliability and serviceability.

## 1. Recommended V1 architecture

| Subsystem | V1 choice | Why (verified) | Realistic alternatives |
| --- | --- | --- | --- |
| Airframe | **Holybro X650** (650 mm, carbon) | 2000 g without battery; max payload 3.1 kg with a 6S 10 Ah battery; up to 30 min hover with no payload. Mounting plate for GPS and a companion computer. Assembles without soldering | X500 V2 (500 mm, 610 g frame, about 18 min hover, 1–1.5 kg payload): lighter (less energy near people) but little margin for thermal camera, lidar and link. Keep it as the bench/learning frame |
| Flight controller | **Pixhawk 6X (Rev 8)** running **ArduCopter 4.7.x** | Triple ICM-45686 IMUs, temperature-controlled and isolated, STM32H753, Ethernet, CAN FD. ArduPilot failsafes, fences and SITL are what Sentinel is built and tested against | Pixhawk 6C (no Ethernet, single domain; fine for bench). Cube Orange+ (similar class). ARK FMU / ModalAI VOXL 2 for an NDAA (US) build |
| Companion | **Raspberry Pi CM4 (8 GB, 32 GB eMMC) on the Holybro Pixhawk RPi CM4 Baseboard** | One board carrying the Pixhawk and CM4. FC↔CM4 over TELEM2 internally or Ethernet. CM4 in production until at least January 2034. Hardware H.264 encode | Pi Zero 2 W (bench rig only). Pi 5/CM5 (no hardware H.264 encoder; more heat). Jetson (contradicts "AI on the Hub") |
| Camera | **SIYI ZT6**: 4K visible plus **640×512 thermal**, 3-axis gimbal | Thermal is what makes a night inspection useful. 197 g, 11–25.2 V, 6.5 W average / 15 W peak, IP53, −10 to 50 °C, ArduPilot SIYI gimbal driver | **SIYI A8 mini** (95 g, 1/1.7" 8 MP, 81° HFOV, 5 W average, Ethernet video): daytime and dusk only, a fraction of the cost. Choose per site |
| Video and data link | **SIYI HM30** (Ethernet video bridge plus MAVLink UART) to a ground unit on the Hub's LAN | Air unit 74 g; the camera plugs into it over Ethernet. Ground unit has a LAN port, so the Hub receives video and telemetry directly. 20 km spec (line of sight) | **CubePilot Herelink 1.1**: integrated pilot controller with screen, about 110 ms, under 4 W, 98 g air unit. Better pilot ergonomics, but the link lands in the pilot's handheld, not at the Hub. LTE: not in V1 (cloud path, carrier and legal review) |
| Pilot control | **ExpressLRS** radio and receiver, independent of the data link | The pilot keeps control if the Hub, the companion or the data link fails | Herelink's integrated RC (then the RC and data links share one radio, which is weaker for failsafe independence) |
| GPS | **Two Holybro M10** receivers (each with a compass), `GPS_AUTO_SWITCH` | Redundancy against a glitch near buildings; GPS quality is checked before every flight | RTK is not needed for inspection |
| Obstacle sensing | **LightWare SF45/B** scanning lidar (0.2–50 m, 320°, 59 g) for ArduPilot proximity: Simple Avoidance in pilot modes, BendyRuler in AUTO/GUIDED/RTL. Plus a downward rangefinder for landing | Supported by ArduPilot (`PRX1_TYPE 8`). Primary protection is still by design: a surveyed safe transit altitude above roofs, trees and wires | None in V1 (altitude-only). Forward rangefinders. Never our own avoidance code |
| Battery | 6S 10 Ah LiPo or Li-ion, three packs per aircraft, current/voltage sensing through the baseboard's PM03D | Within the X650 recommendation (6S 5–12 Ah) | 6S 5.2 Ah for a lighter aircraft. Smart batteries (DroneCAN) for the product |
| Visibility | Anti-collision strobe plus position lights | Required to fly at night in Canada | |

**Recommended configuration: "V1 full".** X650, Pixhawk 6X with CM4 baseboard, ZT6, HM30, ExpressLRS, dual M10, SF45/B.

**Lighter "V1 day" variant** for daytime-only sites: X650 or X500 V2 with the A8 mini and no lidar. Software is identical; the camera model comes from configuration.

### What Sentinel needs from the hardware, and where it is used

| Need | Hardware source | Software (built) |
| --- | --- | --- |
| Flight-state telemetry, battery, GPS, EKF, pre-arm | Pixhawk MAVLink | `MavlinkTelemetryAdapter`, readiness checks |
| Failsafe and fence audit | ArduPilot parameters and fence (read-only) | `ardupilot.audit_parameters`; blocks approval until they are right |
| Video to the Hub | ZT6/A8 Ethernet → HM30 → Hub LAN (or CM4 → MediaMTX) | `DroneVideoWorker` (RTSP), detection on the Hub |
| Camera pose for ground positions | Telemetry plus gimbal attitude (`GIMBAL_DEVICE_ATTITUDE_STATUS` / `MOUNT_STATUS`) | `observation.image_to_ground`; says when no estimate is possible |
| Companion identity and health | CM4 | `sentinel.companion` agent: pairing, HMAC sessions, health |
| Hub cannot fly a real aircraft | CM4 relay policy plus Hub adapter refusal | Two independent layers (tested against SITL) |

## 2. Buy, modify, or assemble?

| Option | Verdict |
| --- | --- |
| **Modify a consumer drone** (DJI Mini/Air, Potensic, Holy Stone) | Rejected. Closed flight control; no companion port. DJI's Mobile SDK runs only on an Android phone on the remote, and failsafe settings are not exposed. Cannot be rebranded. New DJI models are blocked in the US. Possible later as "bring your own DJI Mini" software support (sub-250 g, no pilot certificate in Canada), never as our hardware |
| **Buy an enterprise docked system** (DJI Dock with Matrice, Skydio dock systems) | A real alternative for customers who want docks now. Each locks Sentinel into a vendor cloud/SDK and its data path, and for DJI US availability. Sentinel could integrate as a client of the vendor API later; it would not be a Sentinel drone |
| **Buy an open development drone** (ModalAI Starling 2 Max, from about $3,000) | Strong reference: PX4/ArduPilot, onboard VOXL 2, NDAA. Too small for a thermal gimbal and a link radio; a candidate base for a US build |
| **Assemble on an established open platform** (Holybro frame + Pixhawk + ArduPilot) | **Chosen.** Every interface is documented. Failsafes and fences are ours to configure and audit. SITL runs the same firmware. Parts are replaceable from several vendors. Sentinel can make the Transport Canada safety declaration as the manufacturer |

## 3. Capabilities and limits

- **Low light.**
  - Without thermal, no V1 camera gives useful video of a dark yard from 15–25 m; the A8 mini is a day/dusk camera.
  - The ZT6 thermal channel shows people and vehicles at night. However, **Sentinel's detector is trained on visible-light COCO images and has not been validated on thermal or aerial video.** Thermal detection needs a licence-cleared thermal model and labeled aerial footage (Phase 4 data work).
  - Until then the thermal stream is shown to the operator and recorded, and detections on it are not claimed.
  - Export control: thermal cores above 9 Hz are dual-use controlled; check before export.
- **Obstacles.**
  - The primary protection is the route design: vertical climb at home to the surveyed safe transit altitude, straight transit, no descent away from home.
  - The lidar adds Simple Avoidance (pilot modes) and BendyRuler (AUTO/GUIDED/RTL). ArduPilot allows one proximity source at a time, so a mixed lidar/rangefinder setup is unverified and must be tested in SITL first.
  - Wires are the hardest case: thin and poorly seen by lidar. The site survey must find them.
- **GPS.** Two receivers; HDOP and satellite count gate readiness (defaults: HDOP ≤ 2.0, ≥ 6 satellites for real aircraft).
- **Endurance.** 30 min hover is the manufacturer figure with no payload. With about 650 g of payload, **est. 22–25 min** (hover power scales roughly with mass^1.5). An inspection flight is 2–5 min. Keep a 30% reserve; the readiness minimum is 60% to launch.
- **Wind and weather.**
  - No official wind rating for the X650. V1 limit: **8 m/s sustained** (configurable `drone.weather.max_wind_mps`) until flight tests establish one. The FC wind estimate is shown in flight.
  - No precipitation: the airframe is not sealed; the ZT6 is IP53.
  - Temperature: LiPo capacity collapses below 0 °C, and the ZT6 is rated to −10 °C. V1 default minimum is −5 °C, with batteries kept warm until launch.
- **Manual takeover.** ExpressLRS is independent of the data link. The pilot switches to a manual mode (LOITER/POSHOLD/STABILIZE) at any time, and ArduPilot gives the RC priority. Sentinel shows advisories, never overrides the pilot, and never sends commands to a real aircraft.
- **Return-to-home and emergencies.** Flight-controller failsafes are authoritative:
  - GCS loss (`FS_GCS_ENABLE`), RC loss (`FS_THR_ENABLE`);
  - battery low/critical (`BATT_FS_*`), EKF (`FS_EKF_ACTION`), crash check;
  - fence altitude and polygon (`FENCE_*`), with `RTL_ALT` above the transit altitude.

  Sentinel audits these before every approval and refuses to recommend a flight when they are wrong. All were exercised in SITL (link loss → FC RTL; low battery → RTL). See section 8 for the bench procedure.
- **Repairability and consistency.** Every part is a catalogue item from at least one distributor; arms, props, motors and ESCs are replaceable in the field. Consistency comes from configuration control: pinned firmware, a version-controlled parameter file, and a commissioning fingerprint that Sentinel compares on every connection.

## 4. Weight, power and endurance budget (estimates until weighed)

| Item | Mass (g) | Power (W) | Source |
| --- | --- | --- | --- |
| X650 development-kit aircraft (frame, motors, ESCs, props, FC, GPS) | 2000 | hover: see endurance | Holybro (verified, without battery) |
| Pixhawk RPi CM4 baseboard with CM4 (replacing the kit's FC carrier) | +60 est. | 5–7 est. | CM4 typical |
| SIYI ZT6 | 197 | 6.5 avg / 15 peak | verified |
| SIYI HM30 air unit (12 V BEC) | 74 | 5 est. | weight verified; power unpublished |
| LightWare SF45/B | 59 | under 1 est. | verified weight |
| Second M10 GPS, downward rangefinder, strobe and position lights, Ethernet switch, BEC | 120 est. | 4 est. | |
| Wiring, mounts, vibration damping | 120 est. | | |
| **Dry mass with payload** | **about 2,630** | **about 25 auxiliary** | |
| 6S 10 Ah battery | about 1,300 est. | | |
| **Take-off mass** | **about 3.9 kg** | | under the 6.3 kg MTOW (70% throttle) |

The take-off mass is a *small* RPAS in Canada (250 g–25 kg). The auxiliary load is small next to hover power, so endurance is set by mass.

## 5. Wiring and interfaces

```text
 6S battery ─ PM03D power module ─┬─ Pixhawk 6X (baseboard POWER1)    ─ CM4 5 V (baseboard, separate supply)
                                  ├─ ESCs (power board)
                                  └─ 12 V BEC ─┬─ HM30 air unit
                                               ├─ Ethernet switch
                                               └─ strobe / position lights
            ZT6 (11–25.2 V: from the battery through a filtered tap)

 Pixhawk 6X ── TELEM2 (internal) ── CM4 ── sentinel.companion relay / mavlink-router
     │  GPS1, GPS2 (M10 x2)            │
     │  SERIAL4: SF45/B (Lidar360)     └── Ethernet switch ─┬─ ZT6 (video, gimbal control)
     │  I2C/serial: downward rangefinder                    ├─ HM30 air unit ── radio ── HM30 ground unit ── Hub LAN
     │  RC IN: ExpressLRS receiver (pilot)                   └─ Pixhawk 6X ETH (optional MAVLink over UDP)
     └─ SERIAL (gimbal control alternative): ZT6 SIYI protocol (MNT1_TYPE 8)
```

- **Power isolation:** the CM4's supply is separate from the FC's. A CM4 crash or brown-out must not reset the FC (bench test 8.6).
- **Data path to the Hub:** HM30 ground unit LAN → Hub (Ethernet). Sentinel's `drone.connection` is `udpin:0.0.0.0:14551` on the Hub, and the companion relay sends there.
- **Pilot ground station:** a laptop or tablet on the same LAN receives the companion's ground-station output. Its traffic to the FC is not filtered: the pilot is in command.

## 6. Configuration and firmware

1. **ArduCopter 4.7.x**, pinned. Sentinel checks the version (`drone.firmware_tested`, default `["4.6", "4.7"]`). ArduCopter 4.7 renamed `RTL_ALT` to `RTL_ALT_M`, `ARMING_CHECK` to `ARMING_SKIPCHK` and `SYSID_MYGCS` to `MAV_GCS_SYSID`; the audit understands both.
2. **Commissioning parameter set**: start from `config/drone/sentinel-v1-sitl.parm` (exercised in SITL) and adapt it per aircraft (battery thresholds, `FENCE_ALT_MAX`, `RTL_ALT_M`, `MAV_GCS_SYSID` = the pilot's ground station). Add the hardware-specific parameters:
   - `GPS_AUTO_SWITCH`;
   - `PRX1_TYPE 8` with its serial port at the `Lidar360` protocol;
   - `OA_TYPE 1` (BendyRuler) and `AVOID_ENABLE`, after SITL tests;
   - `MNT1_TYPE 8` (SIYI);
   - `SERIALn_PROTOCOL 2` for the companion;
   - `BRD_SAFETY`, `NTF_` lights.
3. **Fence**: export any inspection from Sentinel (`.plan`), load its fence into the FC with the ground station, then record **commissioning** on the Drone tab. Sentinel stores the parameter fingerprint and reports any later change.
4. **Companion**: Raspberry Pi OS Lite on eMMC; `python -m sentinel.companion provision` writes `companion.json`, `mediamtx.yml` and a systemd unit. Pair with the one-time code from the Drone tab.

## 7. Bill of materials (prototype, one unit)

USD. These are list prices from the cited listings, checked 2026-10-13/14; **est.** marks figures without a confirmed current listing. This is a prototype parts list, not a target price.

| Group | Part | Price |
| --- | --- | --- |
| Airframe | Holybro X650 ARF kit (frame, motors, ESCs, props, LEDs) | $699 (Holybro list) |
| Flight controller and companion | Pixhawk RPi CM4 Baseboard with Pixhawk 6X and PM03D | $550–750 est. |
| | Raspberry Pi CM4 8 GB / 32 GB eMMC | $110–160 est. (2026 memory price rises) |
| Navigation | 2 × Holybro M10 GPS | $90 est. |
| | LightWare SF45/B | $449 (LightWare sheet) |
| | Downward rangefinder (LightWare LW20/C class) | $250–300 est. |
| Camera | SIYI ZT6 (thermal plus 4K) **or** SIYI A8 mini | $3,160–3,700 or $255–300 |
| Links | SIYI HM30 combo (air and ground) | $285–390 |
| | ExpressLRS radio and receiver | $150–250 est. |
| Power | 3 × 6S 10 Ah packs, 6S dual charger, LiPo bags | $700–900 est. |
| Other | 12 V BEC, Ethernet switch, strobe and lights, cables, mounts, damping, labels | $250–350 est. |
| Field | Hard case, ground-unit tripod, spares (props, arm, motor) | $350–500 est. |
| **Total, V1 full (ZT6)** | | **about $7,200–8,500** |
| **Total, V1 day (A8 mini)** | | **about $4,300–5,100** |

**Bench rig (cannot fly), first purchase:**
- Pixhawk 6X on the CM4 baseboard (or a Pixhawk 6C plus a Pi Zero 2 W);
- one M10 GPS;
- an A8 mini;
- a bench 12 V supply.

About $1,000–1,400. It proves telemetry, the audit, video and companion pairing on real hardware with no motors.

**Product cost.** At 100+ units a year, parts typically fall 15–20%. Add 6–8 h of assembly, calibration and a logged acceptance flight per unit, a warranty and crash-replacement reserve, and pilot onboarding. The value of the product is the integration, not the parts:
- inspections triggered by Sentinel incidents;
- an audited flight controller;
- evidence attached to the incident;
- one interface for cameras and drone.

## 8. Bench test procedure (propellers removed)

Every step runs with **propellers off**, the aircraft restrained, a pilot holding the RC, and a fire-safe LiPo area. Record each result in the Drone tab maintenance log.

1. **Power-up and identity.**
   - FC boots on battery and on USB.
   - CM4 boots from eMMC.
   - Sentinel shows telemetry, the version and the parameter count.
   - Pass: `link`, `gps`, `ekf` readiness checks pass outdoors.
2. **Companion pairing.** Pair with the one-time code. Pass: the companion reports within 10 s; revoking stops its reports.
3. **Parameter audit.** Load the commissioning set and the fence. Pass: every audit finding passes; then record commissioning.
4. **Relay filter.** From the Hub, try `takeoff`. Pass: refused by the Hub adapter, and (with a test build that sends anyway) dropped by the relay, with the drop counted. The SITL equivalent is test 2 in `tests/test_sentinel_drone_sitl.py`.
5. **Failsafe triggers, props off.** Arm in STABILIZE with the throttle low, then for each failsafe confirm the configured mode change in the FC log:
   - switch the RC off (RC failsafe → RTL/LAND);
   - stop the Hub/companion link (GCS failsafe);
   - lower the battery failsafe threshold above the current voltage (battery failsafe);
   - set `FENCE_ALT_MAX` below the current altitude reading if feasible on the bench (fence).
6. **Companion power loss.** Pull the CM4 supply while armed (props off). Pass: no FC reset, no change in motor outputs, and a GCS-loss failsafe only if the Hub was the configured GCS.
7. **Video.** Start an inspection in SITL-pilot mode with the bench aircraft. Pass:
   - the Hub receives the stream;
   - detection runs;
   - latency is measured (target under 400 ms glass to Hub);
   - snapshots are fingerprinted.
8. **EMI.** With the HM30, CM4 and camera powered, check `GPS_RAW_INT` satellites and HDOP and the compass interference (`COMPASS_MOT` procedure). Pass: HDOP unchanged within 0.2, compass interference under 30% at full throttle (props off: ESC test only).
9. **Lidar.** Check proximity readings in all sectors against a measured wall. Pass: within 10 cm at 1–10 m.
10. **24 h soak.** Aircraft powered from a bench supply. Pass: no telemetry gap over 3 s, no companion restart, no Hub restart.

## 9. Controlled flight-test prerequisites

None of these is in place yet.

1. Bench procedure passed and logged.
2. ArduCopter tuning on a **tether in an open field**: motor test, then QuickTune or AutoTune. The pilot flies; Sentinel observes only.
3. Pilot with a **Transport Canada Advanced certificate**, a visual observer, and a site meeting basic-operations distances (>30 m from bystanders) for the first flights.
4. Insurance in place. Not federally mandated, but expected; standard commercial general liability often excludes aircraft.
5. Test cards for:
   - manual flight;
   - pilot-flown Sentinel route;
   - each failsafe in the air (RC loss, GCS loss, battery, fence) at a safe altitude;
   - lidar avoidance at low speed.
6. **Sentinel flight commands remain disabled for physical aircraft.** Enabling them is a separate decision gated in `docs/drone-integration.md`.

## 10. Maintenance and serviceability

- **Before every session:** visual inspection, prop condition, battery health, conditions confirmed, readiness green. The Drone tab reminds after 20 flights without a recorded inspection (`inspect_every_flights`).
- **Batteries:** cycle count and resistance per pack (planned field; today recorded as maintenance notes), storage voltage, retirement after damage or capacity loss.
- **Propellers:** replace on any nick; record "propellers" in the log.
- **Firmware:** update only to a version listed in `firmware_tested`. Re-run the audit and re-record commissioning afterwards.
- **Logs:** FC DataFlash logs plus Sentinel's flight log, inspection record and audit trail for every flight.

## 11. Supply and manufacturing

- **Single-source exposure:**
  - Holybro (frame, FC, GPS) and SIYI (camera, link) are both Chinese manufacturers.
  - Under the FCC Covered List (December 2025), new foreign-made drones and critical components cannot get US authorization, with exceptions for Blue UAS-listed and 65% domestic products.
  - Canada is not affected directly. **A US version needs an NDAA parts list** (for example an ARK or ModalAI flight controller and a US/EU camera and link).
- **Manufacturing repeatability:**
  - a fixed wiring harness;
  - jigs for GPS and lidar mounting;
  - torque specs;
  - a scripted acceptance test: Sentinel's bench checks plus one logged hover;
  - the commissioning fingerprint stored per serial number.
- **Radio certification:** use only pre-certified radio modules (ExpressLRS, HM30, CM4 Wi-Fi off or certified), with ISED numbers on the label.
- **Battery logistics:** lithium batteries ship as dangerous goods (UN3480/3481, UN38.3).

## 12. Commercial product readiness: prototype vs product

| Area | Prototype (now) | Product requirement |
| --- | --- | --- |
| Pairing and identity | One-time code, HMAC sessions, secret in a 0600 file on the CM4 and the DPAPI store on the Hub (built) | Secret in a secure element (for example ATECC608 on the carrier). Per-unit certificates at manufacture |
| FC link security | None on the MAVLink link | MAVLink 2 signing with the key derived from the companion secret (`pairing.mavlink_signing_key`, built but not enforced) |
| Command filter | Python relay with CRC-checked allowlist (built, SITL-tested) | mavlink-router plus signing, same policy, with an independent test |
| Updates | Manual `git pull` / file copy | Signed companion images (A/B partitions), firmware compatibility matrix enforced by the audit |
| Field diagnostics | Drone tab: readiness, audit, companion health, flight log (built) | Diagnostics bundle without media; remote support over the customer's VPN |
| Hardware replacement | Re-pair (old companion is revoked automatically), re-commission | Serial-numbered parts, replacement procedures, re-commissioning check |
| Battery maintenance | Notes in the maintenance log | Per-pack identity (smart batteries), cycle counts, automatic retirement warnings |
| Lifecycle | One aircraft per drone device | Fleet records, transfer between sites, end-of-life wipe |
| Support and warranty | None | Onboarding flight with the customer's pilot, warranty with a crash policy, spare kit |
| Compliance | Research only (section 13) | Standard 922 declaration with test evidence, ISED labels, insurance, privacy notice, aviation-law review |
| Installation | Commissioning steps in this document | Professional commissioning visit: site survey (obstacles, wires, safe transit altitude, launch point), fence, flight test, owner training |

Not to build before core flight hardware works: fleet management, docking, cloud relays, automatic dispatch.

## 13. Canadian requirements by operating scenario

Transport Canada rules as amended in 2025, fully in force since 2025-11-04. **This is engineering research, not legal advice.** Unresolved items are listed as blockers below, not treated as solved.

| Scenario | What it is | Pilot | Aircraft | Other approvals | Sentinel V1 fit |
| --- | --- | --- | --- | --- | --- |
| **VLOS, basic** | Uncontrolled airspace; >30 m from bystanders; never over them | Basic certificate | Registered (≥250 g) | None | Rural properties, farms, large commercial lots. **Feasible now** once hardware is validated |
| **VLOS, advanced** | Within 30 m of bystanders (5–30 m "near people"), over people, or in controlled airspace | Advanced certificate with flight review | **Manufacturer's Standard 922 safety assurance declaration** for that operation (922.05 near people, 922.06 over people, 922.04 controlled airspace) | NAV CANADA authorization in controlled airspace | Typical suburban homes. **Blocked** until Sentinel files a declaration backed by test records (false declarations are fined) |
| **Night** | Any of the above at night | As above | Lights keeping the aircraft visible | | Allowed within VLOS; thermal is what makes it useful |
| **EVLOS / sheltered** (2025) | Beyond unaided sight with a visual observer, or near structures | Advanced | | >30 m from uninvolved people | Does not solve suburban use (the 30 m rule still applies) |
| **Lower-risk BVLOS** (2025) | Beyond visual line of sight in uncontrolled airspace below 122 m | Level 1 Complex certificate (exam, 20 h ground school, flight review) | Declarations for the operation | **RPAS Operator Certificate (RPOC)**; at least 1 km from populated areas (small drones may fly over sparsely populated areas) | Remote farms or facilities only. Not residential |
| **Automated or docked, no pilot on site** | Unattended launch, remote pilot | | | **SFOC** case by case ($20–2,000 by complexity) | Not a V1 product. Do not market it |
| **Microdrone (<250 g)** | Any small consumer drone | No certificate | No registration | Must not endanger people | "Bring your own DJI Mini" software option only |

Applies to all scenarios:
- **radio:** ISED-certified equipment;
- **privacy:** PIPEDA for businesses, provincial privacy laws, Criminal Code voyeurism, trespass and nuisance;
- **municipal:** bylaws for parks and roads;
- **transport:** lithium battery shipping.

**Insurance:** recent legal commentary says Transport Canada does not currently mandate liability insurance but strongly recommends it; it is often contractually required.

**Regulatory blockers for selling V1 (unresolved):**
1. Standard 922 declaration for near-people operations: the test plan, single-failure analysis and records, and the decision whether Sentinel declares as manufacturer.
2. Who is the operator: the customer's certified pilot, or a Sentinel service with its own pilots (and later an RPOC).
3. Aviation-law review of product claims and the customer agreement.
4. Product liability insurance as manufacturer.
5. Privacy notice and recording practices for drone video over residential property.

## 14. Exact next steps for the physical prototype

1. **Owner decision:** approve the bench-rig purchase (section 7).
2. **Bench rig (week 1–2):**
   - connect the Pixhawk 6X and CM4 baseboard to the Hub;
   - run `python -m sentinel.companion provision` and pair;
   - load the commissioning parameters and fence;
   - pass bench steps 8.1–8.4 and 8.10;
   - record the results in `ROADMAP.md`.
3. **Video (week 2):** A8 mini → companion MediaMTX or HM30 → Hub. Measure latency and the `pose_sources` the gimbal reports (gimbal attitude or configured). Set `drone.video.camera` from the datasheet.
4. **Owner decision:** approve the V1 full parts (X650, ZT6, HM30, SF45/B, batteries).
5. **Assembly (weeks 3–4):**
   - build to section 5;
   - weigh and update section 4;
   - run bench steps 8.5–8.9;
   - set up the lidar in SITL first (`PRX1_TYPE 8`, BendyRuler).
6. **Regulatory track, in parallel:** aviation lawyer, insurer quotes, Standard 922 test plan, Advanced-certificate pilot.
7. **Tethered and then open-field flights (weeks 5–6, after 1–6):**
   - pilot-flown only;
   - Sentinel observes and proposes routes;
   - measure endurance, wind behaviour, link range around a house, thermal and visible image quality, and position-estimate accuracy against surveyed markers.
8. Re-evaluate whether to enable Sentinel-commanded flight on hardware. That is a separate, gated decision.

## Sources (checked 2026-10-13/14)

- Holybro: [X650 development kit](https://holybro.com/products/x650-development-kit), [X650 kits](https://holybro.com/products/x650-kits), [X500 V2 ARF (Elektor)](https://www.elektor.com/products/holybro-x500-v2-arf-drone-kit), [Pixhawk RPi CM4 Baseboard](https://holybro.com/products/pixhawk-rpi-cm4-baseboard)
- PX4 docs: [Pixhawk RPi CM4 Baseboard](https://docs.px4.io/v1.15/en/companion_computer/holybro_pixhawk_rpi_cm4_baseboard), [Pixhawk 6X](https://docs.px4.io/v1.14/zh/flight_controller/pixhawk6x)
- Getfpv: [Pixhawk 6X Rev 8](https://www.getfpv.com/drone-brands/holybro/holybro-pixhawk-6x-fc-standard-set.html)
- Raspberry Pi: [Compute Module 4 (production until at least January 2034)](https://www.raspberrypi.com/products/compute-module-4/), [H.264 on Pi 5](https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-010033-WP-1-H.264%20encoding%20performance%20on%20Raspberry%20Pi%205_series%20computers.pdf), [2026 price rises](https://www.gigazine.net/gsc_news/en/20260402-raspberry-pi-price-increases)
- SIYI: [ZT6](https://siyi.biz/en/product/tri-axis-multi-sensor-gimbal-pod/zt6/), [ZT6 (irlock)](https://irlock.com/products/siyi-zt6-mini-optical-pod-dual-sensors-4k-8mp-6x-digital-zoom-gimbal-camera), [A8 mini (irlock)](https://irlock.com/products/siyi-a8-mini-4k-8mp-ultra-hd-6x-digital-zoom-gimbal-camera-with-1-1-7-sony-sensor-ai-smart-identify-and-tracking-hdr-starlight-night-vision-mini-3-axis-stabilizer-95g-lightweight-55x55x70mm-for-uav-ugv-usv-rc-planes-and-fpv-drones), [HM30 spec](https://www.siyi.biz/en/product/image-digital-link/hm30/spec/), [HM30 forum](https://discuss.ardupilot.org/t/30km-1080p-60fps-150ms-siyi-hm30-full-hd-digital-video-link-telemetry-radio-system/75189)
- ArduPilot: [SIYI gimbals](https://ardupilot.org/copter/docs/common-siyi-zr10-gimbal.html), [LightWare SF45/B](https://ardupilot.org/rover/docs/common-lightware-sf45b.html)
- [LightWare SF45 sheet](https://cdn.robotshop.com/rbm/a5e9ab51-86bc-497b-adac-003de3088fb7/c/cc603057-8e8c-4a54-8c00-4e83988d1bd2/63a17535_gd1_t23_lightware_product_info_sheets_sf45.pdf); [Herelink (RobotShop Canada)](https://ca.robotshop.com/products/cubepilot-herelink-hd-video-transmission-systembundle); [ModalAI development drones](https://www.modalai.com/collections/development-drones)
- Transport Canada: [2025 changes](https://tc.canada.ca/en/aviation/drone-safety/2025-summary-changes-canada-drone-regulations), [Choosing a drone for advanced operations](https://tc.canada.ca/en/aviation/drone-safety/learn-rules-you-fly-your-drone/choosing-right-drone-advanced-operations), [Safety assurance declaration](https://tc.canada.ca/en/aviation/drone-safety/submitting-drone-safety-assurance-declaration)
- Insurance: [Fasken, "Cleared for take-off" (2026)](https://www.fasken.com/en/knowledge/2026/08/cleared-for-takeoff), [MLT Aikins](https://www.mltaikins.com/?p=31759)
- DJI SDK and US status: [DJI MSDK release notes](https://developer.dji.com/doc/mobile-sdk-tutorial/en), [UAV Coach on the FCC Covered List](https://uavcoach.com/dji-ban/)
