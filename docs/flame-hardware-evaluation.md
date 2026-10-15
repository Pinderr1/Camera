# FLAME Mode: hardware compatibility and commercial evaluation

Status 2026-10-15. Research and desk analysis only. **No accessory, aircraft, thermal camera or ignition equipment has been bought, built, weighed or flown.** Software scope and limits are in [FLAME Mode](flame.md); the aircraft is described in [drone hardware V1](drone-hardware-v1.md) (the Sentinel Thermal reference design).

Every figure is labelled:
- **published**: manufacturer or distributor listing (sources at the end);
- **estimate**: our own scaling or assumption, to be replaced by a measurement;
- **computed**: produced by `drone/accessories.py` from the two above (the same numbers the dashboard shows).

## 1. Answer in brief

- The X650 Thermal configuration can carry a **monitoring-only** accessory with a comfortable margin on paper. It already has the thermal camera FLAME Mode needs.
- It **cannot realistically carry an aerial-ignition payload such as the IGNIS III Mini**:
  - the take-off mass would sit at about 92% of the frame's published limit;
  - the payload uses a proprietary mount and app;
  - Sentinel will not control ignition hardware.
- FLAME Mode is therefore a thermal **watch** of a burn the crew lights by its usual approved means.

## 2. Existing aircraft and payload margin

| Item | Value | Basis |
| --- | --- | --- |
| X650 kit mass without battery | 2,000 g | published (Holybro) |
| X650 maximum take-off | 6,300 g **at 70% throttle** | published (Holybro) |
| X650 payload | 4.3 kg without battery; 3.5 kg with a 5.2 Ah (750 g) pack; 3.1 kg with a 10 Ah (1,200 g) pack | published (Holybro) |
| X650 hover time | up to 30 min, no payload, 6S 10 Ah | published (Holybro) |
| Sentinel Thermal take-off mass (ZT6, HM30, SF45/B, CM4, 2nd GPS, lights, wiring, 6S 10 Ah) | about 3.9 kg | estimate ([drone-hardware-v1.md](drone-hardware-v1.md) §4, items partly published) |
| Sentinel Thermal hover time | about 22 min | estimate |
| Planning limit used by Sentinel | 80% of 6,300 g = 5,040 g | estimate (margin for wind, gusts, battery sag) |

### Accessory budgets (computed)

| Accessory on Sentinel Thermal | Accessory mass | Take-off | Share of 6.3 kg | Margin to 6.3 kg | Margin to planning limit | Hover (est.) | Before 30% reserve |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Sentinel burn-monitoring kit (reference design) | 250 g (estimate) | 4.15 kg | 66% | 2,150 g | 890 g | about 20 min | about 14 min |
| IGNIS III Mini, loaded (225 spheres + antifreeze, includes its LYNX camera) | 1,870 g (published) | 5.77 kg | 92% | 530 g | **−730 g** | about 12 min | about 8.6 min |

The endurance figures scale hover power with mass^1.5 at a fixed battery. They are not measurements.

What the margin means:
- **Monitoring kit:** realistic on paper. Its value is the quick-release mount, identification, heat shield and temperature probes, not lift.
- **IGNIS-class payload:**
  - it exceeds the planning limit;
  - it leaves about 0.5 kg below a limit that is itself specified at 70% throttle;
  - it leaves under 10 minutes of useful flight.

  Burns also create turbulent, convective air, which needs more control authority, not less. The Pro, which shares the X650 frame, has the same mass result. The Lite (X500 V2) has no published take-off limit and no thermal camera.

## 3. Phase 1 audit by topic

