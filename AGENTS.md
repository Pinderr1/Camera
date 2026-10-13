# AGENTS.md

**Read `CLAUDE.md` first.** It is the authoritative development guide for this repository: product vision, architecture, boundaries, decisions, and commands. This file only repeats the essentials for agents that do not load `CLAUDE.md`.

- Product: **Sentinel**, a local-first multi-camera property security brain (`src/sentinel/`). A drone is a future mobile camera behind an operator-approval gate (`src/sentinel/drone/`).
- Legacy and frozen: `src/senior_safety/` and `webapp/` (former CameraAI senior-safety project). Keep their tests passing and do not extend them.
- Tests: `PYTHONPATH=src python -m unittest discover -s tests`. All tests must pass before a commit.
- Demo: `PYTHONPATH=src python -m sentinel serve` serves simulated cameras and a dashboard at http://127.0.0.1:8080.
- Never: claim intent or identity in alert text, merge uncertain cross-camera identities, expose camera streams publicly, commit secrets or media, enable physical drone flight commands, or add AGPL dependencies (for example Ultralytics YOLO).
- Plan of record: `ROADMAP.md`. Architecture: `docs/architecture.md`. API: `docs/api.md`. Drone: `docs/drone-integration.md`.
