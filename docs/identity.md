# Household-member recognition and people safety

Optional, **off by default**. Owner's product decision, 2026-10-16 (recorded in `CLAUDE.md`, boundary 2). Legal review is still required before this is sold (see "Legal and commercial status" below).

This feature adds four things to Sentinel:

1. **Consent-based recognition of enrolled household members** (`src/sentinel/identity/`).
2. **Three statuses for every person**: `known`, `unknown`, `unverified`.
3. **Behaviour rules that can use them**: the `identity_filter` rule field, plus a new `zone_approach` rule for someone heading toward a restricted area.
4. **A person standoff for the drone** (`src/sentinel/drone/standoff.py`): the aircraft never flies to, over, or near a reported person.

None of it describes intent. "Unknown" means "not an enrolled household member": visitors, couriers and neighbours are all unknown. It never means unwelcome, suspicious or a threat.

## What the statuses mean

| Status | Meaning | Typical cause |
| --- | --- | --- |
| `known` | Faces from this subject matched one enrolled member at least `min_agreeing` times, clearly more than any other member and at least as often as they failed to match | An enrolled member walking into the yard facing the camera |
| `unknown` | At least `min_unknown` clear, good-quality faces that matched no enrolled member, with few matches | A visitor, a courier |
| `unverified` | Anything else | Face not visible, too small, turned away, in-between scores, two members matched, outside the recognition zones, recognition off, nobody enrolled |

One comparison never decides anything. A hidden face is `unverified`, never `unknown`. The status belongs to the correlator's site track (the cross-camera subject). Recognition never merges subjects: evidence from linked camera tracks adds up, and contradictory evidence makes the subject `unverified`.

## Where recognition runs

- Only when `identity.enabled` is true **and** at least one member has finished enrollment.
- Only on person tracks whose ground point is inside a zone listed in `identity.zones`. Validation refuses `street`, `sidewalk` and `ignore` zones: passers-by never agreed to anything.
- Only on fixed cameras. Drone video is never used for recognition (boundary 7).
- After privacy masks (masks are applied before detection, as for everything else).
- At most once per `recheck_s` per track and `max_attempts` per track. Settled subjects are re-checked ten times less often, so a wrong early answer can still change.

## Privacy safeguards

- **Consent before anything biometric is stored.** A member record needs: agreement to the current consent statement (`identity.gallery.CONSENT_STATEMENT`, versioned), who agreed, their relationship (`self` or `parent_or_guardian` for a child), and how (`in_person`, `written`, `digital`). Who recorded it, and when, is stored with it.
- **No photos are kept.** Enrollment photos (or a live camera frame) are decoded in memory, turned into 128-value templates and dropped.
- **Templates are encrypted** in `<storage>/identity/gallery.dat` with DPAPI on Windows (a 0600 file elsewhere, like the credential store).
- **Enrollment checks:** exactly one clear face per photo (at least `enroll_min_face_px`); the photos must agree with each other (they must be one person); and they must not match a member who is already enrolled.
- **Faces that match nobody are never kept.** The worker computes a template, compares it, hands back only the verdict, and deletes the template. Nothing about unknown faces is stored, logged, returned by the API, or sent anywhere.
- **Withdrawal deletes everything.** `DELETE /api/v1/identity/members/{id}` (or `python -m sentinel.identity remove`) deletes the record and every template immediately. It forgets live matches and scrubs the member's id and name from stored events (`EventStore.scrub_identity`). The audit log keeps who removed whom and when, never a template.
- **Event text and notifications never name anyone** (boundary 1). They say "recognized as a household member". The member id and name are only in the event details and on the local live view. Push services and the AI Guide's explanation details do not receive them. MQTT mirrors full event details to the local broker.
- **The API never returns templates, face crops or photos.**
- **Health:** a missing model or an unreadable gallery degrades the `identity` component. Everyone is then `unverified`, and alerts behave exactly as with recognition off.

## Rules

### `identity_filter`

