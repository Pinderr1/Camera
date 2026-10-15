# Sentinel drone models: Lite, Pro and Thermal

Status, 2026-10-14:
- Three **reference designs** share one flight stack, one radio architecture, one camera protocol and the same Sentinel software.
- The software model system is built and tested: catalog, registration, model-specific readiness, video settings, troubleshooting, comparison screen and AI Guide.
- Every model is tested in the built-in simulator. Sentinel Lite is tested against **ArduPilot SITL with its own commissioning parameters**; the Pro and Thermal profiles run on the V1 SITL setup.
- **No hardware has been bought and nothing has flown.** Physical flight commands stay disabled for every model.
- Prices are list prices from the listings cited at the end, checked 2026-10-14. **est.** marks figures without a confirmed listing. These are parts estimates, not product prices.

The engineering priority is **Sentinel Lite**: the cheapest drone that is genuinely useful for an outdoor security inspection and integrates reliably with Sentinel. One customer with one drone is the main use case; fleets, coordinated patrols and dispatching are out of scope.

## 1. What "genuinely functional" means

A Sentinel inspection drone must have all of the following. Anything missing is a disqualifier, not a compromise.

| Requirement | Why | How Sentinel checks it |
| --- | --- | --- |
| Open flight controller (ArduPilot) with readable parameters | Failsafes, fence and RTL must be auditable before every approval | Parameter audit; approval blocked on failures |
| GPS and position telemetry to the Hub | Routes, geofence, ground positions of what the camera sees | `link`, `gps`, `ekf` readiness checks |
| Battery monitoring | Battery failsafes, launch minimum, endurance check | `battery` check, `BATT_MONITOR` audit rule, route endurance check |
| Flight-controller failsafes: link loss, low battery, navigation, fence | The aircraft must stay safe without the Hub | Audit of `FS_*`, `BATT_FS_*`, `FENCE_*`, `RTL_ALT` |
| Independent pilot radio | The pilot takes over even if the Hub or video link fails | Model capability; commissioning |
| Stabilised camera with live video to the Hub | An inspection without usable video is pointless | `video` check (blocking for Sentinel models) |
| An independent lockout of Hub flight commands | Defence in depth: a Hub bug or compromise must not fly a real aircraft | `lockout` check: companion relay **or** the flight controller's own lockout |
| Outdoor flight in light wind, serviceable parts | Residential and small-business sites | Model wind/temperature limits in the `conditions` check |

## 2. Options evaluated

| Option | Cost class | Verdict |
| --- | --- | --- |
| **Consumer drone (DJI Mini 3/4 Pro, Air)** | Lowest hardware cost; flies very well | **Not supported.** DJI's Mobile SDK (MSDK v5; Mini 3 since v5.3, Mini 4 Pro since v5.13, March 2025) runs only inside an Android app on the RC-N1/RC-N2 controller's phone. There is no flight-controller access, failsafes cannot be audited, and there is no stream to the Hub without that app. New DJI models are blocked in the US (FCC Covered List). Sentinel has no such app, so **no DJI integration is claimed**. A future "bring your own DJI" Android bridge is possible, but it would never be Sentinel hardware. |
| Other consumer drones (Potensic, Holy Stone, etc.) | Low | Rejected: closed firmware, no SDK for third parties, no failsafe access. |
| **Open development drone, ready to fly** (ModalAI Starling 2, from about $3,000) | High | Strong platform (PX4/ArduPilot, NDAA), but too expensive for Lite and too small for a gimbal plus an HM30. A candidate base for a US build. |
| **Holybro X500 V2 PX4 Development Kit + SIYI A8 mini + HM30** | Low–medium | **Chosen for Sentinel Lite.** The kit bundles frame, motors, ESCs, Pixhawk 6C, M10 GPS and power module for $508, less than buying them separately. Pixhawk 6C runs ArduCopter. Every interface is documented and every part is replaceable. |
| Generic F450/S500 clone frame + Matek H743-class FC | Lowest custom build | Works with ArduPilot and saves an estimated $100–200. But quality and repeatability vary by seller, it needs soldering, and the IMU is not isolated. **A cost-down candidate after the Lite has passed bench and flight tests,** not the reference. |
| 7-inch FPV-style quad running ArduPilot | Low | Light and agile, but loud, with exposed fast propellers and a short hover time with a gimbal. Unsuited to flying near homes. Rejected. |
| Camera without a gimbal (FPV camera, IP camera board) | Saves about $150–250 | Fails "stabilised usable video" in wind. Rejected for every tier. |
| Cheaper video link (Wi-Fi bridge, OpenIPC/WFB-ng) | Saves about $250–350 | Wi-Fi to an outdoor access point works at short range but drops and stalls unpredictably around buildings. WFB-ng needs a Linux board on the aircraft (a companion). **Bench experiments only,** not the product. |
| Herelink (integrated pilot controller with video) | Higher than HM30 | Good pilot ergonomics, but the link ends in the pilot's handheld, not on the Hub's network, and RC shares the radio with data. Alternative for Pro customers who want it. |

