# Sentinel Lite: cost-down study and manufacturing preparation

Status, 2026-10-14. **Nothing has been bought and nothing has flown.** Prices are retailer or manufacturer listings checked on 2026-10-14 (sources at the end), in the currency shown. **est.** marks a figure without a listing. Weights, power and endurance figures are **estimates** unless marked *(spec)*. These are parts costs for one prototype, not product prices.

## 1. Answer in brief

- **The current Lite is already close to the cost floor for its requirements.** The gimbal camera and the video/telemetry radio are about 45 % of the parts cost, and no verified cheaper parts keep all three of: live Ethernet (RTSP) video to the Hub, the gimbal's angle reported through ArduPilot, and telemetry on the Hub's network.
- **Verified savings are modest: about $110–$260 per unit (8–15 %).** They come from the flight controller, the frame, and buying one battery first.
- **The cheapest acceptable Lite (configuration B) is about $1,190–$1,560 USD**, against about $1,410–$1,850 for the current design at today's prices. The baseline estimate in `docs/drone-models.md` ($1,310–$1,760) was a little low on batteries and charger.
- **Recommendation for the prototype: keep the baseline (configuration C = A, with sourcing changes).** The Holybro X500 V2 development kit is the cheapest way to buy a Pixhawk-class stack, frame and GPS from one supplier with plug-in connectors. Its SiK radio pair, which the Lite does not need in the air, becomes the stage-1 bench link.
- **Adopt B's flight controller for low-volume production only after it passes the same bench and flight acceptance.** B swaps in an H743-class board whose ArduPilot firmware build is verified to contain every feature the Lite needs. The hardware itself is unverified.

## 2. Requirements the Lite must keep

The essential requirements did not change. Each is listed with how this study verified it.

| Requirement | How it was checked |
| --- | --- |
| ArduPilot with fence, failsafes, the SIYI gimbal driver and MAVLink signing | The official `features.txt` of each board's **ArduCopter stable** build on firmware.ardupilot.org |
| GPS positioning, return-to-home where navigation permits | M10 GPS on every option; SITL fault scenarios (`docs/drone-reliability.md`) |
| Manual pilot control on an independent radio | ExpressLRS kept on every option; the HM30's own RC input was rejected because it would share the video radio |
| Live video to Sentinel, usable stabilised imagery | 3-axis gimbal with Ethernet RTSP (SIYI A8 mini) on every recommended option |
| Battery telemetry | An analog power module (PM02) or the dev kit's power module |
| Independent protection against Hub commands | FC `MAV_OPTIONS` lockout or MAVLink signing. Both are SITL-verified (scenarios `unauthorized_hub_commands`, `signing_lockout`) |
| Repairability | Holybro frames with spare-part listings; standard 2216 motors and 1045 propellers |

## 3. What was evaluated

### Flight controller: firmware first

A board is only a candidate if its **official stable ArduCopter build** contains the features the Lite relies on. Checked 2026-10-14 from `https://firmware.ardupilot.org/Copter/stable/<board>/features.txt`:

| Board (ArduPilot target) | SIYI gimbal driver | Fence | MAVLink signing | EKF3 | Scripting | Price | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Pixhawk 6C (`Pixhawk6C`), in the X500 dev kit | yes | yes | yes | yes | yes | $211.99 alone; bundled in the $507.99 kit | Baseline. Isolated IMU, JST-GH plug-in harness |
| Pixhawk 6C Mini (`Pixhawk6C`) | yes | yes | yes | yes | yes | $176.99 (Holybro) | No real saving against the bundle |
| Matek H743-SLIM V3 (`MatekH743`) | yes | yes | yes | yes | yes | $139–175 | Not cheaper than the 6C Mini |
| **MicoAir743 V2 (`MicoAir743`)** | yes | yes | yes | yes | yes | **$72.99** (Pyrodrone), £40 (UK) | **Cost-down candidate (B).** Firmware verified; hardware, IMU quality and connectors unverified |
| Kakute H7 v2, TBS Lucid H7, Flywoo H743 Pro | yes | yes | yes | yes | yes | not priced | Same firmware; FPV-stack boards with no advantage over the MicoAir |
| Matek F405-Wing, F405-TE, MicoAir405, SpeedyBee F405 Mini/AIO, BrahmaF4 | **no** | yes | yes | yes | **no** | $40–60 class | **Rejected.** The 1 MB F4 builds leave out the SIYI driver, so there is no gimbal angle for Sentinel and no gimbal control through the FC |

