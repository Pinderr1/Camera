# AI Test Lab

The Lab tab plays recorded footage through Sentinel's real AI and shows every stage on the picture. Nothing in it is scripted: the boxes, tracks, Guardian levels, species decisions, events and alerts come from the same code that runs on live cameras. Labels, where a clip has them, are only compared with the output afterwards.

## Use it

1. `python -m sentinel serve` and open http://127.0.0.1:8080, then the **Lab** tab.
2. Pick a clip on the left. Filter by split (independent test, validation, your footage) or subject.
3. Choose a model, rate (5 fps is the camera default), arm mode and length, then press **Analyse and play**. Frames play as soon as they are analysed. On the i5-1135G7 / Iris Xe laptop that is about 4 analysed frames a second with YOLOX-s, slower with SpeciesNet or MegaDetector.
4. Use Play/Pause (space), frame steps (arrow keys), speed 0.25x-4x, the position slider, and **error ⏮ / ⏭** to jump between frames where the output disagrees with the labels.
5. Layers: zones, detections (confidence; dashed = weak box that only extends a track), tracks (#id), Guardian (trail, level, movement, species), labels (green dashed = labelled person, red = missed), errors (red = box with no labelled person, red frame border = subject missed or empty scene with a box).
6. **Compare with** shows another run of the same clip next to this one, synced by time, plus a comparison table. Run the clip again with another model to get one.
7. The panels list the frame's detections and Guardian tracks, the events (click to jump; reasons, rule, confidence), the simulated alerts the alert policy would have pushed, and the run summary with an error/event strip along the clip.

Your own footage: copy video files into `<storage>/lab/videos/` (shown on the tab; `data/sentinel/lab/videos/` by default). Optional labels go next to the file as `NAME.labels.json`: `{"present_s": [[0, 12]], "absent_s": [[15, 30]], "subjects": ["person"]}`. Your footage stays on this computer and is never used for training unless you turn on personalization (docs/training.md).

## Marking errors

In **This frame**: *Correct*, *Wrong* (false positive) or *Wrong label* on each detection, and **Draw a missed object** (drag a box, pick the label). In the events list: *Right* or *False alarm*. Marks are saved with the run (`corrections.jsonl`), can be undone, and feed `python -m sentinel.training collect`. They never change Sentinel's behaviour by themselves.

## What differs from a live camera (on purpose)

- The clip is decoded on a virtual clock at the chosen rate, so a slow computer gives the same result, only later.
- The detector runs on every analysed frame (no motion gate), so you see what the model sees.
- The species worker is waited for after each frame (up to 8 s), so a replay is repeatable.
- Alerts are simulated: the alert policy decides exactly as live, and the notification is recorded, never sent.
- Household-member recognition is never on in the lab.

Zones and rules: MOT16/CAVIAR clips use their zones and rules from `config/bench/site.json`. Every other clip gets two zones (*Property*, the top 70 % of the picture, and *Near the camera*, the bottom 30 %, which is also Guardian's protected zone) and four rules: person enters near the camera, person moving toward it (`zone_approach`), person lingering 20 s, vehicle stopped 30 s.

## Accuracy shown in the lab

Only against labels, and always named: MOT16/CAVIAR clips are scored per person box (IoU 0.5, as `sentinel.bench`); clips with presence ranges are scored per frame (is there a box of the labelled subject). The lab's numbers cover the analysed part of one clip at the chosen rate; they are not a benchmark. Use `sentinel.bench` for recorded measurements, and never quote lab numbers for unlabelled clips.

## Files and API

Runs live in `<storage>/lab/runs/<id>/` (`frames/*.jpg`, `frames.jsonl`, `run.json`, `corrections.jsonl`, `store/`). Code: `src/sentinel/lab/` (`catalog.py` clip list, `profiles.py` model choices, `session.py` runner, `routes.py` API). API: `/api/v1/lab/*` (docs/api.md). Delete a run from the API (`DELETE /api/v1/lab/runs/{id}`) or remove its folder.