| Topic | Finding | Status |
| --- | --- | --- |
| Carry capacity | Monitoring kit: fits (above). Ignition payload: not realistic | computed from published + estimate |
| Centre of gravity | The kit is designed to mount on the centreline below the battery. A real CG is unknown until the aircraft and kit are built | **needs measurement**: CG with kit and full battery; re-run AutoTune if hover throttle or attitude response change |
| Battery endurance | About −2 min with the kit; about −10 min with an IGNIS-class load | estimate |
| Electrical | Kit about 2 W from the CM4 5 V rail; Thermal auxiliary load is about 25 W | estimate; check rail capacity and the 12 V BEC on the bench |
| Communication | Kit health over the CM4 companion (read-only). IGNIS: proprietary app; no published interface | software built for the simulated kit; companion agent support **not built** |
| Camera and thermal | The SIYI ZT6 already provides 640×512 thermal | ranges published by distributors, not SIYI: high gain −20 to 150 °C, low gain 50 to 550 °C, ±2/±5 °C |
| Field of view | About 32.9° × 26.6° | estimate (13 mm lens on a 12 µm core, assumed) |
| Radiometric data | Whether the ZT6 exposes per-pixel temperatures to the Hub is **unverified** | real thermal ingestion **not implemented** |
| Detection on thermal video | Sentinel's detector is not validated on thermal or aerial video | hotspots are temperature thresholds, simulated only |
| Heat and environment | Aircraft rated to about 40 °C air (estimate); the ZT6 to IP53 (published) | radiant heat, plume turbulence, ash in motors and smoke over optics **untested**; standoff and height limits to set by test |
| Stability and reliability | Payload raises hover throttle and reduces gust margin | needs tethered hover, AutoTune and logs with the kit |
| Reuse | Airframe, FC, companion, ZT6, HM30, ExpressLRS, lidar, Sentinel software, approvals, geofence, logs: all reused unchanged | design |

## 4. What IGNIS III Mini teaches us

Published facts (Drone Amplified):
- 225 spheres of 19 mm / 2.4 g; up to 120 drops per minute; 2–5 min reload;
- spheres ignite 20–40 s after a 0.2 mL injection;
- 1.87 kg loaded and 1.25 kg empty, both including the LYNX camera;
- a smart dovetail electro-mechanical mount;
- sold as one integrated system: the Freefly Astro (3.52 kg empty, 7.46 kg loaded, 22–30 min loaded), a Pilot Pro controller and the D.A. FLIGHT app;
- an IGNIS safety geofence set in the app before take-off;
- vendor training, a field guide, a repair and loaner plan, and a "team burn" service in the US South;
- packed in three cases (92.3 lb);
- patented, with an exclusive licence from NUTech Ventures.

The earlier IGNIS II was offered on heavier US-made aircraft: the Freefly Alta X and the Inspired Flight IF1200A (2024 announcement).

Lessons for Sentinel:
1. **The aircraft is sized for the payload.** IGNIS runs on 7–9 kg-class aircraft. The Astro Max lists 8.7 kg MTOW and 3 kg payload (published). The X650 is a 6.3 kg-at-70%-throttle frame.
2. **Integration is a product.** Mount, power, data, app, geofence, training and service ship together; a third-party aircraft is not a plug-in target. There is no published interface Sentinel could read.
3. **Thermal is part of the ignition package.** Every IGNIS kit includes an EO/IR camera. That is the part Sentinel can offer, on the aircraft it already has.
4. **The ignition geofence lives with the ignition system.** That is the right place for it, and it stays out of Sentinel.
5. **Commercial value comes from logistics and support:** cases, minutes-to-airborne, loaners, training and a burn service. Sentinel's equivalent is one aircraft that serves both security and the farm, records and reports, and the Guide.

## 5. Regulatory notes (engineering research, not legal advice)

- **Dropping objects.** Canada's 2025 drone rules prohibit creating a hazard to people or property by dropping an object from a drone in flight; Transport Canada guidance on "hazard" is pending. Any ignition payload is squarely in scope and would need Transport Canada's view, likely an SFOC.
- **Dangerous goods.** The TDG exemption for dangerous goods carried by aircraft to or from a location for fire *suppression* does not clearly cover *ignition* material. Unresolved.
- **Burn permission.**
  - Manitoba: a daily crop-residue authorization (issued by 11:00), permits in ten municipalities from 1 August to 15 November, no night burning, smoke kept off roads and towns, fines up to $50,000.
  - Saskatchewan: burning is controlled by municipal bylaws and fire bans.

  Sentinel records what the burn boss enters and never states that a burn is approved, legal or safe to light.
- **Drone operations:** a pilot certificate suited to the operation, airspace checks (including forest-fire restriction areas) and insurance, as in [drone-hardware-v1.md](drone-hardware-v1.md) §13.

## 6. What the hardware engineer must validate before any physical integration

