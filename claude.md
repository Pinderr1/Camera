# DIY Senior Night Safety Monitoring: Data-First Build Plan

This file is the product, data, and detection plan for a home safety prototype for an older adult who sleeps alone and may get up at night to use the washroom. The goal is not just "fall detection." The real goal is to catch urgent situations quickly while keeping false alarms low enough that caregivers still trust the system.

The system should be treated as an assistive safety monitor, not a medical device, proof-of-life system, breathing monitor, or emergency service. It can say "someone should check now." It cannot prove that someone is alive, dead, breathing safely, uninjured, or medically okay.

## Product Goal

Detect concerning situations such as:

- Possible fall.
- No movement for too long.
- Bathroom overstay.
- Bed exit with no return.
- Unusual inactivity compared to the normal routine.
- Person lying still after a possible fall.
- Possible breathing concern only as a weak optional signal, never as proof.

The most important product outcome is a fast caregiver check when the system sees a persistent high-risk pattern. The second most important outcome is avoiding alert fatigue. A system that alerts constantly will eventually be ignored, which makes it unsafe.

## Safety Boundaries

Non-negotiable boundaries:

- Do not claim "alive", "dead", "not breathing", "stroke", "heart attack", or any medical diagnosis.
- Do not use breathing estimates as a primary alert trigger in the MVP. At most, use them as a weak extra signal when other urgent signals are already present.
- Do not put a camera inside the bathroom unless the person explicitly wants that and understands the privacy tradeoff. Prefer bathroom door, occupancy, mmWave, pressure, or motion sensors.
- Do not automatically call emergency services in the first prototype. Escalate to caregivers first unless a human configures a tested response policy.
- Do not rely on one signal. Use state, timing, pose, movement, room/zone, and optional door/bed sensors together.
- Do not deploy without a backup manual help method such as a pendant, button, phone, or voice assistant.
- Do not hide monitoring from the person being monitored. Get consent and make it easy to pause.

If there is an actual suspected emergency, caregivers should call local emergency services. If she falls and may have hit her head, is on blood thinners, has chest pain, trouble breathing, new confusion, weakness, or cannot get up, the technology should not slow down a real response.

## Short Answer

Build this as a state machine plus rule-based sensor fusion first, not as a raw-video AI model.

Use camera pose detection where it helps, but the strongest MVP for this use case is:

- Bed occupancy signal.
- Hall/path camera or pose-only camera covering the bed-to-bathroom route.
- Bathroom door or occupancy signal, not bathroom video.
- Local rule engine with personalized nighttime thresholds.
- Alert ladder that starts with a gentle check and escalates only when risk persists.
- Review loop for every alert and missed event.

Use public datasets to understand fall shapes and benchmark basic logic, but collect home-like negative clips early. Public fall datasets are staged, biased by camera angle and environment, and often miss the exact household patterns that create false alerts.

The first data product should be:

- Raw local test clips where appropriate.
- Extracted pose landmarks per frame.
- Sensor events such as bed occupied, bathroom door opened, room occupied, button pressed.
- Clip-level labels.
- Event timestamps inside each clip.
- Detector outputs and reviewed failure labels.

## MVP Monitoring Concept

The MVP should watch the nighttime routine as a sequence:

1. Person is in bed during the night window.
2. Person exits bed.
3. Person moves through the expected path.
4. Person enters or occupies bathroom.
5. Person leaves bathroom.
6. Person returns to bed.

The system should become more concerned when this sequence breaks:

- Bed exit happens, then no bathroom entry and no bed return.
- Bathroom is occupied too long.
- Person becomes floor-level or horizontal outside bed.
- Person stays still after a rapid descent.
- Person disappears from all expected sensors during an active trip.
- Night routine looks unusual compared with her own baseline.

## Core State Machine

Use explicit states instead of one binary "fall/no fall" output:

- `asleep_in_bed`: bed occupied during night window, no concern.
- `bed_exit`: bed changed from occupied to unoccupied.
- `walking_to_bathroom`: motion or pose seen on the normal route.
- `bathroom_occupied`: bathroom door/occupancy indicates use.
- `returning_to_bed`: bathroom exit happened and motion is on the return path.
- `returned_to_bed`: bed occupied again.
- `out_of_bed_unknown`: bed exit happened but expected route/bathroom/return pattern is missing.
- `possible_fall`: rapid descent, floor-level posture, or abnormal body orientation.
- `fallen_no_motion`: possible fall plus persistent low movement.
- `bathroom_overstay`: bathroom occupied beyond personalized threshold.
- `unusual_inactivity`: routine deviates from baseline but is not urgent by itself.
- `needs_check`: caregiver should check soon.
- `urgent_alert`: caregiver should check immediately.
- `offline_or_blind`: camera/sensor/heartbeat failure prevents safe monitoring.

Every state transition should include a reason code, timestamp, sensor source, and confidence.

## Initial Alert Rules

These are starter thresholds only. Tune them to her real routine after a quiet observation period.