### Frame, motors, ESCs

| Option | Price | Spec (Holybro) | Verdict |
| --- | --- | --- | --- |
| X500 V2 ARF (500 mm carbon, 2216 KV920, 20 A BLHeli S, 1045) | $329.00 | 18 min hover with no payload on 5000 mAh; payload 1500 g at 70 % throttle | Baseline frame (inside the dev kit) |
| **S500 V2 ARF** (480 mm, same motors, ESCs and propellers) | **$259.00** | 18 min hover with no payload on 5000 mAh; payload 1500 g at 70 % throttle | **B.** Same propulsion and supplier; $70 less |
| S500 V2 frame kit + generic 2212/2216 motors and ESCs | $42 + about $90 est. | — | Rejected for now: motor and ESC quality vary by seller. Not repeatable before a flight qualification |
| 7-inch FPV-style frame | low | — | Rejected (noise, exposed propellers, short hover with a gimbal) |

### Camera

| Option | Price | Verdict |
| --- | --- | --- |
| **SIYI A8 mini**: 3-axis, Ethernet RTSP (also HDMI and CVBS), 11–25.2 V, about 95 g, about 5 W average / 12 W peak | $246–347 | **Kept.** ArduPilot's SIYI driver supports it (A8 firmware 0.1.7+) |
| SIYI A2 mini: 1-axis tilt, 160° FOV, 2 MP, Ethernet | $101–105 | **Rejected.** ArduPilot's SIYI documentation states "The Siyi A2 is not supported". It has a single axis and an ultra-wide lens (about half the pixels per degree) |
| A8 mini over its CVBS output with an analog 5.8 GHz link | about $50–80 est. for the link | **Rejected** for the product. Standard-definition video weakens detection and evidence, and it needs a second telemetry radio. Bench curiosity only |

### Video and telemetry radio

| Option | Price | Verdict |
| --- | --- | --- |
| **SIYI HM30**: video and telemetry, ground unit with Ethernet to the Hub's LAN, 3S–6S air unit, 74 g, 2.8 W average | $283.53 (SIYI shop) – €389.95 (combo with camera) | **Kept.** Cheapest verified radio that lands video and MAVLink on the LAN. Buy the system without the R1M recording camera when available |
| HM30 also carrying RC (S.Bus) | saves the ELRS (about $85–125) | **Rejected.** A video-link failure would also take the pilot's control |
| Wi-Fi bridge, or OpenIPC/WFB-ng with a Pi on board | $100–150 est. | Rejected for the product (drops near buildings, needs a Linux board and a Linux ground station). Bench only |
| ExpressLRS MAVLink mode with the TX backpack's Wi-Fi as a telemetry path | no extra cost | **Worth a bench test** as a second telemetry path. ELRS docs: MAVLink mode forces a 1:2 telemetry ratio, STM32 hardware cannot run it, and an open issue reports GCS connection problems on 4.0.0 |

### Batteries, charger, power

| Item | Price | Note |
| --- | --- | --- |
| 4S 5000–5200 mAh LiPo | $68.49 (Tattu 5200 35C) – $113.61 (Powerhobby 5000 100C) | Buy in Canada: LiPo cannot travel as air cargo freely |
| Charger | ISDT 608AC $77.99; ToolkitRC M7AC $96.99 / CAD 118.99 (Rotorvillage) | The 608AC's own supply limits 4S charging to about 3.5 A |
| Power module | Holybro PM02 V3 $18.99–25 | Analog. Only needed when the FC is not the dev kit's |
| Shared power | A8 mini (11–25.2 V) and HM30 air unit (3S–6S) run straight off the 4S pack through the PDB's XT30 | No extra BEC |

### Complete aircraft

No ArduPilot ready-to-fly quadcopter with a gimbal camera and an open flight controller turned up under $1,000. ArduPilot forum threads point to kits (Hexsoon EDU-450, Holybro). DJI and other consumer drones remain unsupported (`docs/drone-models.md`, section 2).

## 4. The three configurations

Prices in USD unless shown. *(spec)* = manufacturer figure; everything else about weight and power is an estimate.

### A. Existing Lite baseline

