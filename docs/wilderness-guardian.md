# Wilderness Guardian

Wilderness Guardian extends Sentinel from property security toward protecting people and recognising wildlife. The Sentinel Hub is the shared brain. Fixed cameras come first. Drones, indoor drones, ground rovers and farm or wilderness monitoring equipment are added later as extra sensors for the same Hub.

The goal: **Sentinel should understand how an animal moves and whether it is coming toward people, not just recognise an animal in one picture.**

Development happens on the `guardian` branch. Sentinel V1 on `main` stays feature-frozen (see `CLAUDE.md`), and the Guardian work never changes flight-critical code.

## Status labels

| Label | Meaning |
| --- | --- |
| **Implemented** | Code exists in Sentinel and has automated tests |
| **Simulated** | Tested only on synthetic sequences or simulators. Says nothing about real animals or people |
| **Planned** | Design only. No code |
| **Research concept** | An idea to study. Must not be built without a separate safety, legal and animal-welfare review |
| **Blocked** | Deliberately not built until a recorded decision changes a Sentinel product boundary |

## Status at a glance (2026-10-15)

| Phase | Area | Status |
| --- | --- | --- |
| 1 | Generic animal detection (COCO `bear`, `dog`, `cat`, `horse`, `cow` ...) through the existing detectors | **Implemented** (existing Sentinel `animal` category) |
| 1 | Species identification (black bear, grizzly bear, cougar, wolf, coyote, moose, elk) | **Planned**. No species model is installed. Detector labels are shown as unconfirmed ("Bear (species not determined)") |
| 2 | Movement intelligence: persistent tracks, movement class, relative speed, approach to camera/zone/person, turns, sustained approach, calibrated-only distance, explainable levels, alert suppression, evidence, overlay, API | **Implemented** and **Simulated**: passes synthetic tests only. **Not yet validated on real wildlife footage** |
| 2 | Wildlife alerts while security is disarmed (separate setting) | **Planned** (decision 1 below). Today wildlife alerts follow the arm mode |
| 3 | Human activity recognition beyond today's rules (altercations, following, dangerous areas) | **Planned** |
| 3 | Owner and household facial recognition | **Blocked** (product boundary 2) |
| 3 | Lights, sirens, voice and emergency-assistance integration | **Planned**, human-approved only |
| 4 | Emergency Guardian drone/rover response, interception, deterrents | **Research concept**. Simulation at most. No spray, smoke, payload-release, weapon or interception code |
| 5 | Multi-platform Hub (cameras, drones, rovers, agricultural systems) | Cameras **implemented**; drones **simulated** (SITL; telemetry-only on real aircraft); rovers and indoor drones **planned**; Thermal/FLAME agricultural monitoring **simulated** |

## Decisions for future development (2026-10-15)

1. **Disarmed mode.** Wildlife emergency alerts should eventually work whether or not home security is armed, through a separate configurable Guardian setting (proposed name `guardian.notify_when_disarmed`, default off until tested). The existing arm-mode behaviour is unchanged today: wildlife pushes follow the arm mode and are not sent while disarmed.
2. **Notifications per level.** Observed: record only. Approaching: record only by default. Elevated Concern: notify. Urgent Review: high-priority notification (priority 5). Set by `notify_min_level: elevated_concern`, the default.
3. **Owner facial recognition stays blocked** until privacy, security, legal and product requirements are settled and the decision is recorded in `CLAUDE.md` (boundary 2). A mistaken identification must never trigger a physical defensive response.
4. **Next priorities:** validate Phase 2 on the owner's existing camera with real footage (see [Real camera validation](#real-camera-validation-plan)), then species identification. Real footage comes before more synthetic simulation.

---

## Phase 1: Wildlife recognition

### What Sentinel can detect today

Sentinel's detectors (YOLOX by default, plus RT-DETRv2/D-FINE and an EfficientDet fallback) were trained on COCO. COCO has ten animal classes: bird, cat, dog, horse, sheep, cow, elephant, bear, zebra and giraffe. They feed Sentinel's `animal` category (`models.CATEGORY_LABELS`). The specific label is kept on each track as `kind`. This existing detection is unchanged by Guardian.