| Situation | Starter condition | First action | Escalation |
| --- | --- | --- | --- |
| Possible fall | Rapid drop or floor-level posture outside bed | Watch for 10 to 20 seconds and collect more evidence | Urgent if still floor-level or not moving after 30 to 60 seconds |
| Lying still after possible fall | Possible fall plus low motion | Soft check-in if available | Urgent caregiver alert after 30 to 60 seconds |
| Bed exit with no return | Bed unoccupied during night, no return | Low-severity check after 10 to 15 minutes | Higher severity after 20 to 30 minutes or if route sensors go quiet |
| Bathroom overstay | Bathroom occupied longer than personal normal | Low-severity check after baseline p95 plus 5 minutes, or 15 to 20 minutes before baseline exists | Urgent if 30+ minutes, no movement, or repeated failed check-ins |
| No movement too long | Context-dependent inactivity outside bed | Check state and zone before alerting | Urgent only when outside-bed, floor-level, bathroom, or active-trip context exists |
| Unusual inactivity | Daily routine deviates from baseline | Non-urgent wellness notification | Escalate only if combined with missed medication/meal/room activity or failed check-in |
| Possible breathing concern | Weak signal from video/mmWave while in bed | Log as weak evidence only | Never alert by itself in MVP; combine only with no response and other urgent context |

## Alert Ladder

False alarms need to be managed with process, not just model tuning.

1. `observe`: no notification; continue watching.
2. `soft_check`: local chime or voice prompt, if acceptable: "Are you okay?" with a cancel button, bedside button, voice phrase, or caregiver app acknowledgement.
3. `caregiver_notice`: send low-severity message with state, time, zone, and reason codes.
4. `caregiver_urgent`: call/push/SMS primary caregiver with "check now" language.
5. `backup_escalation`: contact backup caregiver if primary does not acknowledge within a configured window.
6. `emergency_policy`: only after the family has tested the system and explicitly configured when to call emergency services.

Each alert should include:

- Current state.
- Why it fired.
- How long the condition has persisted.
- Last seen zone.
- Whether sensors are healthy.
- Small review clip or pose-only summary, depending on privacy settings.
- Buttons: `I checked - real issue`, `I checked - false alarm`, `She is okay`, `Call backup`, `Pause alerts`.

## Low False Alarm Design

Use these tactics before training any custom model:

- Require persistence for urgent alerts. A single odd frame should never be enough.
- Combine independent signals: pose plus motion plus bed/door/occupancy state.
- Treat bed, chair, couch, and normal floor-exercise zones differently.
- Suppress "normal lie down" when the person starts near the bed and descent is slow.
- Suppress bathroom overstay alerts if bathroom occupancy is noisy but route/bed sensors show a normal return.
- Use personalized baselines: typical wake times, bathroom duration, walking speed, and bed-return duration.
- Add a manual cancel/help button within reach of bed and bathroom path.
- Add alert cooldowns so one incident does not send repeated messages every minute.
- Use `uncertain` as a real state. If visibility is bad, alert as "cannot verify, please check" only when the context is risky.
- Keep a review queue and label every alert outcome.

## Recommended Hardware Layout

Start simple and privacy-preserving:

- Bed occupancy: under-mattress pressure sensor, bed mat, or load/pressure pad.
- Bedside manual button: big, reachable, and simple.
- Hall/path camera: covers the bed exit path or hallway to bathroom, preferably not pointed at private areas.
- Bathroom door sensor: contact sensor for open/close.
- Bathroom occupancy sensor: mmWave or PIR outside/inside bathroom if acceptable; avoid video.
- Night lighting: motion-activated low-glare path lights.
- Local compute: small PC, mini PC, or edge device that can run pose detection locally.
- Network/power reliability: UPS for router/compute/camera and health checks for camera/sensor offline states.
- Optional wearable: pendant/watch only if she will actually wear it.

Best first camera placement is often not "watch the whole bedroom." It is "watch the risky transition path" while using a bed sensor to know she got up. If bedside falls are the main risk, use a privacy-limited bedroom view, pose-only processing, or a non-camera sensor such as mmWave.

## Home Safety Fixes To Do Alongside The Build

The system catches problems after they start. Also reduce the chance of the problem:

- Clear the bed-to-bathroom path.
- Remove loose rugs, cords, low furniture, and clutter.
- Add motion night lights from bed to bathroom.
- Add grab bars and non-slip surfaces where appropriate.
- Keep a phone, button, or pendant reachable from bed and bathroom.
- Consider a bedside commode if the walk itself is the main danger.
- Ask her clinician or pharmacist about fall-risk review, especially after a recent fall.

## Observation-Only Tuning Period

Before trusting alerts:

1. Run the system for 7 to 14 nights in observation mode.
2. Do not wake caregivers for every prototype alert during tuning.
3. Review all overnight state transitions each morning.
4. Set personal baselines for:
   - Typical bed exit count.
   - Bathroom duration.
   - Bed-to-bathroom travel time.
   - Bathroom-to-bed return time.
   - Normal restless movement.
   - Normal quiet sleep.
