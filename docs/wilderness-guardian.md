# Wilderness Guardian

Wilderness Guardian extends Sentinel from property security to wildlife and human protection. Today it runs on a fixed security camera. Drones and ground rovers come later, as extra cameras for the same Hub.

The goal: **Sentinel should understand how an animal moves and whether it is coming toward people, not just recognise an animal in one picture.**

Development happens on the `guardian` branch. Sentinel V1 on `main` stays feature-frozen (see `CLAUDE.md`).

## Status at a glance (2026-10-15)

| Label | Meaning |
| --- | --- |
| **Implemented** | Code exists, runs in Sentinel and has automated tests |
| **Simulated** | Tested only on synthetic sequences or simulators. Says nothing about real animals |
| **Planned** | Design only. No code |
| **Concept (research only)** | Ideas to evaluate. Must not be built without a separate safety, legal and animal-welfare review |

| Phase | Area | Status |
| --- | --- | --- |
| 1 | Generic animal detection (COCO "bear", "dog", "cat", "horse", "cow" ...) through the existing detectors | **Implemented** (existing Sentinel `animal` category) |
| 1 | Species classification (black bear vs grizzly, cougar, wolf, coyote, moose, elk) | **Planned**. No species model is installed. Species are never invented: detector labels are shown as "Bear (species not determined)", "Dog-like animal (could be a dog, coyote or wolf)" |
| 2 | Movement intelligence: persistent tracks, approach/retreat/lateral/stationary, relative speed, approach to camera/zone/person, direction changes, sustained approach, calibrated distance and time-to-zone, explainable risk levels, cooldowns, video evidence, notifications, live overlay, API | **Implemented**. Verified on **synthetic** sequences only (`tests/test_sentinel_guardian.py`, `config/scenarios/wildlife_bear_approach.json`). **Not yet run on real wildlife footage** |
| 3 | Human threat protection | **Planned** (with boundary conflicts noted below) |
| 4 | Emergency Guardian drone/rover | **Concept (research only)**. Simulation is the furthest it may go under the current product boundaries |
| 5 | Multi-platform Guardian Hub | **Planned**. The Phase 2 monitor is already platform-neutral |

## Decisions for future development (2026-10-15)