## 3. The cheapest viable architecture: Sentinel Lite

```text
 Pilot: ExpressLRS radio ───────────────── ELRS receiver ─┐
                                                          ▼
 Hub LAN ── HM30 ground unit ~~~ radio ~~~ HM30 air unit ── Pixhawk 6C (ArduCopter) ── M10 GPS
   │  (MAVLink UDP + RTSP)                    │ Ethernet        │ serial: SIYI gimbal control (MNT1_TYPE 8)
   │                                          └── SIYI A8 mini (3-axis gimbal, RTSP)
   └── pilot's ground station (laptop/tablet) on the same LAN, sharing the one radio link
```

What makes it cheap without being unsafe:

1. **No companion computer.** The Lite's second layer against the Hub flying it is the flight controller itself.
   - Set `MAV_GCS_SYSID` to the pilot's ground station (255) and `MAV_OPTIONS` bit 0 (ArduCopter 4.7; `SYSID_ENFORCE` on 4.6). ArduPilot then accepts commands only from the pilot.
   - **Verified in SITL (ArduCopter 4.7.2):** the Hub's arm and take-off commands are ignored, while configured telemetry still flows.
   - This layer sits on the aircraft and does not depend on the Hub's software.
2. **Telemetry without being allowed to ask for it.** The flight controller also ignores the Hub's stream and parameter requests. Two consequences:
   - **Stream rates are set at commissioning on the radio port.** Careful: ArduCopter 4.7 numbers MAVLink ports from 1, so TELEM1 (SERIAL1) uses `MAV2_*` stream rates; ArduCopter 4.6 calls the same port `SR1_*`. SITL confirmed that `MAV1_*` produces nothing on SERIAL1. The troubleshooter explains this.
   - **Sentinel reads the parameters and fence passively.** When the pilot's ground station downloads them over the shared HM30 link, Sentinel overhears the replies and audits them (`MavlinkTelemetryAdapter` collects every `PARAM_VALUE` and now overhears fence downloads). Verified in SITL: all 1,369 parameters and the 4-vertex fence.
3. **One radio for video and telemetry** (HM30), one for the pilot (ExpressLRS). No separate telemetry radio, and nothing that needs a Linux board on the aircraft.
4. **Gimbal angle from the flight controller.** The A8 mini's gimbal is driven by ArduPilot's SIYI driver, so Sentinel gets its angle from MAVLink with no companion.

**Compromises, stated plainly:**
- **Daytime and dusk only.** Sentinel refuses to approve a Lite inspection when the operator records darkness (`daylight` check); Thermal is the night model.
- **Wind limit 6 m/s and about 15 min hover (estimates).** About 10 minutes of flying above the 30% reserve. Sentinel checks every route against that before approval.
- **One GPS.** A glitch near buildings means a failsafe rather than a switch-over.
- **No obstacle sensing.** Safety relies on the surveyed safe transit altitude.
- **No paired companion identity, health reports or relay drop counters.** A Raspberry Pi companion can be added later as an option without changing the model.
- **Parameters are only re-read when the pilot's ground station connects.** Until then the audit says "not received yet" and approval stays blocked; this fails safe.
- **Not rain-proof.** LiPo packs lose capacity below 0 °C, which is the Lite's minimum temperature.

## 4. The three tiers

| | Sentinel Lite | Sentinel Pro | Sentinel Thermal |
| --- | --- | --- | --- |
| Use | Daytime look-arounds, typical home | Larger or windier sites, inspect from farther away | Night inspections |
| Airframe | Holybro X500 V2 (500 mm) | Holybro X650 (650 mm) | Holybro X650 |
| Flight controller | Pixhawk 6C (in the dev kit) | Pixhawk 6X on RPi CM4 baseboard | Same as Pro |
| Companion | None (FC lockout); optional add-on | CM4: relay, pairing, health | Same as Pro |
| Camera | SIYI A8 mini (3-axis, 6x digital) | SIYI ZR10 (3-axis, 10x optical / 30x hybrid) | SIYI ZT6 (4K visible + 640x512 thermal) |
| GPS | One M10 | Two M10, auto switch | Two M10 |
| Obstacle sensing | None | Optional SF45/B lidar | SF45/B lidar + downward rangefinder |
| Battery | 4S 5000 mAh ×2 | 6S 10 Ah ×2–3 | 6S 10 Ah ×3 |
| Hover (est.) | about 15 min | about 24 min | about 22 min |
| Wind limit (est.) | 6 m/s | 8 m/s | 8 m/s |
| Take-off mass (est.) | about 1.8 kg | about 3.9 kg | about 3.9 kg |
| Night | No | Only where lit (not approved when dark) | Yes, with lights confirmed |