5. Turn on caregiver notifications only after false alerts are understood.

During tuning, the family should still use existing safety checks and manual alert tools.

## Recommended Public Datasets

### 1. OmniFall

Best use: current benchmark and label taxonomy reference.

OmniFall is the most useful starting point for planning because it unifies multiple staged fall datasets, synthetic data, and in-the-wild accident clips under a shared taxonomy with frame-level annotations. The 2026 version reports 15k videos, 80 hours, and 16-class annotations.

Modalities:

- Video references / unified annotations.
- Frame-level labels.
- Dataset identity, camera, subject, start/end segments.
- Not primarily a pose-landmark dataset, but good for temporal labels.

Use it for:

- Label vocabulary inspiration.
- Train/test split ideas.
- Understanding the gap between staged falls and real falls.

Do not use it as the only source of truth for a home prototype.

Source: https://arxiv.org/abs/2505.19889 and https://huggingface.co/datasets/simplexsigil2/omnifall

### 2. UR Fall Detection Dataset (URFD)

Best use: first practical camera + sensor benchmark.

URFD contains 70 sequences: 30 falls and 40 activities of daily living. Falls include two Kinect camera views, RGB, depth, synchronization data, and accelerometer data. ADLs include one camera view and accelerometer data. It also includes extracted depth-map features with labels for lying, not lying, and transitional falling frames.

Modalities:

- RGB image sequences.
- Depth image sequences.
- Accelerometer CSV.
- Synchronization CSV.
- Extracted posture/depth features.
- Labels for fall/ADL and posture state.

Use it for:

- Testing pose/rule logic on known fall vs ADL clips.
- Understanding depth/floor-level cues.
- Learning how difficult transitions are.

Limitations:

- Small.
- Staged.
- Kinect/depth setup may not match a normal webcam.

Source: https://fenix.univ.rzeszow.pl/~mkepski/ds/uf.html

### 3. Le2i / ImViA Fall Detection Dataset

Best use: single-camera surveillance-style RGB video.

This dataset has 191 annotated videos recorded at 25 FPS and 320x240 resolution in realistic indoor surveillance settings. It includes normal daily activities and falls with manually annotated body bounding boxes and fall position information.

Modalities:

- RGB video / JPEG image data.
- Bounding-box annotations.
- Fall position / frame annotations.

Use it for:

- Webcam-like testing.
- False-positive checks in cluttered indoor environments.
- Verifying whether low-resolution video is enough.

Limitations:

- Older, low resolution.
- Staged.
- No pose landmarks included by default.

Source: https://search-data.ubfc.fr/imvia/FR-13002091000019-2024-04-09_Fall-Detection-Dataset.html

### 4. UP-Fall Detection Dataset

Best use: multimodal sensor comparison.

UP-Fall contains 11 activities performed by 17 subjects with 3 attempts each. Its CSV files include wearable IMU streams from ankle, pocket, belt, neck, and wrist, plus EEG and infrared sensors. The project page also provides camera image downloads and optical-flow features.

Modalities:

- Wearable accelerometer and gyroscope.
- Luminosity values.
- Infrared sensors.
- EEG value.
- Camera images.
- Optical-flow features.
- Activity/trial organization.

Use it for:

- Understanding how non-camera signals behave.
- Comparing camera-only vs sensor-fusion ideas.
- Activity classes and trial organization.

Limitations:

- More complex than needed for a first camera-only prototype.
- Wearables are not the main target.

Source: https://sites.google.com/up.edu.mx/har-up/

### 5. MUVIM / Multi Visual Modality Fall Detection Dataset

Best use: privacy and low-light modality research.

MUVIM includes infrared, depth, RGB, and thermal camera modalities. The paper reports that infrared performed best in their anomaly-detection experiments, followed by thermal, depth, and RGB.

Modalities:

- Infrared.
- Depth.
- RGB.
- Thermal.

Use it for:

- Deciding whether RGB is enough.
- Privacy-oriented future hardware planning.
- Low-light tradeoff research.

Limitations:

- Less direct for a cheap DIY webcam MVP.
- Access and format need to be checked before depending on it.

Source: https://arxiv.org/abs/2206.12740

### 6. NTU RGB+D / NTU RGB+D 120

Best use: supplemental pose/action data, not fall-specific evaluation.

NTU RGB+D is a large action-recognition dataset with RGB, depth, infrared, and 3D skeleton data. It includes many activities relevant to false positives, such as sitting down, standing up, pickup, lying/falling-like health actions, and daily movements.

Modalities:

- RGB.
- Depth.
- Infrared.
- 3D skeleton.
- Action labels.

Use it for:

- Testing pose features on non-fall actions.
- Future action-classifier training.
- More variation in bodies, views, and activities.

Limitations:

