# Project Completion Roadmap

Last audited: 2026-07-10

## Project goal

Build a local-first, privacy-preserving nighttime safety monitor for one consenting older adult who may get out of bed and walk to the bathroom. The system should recognize normal nighttime routines, identify concerning patterns such as a possible fall, prolonged floor-level stillness, bathroom overstay, failure to return to bed, manual requests for help, and loss of critical sensor coverage, then give caregivers enough context to perform a timely human check.

This is an assistive monitor. It is not a medical device, proof-of-life system, breathing monitor, diagnosis tool, or automatic emergency-dispatch system. A human caregiver remains responsible for checking the person and deciding whether emergency services are needed.

## What “complete” means

For this repository, complete means a dependable version 1 system for the intended household—not a generally certified commercial product.

The project is complete when all of the following are true:

- The monitored person has knowingly consented and can pause or disable monitoring.
- The actual home, camera, sensors, network, caregivers, and response policy are configured—not example placeholders.
- The monitor starts automatically, survives restarts and temporary failures, reports when it is blind, and cannot fail silently.
- Normal nighttime trips are correctly represented in logs.
- Every agreed safe staged scenario passes end to end.
- Observation-only results meet the notification gates in this document.
- Alerts reach the correct caregiver, can be acknowledged, escalate to a backup when required, and are reviewed afterward.
- Privacy, retention, deletion, access, backup, recovery, and maintenance procedures are documented and tested.
- The system completes a 30-night monitored household pilot without an unresolved safety-critical defect.

Commercial sale, diagnosis claims, automatic emergency calling, multi-household cloud hosting, and regulatory certification are separate future projects.

## Executive status

**Current stage: functional engineering prototype; not ready for unattended caregiver alerting.**

The core design is strong and unusually well documented for this stage. The state machine, camera pose extraction, replay/evaluation tools, MQTT bridge, Home Assistant examples, personal-baseline tooling, and morning-review workflow exist. The largest remaining work is real-world evidence and operational reliability, not adding more detector features.

| Area | Current state | Evidence / completion gap |
| --- | --- | --- |
| Product and safety boundaries | Mostly complete | Goal, privacy posture, safety limits, architecture, setup checklist, and runbook exist. The emergency plan still contains blank household contacts and response details. |
| State machine | Prototype complete | Normal trip, overstay, no-return, fall/stillness, manual help, sensor health, timing, cooldown, and night-window behavior are implemented and tested. |
| Camera pipeline | Functional but not field-validated | MediaPipe extraction, pose-only JSONL, zones, calibration, health events, replay, and threshold tuning exist. Current local recordings expose camera-placement and false-alert problems. |
| Offline evaluation | Functional | URFD download/replay/scoring and threshold sweep exist. Public staged data is useful but cannot replace testing in the actual room. |
| Local evidence | Insufficient | There are 3 local pose recordings; one is empty and only 2 have labels. Both labeled no-fall recordings currently produce false urgent alerts under the audited configuration. |
| Routine baselines | Not started | No personal baseline files exist. Routine-transition and sensor-event label tables contain headers only. |
| Observation trial | Not started / not recorded | The required 7–14 nights of observation evidence and 30-night stable-baseline period are not present. |
| Notifications | Lifecycle implemented; field delivery untested | Durable IDs, acknowledgement/resolution tracking, backup escalation, restart deduplication, and primary/backup Home Assistant examples exist. Local soft check, pause flow, and real-phone delivery drills remain incomplete. |
| Unattended operations | Incomplete | Alert lifecycle state and fail-closed configuration validation now exist. No install/service definition, detector-incident recovery, broker reconnect test, log rotation, disk-space guard, retention enforcement, or backup/restore procedure is present. |
| Security and privacy operations | Partial | Local processing, no audio, pose-first storage, ignored secrets/media, and bathroom-video restrictions are documented. MQTT TLS, access review, consent records, enforced deletion, and production secret setup are not complete. |
| Automated quality controls | Partial | All 65 unit tests pass. Configuration, alert lifecycle, state-machine, pose, replay, and baseline behavior have unit coverage. There is no CI workflow, pinned/locked environment, lint/type gate, MQTT/Home Assistant integration test, hardware test, or release checklist. |
| Repository state | Uncommitted engineering changes | The alert lifecycle, configuration validation, documentation, examples, and roadmap updates are in the working tree. `main` is also one commit ahead of `origin/main`; none of this completion work has been committed or pushed. |