| Part | Model | Price | Weight | Power |
| --- | --- | --- | --- | --- |
| Frame, motors, ESCs, FC, GPS, power module, SiK radios | Holybro X500 V2 PX4 Development Kit (Pixhawk 6C, M10 GPS, PM02) | $507.99 | about 1,050 g (frame 610 g *(spec)* plus propulsion and electronics) est. | FC, GPS: about 2.5 W est. |
| Camera | SIYI A8 mini | $246–347 | 95 g | 5 W average, 12 W peak |
| Video and telemetry radio | SIYI HM30 | $284–420 (€389.95 combo) | 74 g air unit *(spec)* | 2.8 W average *(spec)* |
| Pilot radio | RadioMaster Pocket ELRS + ELRS receiver | about $85–125 (£50–70 + €18–30) | 5 g | <0.5 W |
| Batteries, charger, bag | 2 × 4S 5000–5200 mAh, ISDT 608AC, LiPo bag | $230–345 | 520–560 g per pack est. | — |
| Strobe, Ethernet lead, mounts, damping, connectors | — | $60–100 est. | 80 g est. | 1 W est. |
| **Total** | | **about $1,410–1,850** | **about 1.85–1.95 kg take-off est.** | about 11 W of electronics on about 260 W of hover est. |

### B. Lowest-cost acceptable Lite

| Part | Model | Price | Note |
| --- | --- | --- | --- |
| Frame, motors, ESCs, PDB, props | Holybro S500 V2 ARF | $259.00 | Same 2216 KV920 / 20 A / 1045 propulsion as the X500 |
| Flight controller | MicoAir743 V2 (ArduPilot `MicoAir743`) | $72.99 | Firmware features verified. Needs a bench check of its IMUs, current sensing and connectors |
| GPS and compass | Matek M10Q-5883 | $47.99 | u-blox M10 |
| Power module | Holybro PM02 V3 (analog) | $18.99–25 | Check its current input against the MicoAir's ADC on the bench |
| Camera | SIYI A8 mini | $246–347 | Unchanged |
| Radio | SIYI HM30 | $284–420 | Unchanged |
| Pilot radio | RadioMaster Pocket ELRS + receiver | $85–125 | Unchanged |
| Battery, charger, bag | **1** × 4S 5200 mAh, ISDT 608AC, bag | $160–220 | One pack: one flight, then about 1 h charging |
| Strobe, cables, mounts | — | $40–80 est. | |
| **Total** | | **about $1,190–1,560** | **Saves about $220–290 against A** |

Compromises:
- **The FC is unverified hardware.** The IMUs are not isolated or temperature-controlled, the board needs soldered or JST-SH connections instead of Pixhawk JST-GH plugs, and it comes from a smaller vendor.
- **One battery.** That rules out a second flight soon after the first.
- **The S500 has a smaller deck** for the gimbal and HM30 mounts than the X500's rail system.
- Each of these must pass the bench acceptance in `docs/drone-bench-testing.md` and a flight qualification before it counts as Lite hardware.

### C. Optimized Lite (recommended for the prototype and the first units)

C is configuration A with sourcing changes, because the evidence shows no better balance yet:

1. **Keep the X500 V2 development kit.** It costs less than buying a Pixhawk-class FC, GPS and power module separately ($329 ARF + $211.99 6C + GPS + PM is about $600). It comes from one supplier, its JST-GH harness plugs in without soldering (the biggest assembly-time and repeatability factor), its IMU is isolated, and the X500's rail mounts give the gimbal and HM30 repeatable positions.
2. **Use the kit's SiK radio pair as the stage-1 bench link** (lockout and passive-audit tests before the HM30 is bought). It then stays as a spare telemetry path for commissioning.
3. **Power the A8 mini and HM30 straight from the 4S PDB.** Both accept the pack voltage, so no BEC is needed. There is one fewer part, though the wiring still needs its own bench check.
4. **Buy the batteries and charger in Canada** (Rotorvillage lists the M7AC at CAD 118.99). LiPo by air is restricted and slow.
5. **Two packs** for the first units: an incident response should not wait for a charge.

**Total: about $1,410–1,850 USD**, the same as A. Moving production to B's FC is the documented next cost-down once that board passes the same acceptance.

### Endurance (estimate)

- Holybro's figure: about 18 min hover with no payload on a 5000 mAh 4S pack *(spec)*.
- The Lite adds about 350 g (camera, radio, receiver, strobe, mounts), taking the aircraft from about 1.55 kg to about 1.9 kg.
- Hover power scales roughly with mass^1.5, which gives **about 13 min** (18 × (1.55/1.9)^1.5).
- `catalog.py` now uses 13 min instead of 15. Every route approval checks against the more conservative figure.
- With the 30 % return reserve that leaves about 9 minutes of flying: enough for the 1–3 minute inspection routes of a typical property.
- SITL cannot validate this. Its simulated battery is not the Lite's. Scenario `battery_drain_and_estimate` records SITL's drain next to the estimate only to show the comparison works.