- It is not a dedicated fall-monitoring dataset.
- Labels are action classes, not fall-event timelines.

Source: https://arxiv.org/abs/1604.02808 and https://arxiv.org/abs/1905.04757

### 7. SisFall

Best use: optional wearable-only reference.

SisFall is useful for understanding accelerometer/gyroscope fall signatures, but it is not a camera dataset. Keep it as a reference only unless the prototype later adds a watch or phone sensor.

Modalities:

- Wearable accelerometer.
- Gyroscope / inertial signals.
- Activity and fall labels.

Use it for:

- Future sensor-fusion ideas.
- Understanding impact and inactivity timing.

Limitations:

- Not useful for validating camera-only pose detection.

## Recommendation: Rules First, Training Later

### Use rule-based pose detection first

Reason: a useful prototype can be built faster by extracting pose landmarks with a pre-trained pose model, then applying transparent temporal rules. MediaPipe Pose Landmarker outputs 33 landmarks in image and world coordinates. MoveNet outputs 17 body keypoints and is designed for fast real-time pose estimation.

Sources:

- MediaPipe Pose Landmarker: https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker
- MoveNet: https://www.tensorflow.org/hub/tutorials/movenet

Initial detector logic should track:

- Person presence confidence.
- Hip/torso center y-position over time.
- Shoulder-to-hip torso angle.
- Bounding-box aspect ratio.
- Head/shoulder/hip/ankle floor proximity.
- Body-center velocity.
- Horizontal posture duration.
- Post-event motion score for 30 to 60 seconds.

### Train only after collecting failure cases

Training too early will mostly teach the model staged-fall bias. Add a trained classifier only after there are enough locally collected clips of:

- Real false positives.
- Real missed staged falls.
- Normal household activities.
- Bad lighting and occlusion.
- Person partially out of frame.

First trained model should be small and interpretable:

- Input: pose/time-series features, not raw video.
- Output: fall probability and reason codes.
- Candidate models: logistic regression, random forest, gradient boosted trees, or small temporal model.

Avoid raw-video deep learning until the prototype has hundreds or thousands of well-labeled clips.

## Own Data Collection Format

Store raw media separately from derived pose/features. Never overwrite raw data.

Recommended root:

```text
data/
  README.md
  dataset_manifest.csv
  raw/
    public/
      urfd/
      le2i/
      upfall/
      omnifall/
    local/
      camera_01/
        2026-07-06/
          clips/
  processed/
    pose/
      camera_01/
    features/
      camera_01/
    sensor_events/
      sensor_bed_01/
      sensor_bathdoor_01/
    thumbnails/
  labels/
    clips.csv
    events.csv
    sensor_events.csv
    routine_transitions.csv
    alert_reviews.csv
    review_log.csv
    label_schema.json
  baselines/
    nightly_routine_baseline.json
    bathroom_duration_baseline.json
  splits/
    train.csv
    validation.csv
    test.csv
    holdout_home.csv
  detector_runs/
    rules_v001/
      predictions.csv
      metrics.json
      false_positives.csv
      false_negatives.csv
```

### Clip naming

Use stable, boring names:

```text
cam01_20260706T143015-0700_00030s_kitchen_stagedfall_001.mp4
cam01_20260706T150240-0700_00060s_livingroom_adl_sitting_003.mp4
```

Filename fields:

- Camera ID.
- Local timestamp with timezone.
- Duration.
- Room.
- Scenario group.
- Sequence number.

Do not put real names in filenames.

### dataset_manifest.csv

One row per source file.

```csv
clip_id,source,source_dataset,camera_id,room,start_time_local,duration_s,width,height,fps,has_audio,has_depth,has_pose,has_sensor_events,privacy_level,consent_status,file_path,sha256,notes
```

Recommended values:

- `source`: `public`, `local_staged`, `local_daily`, `synthetic`
- `privacy_level`: `raw_identifiable`, `face_blurred`, `body_crop`, `pose_only`, `sensor_only`, `public`
- `consent_status`: `public_license`, `self_recorded`, `written_consent`, `delete_requested`, `unknown`

### Pose file format

Use JSONL or Parquet. JSONL is easiest to debug; Parquet is better once files get large.

Recommended JSONL row:

```json
{
  "clip_id": "cam01_20260706T143015-0700_00030s_kitchen_stagedfall_001",
  "frame_index": 42,
  "timestamp_ms": 1400,
  "person_id": 0,
  "pose_model": "mediapipe_pose_landmarker_full",
  "pose_confidence": 0.91,
  "landmarks": [
    {"name": "left_shoulder", "x": 0.42, "y": 0.31, "z": -0.12, "visibility": 0.98}
  ],
  "bbox": {"x": 0.33, "y": 0.20, "w": 0.22, "h": 0.58},
  "quality_flags": []
}
```

### Feature file format

One row per frame or sliding window. These are the values the rule engine should use so it can run without re-reading raw video.