## Verified snapshot

The following was rechecked during this audit rather than inferred from the README:

- **Tests:** 65 of 65 pass with `python -m unittest discover -s tests`.
- **Current URFD replay:** 70 clips scored; 29 of 30 fall clips detected (96.7% recall); 15 of 40 no-fall clips produced a possible-fall signal; possible-fall precision is 65.9%; 0 urgent false clips; 19 fall clips reached urgent severity. Thirteen of the possible-fall false positives are deliberate floor activities.
- **Current local replay:** 2 labeled no-fall clips scored; both produce possible-fall and urgent false alerts. A third pose recording is empty.
- **Local labels:** 2 clip rows; 0 labeled event rows; 0 sensor-event rows; 0 routine-transition rows; 0 alert-review rows.
- **Personal baselines:** none generated.

The public benchmark demonstrates that the pipeline can detect staged fall shapes. It does not establish household readiness. The local false alerts, limited sample size, able-bodied test subject, camera-angle warning, and lack of overnight data are the governing evidence.

## What is already built

- A normalized sensor-event and detector-decision contract.
- A timestamp-driven nighttime state machine with periodic ticks.
- Normal bathroom-trip, bed-exit/no-return, bathroom-overstay, fall/stillness, manual-help, manual-cancel, sensor-offline, cooldown, and night-window logic.
- Pose extraction with zone logic, landmark-quality handling, track-discontinuity protection, pose-only recording, camera heartbeat events, and calibration support.
- A single-process local pilot that does not require MQTT.
- An MQTT bridge that publishes retained state and alert messages and writes decision/transition logs.
- Home Assistant notification/dashboard examples and ESPHome sensor examples.
- Morning review and personal-baseline computation tools.
- Pose replay with predictions, false-positive/negative lists, latency, recall, precision, coverage, and urgent-alert metrics.
- URFD ingestion, cached pose extraction, replay, and threshold sweeping.
- Privacy-aware `.gitignore` rules for media, secrets, models, and operational logs.
- Unit tests covering the primary decision paths and important camera-pose edge cases.

## Work left before completion

### 1. Close the safety and household decisions

- [ ] Fill out a private copy of `docs/emergency-response-plan.md` with the real primary caregiver, backup caregiver, nearby keyholder, address/access details, availability, and response timeouts.
- [ ] Record informed consent outside the repository and define how the monitored person pauses monitoring.
- [ ] Confirm at least one manual backup that works during camera, network, and power failure: help button, phone, pendant, or reliable voice command.
- [ ] Agree on monitoring hours, notification recipients, acknowledgement timeout, backup timeout, and what each severity means.
- [ ] Decide the local media/pose/log retention periods and who may access or delete the data.
- [ ] Explicitly keep automatic emergency calls disabled for version 1.

**Exit gate:** the household can explain what happens for low, urgent, offline, unacknowledged, false, and missed alerts without relying on unwritten assumptions.

### 2. Fix the physical setup before tuning algorithms

- [ ] Remount the route camera at chest height or above, angled down, with the full floor path and full body visible.
- [ ] Keep bathroom interior, screens, medication, documents, and other private areas out of frame.
- [ ] Create ignored household-specific copies of the rules, sensors, and zones configs; do not operate from `*.example.json`.
- [ ] Run camera calibration after the final mount is fixed.
- [ ] Redraw and visually verify bed, bed-exit, route, bathroom-door, and floor-risk zones.
- [ ] Install and validate the real bed, route, bathroom, door, help/cancel, power-backup, and network-health components that the final design uses.
- [ ] Test night lighting, occlusion, blankets, pets, visitors, mobility aids, furniture movement, and multiple people in frame where relevant.

**Exit gate:** a staged normal walk covers the entire route without pose loss or floor misclassification, every critical sensor produces health events, and no private zone is visible.

### 3. Complete the alert and response workflow

The core alert lifecycle is implemented and unit-tested. The remaining work is the optional local soft check, pause/resume behavior, richer caregiver outcomes, and real-device delivery drills.