Any person rule can set `"identity_filter": "not_known"`. When recognition **already** says the subject is `known`, the event is recorded as `info` with the reason `recognized_household_member` (the same mechanism as a cooldown): visible in the timeline, no alert. `unknown` and `unverified` people still alert, including anyone whose face was not seen. With recognition off, the filter has no effect.

A `zone_entry` rule with the filter may wait up to `identity_wait_s` (default 3 s, at most 10) while the face is still being checked, so a member's own arrival does not alert. The wait is bounded: if recognition has not decided by then, the rule fires.

Warning: face recognition can be fooled, for example by a printed photo (there is no liveness check). Do not use `not_known` on rules that protect anything important in Away or Vacation mode.

### `zone_approach` (new rule type)

"Moves toward an area": a subject outside one of the rule's `zones` moves at least `min_closing` body heights closer to it within `window_s`, and is now within `within` body heights. It fires once per track and zone. Distances are measured in body heights on the image, so no calibration is needed. Defaults: `within` 2.0, `min_closing` 1.0, `window_s` 4. Example text: "Person moved toward Shed (about 1.4 body heights away)".

Together with the existing rules, this covers the behaviour the owner asked for, in observed terms only:

| Asked for | Rule |
| --- | --- |
| Trespassing | `zone_entry` on a `restricted` zone (template `restricted_entry`, or `restricted_entry_not_household`) |
| Lurking | `dwell` with `max_roam` (template `linger_entrance`) |
| Approaching restricted areas | `zone_approach` (template `approach_restricted`) |
| Repeated visits | `repeated_presence` |

No rule text says intruder, burglar, suspicious or trespasser. Every notification ends with the observed-activity disclaimer.

## Drone person standoff

People's safety, the owner's included, comes before any inspection.

- **Standoff distance:** `drone.safety.person_standoff_m`, default **30 m** horizontal. That is Transport Canada's basic-operations distance from bystanders (more than 30 m, never over them). It can be raised, or lowered to a floor of **10 m** (for example by a pilot whose certificate allows it); it can never be switched off.
- **Where people are reported:**
  - the area of the incident that triggered the request, when it involved a person;
  - zones where the fixed cameras see a person right now;
  - people seen on the drone's own video, with their position uncertainty (subtracted, so it counts against the aircraft).
- **Planning:** if people are near the target, the hover point (`route.station`) moves back along the line toward home until it is clear by the standoff. The aircraft looks from there. If no such point exists, the route is refused (`no_inspection_point_clear_of_people`). A route whose path passes within the standoff of a reported person is refused (`route_passes_near_people`). People near the launch point are a warning for take-off and landing. The exported `.plan` hovers at the station.
- **Manual goto (simulators):** refused within the standoff (`target_near_person`).
- **In flight:** a person reported inside the standoff stops the inspection (`person_too_close`, audited as `drone_person_standoff`).
  - In a simulator that Sentinel flies, the aircraft holds position. It never keeps approaching or descends.
  - For a real aircraft (telemetry only), the pilot gets an advisory: stop, climb if needed, move away, never descend toward a person.
- **Limits:** camera positions are zone-level and drone positions carry uncertainty, so both are treated as areas. The absence of a report does not mean nobody is there: the pilot remains responsible for people they can see.

On a small lot (the 40 × 28 m example site), a 30 m standoff often leaves no clear hover point near a person. Sentinel then refuses the inspection. That is the intended outcome. The SITL demo uses the 10 m floor and says so.

## Configuration

```json
"identity": {
  "enabled": true,
  "zones": ["front_porch", "driveway"],
  "match_threshold": 0.45, "unknown_below": 0.30, "min_margin": 0.06,
  "min_agreeing": 3, "min_unknown": 3, "min_face_px": 40, "min_face_score": 0.85,
  "recheck_s": 0.5, "max_attempts": 40, "queue_size": 4, "cpu_threads": 1,
  "enroll_min_face_px": 80, "enroll_min_photos": 3, "max_templates": 20
}
```