## 5. Canadian landed cost (estimate)

- **Currency:** most listings are USD. At an assumed 1.37–1.40 CAD/USD, configuration C is about **CAD 1,950–2,600** before tax. Use the day's rate.
- **Duty:** UAV parts fall under Customs Tariff chapter 88 (8806/8807); radios and cameras under chapters 85/90. Many lines are duty-free MFN, but **each tariff item must be checked** in the CBSA 2026 schedule. This study did not verify every line.
- **Tax:** GST 5 %, or HST (13 % in Ontario) where it applies, on the duty-paid value.
- **Brokerage:** courier brokerage of about CAD 10–40 per shipment est. Consolidating orders (Holybro, SIYI, ELRS) saves more than chasing small price differences.
- **Batteries:** buy domestically.
- **Rough landed total for C:** about **CAD 2,250–3,050** est. (parts, HST, brokerage, domestic shipping).

## 6. Manufacturing preparation

Prototype and low-volume costs are estimated separately. **Retail prices are not manufacturing cost.**

### Prototype (1–3 units)

- **Parts:** at retail, as above.
- **Assembly:** 8–12 h for the first unit. That covers the frame, harness, gimbal and HM30 mounting, configuration, and commissioning with the Lite parameter set.
- **Bench acceptance:** 2–3 h.
- **Tethered and first flight tests:** 1 day with a qualified pilot.

### Low-volume (25–100 units/year) estimate

| Item | Prototype | Low-volume est. | Basis |
| --- | --- | --- | --- |
| Holybro kit/frame | retail | −10 % | Holybro store volume discount (10 % for 10–99 units) |
| SIYI A8 mini + HM30 | retail | −10–20 % est. | Distributor pricing, not quoted yet |
| Harness | hand-made | custom pre-made harness, CAD 15–30 est. | Removes most soldering. The single biggest assembly-time saving |
| Assembly labour | 8–12 h | 2.5–4 h est. | Fixtures, pre-made harness, flashing and parameter load scripted |
| End-of-line test | — | 1 h bench + 15 min tethered hover est. | `sentinel.drone.bench` automates the bench part |
| Spares per unit | — | 1 set of propellers, 1 arm, 1 pack est. | Batteries are consumables |

Low-volume parts cost for C is roughly 10–15 % below the prototype retail total. Labour, test, warranty reserve and certification overheads (`docs/drone-hardware-v1.md` sections 12–13) are added on top. That is not done here and is not a price.

### Harness simplification

- **One power path:** 4S PDB → XT30 to the HM30 air unit and the A8 mini (both 3S–6S); the PM02 feeds the FC.
- **One Ethernet lead:** A8 mini → HM30 air unit.
- **One serial lead:** FC TELEM1 → HM30 (MAVLink).
- **One serial lead:** FC TELEM2 → A8 mini (SIYI protocol, `MNT1_TYPE 8`), or the A8 mini's Ethernet virtual serial (`NET_*`), which only H7 boards with Ethernet support.
- **ELRS receiver:** to an FC UART (CRSF).
- **Every connector polarised and labelled.** No splices.

### Mechanical repeatability

- Gimbal and HM30 positions are fixed on the X500 rail mounts with a printed jig.
- The GPS mast is fixed to the same corner on every unit, away from the HM30 antennas.
- The compass is calibrated after the final assembly (record it in the maintenance log as `calibration`).

### Serviceability, spares and repair costs (estimates)

- **Propellers:** 1045 pairs at about $5–10. Replace after any ground contact.
- **Arms:** X500 V2 carbon arm spare listings exist (Unmanned Tech Shop).
- **Motors:** about $20–30 each est.
- **Gimbal:** the A8 mini is the most expensive item likely to be hit in a crash. Plan for one spare camera per 10–20 units in service.
- **Batteries:** replace at about 150–300 cycles, or when swollen or showing high internal resistance. Sentinel's maintenance log records battery replacements (`battery`).

### End-of-line quality control

Every unit, before it leaves:

1. **Propeller-free bench run:** `python -m sentinel.drone.bench run ...` (docs/drone-bench-testing.md), with **all automatic checks passing.**
2. **Firmware:** the pinned version, verified against the release hash.
3. **Commissioning:** the Lite parameter set loaded; `commissioning` recorded in Sentinel. The fingerprint is stored.
4. **Calibration:** compass and accelerometer calibrated and logged.
5. **Tethered hover:** 2 minutes, by a qualified pilot.
6. **Labels:** serial number, Transport Canada registration/marking space, battery warning, "pilot in command required" label.