1. **Disarmed mode.** Wildlife emergency alerts should eventually work even when home security is disarmed, through a separate configurable Guardian setting (for example `guardian.notify_when_disarmed`). Today wildlife events follow the arm mode, so nothing is pushed while disarmed. Not implemented yet.
2. **Approaching is recorded, not pushed.** Notifications are for Elevated Concern and Urgent Review (`notify_min_level: elevated_concern`, the default).
3. **Owner facial recognition stays blocked** until privacy and safety requirements are settled and the decision is recorded in `CLAUDE.md` (boundary 2).
4. **Next priorities:** validate Phase 2 on a real camera (prerecorded wildlife clips, then the owner's live camera with labelled tracks), then species identification (Phase 1 classifier, licence-cleared and benchmarked before adoption).

---

## Phase 1: Wildlife recognition

### What Sentinel can already detect

Sentinel's detectors (YOLOX by default, plus RT-DETRv2/D-FINE and an EfficientDet fallback) were trained on COCO. COCO has ten animal classes: bird, cat, dog, horse, sheep, cow, elephant, bear, zebra and giraffe. They feed Sentinel's `animal` category (`models.CATEGORY_LABELS`). The specific label is kept on each track as `kind`.

What this means for target species:

| Target | What a COCO detector does | Guardian display |
| --- | --- | --- |
| Black bear, grizzly bear | One `bear` class for all bears. Black and grizzly bears cannot be told apart | "Bear (species not determined)" |
| Cougar | No class. Often reported as `cat` or `dog`, or missed | "Cat-like animal (species not determined)" |
| Wolf, coyote | No class. Usually reported as `dog` | "Dog-like animal (could be a dog, coyote or wolf)" |
| Moose, elk, deer | No class. Sometimes reported as `horse` or `cow` | "Large hoofed animal (detector label: horse)" |
| Other wildlife | Missed, or reported as the nearest COCO class | "Animal (detector label: ...)" |

Confusing black bears with grizzly bears has real consequences. Agency safety advice for the two species differs, so Sentinel must never give species-specific advice from an unconfirmed species (see Phase 4).

### Datasets to evaluate

Before any data is used, its licence and the licence of any pre-trained weights must be checked separately and recorded in `sources.md`, following the rule in `CLAUDE.md`. The licences below come from memory of the publishers' pages and are marked **verify**: check the current terms before downloading.

| Dataset | Content | North American target coverage | Licence (verify) | Use for Sentinel |
| --- | --- | --- | --- | --- |
| **Caltech Camera Traps (CCT)** (LILA) | ~240k camera-trap images, ~140 locations in the US Southwest; boxes for a subset | Coyote, bobcat, deer, and a small number of mountain lion images; no bears, wolves or moose | CDLA-Permissive 1.0 | Detector and classifier training for southwestern species; good for "empty frame" negatives and night IR |
| **NACTI** (North American Camera Trap Images, LILA) | ~3.7M images, 5 US locations, ~28 classes, image-level labels | Ungulates (deer, elk), black bear, cougar, coyote and others: check the exact category list | CDLA-Permissive 1.0 | The best large source for a North American species classifier. Image-level labels: boxes would come from MegaDetector |
| **ENA24-detection** (LILA) | ~10k images, 23 classes, eastern North America, with bounding boxes | American black bear, coyote, bobcat, white-tailed deer; no grizzly, wolf, moose, elk | CDLA-Permissive 1.0 | Small, box-labelled evaluation set for black bear and coyote |
| **Animal Kingdom** (CVPR 2022) | ~50 h of annotated video, ~850 species; behaviour, pose and video grounding labels | Broad species coverage, little North American camera-trap context | Research / non-commercial terms | Behaviour research and evaluation ideas only; **not usable in a sold product** without permission |
| **WCS Camera Traps** (LILA) | ~1.4M images, ~675 species, mostly tropical Africa, Asia and Latin America; boxes for a subset | Low coverage of the Phase 1 targets | CDLA-Permissive 1.0 | Generic "animal" detection robustness and negatives |
| **MegaDetector** (model, not a dataset) | Camera-trap detector with three classes: animal, person, vehicle | Detects animals in general; no species | MDv5: MIT weights, but built on YOLOv5 (Ultralytics code is AGPL-3.0). MDv6 publishes variants under MIT and Apache-2.0 | Strong candidate for a better generic `animal` detector at night and in vegetation. Choose an MIT/Apache MDv6 variant and benchmark it with `sentinel.bench` before adoption |

Other candidates worth checking: **Idaho Camera Traps** (LILA; wolves, elk, moose, black bear and cougar from Idaho), **SpeciesNet** (Google's camera-trap species classifier), and animal multi-object tracking datasets such as **AnimalTrack** (licence unknown).

### Missing training data

No public dataset matches Sentinel's situation. Gaps:

1. **Security-camera viewpoint.** Camera traps sit at knee height. Home cameras look down from 2.5–4 m through wide-angle lenses on compressed sub-streams at 5 fps.
2. **Video sequences with tracks.** Approach, retreat and turning need labelled track IDs over time. Almost every camera-trap dataset is still images or short bursts.
3. **Expert-verified black bear vs grizzly labels**, including night IR footage, where colour cues disappear.
4. **Cougar, wolf and moose at night near buildings**: rare in every dataset.
5. **Hard negatives from residential scenes**: leashed dogs, deer on lawns, livestock, wind-blown vegetation, snow, rain and insects on the lens.
6. **Calibrated footage with measured distances**, needed to check the Phase 2 distance and time-to-zone estimates.
7. **Movement behaviour labels by wildlife professionals.** Without them, Sentinel can only describe movement. It cannot classify behaviour.

Plan: record and label your own footage with the existing benchmark tools (`sentinel.bench`, Sentinel label format). Every accuracy claim must name the labelled footage behind it.

---

## Phase 2: Movement intelligence (implemented)

### What it does

`src/sentinel/guardian/` is a stdlib-only package that runs beside the security engine:

```text
camera -> detector -> IoUTracker --+--> TemporalEventEngine (security rules, unchanged)
                                   +--> GuardianMonitor (animals) --> wildlife_* events
                                                                         |
                       incidents -> alert policy -> notifications, clips, timeline, live overlay
```

| Module | Role |
| --- | --- |
| `guardian/monitor.py` | `GuardianMonitor`: guardian tracks, re-acquisition, zone/person/camera targets, levels, cooldown, events, overlays |
| `guardian/movement.py` | Least-squares trends over a 2.5 s window: relative speed, box growth, movement class, turns, speed increase |
| `guardian/risk.py` | Species profiles (honest wording), additive explainable scoring, levels, forbidden claim words |
| `guardian/geometry.py` | Polygon distance and the optional ground-plane homography |
| `guardian/validate.py` | Validation for the `guardian` config block and per-camera `guardian` options |

Integration points (small, additive):

- `runtime.SentinelRuntime`: creates the monitor, adds its events to the normal event path, and contains any failure as a `guardian` health component, so security detection never stops.
- Wildlife events skip the category alert switch (`categories.animal.alerts`, off by default) because enabling Guardian is the opt-in.
- `vision.annotate`: draws the trail, image heading arrow and level on the live view and on incident snapshots.
- `GET /api/v1/guardian` and a `guardian` section in `GET /api/v1/state`.
- Prerecorded video works through the existing `file` camera source. Live feeds work through RTSP/HTTP/USB sources.

### Signals and how they are measured

| Requirement | How | Honest limits |
| --- | --- | --- |
| Persistent IDs | The camera's IoU tracker, plus guardian tracks (`g:<camera>:<n>`) that survive up to `reacquire_s` (8 s) of loss. A new track continues a lost one only if it appears near the predicted position, has a similar size and the same label, **and there is exactly one candidate each way**. Otherwise a new ID is started and `ambiguous_with` is recorded | Two similar animals crossing paths may swap IDs inside the camera tracker itself |
| Approaching / retreating the camera | Box height growth rate (`d ln h / dt`). At least 6 %/s counts as approaching | Ignored while the box touches the frame edge or its size is noisy; posture changes (standing up) also change box size |
| Lateral / stationary | Image speed in **body heights per second** below 0.15 counts as stationary | Relative only: a fast-moving distant animal and a slow nearby one can look the same |
| Relative speed and direction | Reported as body heights/s and an image heading (left/right/up/down) | No compass directions or m/s without calibration |
| Toward a protected zone | Image distance from the ground point (box bottom-centre) to the zone polygon, divided by body height, shrinking by at least 0.15 body heights/s | Perspective distorts image distances; wording says "moving toward", not a distance |
| Toward a person | Distance to the nearest person track on the same camera, shrinking | Same perspective limits. Both must be visible |
| Sudden direction change | Heading in the latest window vs the previous window, at least 100° apart, both moving | Needs ~5 s of history |
| Sustained approach | Approach (or presence in a protected zone) for at least `sustained_s` (3 s), allowing a 1 s gap | |
| Distance and time-to-zone | **Only for calibrated cameras**: a homography from at least 4 image points to measured ground points in metres. Reported as "approximate, flat ground" with calibration status (`unverified` with exactly 4 points, `verified` with more than 4 points and RMS error ≤ 0.5 m) | Wrong on slopes, and when the box bottom is not the feet (occlusion) |
| Camera movement | `moving_camera: true` (PTZ, vehicle, drone) caps every level at Observed. All objects sliding together is also treated as camera motion for 3 s | A single animal on a shaking camera cannot be told from real movement |
| Tracking glitches | A jump of more than 2.5 body heights in under 1 s resets the movement history instead of being reported as a dash | |

### Risk levels

| Level | Severity | Default push | Typical example (synthetic) |
| --- | --- | --- | --- |
| **Observed** | info | never | Any animal in view, stationary, retreating or crossing |
| **Approaching** | low | never (recorded in incident and timeline) | Moving toward the camera, a protected zone or a person |
| **Elevated Concern** | medium | yes (`notify_min_level`) | Sustained approach by a moderate- or high-concern animal, or an animal inside a protected zone |
| **Urgent Review** | high | yes | High-concern animal (e.g. a bear) closing on a person or inside a protected zone while people are in view |

The score is additive, and **each point is a named factor shown in the event** (`details.assessment.factors`, each with `observed`, `inferred` or `estimate` as its basis):

| Factor | Points |
| --- | --- |
| Species concern: high / moderate / unknown / low | +2 / +1 / 0 / −1 (inferred from the detector label) |
| Approaching something | +1 |
| Sustained approach | +1 |
| Sustained approach toward a protected zone or person | +1 |
| Inside a protected zone | +2 |
| Distance to a person shrinking | +2 (or +1 if a person is simply in view) |
| Sharp turn followed by approach | +1 |
| Speed increase while approaching | +1 |
| In view ≥ 60 s | +1 |
| Calibrated time to zone ≤ 10 s, distance ≤ 15 m | +1 each (estimate) |

Thresholds: Approaching ≥ 2, Elevated Concern ≥ 4, Urgent Review ≥ 6, and every level above Observed needs an actual approach, zone presence or closing distance. `sensitivity` shifts all thresholds by one point. Low confidence (track younger than `min_track_s` or mean detector confidence below `min_confidence`) and camera motion **cap** the level at Observed, and the event says why (`held_back`).

Language rules (tested): event text describes movement ("moving toward Back door for 6 s"). It never says attack, charging, aggressive, hunting, stalking, predatory, intends or safe. Notifications end with Sentinel's observed-activity disclaimer.

### Avoiding repeated false alarms

- A level must hold for `hold_s` (1 s) before it is emitted.
- Each guardian track emits each level once. A new alert for the same track needs the level to stay lower for `cooldown_s` (120 s) and then rise again.
- Events join the animal's incident. The existing alert policy notifies once per incident and again only when severity rises, under the global rate limit.
- Approaching is recorded without a push by default.
- Birds are ignored (`ignore_kinds`).

### Evidence

Wildlife events go through Sentinel's normal media path: the incident clip **with pre-roll** (`recording.pre_roll_s`), extended while the animal stays in view, plus an annotated snapshot with trail and level. Both are SHA-256 fingerprinted and covered by retention and protection. Each event stores the full movement and assessment record for later review.

### Configuration

```json
"guardian": {
  "enabled": true,
  "cameras": [],
  "protected_zones": ["back_door"],
  "protected_zone_types": ["restricted"],
  "approach_camera": true,
  "min_confidence": 0.4,
  "min_track_s": 1.0,
  "sustained_s": 3.0,
  "hold_s": 1.0,
  "cooldown_s": 120,
  "reacquire_s": 8,
  "notify_min_level": "elevated_concern",
  "ignore_kinds": ["bird"],
  "sensitivity": "normal",
  "species": {"dog": {"concern": "low"}}
}
```

Per camera: `"guardian": {"moving_camera": false, "calibration": {"image_points": [[x, y], ...], "ground_points_m": [[X, Y], ...]}}`. Image points are normalised (0..1). Measure at least five ground marks with a tape so the calibration can check itself.

`protected_zones` empty means every zone whose type is in `protected_zone_types`. `cameras` empty means every camera. `species` overrides only the concern level, never the displayed wording. Guardian is **off by default**, so V1 behaviour is unchanged.

Demo: `python -m sentinel serve --config config/sentinel.guardian.example.json` (simulated bear-labelled animal and person; watch the Live view and Timeline).

### Verification so far

`tests/test_sentinel_guardian.py` covers: approaching the camera, leaving, crossing, stationary, sharp turn, approaching and entering a protected zone, multiple animals, person and animal together, loss and re-acquisition, ambiguous re-acquisition, tracker jumps, low-confidence flicker, one emission per level, ignored birds, coherent camera motion, moving cameras, calibration round trip, metres only when calibrated, config validation, runtime alert plus clip evidence, unchanged security events with Guardian on, and a scenario replay. All of these use **synthetic sequences**. No real wildlife footage has been run yet, so no accuracy or reliability claim is made.

---

## Phase 3: Human threat protection (planned)

Goal: protect people on the property. Some requested capabilities conflict with Sentinel's product boundaries (`CLAUDE.md`), and those boundaries take precedence until the owner records a decision that changes them.

| Capability | Plan | Boundary notes |
| --- | --- | --- |
| Possible break-ins | Sequences already supported: dwell at doors/windows, restricted-zone entry at night, repeated presence; add door/window contact sensors and glass-break audio as corroboration | Describe behaviour ("Person at rear window for 40 s, then window sensor opened"), never "burglar" (boundary 1) |
| Assault or threatening behaviour | Research pose-based "two people, rapid contact movements, one falls" sequences; human review only | High false-positive risk (play, sports, hugging). Wording stays observational. No automatic police calls (boundary 8) |
| Stalking-like patterns | Existing repeated-presence and multi-camera rules | "May be by the same or different people" (boundary 2) |
| Enrolled owners and household members | Opt-in enrolment, on-device templates, deletion on request, never used for strangers. An uncertain match is treated as unknown, never as a trusted owner (no auto-disarm on a weak match) | **Blocked** by boundary 2 until a product, legal and privacy decision is recorded. Biometric laws apply (PIPEDA, Quebec Law 25 biometric declaration, Illinois BIPA, GDPR Art. 9) |
| Potential weapons | Evaluate firearm/knife detectors with a measured false-positive rate on residential footage; "possible object resembling a firearm, unverified", human review only | Never escalates automatically; a phone or tool must not trigger police |
| Not mistaking strangers for threats | Unfamiliar ≠ threat: unknown people are logged as activity, as today. "Unusual is not a threat" | Existing convention |
| Lights, sirens, voice warnings | Home Assistant/MQTT actions tied to rules, either pre-authorised by the owner per rule or confirmed by a person; every action audited; quiet-hours and neighbour limits | No autonomous physical attack or weapon of any kind |
| Emergency response | One-tap "call for help" flows that notify people the owner chose, with the clip | No automatic calls to police or emergency services (boundary 8) |

---

## Phase 4: Emergency Guardian, drone and rover (concept, research only)

Sentinel's drone boundary (boundary 7) says no autonomous real-world flight. A human approves every inspection and a certified flight stack flies it. Everything in this phase stays **concept and simulation** until a separate safety case, legal review and animal-welfare review exist. **Nothing here is to be built as hardware or real-flight code**: no spray mechanisms, smoke generators, payload releases, weapon controls or autonomous interception.

### Concepts discussed

| Concept | Research question | Concerns |
| --- | --- | --- |
| Respond to a fast approach toward a person | Could a drone already in the air (operator-approved) be redirected to watch the animal and report movement to the person's phone faster than a fixed camera? | Response time: a bear can cover 50 m in seconds. A drone launched after detection is very likely too late |
| Position between person and animal (simulation only) | In SITL: can geometry keep a drone between two moving tracks while respecting fences and minimum distances? | Flying near the person violates basic-operations distance rules and risks injuring the person you meant to protect |
| Track redirection | Re-plan the simulated position when the animal changes direction (Phase 2 provides turns and closing distance) | Drone video is not used for zone rules today (boundary 7) |
| Distraction and repeated interception (simulation only) | Does any evidence show drone presence diverts large carnivores, or does it provoke them? | Ditmer et al. (2015) measured large heart-rate spikes in American black bears near small drones. Drones have been used to haze elephants (Hahn et al., 2017); evidence for bears and cougars is thin |
| Owner safety over drone recovery | Failsafe ordering: if the aircraft is a hazard to the person, it lands or leaves, even if it is lost | Must be enforced by the flight controller and operator procedure, not Sentinel |
| Species-adapted recommendations | Show vetted agency guidance (Parks Canada, provincial wildlife agencies) per species | Black bear and grizzly guidance differ, and the detector cannot tell them apart. Guidance must be shown only for a confirmed species and quoted, never generated |

### Deterrents

| Deterrent | What is known | Main risks | Status |
| --- | --- | --- | --- |
| Directional speakers, audible alerts | Some short-term effect; animals habituate | Could panic an animal toward people | Plan for fixed outdoor speakers via Home Assistant, human-triggered (Phase 3) |
| Sirens, non-harmful visual warnings | Mixed evidence; strobe devices such as Foxlights are used for livestock predators with mixed results | Neighbours; habituation | Same as above |
| Spotlights | Lighting helps people see and avoid; little evidence it deters large carnivores | Glare for the person retreating | Fixed floodlights via Home Assistant |
| Bear spray | Hand-held bear spray has strong field evidence when used by a person at close range (Smith et al., 2008, Alaska) | Aerial release drifts with wind and rotor wash, likely onto the person; legal status (prohibited weapon if not labelled for animals in Canada); dangerous goods | **Concept only. Do not implement** |
| Smoke screens | No evidence of effectiveness on wildlife | Hides the escape route; wildfire ignition risk; inhalation | **Concept only. Do not implement** |
| Escape assistance | Phone/speaker guidance to the nearest building, with lights turned on along the path | Advice must match confirmed species and agency guidance | Plausible software feature after Phases 1–2 are validated on real footage |

Regulatory notes to verify with professionals: Transport Canada CARs Part IX (visual line of sight, distance from bystanders, night operations, restrictions on releasing objects), provincial wildlife acts and federal park rules that prohibit harassing wildlife and often prohibit drones, and the Criminal Code status of sprays. Consult wildlife biologists, conservation officers and Parks Canada before any physical trial.

**Rule: the system must never endanger its owner, bystanders or wildlife through an uncontrolled response.**

---

## Phase 5: Multi-platform Guardian (planned)

A single Sentinel Hub coordinates:

- fixed security cameras (implemented),
- outdoor security drones (simulated and SITL; telemetry-only on real aircraft),
- indoor drones, ground rovers, and farm or wilderness monitoring devices (planned; registry entries only).

**Shared across platforms:** detections, `Track`s, guardian tracks and assessments, `SecurityEvent`s, incidents, evidence and alerts. `GuardianMonitor` is platform-neutral: any source that produces tracks can feed it, and it flags moving viewpoints (`moving_camera`).

**Platform-specific:** physical movement and safety. Flight stays with the flight controller and the operator-approval workflow. Rover drive limits stay on the rover's own controller. A Hub fault must never command movement.

Needed before mobile platforms can use Phase 2 levels: ego-motion compensation (gimbal and vehicle pose, `drone/observation.py` already projects drone video to the ground with uncertainty), an explicit product decision on whether drone/rover tracks may feed wildlife levels (today drone video never feeds zone rules), and cross-platform identity rules (never merge identities on weak evidence).

---

## What you can test now with your existing camera

1. **Prerecorded wildlife video.** Add a camera with `"source": {"type": "file", "path": "<clip.mp4>"}`, a protected zone around your door, and the `guardian` block. Run `python -m sentinel serve --config <your config>` and watch the Live overlay and Timeline. Good first clips: a bear, deer or dog walking toward a camera mounted at house height.
2. **Your live camera with a dog or a person carrying a toy.** COCO "dog" works well. Walk the dog toward, away from and across the view and check the movement label and level. (A teddy bear is COCO `teddy bear`, not `bear`, so it will not trigger.)
3. **Person and animal together.** Stand in view while the dog approaches you: expect "closing on person" on the dog's event.
4. **Calibration.** Put five tape-measured marks on flat ground, read their image positions in the setup editor, and add `guardian.calibration`. Check `GET /api/v1/guardian` for distance estimates against a tape measure.
5. **Night/IR.** Record a night clip. Expect weaker detection. Note misses for the missing-data list.
6. **False alarms.** Leave Guardian running for a few days and count Elevated/Urgent events per day with the feedback labels (`false_alarm`, `expected_activity`).

Not testable yet: real species identification (needs Phase 1 models), and any accuracy figure (needs labelled footage).