On the bench (propellers off):
1. Weigh the built kit; record the measured mass in commissioning (it replaces the 250 g estimate).
2. Kit electrical draw on the CM4 5 V rail, and the 12 V BEC headroom with the ZT6, HM30 and lights all on.
3. Read the ID chip and the latch switch, and both temperature probes, through the CM4. Then build the companion report the software expects: `attached`, `latch_closed`, `identity_ok` and the two temperatures. Today this report exists only in the simulator.
4. ZT6: confirm the field of view and measuring ranges against SIYI's manual, and whether radiometric data reach the Hub (SDK or stream).
5. EMI: GPS satellites and HDOP, and compass interference, with the kit powered.

Tethered, then open field (pilot-flown, Sentinel observes only):
1. CG check and AutoTune with the kit; hover throttle (`MOT_THST_HOVER`) with and without it.
2. Measured endurance with the kit, to replace the 20 min estimate.
3. Thermal performance against a controlled heat source:
   - detection range and position error against surveyed markers;
   - saturation behaviour;
   - smoke attenuation.
4. Heat exposure: shield and bay temperatures at increasing heights and offsets downwind of a small controlled fire, to set the minimum standoff and height Sentinel should enforce.
5. Ash and smoke: inspect motors, gimbal and lidar after exposure, and define the post-operation maintenance step.
6. Link and video range over open farmland at the planned watch distances.

Before any ignition-payload discussion (not planned):
- a vendor-supported integration on an aircraft sized for it;
- a Transport Canada position (SFOC) and a dangerous-goods review;
- insurance;
- a recorded product, legal and safety decision in CLAUDE.md.

**Improvised ignition devices (for example a lighter and an aerosol can) are excluded outright.** They are hazardous to the crew and aircraft, uncontrolled, and fall under the hazard rule above.

## 7. Implemented now vs blocked

| Implemented and simulated (works today) | Blocked by hardware validation or decisions |
| --- | --- |
| Accessory registry, compatibility records, commissioning, inspections, faults (simulated kit) | Real kit build, ID/latch/temperature reports from the CM4 |
| Security ↔ agricultural configuration with ground-only switching and request separation | Measured mass, CG and endurance with the kit |
| Fields, watch strips, sensitive places, smoke direction against operator limits | Real thermal ingestion (ZT6 radiometric interface) |
| Checklists, crew, equipment, permits and qualifications recorded (not verified) | Validated thermal detection, saturation and smoke behaviour |
| Thermal watch with coverage and position uncertainty; heat outside, at and near the boundary; notifications | Heat standoff and height limits from tests |
| Reports with fingerprint; AI Guide tools; dashboard tab | Any ignition payload (excluded) |
| SITL: the scenario runs on ArduCopter 4.7.2 with simulated thermal, fault, low-battery RTL and link-loss failsafe | Physical flight of any Sentinel aircraft |

## Sources (checked 2026-10-14)

- Drone Amplified: [IGNIS III / III Mini](https://droneamplified.com/ignis-iii/), [IGNIS II](https://droneamplified.com/ignis/), [Inspired Flight IF1200A partnership (2024-04-22)](https://droneamplified.com/drone-amplified-ignis-inspired-flight-partnership/)
- Holybro: [X650 development kit](https://docs.holybro.com/drone-development-kit/x650-development-kit/overview)
- Freefly: [Astro Max specifications](https://freeflysystems.com/astro/specs)
- SIYI ZT6 (distributor listing): [irlock](https://irlock.com/products/siyi-zt6-mini-optical-pod-dual-sensors-4k-8mp-6x-digital-zoom-gimbal-camera)
- Regulation: [Mondaq on Canada's 2025 drone rules](https://www.mondaq.com/canada/aviation/1611048/out-of-sight-and-oversight-business-opportunities-and-obligations-of-new-drone-regulations), [TDG Regulations s. 12.7](https://laws.justice.gc.ca/eng/regulations/SOR-2001-286/section-12.7-20260617.html), [Manitoba crop residue burning program](https://www.gov.mb.ca/agriculture/crops/crop-residue-burning-program/), [Saskatchewan fire bans (municipal)](https://suma.org/advocacy/resolutions?id=32)