### Firmware updates

- ArduCopter is pinned per release (currently 4.6/4.7; tested 4.7.2 in SITL).
- An update is a maintenance event: flash, re-load the commissioning set, re-run the bench acceptance, and record `firmware` plus a new `commissioning` in the log.
- Sentinel's readiness warns on any untested firmware version.

### Warranty-relevant failure modes (to design out or test)

- **Vibration-induced EKF errors:** gimbal or HM30 mounts loosening.
- **Connector fatigue:** JST-GH unseating, Ethernet lead to the gimbal.
- **LiPo abuse:** over-discharge, cold, storage at full charge.
- **Crash damage:** propellers, arms, gimbal.
- **Interference:** the HM30 antennas close to the GPS.
- **Moisture:** the Lite is not rain-proof.

## 7. Purchasing sequence (smallest useful first)

| Stage | Buy | Cost | Proves |
| --- | --- | --- | --- |
| **1. Bench, no motors** | X500 V2 PX4 dev kit (assemble the electronics, **leave the propellers off**) + SIYI A8 mini + one 4S pack or a bench supply + charger | **about $830–1,030** | On a real Pixhawk 6C: lockout probe blocked (`MAV_OPTIONS`), passive parameter and fence capture over the SiK link, `MAV2_*` stream rates, gimbal angle in telemetry, A8 mini RTSP into the Hub, power-cycle recovery, signing detection |
| 2. Radio | SIYI HM30 | $284–420 | Shared radio, video latency and range around a house, passive audit over the HM30 |
| 3. Flight | ExpressLRS radio and receiver, second pack, strobe | $165–270 | Tethered then open-field pilot-flown inspections, real endurance, wind, failsafes in the air (qualified pilot, insurance) |

Stage 1 is the recommended first purchase. Its exact acceptance criteria are in `docs/drone-bench-testing.md`. **Approval is the owner's decision.**

## Sources (checked 2026-10-14)