- [x] Define normalized events for caregiver acknowledgement and alert resolution.
- [ ] Define normalized events for pause/resume and soft-check response.
- [x] Give every alert a durable unique ID and include timestamp, last zone, sensor health, and incident age in the payload.
- [x] Implement acknowledgement tracking and record responder plus acknowledgement time.
- [x] Implement and test backup-caregiver escalation after the configured timeout.
- [ ] Implement the optional local soft check and a clear “I am okay”/cancel path if the monitored person accepts it.
- [ ] Add caregiver actions for checked-real, checked-safe, false alarm, nuisance, uncertain, pause, and call-backup.
- [x] Ensure observation-only mode blocks external low/urgent notifications even if Home Assistant is misconfigured.
- [x] Prevent duplicate notifications across process restarts and MQTT redelivery.
- [ ] Test notification delivery when phones are locked, in sleep/focus mode, on cellular data, and temporarily offline.

**Exit gate:** a scripted manual-help and no-ack scenario reaches the primary, records acknowledgement when provided, escalates to the backup when not provided, and leaves a complete review row.

### 4. Make the runtime safe for unattended operation

- [ ] Package the bridge and extractor for reproducible installation on the target computer.
- [ ] Pin a known-good dependency set and document the Python and OS versions.
- [ ] Add automatic startup, process supervision, bounded restart/backoff, and a visible service-health indicator.
- [ ] Add robust MQTT reconnect/resubscribe behavior and test broker restart, Wi-Fi loss, duplicate messages, malformed messages, and out-of-order events.
- [ ] Decide what state persists across restart so an active incident is not silently forgotten.
- [ ] Validate local timezone and daylight-saving transitions; store machine timestamps unambiguously while keeping caregiver-friendly local time.
- [ ] Add startup readiness checks for camera, model, broker, configuration, clock, writable storage, and critical sensors.
- [ ] Enforce log/pose retention, rotation, maximum disk usage, and low-disk alerts.
- [ ] Add MQTT authentication and TLS or isolate the broker on a trusted local network with a documented threat decision.
- [x] Add config validation so invalid zones, thresholds, sensor IDs, or alert settings fail closed with a clear error.
- [ ] Define behavior for corrupted models/files, camera replacement, changed resolution, and unplug/replug events.

**Exit gate:** after power loss, broker restart, camera disconnect, and full-computer restart, the system automatically recovers or sends a clear offline/blind alert; it never appears healthy while inactive.

### 5. Build the local evidence set

Follow the existing project target of at least 200 labeled local clips or sensor sequences and 7–14 observation nights. Use able-bodied staged falls only when safely supervised; never ask the monitored older adult to stage a fall.

- [ ] Label empty room, normal walk, bed exit, bathroom trip/return, sitting, standing, slow lie-down, bending, kneeling, floor activity, blankets/laundry, pets, poor light, occlusion, mobility aids, visitors, sensor loss, and safe staged falls.
- [ ] Capture actual routine sensor transitions over at least 7–14 nights with notifications disabled.
- [ ] Review every unusual transition, possible fall, offline period, nuisance candidate, and missed concern each morning.
- [ ] Populate `clips.csv`, `events.csv`, `sensor_events.csv`, `routine_transitions.csv`, and `review_log.csv` rather than relying on detector output alone.
- [ ] Generate first personal bathroom-duration and bed-return baselines after enough valid nights.
- [ ] Keep training, tuning, and final evaluation scenarios separated so thresholds are not scored on the same examples used to choose them.

**Exit gate:** 200+ reviewed local scenarios, at least 7 valid nights, no unexplained monitoring gaps, and a documented list of every known failure mode.

### 6. Tune and validate against household acceptance gates

Fix systematic causes before adjusting thresholds. Camera placement, zone geometry, sensor coverage, labeling errors, and timestamp issues should not be hidden by looser rules.

- [ ] Eliminate the two known local urgent false alerts and add regression cases for their exact causes.
- [ ] Review the one missed URFD fall and all local missed/high-risk scenarios.
- [ ] Treat deliberate floor activity separately from accidental fall shapes and require context or a soft check before escalation where appropriate.
- [ ] Measure normal-trip correctness, possible-fall recall/precision, urgent precision, alert latency, false alerts per night/week, pose coverage, sensor uptime, and acknowledgement time.
- [ ] Re-run the full local holdout, URFD, state-machine suite, and end-to-end scenarios after every threshold/config change.
- [ ] Record the final chosen configuration and why it was selected.

