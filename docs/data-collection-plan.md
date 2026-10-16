# Data Collection Plan

The detector is not household-ready. It currently has only two labeled local no-fall recordings, both from an invalid low camera position. Unit tests and public staged-fall benchmarks show that the pipeline runs; they do not establish real-world reliability.

## Evidence Target

Before training a classifier or enabling urgent overnight notifications, collect:

- 250 or more reviewed local clips or sensor sequences from the scenario matrix.
- At least 7 observation nights, with 14 preferred.
- At least 3 able-bodied adult participants for staged and hard-negative variation when practical.
- Normal, self-selected routine data from the monitored older adult, with consent.
- A participant/session holdout that is never used to choose thresholds.
- At least 100 reviewed detector failures before training the first pose-feature classifier.

The working queue is [scenario_matrix.csv](../data/scenario_matrix.csv). Counts are targets, not permission to repeat an unsafe action.

## Four Collection Lanes

### 1. Older-adult routine data

Collect normal walking, bed exit/return, bathroom-route timing, sitting, standing, bending, and normal mobility-aid use. Do not ask an older, frail, injured, dizzy, or balance-impaired person to stage a fall, stumble, collapse, crawl, or get onto the floor.

Age is not a fall label. Track it because body proportions, walking speed, posture, clothing, mobility aids, and recovery movement can change detector behavior. Do not infer age from video; record only a broad consented age group.

### 2. Hard negatives

Prioritize actions likely to fool the detector:

- Walking across and around the secured rug.
- Bending, kneeling, sitting, and lying down on the rug.
- Yoga, stretching, floor exercise, dancing, jumping, playful movement, and pet interaction.
- Blankets, pillows, laundry, shadows, visitors, and two people.
- Low light, partial occlusion, and leaving the frame.

Hard negatives should outnumber staged falls. They determine whether caregivers will trust the alerts.

### 3. Safe fall and post-fall simulations

Use able-bodied adults, a thick gym mat, a spotter, a clear area, slow rehearsal, and limited repetitions. Prefer controlled side descents, kneel-to-mat transitions, and chair slides. Do not perform real trips, uncontrolled backward falls, head impacts, falls from height, or testing alone.

Do not label someone as `hurt` from appearance. Record observable post-fall behavior:

- `still`
- `moving_on_floor`
- `recovery_success`
- `recovery_failed_simulation`
- `reaches_for_help`
- `leaves_frame`

Record the help signal separately, such as `manual_help_button`. A safe simulation can represent “I fell and need help”; nobody should be injured to create data.

### 4. Public and controlled-access evidence

Public data supplements local data; it cannot replace the mounted-camera holdout.

- OmniFall, URFD, Le2i, UP-Fall, MUVIM, NTU RGB+D, and SisFall remain the baseline sources already listed in the project plan.
- CIRDO is relevant for carpet falls and help-call scenarios created from field work involving older people. Confirm corpus access and media terms before downloading or using it.
- Simon Fraser University's Injury Prevention and Mobility Laboratory offers controlled research access to real long-term-care fall videos. Treat these as restricted research data; never copy them into the repository or redistribute them.
- Prefer pose-only derived records when source terms permit derivation, and keep source identity plus license/access terms in `dataset_manifest.csv`.

Sources:

- CIRDO corpus: https://aclanthology.org/L16-1221/
- SFU Injury Prevention and Mobility Laboratory data sharing: https://www.sfu.ca/ipml/research/data-sharing.html

## Split Rules

- Split by participant first, then by recording session. Do not scatter nearly identical clips from one performance across train and test.
- Keep repeated takes of one action in one split.
- Reserve the final mounted-bedroom scenarios as `holdout_home`.
- Do not tune thresholds on `holdout_home`.
- Keep public-dataset scores separate from local household scores.

## Collection Workflow

1. Mount and calibrate the camera in its final dresser position.
2. Copy example configs to ignored household `*.local.json` files and validate them.
3. Record one short scenario at a time without audio.
4. Save raw media privately and extract pose JSONL locally.
5. Add the clip to `dataset_manifest.csv` and `labels/clips.csv`.
6. Label meaningful event timestamps in `labels/events.csv`.
7. Replay the detector and record the reviewed outcome in `review_log.csv`.
8. Update the scenario row status and collected count outside raw filenames.
9. Review false positives and false negatives before collecting more repetitions of an already-covered action.

Stop immediately for pain, dizziness, fatigue, uncertainty, unsafe flooring, a shifting rug, a moving chair, missing spotter, camera privacy problems, or withdrawn consent.
