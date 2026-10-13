# Archive: Senior Night Safety / BedWatch (CameraAI, July 2026)

These documents describe the project before the October 2026 pivot to **Sentinel**. They are kept for technical history and because the code they describe still exists:

- `src/senior_safety/`: the nighttime state machine, pose pipeline, URFD benchmark, and MQTT bridge. Sentinel reuses its polygon geometry, config-validation helpers, and alert-lifecycle ideas. It is frozen, and its tests still run.
- `webapp/`: BedWatch, the phone-to-phone bed-exit alert. It is frozen and gets no further development.

Nothing in this folder is current guidance. For current direction, see `CLAUDE.md` and `ROADMAP.md` at the repo root.

| File | What it was |
| --- | --- |
| `plan.md` | The former root `CLAUDE.md`: product, data, and detection plan for senior night monitoring |
| `completion-roadmap.md` | The former `PROJECT_COMPLETION_ROADMAP.md` |
| `architecture.md`, `event-schema.md` | Runtime contract for the senior-safety state machine |
| `caregiver-runbook.md`, `emergency-response-plan.md`, `home-setup-checklist.md`, `observation-trial.md` | Household operating docs |

Lessons that carried over into Sentinel:

- Persistence and hysteresis before any alert. A single odd frame is never enough.
- Reason codes and durations on every decision, so each alert can be reviewed.
- Measure alert fatigue as seriously as missed detections.
- Raw media stays local and out of git. Prefer derived data.
- Ultralytics YOLO is AGPL, so it was avoided (see `sources.md`).