```csv
clip_id,window_start_ms,window_end_ms,person_id,pose_ok,torso_angle_deg,bbox_aspect,hip_y,head_y,ankle_y,center_vy,center_speed,posture_state,motion_score,occlusion_score,lighting_score,zone_id,bed_occupied,bathroom_occupied,bathroom_door_state,routine_state
```

Useful derived states:

- `upright`
- `bending`
- `sitting`
- `lying`
- `fallen`
- `unknown`
- `out_of_frame`

Useful routine states:

- `asleep_in_bed`
- `bed_exit`
- `walking_to_bathroom`
- `bathroom_occupied`
- `returning_to_bed`
- `returned_to_bed`
- `out_of_bed_unknown`
- `possible_fall`
- `fallen_no_motion`
- `bathroom_overstay`
- `unusual_inactivity`
- `offline_or_blind`

### sensor_events.csv

One row per non-camera event. This lets the rule engine work even when a camera is occluded or intentionally avoided for privacy.

```csv
event_id,sensor_id,sensor_type,room,zone_id,event_time_local,event_time_ms,event_name,value,confidence,battery_ok,network_ok,notes
```

Examples:

- `bed_occupied_true`
- `bed_occupied_false`
- `bathroom_door_open`
- `bathroom_door_closed`
- `bathroom_motion_start`
- `bathroom_motion_stop`
- `manual_help_pressed`
- `manual_cancel_pressed`
- `sensor_offline`
- `sensor_online`

### routine_transitions.csv

One row per inferred state transition.

```csv
transition_id,start_time_local,end_time_local,from_state,to_state,trigger_event_ids,duration_s,confidence,reason_codes,review_status,notes
```

Use this to tune bed-exit, bathroom-overstay, and return-to-bed logic without needing to watch raw clips.

## Labeling Rules

Use two layers of labels:

- Human truth labels: what actually happened.
- Detector review labels: whether the system behaved correctly.

### clips.csv

One row per clip.

```csv
clip_id,primary_label,scenario,fall_direction,intentional,subject_type,room,lighting,occlusion,camera_angle,review_status,reviewer,notes
```

`primary_label` values:

- `fall`
- `no_fall`
- `routine_event`
- `sensor_only`
- `offline_or_blind`
- `uncertain`

`scenario` examples:

- `fall_forward`
- `fall_backward`
- `fall_side`
- `collapse`
- `sit_down`
- `lie_down`
- `stand_up`
- `pick_up_object`
- `bend_over`
- `kneel`
- `crawl`
- `exercise_floor`
- `pet`
- `blanket_or_laundry`
- `bad_lighting`
- `empty_room`
- `occluded_person`
- `bed_exit_normal`
- `bed_exit_no_return`
- `bathroom_trip_normal`
- `bathroom_overstay`
- `night_wandering`
- `unusual_inactivity`
- `manual_help_button`
- `manual_cancel`
- `sensor_offline`

### events.csv

One row per meaningful event inside a clip.

```csv
event_id,clip_id,event_label,start_ms,impact_ms,end_ms,confidence,notes
```

`event_label` values:

- `fall_start`
- `impact_or_floor_contact`
- `fallen_state_start`
- `recovery_start`
- `floor_level_posture_start`
- `no_motion_start`
- `no_motion_end`
- `normal_sit`
- `normal_lie_down`
- `bend_or_pickup`
- `bed_exit`
- `bed_return`
- `bathroom_enter`
- `bathroom_exit`
- `bathroom_overstay_start`
- `bathroom_overstay_end`
- `route_motion_start`
- `route_motion_end`
- `manual_help_pressed`
- `manual_cancel_pressed`
- `possible_breathing_weak_signal`
- `sensor_offline`
- `sensor_online`
- `occlusion`
- `person_leaves_frame`

For a fall, label:

- Start of loss of balance or sudden descent.
- Impact or floor contact if visible.
- Start of lying/fallen state.
- Recovery start if the person gets up.

For a nighttime bathroom trip, label:

- Bed exit.
- First route/path motion if visible.
- Bathroom entry or occupancy start.
- Bathroom exit or occupancy end.
- Bed return.
- Any missing segment that makes the state uncertain.

For bathroom overstay or bed-exit-no-return events, label the first time the rule would have become concerned, not only the final alert time.

### review_log.csv

One row per detector decision.

```csv
run_id,clip_id,predicted_label,truth_label,alert_time_ms,decision_latency_s,review_label,severity,reason_code,ack_time_ms,responder,reviewer,notes
```

`review_label` values:

- `true_positive`: fall happened and detector alerted.
- `true_negative`: no fall happened and detector stayed quiet.
- `false_positive`: no fall happened but detector alerted.
- `false_negative`: fall happened but detector missed it.
- `helpful_check`: alert was not a confirmed fall but a caregiver check was appropriate.
- `nuisance_alert`: technically explainable but too noisy for real life.
- `suppressed_correctly`: rule would have alerted but suppression was appropriate.
- `suppressed_incorrectly`: rule suppression hid a real concern.
- `uncertain`: reviewer cannot confidently decide.