Minimum gates before low-severity notifications:

- [ ] At least 7 observation nights.
- [ ] Normal bed-to-bathroom-to-bed transitions are understandable and correctly sequenced in logs.
- [ ] Critical sensor failures always become `offline_or_blind` or an equivalent explicit alert.
- [ ] Fewer than 1 low-severity nuisance candidate per night.
- [ ] Every agreed safe staged fall/help/offline scenario is detected in the final household setup.

Minimum gates before urgent overnight notifications:

- [ ] At least 14 observation nights preferred.
- [ ] Fewer than 1 urgent false-alert candidate per week during observation.
- [ ] Zero unexplained urgent misses in the final staged household scenario set.
- [ ] Primary and backup delivery, acknowledgement, cancellation, and escalation drills all pass.
- [ ] Caregivers explicitly accept the observed false-alert rate and response burden.

Public-dataset scores are supporting evidence, not a substitute for these household gates.

### 7. Add software quality and release controls

- [ ] Add continuous integration that runs all unit tests on every change.
- [ ] Add formatting/linting and, where useful, static typing checks.
- [x] Add tests for alert payloads, malformed configuration, sensor-ID event validation, MQTT redelivery deduplication, and acknowledgement escalation.
- [ ] Add tests for MQTT reconnect, detector restart recovery, retention, and timezone boundaries.
- [ ] Add a small end-to-end test using a local broker and simulated Home Assistant events.
- [ ] Add a hardware acceptance script/checklist for camera and each configured sensor.
- [ ] Add a versioned release process, changelog, rollback steps, and configuration backup/restore test.
- [ ] Add license metadata for this repository and preserve the existing third-party reuse audit.
- [ ] Push or otherwise back up the current local commit after review.

**Exit gate:** a fresh target machine can be installed from the documented release, passes automated and hardware checks, and can roll back without losing private operational records.

### 8. Run the notification pilot and completion trial

- [ ] Enable low-severity notifications first; keep urgent wakeups observation-only until their gate passes.
- [ ] Review every delivered notification within 24 hours.
- [ ] Classify each as real issue, helpful check, false positive, nuisance, false negative, or uncertain.
- [ ] Measure delivery time, acknowledgement time, escalation success, monitoring uptime, nuisance rate, and missed concerns weekly.
- [ ] Freeze detector changes for a final 30-night validation period unless a safety-critical defect requires a reset of the trial.
- [ ] Train caregivers using the runbook and run monthly help, offline, no-ack, and backup drills.
- [ ] Conduct final privacy, retention, access, safety, and failure-mode review.

**Completion gate:** 30 valid nights with all agreed acceptance thresholds met, no unresolved safety-critical defect, no silent outage, all alerts reviewed, and written household sign-off that the monitor is useful and its limits are understood.

## Recommended execution order

1. **Safety decisions:** complete consent, response plan, manual backup, notification policy, and data-retention policy.
2. **Physical correction:** remount/calibrate the camera and install the final sensor layout.
3. **Engineering closure:** acknowledgement, backup escalation, soft check, durable alert IDs, restart/reconnect behavior, config validation, service supervision, security, and retention.
4. **Observation:** collect and label local scenarios plus 7–14 nights with all external notifications disabled.
5. **Failure-driven tuning:** fix local false alerts/misses, freeze a configuration, and validate on held-out local data plus URFD.
6. **Notification pilot:** low severity first, then urgent only after its acceptance gate passes.
7. **Hardening trial:** run 30 nights, drill failure paths, finish documentation, and sign off version 1.

Do not tune the current camera data further before correcting the camera position. The recordings themselves identify the low camera angle as a dominant error source.

## Implementation log

### 2026-07-10 - Fail-closed deployment configuration