**Shared across all three:**
- ArduCopter 4.6/4.7 and the same failsafe, fence and audit baseline;
- the SIYI HM30 video and telemetry link to the Hub's network, plus an ExpressLRS pilot radio;
- SIYI gimbal cameras over Ethernet (RTSP), with the gimbal angle through ArduPilot (`MNT1_TYPE 8`);
- M10 GPS, Pixhawk-standard connectors and the commissioning procedure;
- all Sentinel software.

The Pro and Thermal share the same airframe, flight controller and companion, and differ only in camera and sensors.

## 5. Bills of materials (one unit, prototype)

USD. Ranges reflect different sellers.

### Sentinel Lite

| Part | Estimate |
| --- | --- |
| Holybro X500 V2 PX4 Development Kit (frame, motors, ESCs, propellers, Pixhawk 6C, M10 GPS, power module, SiK telemetry radio) | $508 (Holybro store) |
| SIYI A8 mini gimbal camera | $225–350 |
| SIYI HM30 air and ground units | $285–430 (€375–390 listings) |
| ExpressLRS transmitter and receiver | $80–150 est. |
| 2 × 4S 5000 mAh LiPo, charger, LiPo bag | $150–220 est. |
| Strobe, Ethernet cable, mounts, damping, connectors | $60–100 est. |
| **Total** | **about $1,310–1,760** |

Cost-down paths, each needing its own bench test first:
- a separate X500 V2 ARF kit (from about $261) plus a Matek H743-class flight controller ($85–145) and an M10 GPS;
- a cheaper 4S charger.

Together these could save roughly $100–200. Holding off on rain sealing or a second GPS is already reflected in the design.

### Sentinel Pro

| Part | Estimate |
| --- | --- |
| Holybro X650 ARF kit | $699 |
| Pixhawk 6X on RPi CM4 baseboard with PM03D | $550–750 est. |
| Raspberry Pi CM4 8 GB / 32 GB eMMC | $110–160 est. |
| 2 × M10 GPS | $90 est. |
| SIYI ZR10 | $520–650 |
| SIYI HM30 | $285–430 |
| ExpressLRS | $80–150 est. |
| 2–3 × 6S 10 Ah, 6S charger | $500–900 est. |
| Lights, BEC, Ethernet switch, cables, mounts | $250–350 est. |
| **Total** | **about $3,100–4,200**; optional SF45/B lidar +$449 |

### Sentinel Thermal

The Pro platform with:
- the SIYI ZT6 ($3,160–3,700) instead of the ZR10;
- the SF45/B lidar ($449) and a downward rangefinder ($250–300 est.);
- three battery packs.

**Total about $6,400–8,000.** This is the "V1 full" configuration of `docs/drone-hardware-v1.md`.

Regulatory and product costs (assembly, acceptance flight, warranty, insurance, Transport Canada declaration) are in `docs/drone-hardware-v1.md` sections 7, 12 and 13 and apply to all tiers.

## 6. Smallest sensible bench prototype (Sentinel Lite)

Buy the parts that prove the Lite's two unverified assumptions on real hardware: the flight-controller lockout with passive audit, and stabilised video into the Hub.

| Stage | Purchase | Proves |
| --- | --- | --- |
| **1. Bench (no motors)** | Holybro X500 V2 PX4 Development Kit **without assembling the motors**, plus SIYI A8 mini and a 4S pack or bench supply. **About $760–920** | On a real Pixhawk 6C: `MAV_OPTIONS` lockout refuses the Hub (USB or SiK link); passive parameter capture; `MAV2_*` stream rates; A8 mini gimbal angle via `MNT1_TYPE 8` in telemetry (`pose_sources: gimbal_reported`); A8 mini RTSP straight into the Hub over Ethernet; latency measured |
| 2. Radio | SIYI HM30. **About $285–430** | One shared radio: the pilot's ground station and the Hub on the LAN; passive audit across the radio; video latency and range around a house |
| 3. Flight | ExpressLRS, batteries and charger, strobe; assemble the X500. **About $300–450** | Tethered, then open-field pilot-flown inspections: endurance, wind behaviour, gimbal stability, failsafes in the air (with an Advanced-certificate pilot) |