Important: `false_positive` and `false_negative` are not human truth labels. They are detector outcome labels for a specific run.

## Labeling Guidance

Use `fall` when:

- There is uncontrolled descent, collapse, trip, slip, or loss of balance.
- The body rapidly transitions downward and ends floor-level or supported by floor/furniture.
- The person appears unable to immediately continue the previous activity.

Use `no_fall` when:

- The person intentionally sits, lies down, stretches, kneels, crawls, picks up an object, plays with a pet, exercises, or gets on the floor in a controlled way.
- The person is already lying down before the clip begins.
- Only a blanket, pet, shadow, reflection, or object moves.

Use `routine_event` when:

- The event is a normal bed exit, bathroom trip, bed return, or other daily pattern that matters for state tracking.
- There is no fall, but the sequence timing is useful for personal baselines.
- The clip or sensor record is primarily about routine duration rather than body posture.

Use `uncertain` when:

- The body is mostly occluded during the key moment.
- The person leaves frame before impact or recovery.
- The clip starts too late or ends too early.
- Lighting prevents confident judgment.
- The action could be either a controlled lie-down or a fall.

Do not force uncertain clips into fall/no-fall during early development. They are gold for later review.

## Minimum Data Needed

### Week-one useful prototype

Target:

- 30 to 50 staged fall clips.
- 150 to 300 no-fall clips.
- 30 to 60 hard negative clips.
- 7 to 14 nights of observation-only bed/bathroom routine logs.
- 30+ normal bed-exit and bathroom-return sequences if safely observable.
- 10+ examples of sensor noise: door left open, missed motion, bed sensor bounce, camera occlusion, network drop.
- 2 to 4 rooms or camera angles.
- 2 to 5 able-bodied adult volunteers if possible.

Hard negatives matter more than extra staged falls. Include sitting, lying down, floor exercises, bending, picking things up, blankets, pets if applicable, dim lighting, partial occlusion, people leaving frame, normal bathroom trips, and normal bed exits.

### Rule tuning milestone

Target:

- 100 staged fall clips.
- 500 no-fall clips.
- 150 hard negatives.
- 30+ nights of routine state logs.
- Personalized p50, p90, and p95 timing for bathroom duration and bed-return duration.
- At least 10 clips each for sitting, lying down, bending/pickup, kneeling, exercising, crawling-like movement, bad lighting, occlusion, and empty/pet/blanket motion.

### First small trained classifier

Target:

- 200+ fall clips.
- 1,000+ no-fall clips.
- 300+ hard negatives.
- At least 100 reviewed detector failures.

If you do not have reviewed false positives and false negatives yet, keep improving rules instead of training.

## Safe Staged-Fall Testing

Do not ask seniors, frail people, injured people, or anyone with balance/medical issues to perform staged falls.

Use:

- Able-bodied adult volunteer only.
- Thick gym mats or mattress pads.
- Spotter present.
- Clear floor area.
- No furniture corners, cords, rugs, glass, pets, or clutter.
- Slow rehearsals first.
- Controlled kneel-to-mat and side-roll simulations before any faster movement.
- Stop immediately if there is pain, dizziness, fatigue, or hesitation.

Safer staged scenarios:

- Controlled sit-to-floor.
- Kneel-to-side on mat.
- Couch/chair slide to floor.
- Slow sideways loss of balance onto mat.
- Drop object, bend, recover.
- Lie down normally.
- Floor stretch/exercise.

Avoid:

- Real trips.
- Head impact.
- Backward falls without trained supervision.
- Staged falls from height.
- Testing alone.
- Repeating falls until tired.

The goal is detector data, not realism at any cost.

## Privacy Rules

Default privacy posture:

- Process locally.
- Keep raw videos out of cloud sync.
- Store pose landmarks and derived features as the main working dataset.
- Keep raw video only as long as needed for review.
- Blur faces or crop bodies where practical.
- Disable audio unless there is a clear reason to keep it.
- Do not record bedrooms, bathrooms, medication areas, computer screens, mail, financial papers, or visitors without consent.
- Use visible signage during testing.
- Get written consent from every staged participant.
- Give participants deletion rights.
- Keep a `consent/` folder outside public repos if documents are needed.
- Never commit raw video or identifiable frames to git.

Privacy levels:

- `raw_identifiable`: original clip with face/room details.
- `face_blurred`: video with faces blurred.
- `body_crop`: video cropped around person.
- `pose_only`: landmarks/features only.
- `synthetic_or_public`: public/non-identifying source.

For normal development, prefer `pose_only`.

## Testing Strategy

Measure these separately:

- Detection: did it alert when a fall happened?
- Specificity: did it stay quiet during normal actions?
- Latency: how long after impact/fallen-state did it alert?
- Routine correctness: did it track bed exit, bathroom occupancy, and bed return correctly?
- Alert usefulness: did a caregiver agree the alert was worth receiving?
- Coverage: was the system online and able to see or sense the monitored zones?