- Added a standard-library validator for rules, sensor inventory, and camera zones, including threshold ordering, privacy/safety invariants, polygon bounds/area, MQTT settings, unique IDs, and obsolete keys.
- Added a strict deployment bundle check that rejects `*.example.json`, requires an explicit post-review `deployment_ready=true` marker, cross-references every sensor zone, requires bed/bathroom/route/help coverage, and requires a critical `pose_<camera_id>` heartbeat sensor.
- Integrated validation into rules/zones loading, the local pilot, offline runner, pose extractor, and MQTT bridge. Live camera publishing/calibration rejects example zones, and caregiver notification mode requires all three household config files.
- Added a live sensor allowlist so unknown ordinary MQTT sensor IDs are rejected before detector processing; aligned the Home Assistant example IDs with the sensor inventory.
- Verified with 65 passing unit tests plus structural-validator and fail-closed CLI smoke checks.

### 2026-07-10 - Durable alert response lifecycle

- Added restart-safe alert IDs and persisted lifecycle state in the operational log directory.
- Added normalized caregiver acknowledgement and alert-resolution events, responder/outcome audit rows, primary-to-backup escalation, and an explicit all-caregivers overdue notice. Automatic emergency calling remains disabled.
- Made bridge notifications observation-only by default; `--notifications-enabled` is now required to publish to the external caregiver topic.
- Added Home Assistant primary/backup routing plus acknowledge and checked-safe mobile actions.
- Verified with 49 passing unit tests, including the observation-only topic gate, acknowledgement timing, resolution logging, backup escalation, final overdue escalation, enriched payload context, and duplicate suppression after restart.

## Immediate next actions

These are the highest-value next tasks:

1. Fill out the private emergency-response plan and choose the primary/backup acknowledgement policy.
2. Remount the camera, calibrate it, redraw zones, and re-record the two known no-fall scenarios.
3. Create the ignored household-specific configuration files, replace every placeholder, and pass the new deployment validator.
4. Add service startup/restart, MQTT reconnect, and an external health heartbeat.
5. Add retention/disk-space enforcement and decide whether MQTT is protected by TLS or network isolation.
6. Add CI and regression tests for the two local false alerts.
7. Begin the labeled scenario matrix and observation-only nightly log.
8. Generate personal baselines only after enough valid routine transitions exist.
9. Start the low-severity notification pilot only when the documented gate is met.

## Easily forgotten scenarios to test

- Power returns while the person is already out of bed or an incident is active.
- The MQTT broker, camera, router, or computer restarts independently.
- The clock changes because of daylight saving, manual correction, or loss of time synchronization.
- A sensor repeats, delays, drops, or reorders an event.
- The camera resolution, mount, furniture, lighting, or field of view changes after calibration.
- A visitor, caregiver, pet, walker, wheelchair, blanket, laundry pile, mirror, or television enters the scene.
- The person crawls, kneels, sits on the floor, retrieves an object, or intentionally lies down.
- The person falls partly out of frame or remains moving after a fall.
- The help or cancel button battery dies, sticks, or is unreachable.
- The primary caregiver is unavailable, asleep, in focus mode, without data, or has changed phones.
- A caregiver acknowledges but does not actually check, or two caregivers respond simultaneously.
- Storage fills, a log becomes corrupt, the pose model download is unavailable, or a configuration file is invalid.
- Monitoring is paused and never resumed.
- Private data must be exported or deleted after consent is withdrawn.
- The monitored person has difficulty hearing a soft check or using the response control at night.

## Stop conditions

Pause deployment and return to observation-only if any of these occur:

- A critical sensor or process can fail without an offline/blind indication.
- A known normal routine repeatedly produces urgent alerts.
- A staged high-risk scenario is missed without a clear, mitigated reason.
- Caregivers stop reviewing or responding to alerts because of alarm fatigue.
- Camera placement captures a private area or consent changes.
- The system’s behavior after restart, clock change, or network loss is unknown.
- The response plan, manual backup, or primary/backup caregiver coverage is no longer valid.

## Final sign-off record

Complete this only after the 30-night trial:

- Version/configuration ID:
- Trial dates:
- Valid observation nights:
- Monitoring uptime:
- Normal trips reviewed:
- Staged high-risk scenarios passed:
- Low alerts / nuisance alerts:
- Urgent alerts / false urgent alerts:
- Missed concerns:
- Median and worst acknowledgement time:
- Offline/blind drills passed:
- Power/restart/recovery drills passed:
- Retention/deletion test passed:
- Primary caregiver sign-off:
- Backup caregiver sign-off:
- Monitored person consent reconfirmed:
- Remaining accepted limitations:
