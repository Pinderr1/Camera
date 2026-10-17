# cam01 pose audit (2026-10-16)

All nine originals remain under `data/processed/pose/cam01/`, ignored by Git.
The audit never moves, truncates, relabels or rewrites them. It verifies their
SHA-256 hashes again after replay. No branch, commit or history rewrite is needed.

## Findings and provenance

The corpus is **real-camera-derived pilot/test data**, supported by the capture
code, existing pilot annotations, variable landmark measurements, wall-clock
timestamps and repository history. It is not evidence of spontaneous real falls.
The six later sessions have no reviewed activity labels, so their particular
activities and whether they were staged remain unknown. JSON alone cannot prove
the physical capture source independently.

- Nine JSONL files, 26,986,769 bytes; eight nonempty, one zero-byte session.
- 16,450 records, approximately 33.1 minutes across eight session spans.
- MediaPipe Pose Landmarker Full output: up to 33 named landmarks with normalized
  x/y, relative z, visibility, estimated boxes, confidence and quality flags.
  `person_id: 0` is a pose slot, not an identified person. No pixels or audio.
- Filename session dates are July 6 and July 11, 2026. Embedded first-frame
  timestamps fall on July 6/7 and July 12 UTC (consistent with Pacific local
  filenames). The filename marks writer startup, sometimes before the first
  captured frame; its timezone is not stored.
- Filesystem creation dates differ: the first three appear copied on July 11;
  six appear restored/copied October 16 with July 12 modification times. These
  are not reliable capture dates. The private manifest records each separately.
- Commit `4b23826` added the first three as live pilot data. Historical local
  commit `cf9033a` explicitly describes the other six as private real bedroom
  camera landmarks. `0746766` removed tracking while retaining local originals.
  **Earlier Git objects still contain recordings**; ignore rules do not remove
  history. No history was rewritten or pushed during this audit.
- Two historical clip labels describe a no-fall occlusion/mistracking test and a
  normal bathroom-trip pilot. Both say `needs_review`; they are preserved as
  historical annotations, not silently promoted into ground truth.
- Two sessions (7,812 records together) contain no landmarks at all. They are
  useful absence/dropout regression inputs, not verified empty-room negatives.
  Several other sessions have extensive partial-body flags. Older records can
  have landmarks despite `no_person_detected`; recomputed features and recorded
  flags must be distinguished.
- Capture gaps reach about 84 seconds in the earliest session. The Sentinel
  adapter expires a sample after 250 ms instead of inventing movement throughout
  a gap; it does not interpolate poses or reuse cached boxes for empty samples.

## What can use it

Before this audit, the legacy `PoseJsonlWriter` produced this format and
`scripts/sweep_thresholds.py` already included the cam01 directory as its live
corpus through `senior_safety.pose_replay`. That sweep uses `zones.example.json`
and the two historical labels regardless of their pending review; its results
should not be treated as validated accuracy. Sentinel's production camera
workers and Guardian did not consume these files. The new audit connects them
to offline evaluation without altering any live camera, rule or detector.

| Use | Integration and limits |
| --- | --- |
| Pose / human movement regression | Usable now: replay recomputes features from saved landmarks, retaining missing poses. Original video cannot be reconstructed or detection accuracy measured. |
| Fall detection | Existing `senior_safety.pose_replay` runs all eight nonempty sessions. The dedicated night-zone config was derived for one session and is only provisional for others. One later session produces `possible_fall` at about 104.4 s; without reviewed truth this is neither a confirmed fall nor a false positive. |
| Sentinel | `sentinel.bench.pose.PoseRecordingScenario` supplies pose-derived person observations to the existing offline tracker → engine → incident/alert/store replay. Six sessions produced activity, two no-pose sessions stayed quiet; no incidents or notifications. Boxes and full-frame zone are illustrative, not verified feet, identity, calibrated site geometry or object-detector ground truth. No live ingestion or monitoring claim is added. |
| Training | Candidate for future temporal pose training after consent and human labels are verified. Not currently eligible, not auto-trained, and kept out of train/validation splits. No RGB exists here for training YOLOX or SpeciesNet. |
| Guardian | No wildlife or species evidence. Do not import these as animal examples or verified wildlife-negative images. They can exercise shared person tracking, but cannot validate animal approach, human–animal interactions or species classification. |

## Reproduce and review locally

From the original checkout on `main`, in PowerShell:

```powershell
$env:PYTHONPATH='src'
python scripts/audit_pose_recordings.py
```

Each run creates a new ignored `detector_runs/cam01_<run time>/` directory and
refuses an existing output directory. It creates:

- `manifest.jsonl`: per-file hashes, paths, sizes, filename/frame/filesystem dates,
  frame counts, presence, flags, gaps, schema/secret-pattern findings and original
  annotation notes; private, consent unverified, regression-only classification.
- `dataset_manifest.csv`: the existing `data/dataset_manifest.csv` column schema,
  pointing to originals in place; unknown source properties remain blank.
- `labels/clips.csv`: every session registered as `uncertain` / `needs_review`,
  without inventing fall, empty-room, demographic or routine-event labels.
- `legacy_replay/`: per-clip decisions and predictions; no scored ground-truth
  labels, so recall/precision remain undefined.
- `*_sentinel.json`: offline activity/incident results using derived boxes, a
  neutral illustrative zone, disarmed mode and only the replay's capture callback.
- `summary.json`: aggregate results and original-hash preservation check.

Review copies of the labels in a separate ignored directory; never overwrite the
original annotations. After consent, activities and per-session zones have been
reviewed, those labels can be passed to the existing legacy replay CLI with
`--labels`, `--pose`, `--zones` and `--out`. Preserve uncertain labels wherever
there is no independent evidence. Human review should distinguish tracking errors,
occlusion, deliberate movement and actual falls; pose alone may not settle this.

## Privacy and verification

Inspection of all rows found the expected pose fields and no URL, email or
password/token/secret-key patterns. Landmark names include face keypoints but no
face images. Movement, bedroom routines, exact timestamps and private context
still make these sensitive. Consent and redistribution permission are unverified.
All recordings, local manifests, labels, decisions and derived boxes remain
ignored; only reusable code, synthetic test fixtures and this aggregate report
are candidates for Git. Do not force-add originals or derived private data.

The adapter tests use public synthetic fixtures to verify capture gaps, missing
poses, clipping and delivery into Sentinel's real activity engine. Private files
are not required for the test suite. Production detection/tracking thresholds,
Guardian defaults and the frozen legacy application are unchanged.

Validation on October 16: all eight nonempty originals replayed through both
engines, all nine original hashes matched, and schema/secret-pattern checks
reported zero unexpected fields or matches. The repository virtual environment's
full unittest discovery completed successfully (690 tests, 13 skipped); the five
focused pose/catalog tests also passed. Local outputs are under
`detector_runs/cam01_20261016T053519151546/`; the full-suite log is in the earlier
`detector_runs/cam01_20261016T053317785928/unittest.log` run directory.
