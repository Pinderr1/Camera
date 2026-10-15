# Drone bench testing (hardware in the loop, propellers off)

Status, 2026-10-14:
- The bench tool (`sentinel.drone.bench`) is built and unit-tested.
- It has been run against ArduPilot SITL as a stand-in for hardware.
- **It has never touched a real flight controller.** No hardware has been bought.

Purpose: prove the chain **Sentinel Hub → link → flight controller → camera** on real parts before anything flies, and keep the evidence. The bench stage is the first purchase in `docs/drone-lite-cost.md` section 7.

## Safety rules (enforced by the tool)

- **Propellers off.** The tool refuses to start until the operator types `props off`. Every manual checklist ends with the propellers still off.
- **Never armed.** If the aircraft reports armed at any moment, the run aborts and fails.
- **No flight commands.** The tool connects with `MavlinkTelemetryAdapter`, which refuses every flight command in code. It sends only:
  - the Hub's normal ground-station heartbeat;
  - telemetry-rate requests;
  - one read-only request (camera information, `probe_command_path`) whose acknowledgement shows whether the Hub's commands reach the flight controller.
- **No parameter writes.** Parameters are read and audited. Fixes are made by a person in the ground station.

## Supported firmware

| Item | Supported | How it is verified |
| --- | --- | --- |
| Flight stack | ArduCopter 4.6.x, 4.7.x (SITL-tested: 4.7.2) | `firmware` check reads `AUTOPILOT_VERSION` (version and git hash). Use `--expected-firmware 4.7.2` to pin it |
| Board | Pixhawk 6C (Lite baseline). H743 boards whose official build includes the SIYI driver, fence and signing (`docs/drone-lite-cost.md` section 3) | Official `features.txt` before purchase; the bench run after |
| Gimbal | SIYI A8 mini, firmware 0.1.7 or later (ArduPilot SIYI driver) | `gimbal` check: attitude reports present |

With the Lite's command lockout on, the Hub cannot request `AUTOPILOT_VERSION` itself. Connect the pilot's ground station once (it asks), or record the version by hand. The tool then marks the check `manual`.

## Running it

```powershell
$env:PYTHONPATH='src'
python -m sentinel.drone.bench run --connection udpin:0.0.0.0:14551 --model sentinel_lite `
    --video rtsp://192.168.144.25:8554/main.264 --measured-voltage 16.42 --expected-firmware 4.7 --interactive `
    --out bench_runs/lite_bench_1