**Limitation: current general-purpose detectors cannot reliably tell the requested species apart.**

| Target | What a COCO detector does | Guardian display |
| --- | --- | --- |
| Black bear, grizzly bear | One `bear` class for all bears. Black and grizzly bears cannot be told apart | "Bear (species not determined)" |
| Cougar / mountain lion | No class. Often reported as `cat` or `dog`, or missed | "Cat-like animal (species not determined)" |
| Wolf, coyote | No class. Usually reported as `dog` | "Dog-like animal (could be a dog, coyote or wolf)" |
| Moose, elk, deer | No class. Sometimes reported as `horse` or `cow` | "Large hoofed animal (detector label: horse)" |
| Other wildlife | Missed, or reported as the nearest COCO class | "Animal (detector label: ...)" |

Confusing black bears with grizzly bears has real consequences. Agency safety advice for the two species differs, so Sentinel must never give species-specific advice from an unconfirmed species (see Phase 4). The goal of Phase 1 is to name a species only when a model's confidence and validation support it, and to keep "species not determined" otherwise.

### Datasets and models

Checked on 2026-10-15 against each publisher's own page (LILA BC, GitHub). A first licence-checked sample of individual images has since been downloaded with the Guardian dataset importer; species coverage verified from the metadata, exact licences, versions and what is missing are in [guardian-datasets.md](guardian-datasets.md). Record any new dataset or pre-trained weights in `sources.md`, and check weights licences separately from code licences.