Stage 1 is the smallest sensible purchase. Every Lite component bought there is used in the final aircraft. Approval is the owner's decision: **nothing has been bought.**

## 7. Software: what is built and how it is tested

| Piece | Where | Test |
| --- | --- | --- |
| Model catalog: capabilities, hardware, camera geometry, flight estimates, requirements, model-specific parameter rules, limitations; comparison and recommendation | `src/sentinel/drone/catalog.py` | `tests/test_sentinel_drone_models.py` (catalog integrity, no prices, honest status) |
| Registration: `devices[].drone_model`, `drone_options`, write-only `video_url` | `devices.py`, `site_config.py`, Install tab | API and Edge tests |
| Model-specific readiness: Hub command lockout (companion relay with telemetry policy, or FC `MAV_OPTIONS`); companion required (Pro/Thermal) or not part of the model (Lite); camera stream required; daylight vs night capability and lights; wind and temperature limits; model parameter rules (`BATT_MONITOR`, `MNT1_TYPE`, `GPS_AUTO_SWITCH`, `PRX1_TYPE` when a lidar is fitted) | `drone/manager.py`, `drone/ardupilot.py` | fake ArduPilot per model; SITL |
| Route endurance check at approval, from the model's estimated hover time and the return reserve | `catalog.endurance_check` | unit and API tests |
| Video settings: camera field of view per model, simulated aerial camera for simulated drones, thermal channel noted as not analysed | `catalog.video_settings`, `drone/video.py` | per-model simulated inspections |
| Simulator performance per model (speed, climb, battery drain, gimbal angle) | `catalog.simulator_settings`, `drone/mock.py` | the Lite flies slower than the Pro in the same workflow |
| Troubleshooting with model-specific steps (HM30 link, lockout, stream-rate numbering, camera address, conditions) | `drone/troubleshoot.py`, `GET /api/v1/drone/diagnosis`, Drone tab, AI Guide `diagnose_drone` | unit, AI Guide and Edge tests |
| Comparison screen | Install tab registration form and Drone tab | Edge test |
| AI Guide: `get_drone_models` (comparison + recommendation), `diagnose_drone`, model-aware `propose_register_device` | `assistant/tools.py`, `assistant/guide.py` | `tests/test_sentinel_assistant.py` |
| Lite commissioning parameters for SITL | `config/drone/sentinel-lite-sitl.parm` | `tests/test_sentinel_drone_lite_sitl.py` |

### Simulation results (2026-10-14)

- **Built-in simulator, every model** (`ModelSimulationTests`):
  - an incident-sourced request waits for approval;
  - the simulator flies the approved route;
  - the simulated aerial camera with the model's field of view sees the scripted person;
  - observations, a fingerprinted snapshot and the aircraft label are stored with the inspection;
  - link loss returns home and the inspection is aborted;
  - low battery triggers Sentinel's return home;
  - the Lite refuses a night inspection that the Thermal flies with lights confirmed.