All keys are optional except `zones` when `enabled` is true. Validation is in `identity/validate.py`.

## Enrollment

Dashboard and mobile clients use the API (`docs/api.md`, "Household-member recognition"). From the command line:

```powershell
python scripts/fetch_face_models.py                                   # once: models into models/face/ (checked by SHA-256)
python -m sentinel.identity consent                                   # show the person the statement
python -m sentinel.identity add --config config/sentinel.local.json --name "Alex" --given-by "Alex" --relationship self --method in_person --agreed
python -m sentinel.identity enroll --config config/sentinel.local.json --member m_1234abcd front.jpg left.jpg right.jpg
python -m sentinel.identity list --config config/sentinel.local.json
python -m sentinel.identity remove --config config/sentinel.local.json --member m_1234abcd
```

A running Sentinel picks up gallery changes within a few seconds.

## Models (`identity/faces.py`, I/O edge)

| Model | Licence | Notes |
| --- | --- | --- |
| YuNet face detector 2023mar (OpenCV Zoo) | MIT | Five landmarks for alignment |
| SFace recognizer 2021dec (OpenCV Zoo) | Apache-2.0 | MobileFaceNet with the SFace loss; 128-value templates. **Training data not documented by OpenCV Zoo**; the SFace paper trained on CASIA-WebFace, VGGFace2 and MS-Celeb-1M, whose terms are research-oriented |

Both run through OpenCV's DNN module on the CPU (`cv2.FaceDetectorYN`, `cv2.FaceRecognizerSF`), not through ONNX Runtime, so they never compete with the detector's DirectML session. InsightFace/ArcFace models were not used: their pretrained weights are for non-commercial research only.

## Measured so far

`scripts/identity_eval.py` uses MOT16-09 (public; its ground-truth pedestrian identities label every face). Faces were taken from 525 frames, every third frame; 146 faces of 8 pedestrians; median face 56 px. Machine: i5-1135G7, CPU. Each figure is the share of single face-pair comparisons:

| Pairs | Count | Mean similarity | At or above 0.45 (match) | Below 0.30 (no match) | In between (undecided) |
| --- | --- | --- | --- | --- | --- |
| Same pedestrian, at least 1 s apart | 1,444 | 0.655 | 86.9% | 1.5% | 11.6% |
| Different pedestrians | 8,167 | 0.126 | 0.3% | 90.5% | 9.2% |

What this is **not**: a recognition accuracy figure for a home. These are small, turned, motion-blurred faces of strangers from one 17-second street clip. They are not enrollment-quality photos compared with doorway footage weeks later, under night IR, with hats, glasses or ageing. Sentinel's per-subject decision also needs several agreeing frames, which this per-pair view leaves out. Before relying on `not_known` rules, measure on the owner's own cameras with consenting household members.

Tests: `tests/test_sentinel_identity.py`. Statuses, consent, gallery encryption and withdrawal, validation, zones, cross-camera aggregation, engine filters and wait, approach rule, runtime worker, API and drone standoff all use a fake face model. A real-model check runs on MOT16-09 when the models and footage are present. Scenario: `config/scenarios/approach_shed.json`.

## Legal and commercial status (not legal advice)

The owner decided to build this for their own household. Before it is sold, it needs:

- legal review of biometric rules where it would be sold. Examples: PIPEDA and provincial privacy law in Canada (Quebec requires biometric databases to be declared to the Commission d'accès à l'information); Illinois BIPA, Texas CUBI and Washington's biometric law in the US; GDPR Article 9 in the EU. Rules on consent forms, retention schedules, notices to visitors (signage) and children's data apply too;
- a licence decision on SFace's undocumented training data, or a recognizer trained on data with commercial terms;
- an accuracy and bias evaluation on representative, consented footage, including night IR;
- a liveness or anti-spoofing decision before `not_known` filters are offered for Away or Vacation modes.