| Dataset or model | Species covered | Images / video | Labels and annotations | Licence and commercial use | Detection | Tracking | Behaviour | Training gaps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Caltech Camera Traps** ([LILA](https://lila.science/datasets/caltech-camera-traps)) | 21 animal categories plus empty, mostly species level, from 140 locations in the US Southwest (e.g. coyote, raccoon, opossum) | ~243,100 still images | Image labels; ~66,000 bounding boxes | CDLA-Permissive 1.0 (per LILA). Permissive, commercial use allowed under its terms | Good for night IR and "empty" negatives | No: stills, not tracked video | No | No bears, wolves or moose; southwestern species; knee-height viewpoint |
| **NACTI** ([LILA](https://lila.science/datasets/nacti)) | 28 categories, mostly species level, 5 US locations; most common are cattle, boar and red deer. The full list must be read from the metadata before relying on bear, cougar, elk or moose coverage | ~3.7 million still images, ~12 % empty | Image-level labels; boxes on only 8,892 images, mostly vehicles and birds | CDLA-Permissive 1.0 (per LILA) | Classifier training (boxes would come from MegaDetector) | No | No | Few boxes; class balance dominated by livestock; species list unverified for the Phase 1 targets |
| **ENA24-detection** ([LILA](https://lila.science/datasets/ena24detection)) | 23 classes from eastern North America; most common are American crow, American black bear and dog | ~10,000 still images | Bounding boxes on every image (COCO Camera Traps format). Human images removed | CDLA-Permissive 1.0 (per LILA) | Small box-labelled evaluation set for black bear and dog-like animals | No | No | No grizzly, wolf, cougar or moose; small |
| **Animal Kingdom** ([GitHub](https://github.com/sutdcv/Animal-Kingdom)) | 850+ species across 6 animal classes | ~50 h of annotated video for grounding, 30K video sequences for action recognition, 33K pose frames | 140 behaviour (action) labels, pose keypoints, video grounding | **No licence published** on the repository page; access is through a request form. Treat as research only, not for a commercial product, until written terms are obtained | Limited | Some (video) | Yes: the only candidate with behaviour labels | Wild settings unlike yard cameras; terms unknown |
| **WCS Camera Traps** ([LILA](https://lila.science/datasets/wcscameratraps)) | ~675 species from 12 countries; North American coverage not stated (mostly not North American) | ~1.4 million still images | Image and sequence labels; ~375,000 boxes on ~300,000 images (with species, or animal/person/vehicle only) | CDLA-Permissive 1.0 (per LILA) | Generic "animal" detection robustness and negatives | No | No | Little coverage of the Phase 1 targets |
| **MegaDetector** (model; [repo](https://github.com/agentmorris/MegaDetector), [Pytorch-Wildlife](https://github.com/microsoft/Pytorch-Wildlife)) | Classes: animal, person, vehicle. Does not identify species | Pretrained detector for camera-trap stills; also run on video frames | Boxes for the three classes | Code: MIT. **Weights licence is not stated on the repository pages**: MDv5 is YOLOv5-based (Ultralytics is AGPL-3.0) and MDv6 has YOLOv9/YOLOv10/RT-DETR variants, so the licence of each variant must be confirmed from its release notes before commercial use | Strong candidate for a better generic animal detector at night and in vegetation | Detections can feed Sentinel's tracker | No | No species; tuned for camera traps, not elevated house cameras |

Other candidates checked:

| Candidate | What the source says | Note |
| --- | --- | --- |
| **Idaho Camera Traps** ([LILA](https://lila.science/datasets/idaho-camera-traps/)) | ~1.5M images, 62 categories (deer, elk and cattle most common), sequence-level labels | "Images may not be sold in any format, but may be used for scientific publications": **not suitable for a commercial product without permission** |
| **SpeciesNet** ([GitHub](https://github.com/google/cameratrapai)) | MegaDetector plus a Google species classifier with 2,000+ labels | Repository is Apache-2.0; weights licence and North American species list not stated on the page. Verify before use |
| **Pytorch-Wildlife classifiers** | Amazon, Serengeti, opossum, Deepfaune (Europe), DFNE (Deepfaune fine-tuned for northeastern North America) | DFNE is the only listed North American classifier; licence and species list to check |

### Missing training data

No public dataset matches Sentinel's situation:

1. **Security-camera viewpoint.** Camera traps sit at knee height. Home cameras look down from 2.5–4 m through wide-angle lenses on compressed sub-streams at 5 fps.
2. **Tracked video.** Approach, retreat and turning need labelled track IDs over time. Almost every camera-trap dataset is still images or short bursts.
3. **Expert-verified black bear vs grizzly labels**, including night IR, where colour cues disappear.
4. **Cougar, wolf and moose at night near buildings**: rare in every dataset.
5. **Hard negatives from residential scenes**: leashed dogs, deer on lawns, livestock, wind-blown vegetation, snow, rain and insects on the lens.
6. **Calibrated footage with measured distances**, to check the Phase 2 distance and time-to-zone estimates.
7. **Behaviour labels by wildlife professionals.** Without them Sentinel can describe movement, but it cannot classify behaviour.
8. **Commercially usable licences.** The richest North American set (Idaho) and the only behaviour set (Animal Kingdom) are not cleared for a sold product.

Plan: record and label your own footage with the existing benchmark tools (`sentinel.bench`, Sentinel label format). Every accuracy claim must name the labelled footage behind it.

---

## Phase 2: Movement intelligence (implemented, synthetic tests only)

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
| Toward a person | Distance to the nearest person track on the same camera, shrinking, **and** the animal's own image velocity points at that person. When the gap shrinks because the person is walking up to the animal, it is reported as `person_nearing` ("in view while a person gets closer to it"), never as the animal moving | Same perspective limits. Both must be visible |
| Sudden direction change | Heading in the latest window vs the previous window, at least 100° apart, both moving | Needs ~5 s of history |
| Sustained approach | Approach (or presence in a protected zone) for at least `sustained_s` (3 s), allowing a 1 s gap | |
| Distance and time-to-zone | **Only for calibrated cameras**: a homography from at least 4 image points to measured ground points in metres. Reported as "approximate, flat ground" with calibration status (`unverified` with exactly 4 points; `verified` with more than 4 points when both the fit error and the leave-one-out check, each mark predicted from the others, are ≤ 0.5 m) | Wrong on slopes, and when the box bottom is not the feet (occlusion) |
| Camera movement | `moving_camera: true` (PTZ, vehicle, drone) caps every level at Observed. All objects sliding together is also treated as camera motion for 3 s | A single animal on a shaking camera cannot be told from real movement |
| Tracking glitches | A jump of more than 2.5 body heights in under 1 s resets the movement history instead of being reported as a dash | |
| Species label | Confidence-weighted vote over the whole track (detectors flip between `bear` and `dog` on one animal). When a second label has at least 25 % of the votes the display says so ("Animal (species not determined; detector labels: bear 64 %, dog 36 %)") and the higher concern of the labels is used | |
| Worker restarts | Track ids of a restarted camera worker carry the run (`cam:r2-1`), and a stopping worker ends its tracks, so a new animal never continues an old one. A guardian track nothing has mentioned for `reacquire_s` + 5 s is dropped | |

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
| Moving toward a person in view (`closing_on_person`) | +2 |
| A person in view getting closer to the animal (`person_nearing`) | +2 |
| A person simply in view | +1 |
| Sharp turn followed by approach | +1 |
| Speed increase while approaching | +1 |
| In view ≥ 60 s | +1 |
| High-concern animal staying in view ≥ `risk.linger_s` (60 s) (`lingering`), even without approaching (bins, feeders) | +1 |
| Calibrated time to zone ≤ 10 s, distance ≤ 15 m | +1 each (estimate) |

Thresholds: Approaching ≥ 2, Elevated Concern ≥ 4, Urgent Review ≥ 6, and every level above Observed needs an actual approach, zone presence, closing distance (either way) or a lingering high-concern animal. A bear staying 60 s by the house therefore reaches Elevated Concern (2 + 1 + 1). `sensitivity` shifts all thresholds by one point. Low confidence (track younger than `min_track_s` or mean detector confidence below `min_confidence`) and camera motion **cap** the level at Observed, and the event says why (`held_back`).

Language rules (tested): event text describes movement ("moving toward Back door for 6 s"). It never says attack, charging, aggressive, hunting, stalking, predatory, intends or safe. Notifications end with Sentinel's observed-activity disclaimer.

### Avoiding repeated false alarms

- A level must hold for `hold_s` (1 s) before it is emitted. It counts as held while the track is at that level *or above*, across dropouts of up to 1 s, so one missed detection or a score moving between two levels does not restart the clock (before 2026-10-16 it silenced every alert).
- Each guardian track emits each level once. A new alert for the same track needs the level to stay lower for `cooldown_s` (120 s) and then rise again.
- Events join the animal's incident (same site track, or a wildlife incident on the same camera). They never join a security incident by camera, and one animal's level steps count as one behaviour for corroboration, so a wildlife push carries its own level's severity. The existing alert policy notifies once per incident and again only when severity rises, under the global rate limit; an Urgent Review is never held back by that limit (it still counts toward it).
- A failure in the Guardian monitor is a `guardian` health component ("Wildlife movement analysis"): notified like other outages if it lasts, and back to normal on the next successful frame.
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

## Phase 3: Human protection (planned; facial recognition blocked)

Goal: protect people on the property. Some requested capabilities conflict with Sentinel's product boundaries (`CLAUDE.md`), and those boundaries take precedence until the owner records a decision that changes them.

| Capability | Plan | Boundary notes |
| --- | --- | --- |
| Possible break-ins | Sequences already supported: dwell at doors/windows, restricted-zone entry at night, repeated presence; add door/window contact sensors and glass-break audio as corroboration | Describe behaviour ("Person at rear window for 40 s, then window sensor opened"), never "burglar" (boundary 1) |
| Assault or threatening behaviour | Research pose-based "two people, rapid contact movements, one falls" sequences; human review only | High false-positive risk (play, sports, hugging). Wording stays observational. No automatic police calls (boundary 8) |
| A person being followed; stalking-like patterns | Existing repeated-presence, sequence and multi-camera rules | "May be by the same or different people" (boundary 2) |
| Trespass into protected or dangerous areas; prolonged activity; activity around vehicles, doors and windows | Existing zone-entry, dwell, roaming and sequence rules with temporal evidence; add zones for pools, machinery and drop-offs | Unfamiliar is not dangerous: no intent or criminality from appearance |
| Enrolled owners and household members | Opt-in enrolment, on-device templates, deletion on request, never used for strangers. An uncertain match is treated as unknown, never as a trusted owner (no auto-disarm on a weak match) | **Blocked** by boundary 2 until a product, security, legal and privacy decision is recorded. A mistaken identification must never trigger a physical defensive response. Biometric laws apply (PIPEDA, Quebec Law 25 biometric declaration, Illinois BIPA, GDPR Art. 9) |
| Potential weapons | Evaluate firearm/knife detectors with a measured false-positive rate on residential footage; "possible object resembling a firearm, unverified", human review only | Never escalates automatically; a phone or tool must not trigger police |
| Not mistaking strangers for threats | Unfamiliar ≠ threat: unknown people are logged as activity, as today. "Unusual is not a threat" | Existing convention |
| Lights, sirens, audible and voice warnings, two-way voice, live feeds | Home Assistant/MQTT actions tied to rules, either pre-authorised by the owner per rule or confirmed by a person; every action audited; quiet-hours and neighbour limits | No autonomous physical attack or weapon of any kind |
| Emergency response | One-tap "call for help" flows that notify people the owner chose, with the clip | No automatic calls to police or emergency services (boundary 8) |

---

## Phase 4: Emergency Guardian, drone and rover (research concept)

Sentinel's drone boundary (boundary 7) says no autonomous real-world flight. A human approves every inspection and a certified flight stack flies it. Everything in this phase stays **concept and simulation** until a separate safety case, legal review and animal-welfare review exist. **Nothing here is to be built as hardware or real-flight code**: no spray mechanisms, smoke generators, payload releases, weapon controls or autonomous interception.

### Emergency response sequence to research and simulate

1. A bear or other large animal approaches a person (Phase 2 provides sustained approach and closing distance).
2. Sentinel alerts the person (phone, outdoor speaker) and the people they chose.
3. A drone or rover evaluates safe *observation* positions that keep distance from both the animal and the person.
4. Sentinel shows situational awareness and possible escape routes (nearest building, lit path).
5. It keeps monitoring whether the animal continues approaching or changes direction.
6. It shows species-appropriate agency guidance only for a confirmed species, prioritising human and animal safety.

### Concepts discussed

| Concept | Research question | Concerns |
| --- | --- | --- |
| Respond to a fast approach toward a person | Could a drone already in the air (operator-approved) be redirected to watch the animal and report movement to the person's phone faster than a fixed camera? | Response time: a bear can cover 50 m in seconds. A drone launched after detection is very likely too late |
| Position between person and animal (simulation only) | In SITL: can geometry keep a drone between two moving tracks while respecting fences and minimum distances? | Flying near the person violates basic-operations distance rules and risks injuring the person you meant to protect |
| Track redirection | Re-plan the simulated position when the animal changes direction (Phase 2 provides turns and closing distance) | Drone video is not used for zone rules today (boundary 7) |
| Distraction and repeated interception (simulation only) | Does any evidence show drone presence diverts large carnivores, or does it provoke them? | Ditmer et al. (2015) measured large heart-rate spikes in American black bears near small drones. Drones have been used to haze elephants (Hahn et al., 2017); evidence for bears and cougars is thin |
| Expendable protective drone | Would an aircraft the owner accepts losing change any outcome, or only add a flying hazard near a frightened person and animal? | Drone movement itself can aggravate wildlife or strike the person. Research only |
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
| Distraction mechanisms (noise makers, moving decoys) | Little published evidence for large carnivores; habituation likely | May redirect the animal toward someone else | **Research concept** |
| Escape assistance | Phone/speaker guidance to the nearest building, with lights turned on along the path | Advice must match confirmed species and agency guidance | Plausible software feature after Phases 1–2 are validated on real footage |

Hardware and oversight per deterrent (estimates to verify; nothing is to be built):

| Deterrent | Weight and hardware implications | Professional oversight |
| --- | --- | --- |
| Speakers, sirens, spotlights (fixed) | Mains-powered outdoor devices via Home Assistant; no airborne payload | Electrician for wiring; local noise bylaws |
| Speaker or light on a drone | Adds payload mass and power draw, cutting endurance (see the X650 budget in `flame-hardware-evaluation.md`) | Transport Canada rules; wildlife agency advice |
| Bear spray, smoke | Pressurised canister or pyrotechnic plus a release mechanism: excluded | Not to be pursued without regulators, wildlife biologists and legal counsel |
| Expendable drone, distraction | A second aircraft or rover; crash and battery-fire hazards | Wildlife agency and aviation review |

Regulatory notes to verify with professionals: Transport Canada CARs Part IX (visual line of sight, distance from bystanders, night operations, restrictions on releasing objects), provincial wildlife acts and federal park rules that prohibit harassing wildlife and often prohibit drones, and the Criminal Code status of sprays. Consult wildlife biologists, conservation officers and Parks Canada before any physical trial.

**Rule: the system must never endanger its owner, bystanders or wildlife through an uncontrolled response.**

---

## Phase 5: Multi-platform Sentinel (planned)

A single Sentinel Hub coordinates:

- fixed security cameras (implemented),
- outdoor security drones (simulated and SITL; telemetry-only on real aircraft),
- indoor drones, ground rovers, and farm or wilderness monitoring devices (planned; registry entries only).

**Ground rovers** may be the first physical Guardian prototype: cheaper and easier to test than a flying drone, with no aviation rules, and able to patrol yards, driveways and farms. Navigation, collision avoidance and an independent emergency stop stay on the rover.

**Agricultural systems** reuse Sentinel Thermal and FLAME monitoring (`docs/flame.md`, simulated) for agricultural observations and safety alerts.

**Shared across platforms:** detection events, tracking data, incident history, confidence and risk assessments, notifications, video evidence and protected-zone configurations. Concretely: detections, `Track`s, guardian tracks and assessments, `SecurityEvent`s, incidents, evidence and alerts. `GuardianMonitor` is platform-neutral: any source that produces tracks can feed it, and it flags moving viewpoints (`moving_camera`).

**Platform-specific:** physical movement and safety: navigation, collision avoidance and emergency stop. Flight stays with the flight controller and the operator-approval workflow. Rover drive limits stay on the rover's own controller. A Hub fault must never command movement.

Needed before mobile platforms can use Phase 2 levels: ego-motion compensation (gimbal and vehicle pose, `drone/observation.py` already projects drone video to the ground with uncertainty), an explicit product decision on whether drone/rover tracks may feed wildlife levels (today drone video never feeds zone rules), and cross-platform identity rules (never merge identities on weak evidence).

---

## Real camera validation plan

Phase 2 has passed synthetic tests only. These steps validate it on real footage. Record the results in `ROADMAP.md` (Wilderness Guardian track) before any accuracy statement.

### A. Prerecorded wildlife videos (start here)

1. Put clips in `data/guardian_clips/`, which is git-ignored with the rest of `data/`. Use footage you are allowed to use: your own recordings, or clips whose licence permits it. Good first clips: a bear or deer walking toward a camera mounted at house height, a dog crossing a yard, an animal walking away.
2. Copy `config/sentinel.guardian.example.json` to `config/guardian.video.local.json`. Replace the camera source with `{"type": "file", "path": "data/guardian_clips/bear1.mp4", "loop": false}` and set `"storage": {"dir": "data/sentinel/guardian-test"}` so your real site is untouched.
3. Run `python -m sentinel validate --config config/guardian.video.local.json`, then `python -m sentinel serve --config config/guardian.video.local.json` and open http://127.0.0.1:8080.
4. In Setup, draw a protected zone (type `restricted`, or list its id in `guardian.protected_zones`) where a door or path would be.

### B. Your existing security camera

1. Stop Sentinel and make a backup: `python -m sentinel backup create --config <your config>`.
2. If you have edited the site in the dashboard, `<storage>/site.json` takes precedence over the start config, so add the `guardian` block there. Validate with `python -m sentinel validate --config <storage>/site.json`. Your camera credentials stay in the credential store.
3. Start Sentinel normally and keep the arm mode on Home or Away. While disarmed nothing is pushed (decision 1).

### Tests and expected results

| Test | How | Expected (if detection works) |
| --- | --- | --- |
| Approaching | Bear clip toward the camera, or walk a dog toward it | Movement `approaching`, target "the camera"; Approaching, then Elevated Concern for a bear after about 3 s. A dog-like animal stays at Approaching (3 points) unless it also heads for a protected zone or a person |
| Retreating | Clip of an animal walking away | `retreating`; no event above Observed |
| Lingering | Bear clip that stays in view for over a minute without approaching | Elevated Concern with factor `lingering` after about 60 s |
| Person walks up to an animal | Walk toward a still dog or deer | Factor `person_nearing`; the text says a person is getting closer, never that the animal moves |
| Dog across the yard | Walk a dog left to right at a steady distance | `lateral`, image heading `right`; no alert |
| Approaching a person | Stand still in view while the dog walks to you | Factor `closing_on_person`; Elevated Concern (dog) |
| Multiple detections | Two animals, or an animal plus people | Separate `guardian_track_id`s; only the approaching animal escalates |
| Protected-zone entry | Dog walks into the drawn zone | Target is the zone name, then `in_protected_zone` |
| Perspective change | Pan or zoom a PTZ camera, or a handheld clip | `held_back: camera_motion_suspected`, no alert; set `"guardian": {"moving_camera": true}` on PTZ cameras |
| Occlusion | Animal passes behind a bush or car for 3–8 s | Same guardian ID with `reacquired: 1`, or a new ID with `ambiguous_with` if two candidates exist |
| Ground-marker calibration | Five tape-measured marks on flat ground; read their image positions from a frame (normalised 0..1); add `cameras[].guardian.calibration` | `estimates.distance_to_zone_m` within about a metre of the tape at short range; `calibration: verified` only with more than 4 points and fit and leave-one-out errors ≤ 0.5 m (otherwise the status names the check error: re-measure) |

A teddy bear is COCO `teddy bear`, not `bear`, so it will not trigger the bear profile.

### How to inspect results

- **Live view:** box, trail, direction arrow and level ("Bear 1 approaching Elevated").
- **Guardian API:** `curl http://127.0.0.1:8080/api/v1/guardian` (no token needed on loopback without `SENTINEL_API_TOKEN`). Look at `movement`, `targets`, `estimates`, `assessment.factors` and `held_back`.
- **Timeline and Incidents:** `wildlife_approaching`, `wildlife_elevated_concern` and `wildlife_urgent_review` events, each with the factor list; the incident holds the clip (with pre-roll) and an annotated snapshot.
- **Recordings:** `<storage>/media/`. Clips play in the incident view.
- **Notifications:** the console log notifier always runs; ntfy/MQTT channels too if configured. Elevated Concern and Urgent Review only, once per level rise.
- **Feedback:** resolve each incident as `real_concern`, `false_alarm`, `expected_activity` or `unclear`. The nuisance report then counts false alarms per camera.

### Limitations to expect

- Night IR, small or distant animals, and partial bodies are often missed by the COCO detector, and Guardian can only track what is detected.
- Species are not identified. Cougars, wolves, coyotes and moose may appear as `cat`, `dog`, `horse`/`cow`, or not at all.
- Without calibration, all distances are relative and in the image. Box growth can come from posture changes.
- Thresholds are untuned on real animals: note every wrong level with its clip and timestamp.