- **ArduPilot SITL 4.7.2, Lite commissioning** (`SENTINEL_SITL=lite`, 4 tests, about 70 s):
  1. The flight-controller lockout ignores the Hub's arm and take-off even from an adapter allowed to command. Telemetry flows from the commissioned `MAV2_*` rates, and the audit passes from the parameters and fence the pilot's ground station downloaded.
  2. A pilot-flown, Sentinel-proposed inspection is followed from telemetry. The simulated A8 mini camera, posed by SITL, sees the person; the ground position is within 6 m of the target and the evidence is fingerprinted.
  3. Losing the single radio makes the flight controller return home (GCS failsafe on the pilot's heartbeats). Sentinel records the link loss and sends no command.
  4. A low battery triggers the flight controller's failsafe; Sentinel tells the operator "Battery low: the aircraft is returning home".
- **ArduPilot SITL 4.7.2, V1 commissioning** (`SENTINEL_SITL=1`, 7 tests): the six existing tests plus the Pro and Thermal profiles.
  - The Thermal profile is blocked because the firmware has no lidar configured (`PRX1_TYPE`).
  - The Pro passes through its companion relay and flies the same proposed inspection, with nothing dropped.

- **End-to-end demo through the real service and API** (`scripts/drone_sitl_demo.py --model sentinel_lite` with the Lite commissioning, and `--model sentinel_pro` with the V1 commissioning):
  - a fixed-camera "Person entered Shed" incident created the request; the operator approved it, and the pilot stand-in flew the exported plan;
  - Lite: 25 s and 60 m flown; 123 drone frames analysed, all with a camera pose; the person was located about 1.4 m from the inspection point (±3.3 m); one fingerprinted snapshot on the incident; lockout by the flight controller (system id 255), no companion;
  - Pro: all 17 readiness checks passed, with the lockout provided by the companion relay; 117 frames analysed; the same evidence.

### Still simulated or unverified

- The aircraft (SITL physics), both cameras, the pilot and the HM30 radio (emulated by Sentinel's relay in pass-through mode).
- Every hover time, wind limit and mass is an estimate.
- The ZR10 and ZT6 fields of view come from retailer listings, or are assumed, and must be measured.
- The A8 mini's default RTSP address comes from its manual and is not confirmed on a unit.
- `MAV_OPTIONS` behaviour is verified in SITL only, not yet on a Pixhawk 6C.

## 8. What needs physical hardware

1. Lockout, passive audit and gimbal-angle reporting on a real Pixhawk 6C and A8 mini (bench stage 1).
2. HM30 link behaviour: a shared ground station, video latency, range around a house (stage 2).
3. Endurance, wind limit and stability with the real payload; failsafes in the air (stage 3, with a qualified pilot and insurance).
4. Thermal detection: a licence-cleared thermal model and labelled aerial footage (Phase 4 data work) before any detection on thermal video is claimed.

## Sources (checked 2026-10-14)

- Holybro: [X500 V2 PX4 Development Kit ($507.99)](https://shop.holybro.com/x500-kit_p1180.html), [Pixhawk 6C ($211.99)](https://holybro.com/products/pixhawk-6c), [Pixhawk 6C Mini (Pyrodrone)](https://pyrodrone.com/collections/advanced-education-development/brand_holybro); X500 V2 ARF listings: [Worldronemarket ($260.99)](https://www.worldronemarket.com/?p=23475), [Openelab](https://openelab.com/products/holybro-x500-development-kit)
- SIYI: [A8 mini (irlock)](https://irlock.com/products/siyi-a8-mini-4k-8mp-ultra-hd-6x-digital-zoom-gimbal-camera-with-1-1-7-sony-sensor-ai-smart-identify-and-tracking-hdr-starlight-night-vision-mini-3-axis-stabilizer-95g-lightweight-55x55x70mm-for-uav-ugv-usv-rc-planes-and-fpv-drones), [A8 mini RTSP latency reports](https://discuss.ardupilot.org/t/siyi-a8-mini-4k-ai-mini-zoom-gimbal-camera-ai-identify-tracking-4k-1-1-7-inch-8mp-sony-sensor-6x-zoom/92133?page=33), [ZR10 (irlock)](https://irlock.com/products/copy-of-siyi-a8-mini-4k-8mp-ultra-hd-6x-digital-zoom-camera-with-3-axis-gimbal-2), [ZR10 manual](https://siyi.biz/siyi_file/ZR10/ZR10%20User%20Manual%20EN%20v1.6.pdf), [HM30 (OpenELab)](https://openelab.io/a/s/products/siyi-hm30-fpv-system), [HM30 (mybotshop)](https://www.mybotshop.de/SIYI-HM30-Combo_1), [HM30 forum](https://discuss.ardupilot.org/t/30km-1080p-60fps-150ms-siyi-hm30-full-hd-digital-video-link-telemetry-radio-system/75189)
- Matek: [H743-SLIM V3 (NextFPV)](https://www.nextfpv.com/products/matek-h743-slim-flight-controller), [Rotorvillage](https://rotorvillage.ca/matek-h743-slim-h7-flight-controller)
- ArduPilot: [MAVn_OPTIONS source](https://raw.githubusercontent.com/ArduPilot/ardupilot/master/libraries/GCS_MAVLink/GCS_MAVLink_Parameters.cpp); [SYSID_ENFORCE discussion](https://discuss.ardupilot.org/t/controlling-one-aircraft-with-two-ground-control-stations/23638); `MAV_OPTIONS`, `MAV_GCS_SYSID` and `MAVn_*` stream behaviour measured on the official ArduCopter 4.7.2 SITL build
- DJI: [MSDK release notes](https://developer.dji.com/doc/mobile-sdk-tutorial/en), [Mini 4 Pro MSDK support (DroneDJ)](https://dronedj.com/2025/03/21/dji-mini-4-pro-msdk/), [Mini 3 MSDK (DroneDJ)](https://dronedj.com/2023/04/13/dji-sdk-mini-3-pro/)
- Pro and Thermal parts: sources in `docs/drone-hardware-v1.md`