Recommended metrics:

- Recall for falls.
- Recall for bed-exit-no-return events.
- Recall for bathroom-overstay events.
- False urgent alerts per night.
- False low-severity notices per week.
- Precision for urgent alerts.
- Alert latency median and 95th percentile.
- Bathroom trip state accuracy.
- Bed occupancy transition accuracy.
- Sensor uptime.
- Percent of time in `offline_or_blind`.
- Caregiver acknowledgement time.
- Missed-fall reason counts.
- Nuisance-alert reason counts.

Early acceptance target:

- Detect most obvious staged falls.
- Alert after 30 to 60 seconds of no movement.
- Detect normal bed-exit and bed-return sequences reliably during observation mode.
- Detect bathroom occupancy long enough to tune overstay rules.
- Fewer than 1 urgent false alert per week before relying on overnight wakeups.
- Fewer than 1 low-severity nuisance notice per night.
- Never alert on empty room, pets, blankets, or normal sitting/lying in the basic test set.
- Enter `offline_or_blind` within 60 seconds of losing critical camera/sensor input.

Do not optimize only for accuracy. A dataset with many no-fall clips can look accurate while missing falls.

## Detection Engine Plug-In Contract

Design the later app around a detector module that consumes frame/pose records plus sensor events and emits state transitions. Keep it independent from UI, storage, notifications, and camera drivers.

Input:

```json
{
  "camera_id": "cam01",
  "timestamp_ms": 123456,
  "frame": "optional frame bytes or image reference",
  "pose": "optional pose landmarks",
  "sensor_events": [
    {
      "sensor_id": "bed_01",
      "sensor_type": "bed_pressure",
      "event_name": "bed_occupied_false",
      "value": false,
      "timestamp_ms": 123450,
      "confidence": 0.98
    }
  ],
  "current_state": "asleep_in_bed",
  "zone_id": "bedroom_path",
  "metadata": {
    "fps": 30,
    "width": 1280,
    "height": 720,
    "night_window_active": true
  }
}
```

Output:

```json
{
  "camera_id": "cam01",
  "timestamp_ms": 123456,
  "state": "asleep_in_bed | bed_exit | walking_to_bathroom | bathroom_occupied | returning_to_bed | returned_to_bed | out_of_bed_unknown | possible_fall | fallen_no_motion | bathroom_overstay | unusual_inactivity | needs_check | urgent_alert | offline_or_blind",
  "score": 0.0,
  "severity": "none | info | low | urgent",
  "reason_codes": ["rapid_drop", "horizontal_body", "no_motion_45s", "outside_bed"],
  "person_id": 0,
  "recommended_action": "observe | soft_check | notify_caregiver | urgent_caregiver_check | backup_escalation",
  "suppressions": [],
  "debug": {
    "torso_angle_deg": 78,
    "motion_score": 0.03,
    "duration_no_motion_s": 45,
    "bed_unoccupied_s": 80,
    "bathroom_occupied_s": 0,
    "sensor_health": "ok"
  }
}
```

Later app components can subscribe to these detector events:

- Live status UI.
- Clip recorder.
- Review queue.
- Alert/notification service.
- Metrics dashboard.
- Baseline updater.
- Sensor health monitor.

## MVP Phases

### Phase 0: Data setup

Create folders, manifests, label schema, example rules, and the response plan. Download or reference public datasets only for fall-shape benchmarking. Do not build a polished app yet.

Deliverables:

- `data/dataset_manifest.csv`
- `data/labels/label_schema.json`
- `data/labels/clips.csv`
- `data/labels/events.csv`
- `data/labels/sensor_events.csv`
- `data/labels/routine_transitions.csv`
- `data/labels/review_log.csv`
- `config/monitoring-rules.example.json`
- `docs/emergency-response-plan.md`
- `docs/home-setup-checklist.md`

### Phase 1: Home setup and observation

Install only the minimum sensor layout needed to observe the risky nighttime sequence. Run in observation-only mode first.

Collect:

- Empty room.
- Normal walking.
- Normal bed exit.
- Normal bathroom trip.
- Normal bed return.
- Sitting down.
- Standing up.
- Lying down intentionally.
- Bending/picking up object.
- Kneeling.
- Floor exercise/stretching.
- Blanket/laundry motion.
- Pet motion if relevant.
- Bad lighting.
- Partial occlusion.
- Safe staged fall simulations.
- Bed sensor changes.
- Bathroom door or occupancy events.
- Camera/sensor offline cases.

Deliverable:

- 200+ labeled local clips or sensor sequences.
- 7 to 14 nights of routine logs.
- First personal baseline for bathroom duration and bed-return duration.

### Phase 2: Pose extraction

Run a pre-trained pose detector over every clip where video is used. Save pose JSONL, sensor events, routine transitions, and per-window features.

Deliverables:

- `processed/pose/...jsonl`
- `processed/features/...csv`
- `processed/sensor_events/...csv`
- `labels/routine_transitions.csv`
- Quality flags for missing/low-confidence pose.

### Phase 3: Offline rule baseline

Build only the detection logic needed to score clips and sensor sequences offline. No full caregiver alerting yet.

Baseline rules:

- Detect rapid downward movement.
- Confirm horizontal/floor-level posture.
- Wait for low motion for 30 to 60 seconds.
- Suppress alerts for controlled sit/lie patterns where descent is slow and posture starts near furniture or expected zones.
- Detect bed exit during the night window.
- Track expected route, bathroom occupancy, bathroom exit, and bed return.
- Flag bathroom overstay after personalized threshold or starter threshold.
- Flag bed exit with no return after personalized threshold or starter threshold.
- Enter `offline_or_blind` when critical sensors stop reporting.

Deliverables:

- Predictions CSV.
- Metrics JSON.
- False-positive and false-negative review lists.
- Bathroom-overstay and bed-exit-no-return review lists.
- Nuisance-alert review list.

### Phase 4: Soft check and caregiver notification pilot

Turn on low-severity notifications only after observation-mode metrics are acceptable.

Pilot rules:

- Start with caregiver notices, not emergency calls.
- Include reason codes and duration in every message.
- Require acknowledgement tracking.
- Keep urgent alerts limited to persistent high-risk cases.
- Review every notification within 24 hours.

Deliverable:

- Reviewed alert log with real issue, helpful check, nuisance alert, and missed concern labels.

### Phase 5: Failure-driven collection

Use detector failures to decide what to record next.

Examples:

- False positive on lying down: collect more controlled lie-down clips.
- False positive on blanket: collect more no-person motion.
- False negative on occluded fall: collect occlusion variants.
- Pose fails in dim light: collect lighting variants or consider IR/depth camera.
- Bathroom overstay nuisance: adjust baseline or add better occupancy sensor.
- Bed-exit-no-return false alert: improve route zones or bed sensor debounce.
- Missed route motion: adjust camera placement or add low-cost motion sensor.

Deliverable:

- Reviewed failure library with reason codes.

### Phase 6: Small classifier only if needed

Train a classifier on derived pose/time features if rule complexity starts getting messy.

Inputs:

- Windowed pose features.
- Rule outputs.
- Quality flags.
- Human labels.

Outputs:

- `fall_probability`
- `fallen_probability`
- `bathroom_overstay_probability`
- `bed_exit_no_return_probability`
- `normal_activity_type`
- `uncertain_quality_flag`

Keep the rule engine as a safety wrapper around the classifier.

## What To Build First

Build these data assets first:

1. Label schema.
2. Manifest format.
3. Home setup checklist.
4. Emergency response plan.
5. Local clip and sensor collection checklist.
6. Pose extraction format.
7. Sensor event format.
8. Offline evaluation table.
9. Rule-based baseline specification.
10. Example monitoring rules config.

Do not build first:

- User accounts.
- Automated emergency calls.
- Mobile app.
- Cloud sync.
- Polished dashboard.
- Raw-video training pipeline.

Do build early:

- Local logging.
- Sensor health checks.
- Reviewable state transitions.
- A caregiver notification pilot after observation-only tuning.

## MVP Decision

The simplest MVP path is:

1. Install bed occupancy, bathroom door/occupancy, and route monitoring.
2. Run observation-only for 7 to 14 nights.
3. Collect local clips only where privacy allows.
4. Extract pose landmarks and sensor events.
5. Label clips, routine transitions, and alert outcomes.
6. Run transparent temporal rules offline.
7. Tune thresholds around her real bathroom and bed-return baselines.
8. Pilot caregiver notifications.
9. Review false positives, missed concerns, and nuisance alerts.
10. Only then decide whether training is worth it.

This path gives a working prototype fastest because the real problem is temporal monitoring plus false-positive management. The data layer should make every detector decision reviewable.

## Source Notes Checked

- OmniFall: https://arxiv.org/abs/2505.19889
- OmniFall Hugging Face dataset card: https://huggingface.co/datasets/simplexsigil2/omnifall
- URFD: https://fenix.univ.rzeszow.pl/~mkepski/ds/uf.html
- Le2i / ImViA: https://search-data.ubfc.fr/imvia/FR-13002091000019-2024-04-09_Fall-Detection-Dataset.html
- UP-Fall: https://sites.google.com/up.edu.mx/har-up/
- MUVIM: https://arxiv.org/abs/2206.12740
- NTU RGB+D: https://arxiv.org/abs/1604.02808
- NTU RGB+D 120: https://arxiv.org/abs/1905.04757
- MediaPipe Pose Landmarker: https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker
- MoveNet: https://www.tensorflow.org/hub/tutorials/movenet
- CDC Facts About Falls: https://www.cdc.gov/falls/data-research/facts-stats/index.html
- CDC STEADI older adult fall prevention resources: https://www.cdc.gov/steadi/index.html