- **ArduPilot firmware feature lists (ArduCopter stable):**
  - [MatekH743](https://firmware.ardupilot.org/Copter/stable/MatekH743/features.txt)
  - [MicoAir743](https://firmware.ardupilot.org/Copter/stable/MicoAir743/features.txt)
  - [Pixhawk6C](https://firmware.ardupilot.org/Copter/stable/Pixhawk6C/features.txt)
  - [MatekF405-Wing](https://firmware.ardupilot.org/Copter/stable/MatekF405-Wing/features.txt) (and the other boards in the table, same URL pattern)
- **ArduPilot SIYI driver:** [ArduPilot SIYI gimbal page](https://ardupilot.org/copter/docs/common-siyi-zr10-gimbal.html) (supported models; "The Siyi A2 is not supported")
- **ArduPilot source** (MAVLink signing; system-id enforcement and routing order; Copter does not persist stream rates):
  - [GCS_Signing.cpp (Copter-4.7.0)](https://raw.githubusercontent.com/ArduPilot/ardupilot/Copter-4.7.0/libraries/GCS_MAVLink/GCS_Signing.cpp)
  - [GCS_Common.cpp](https://raw.githubusercontent.com/ArduPilot/ardupilot/master/libraries/GCS_MAVLink/GCS_Common.cpp)
  - [GCS_Param.cpp](https://raw.githubusercontent.com/ArduPilot/ardupilot/master/libraries/GCS_MAVLink/GCS_Param.cpp)
- **Holybro:**
  - [X500 V2 kits](https://holybro.com/products/x500-v2-kits) (ARF $329, frame $135, 18 min, 1500 g payload)
  - [S500 V2 kit](https://holybro.com/products/s500-v2-kit) (ARF $259, frame $42)
  - [X500 V2 PX4 dev kit ($507.99)](https://shop.holybro.com/x500-kit_p1180.html)
  - [Pixhawk 6C Mini ($176.99)](https://holybro.com/products/pixhawk-6c-mini)
  - [Pixhawk 6C ($211.99)](https://holybro.com/products/pixhawk-6c)
  - [PM02 V3 (GetFPV $18.99)](https://www.getfpv.com/holybro-apm-power-module-12s-pm02-v3.html)
- **Flight controllers:**
  - [MicoAir H743 V2 (Pyrodrone $72.99)](https://pyrodrone.com/products/micoair-h743-v2-2-6s-flight-controller-with-bluetooth-and-pre-loaded-with-ardupilot-30-30mm)
  - [MicoAir (Unmanned Tech Shop, from £40)](https://www.unmannedtechshop.co.uk/collections/micoair)
  - [Matek H743-SLIM V3 (RaceDayQuads $145.49)](https://racedayquads.com/products/matek-h743-slim-30x30-flight-controller)
  - [Matek H743-SLIM V3 (KiwiQuads)](https://www.kiwiquads.co.nz/product/mateksys-flight-controller-h743-slim-v3)
- **SIYI:**
  - [HM30 specification](https://www.siyi.biz/en/product/image-digital-link/hm30/spec/) (74 g, 3S–6S, 2.8 W, Ethernet, S.Bus)
  - [HM30 (OpenELAB €389.95)](https://openelab.io/a/s/products/siyi-hm30-fpv-system)
  - [HM30 (World Drone Market)](https://worldronemarket.com/product/siyi-hm30-video-transmission/)
  - [A8 mini (Irlock)](https://irlock.com/products/siyi-a8-mini-4k-8mp-ultra-hd-6x-digital-zoom-gimbal-camera-with-1-1-7-sony-sensor-ai-smart-identify-and-tracking-hdr-starlight-night-vision-mini-3-axis-stabilizer-95g-lightweight-55x55x70mm-for-uav-ugv-usv-rc-planes-and-fpv-drones)
  - [A8 mini launch thread (ArduPilot forum, specs and price)](https://discuss.ardupilot.org/t/siyi-a8-mini-4k-ai-mini-zoom-gimbal-camera-ai-identify-tracking-4k-1-1-7-inch-8mp-sony-sensor-6x-zoom/92133/1)
  - [A8 mini (studiosport.fr: 11–25.2 V, 5 W average)](https://www.studiosport.fr/nacelle-camera-a8-mini-pour-drone-siyi-a31536.html)
  - [A8 mini product page](https://www.siyi.biz/en/product/tri-axis-single-camera-gimbal/a8-mini)
  - [A2 mini (Irlock $101.29)](https://irlock.com/collections/all/products/copy-of-siyi-a8-mini-4k-8mp-ultra-hd-6x-digital-zoom-camera-with-3-axis-gimbal-1)
- **GPS:** [Matek M10Q-5883 (RaceDayQuads $47.99)](https://www.racedayquads.com/products/matek-m10q-5883-gnss-compass)
- **ExpressLRS:**
  - [ExpressLRS MAVLink docs](https://www.expresslrs.org/software/mavlink/)
  - [ELRS issue #3553](https://github.com/ExpressLRS/ExpressLRS/issues/3553)
  - [RadioMaster Pocket (FlyingTech)](https://www.flyingtech.co.uk/product/radiomaster-pocket-radio-transmitter-2-4g-cc2500-elrs/)
  - [ELRS receivers (Motion RC EU)](https://motionrc.eu/collections/radiomaster-rc-electronics)
- **Batteries and chargers:**
  - [Tattu 4S 5200 (RaceDayQuads $68.49)](https://racedayquads.com/products/tattu-14-8v-4s-5200mah-35c-lipo-battery-xt60)
  - [Powerhobby 4S 5000 (WREKD Canada)](https://ca.wrekd.com/products/powerhobby-4s-14-8v-5000mah-100c-lipo-battery-w-xt60-plug-hard-case-lcg)
  - [ISDT 608AC (RaceDayQuads $77.99)](https://www.racedayquads.com/products/isdt-ac608-200w-ac-60w-dc-8a-2-6s-smart-lipo-charger-w-detachable-ac-power-supply?aff=2)
  - [ToolkitRC M7AC (Rotorvillage CAD 118.99)](https://rotorvillage.ca/toolkitrc-m7ac-charger-300w-15a/)
- **Canada:** [CBSA Customs Tariff chapter 88 (2026)](https://www.cbsa-asfc.gc.ca/trade-commerce/tariff-tarif/2026/01-99/ch88-2026-eng.pdf)
- **Complete aircraft:** ArduPilot forum threads on RTF options ([1](https://discuss.ardupilot.org/t/in-market-for-rtf-arf-ardu-based-copter/25703), [2](https://discuss.ardupilot.org/t/cancelled-two-low-cost-rtf-drones-for-small-camera-investigation/126713))