```

- **Stage 1 (USB or SiK, before the HM30 exists):** `--connection COM5` (USB) or the SiK radio's COM port. Over USB, ArduPilot accepts unsigned packets on channel 0, so **run the lockout check over a radio port, never USB**.
- **The bundle it writes:**
  - `report.md` and `report.json`: every check, its evidence, how to fix it;
  - `parameters.param` (Mission Planner format: the commissioning record);
  - a telemetry snapshot, link statistics and firmware details.

## The checks

| # | Check | Automatic | Pass criterion |
| --- | --- | --- | --- |
| 1 | Telemetry link | yes | Heartbeat from exactly one autopilot within 20 s |
| 2 | Firmware | yes (manual under the lockout) | ArduCopter 4.6/4.7; matches `--expected-firmware` |
| 3 | Parameters | yes | Full set read. Failsafe, fence and model audit with **no blocking finding** (`MNT1_TYPE` absent is a warning) |
| 4 | **Hub command lockout** | yes | `probe_command_path` gets **no acknowledgement**, **and** the parameters show a recognised lockout (`MAV_OPTIONS` bit 0 with `MAV_GCS_SYSID` ≠ 245, MAVLink signing, or a companion relay) |
| 5 | Telemetry rates | yes | At the Hub, measured over 10 s: HEARTBEAT ≥ 0.8 Hz, GLOBAL_POSITION_INT ≥ 1.5 Hz, SYS_STATUS ≥ 0.8 Hz, GPS_RAW_INT ≥ 0.8 Hz, ATTITUDE ≥ 1.5 Hz |
| 6 | GPS detected | yes | GPS reports present. A 3D fix is required outdoors (a warning indoors) |
| 7 | Battery monitor | yes | Voltage plausible for the cell count; **within 0.15 V of a multimeter**; current 0.05–8 A with the propellers off |
| 8 | Navigation and pre-arm | yes | EKF reported. Pre-arm failures explained (GPS ones are expected indoors) |
| 9 | Gimbal angle | yes | Gimbal attitude reports present (`MNT1_TYPE 8`) |
| 10 | Camera stream | yes | At least 3 **proven-live** frames (sensor noise or change, never a frozen or blank picture), ≥ 2 fps |
| 11 | Evidence storage | yes | Write, read back and hash a file in the media folder; ≥ 2 GB free |
| 12 | Link reconnection | operator acts, tool observes | Unplug the link for 5 s: the loss is seen and telemetry returns within 60 s |
| 13 | Power cycle | operator acts, tool observes | Restart detected (the boot clock goes back and three messages confirm it); settings re-read; telemetry returns within 90 s |
| 14 | Pilot radio failsafe | operator acts, tool observes | Transmitter off: the flight controller reports a radio failsafe |
| 15–19 | Manual items | operator confirms | Propellers off; compass and accelerometer calibrated; **video latency measured** glass-to-glass; gimbal follows the pilot's pitch control; GPS keeps its satellites with the video radio transmitting |

Verdicts:
- **pass:** no check failed and every manual item is confirmed.
- **incomplete:** nothing failed, but manual items are open.
- **fail:** any check failed, the aircraft armed, or there was no link.

## Acceptance criteria before the first hardware purchase

These must all hold **in software** before money is spent. They are the go/no-go for buying stage 1:

| # | Criterion | Status (2026-10-14) |
| --- | --- | --- |
| A1 | The bench tool runs end-to-end against ArduPilot SITL and reports `pass` on a correctly commissioned Lite (`tests/test_sentinel_drone_bench_sitl.py`) | see the final engineering report |
| A2 | The lockout probe is `blocked` with the Lite commissioning (`MAV_OPTIONS`), `blocked` with signing alone, and `open` without either (negative control) | SITL scenarios `unauthorized_hub_commands`, `signing_lockout` |
| A3 | Every failure scenario in `docs/drone-reliability.md` either passes or is listed as `not modelled`, with the reason | SITL fault report |
| A4 | Full offline test suite passes | see the final engineering report |
| A5 | No path in Sentinel sends a flight command to a non-simulator (the code review boundary in CLAUDE.md, product boundary 7) | unchanged: flight commands exist only in `SitlMavlinkAdapter` |

## Acceptance criteria for the bench stage (on real parts)

The Lite bench stage is accepted when, **on a real Pixhawk 6C, A8 mini and 4S pack, with the propellers off**:

1. The bench tool reports **pass** over a radio port (SiK at stage 1, HM30 at stage 2).
2. The lockout probe is **blocked** with `MAV_OPTIONS 1`.
3. The negative control: with `MAV_OPTIONS 0` temporarily, the probe is **open**. That proves the radio carries the Hub's packets, so "blocked" really means the lockout. **Restore `MAV_OPTIONS 1` afterwards**, and re-run check 4.
4. The parameter fingerprint is recorded as `commissioning` on Sentinel's Drone tab.
5. Gimbal attitude reports arrive and the pose source in a test inspection video is `gimbal_reported`.
6. Glass-to-glass video latency is measured and recorded. **Target ≤ 500 ms at the Hub. Over 1 s blocks the flight stage.**
7. Five power cycles and five link unplugs are all recovered by Sentinel without a restart of Sentinel. Each FC power cycle is detected as a restart.
8. The battery reading is within 0.15 V of a multimeter at three charge levels (full, storage, near the low threshold).
9. The A8 mini's horizontal field of view is measured (target at a known distance) and entered in the drone's video settings. The catalog values are listings, not measurements.

Only then does stage 3 (propellers on, tethered hover, qualified pilot) begin.

## What the bench cannot show

- Behaviour in the air: vibration, EKF in flight, failsafes in flight, endurance, wind.
- Radio range and interference around a real house.
- That ArduPilot's in-flight failsafes behave on real hardware as they do in SITL. SITL runs the same firmware, but its sensors, timing and physics are simulated.
