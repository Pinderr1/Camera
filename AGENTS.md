# AGENTS.md

**Read `CLAUDE.md` first.** It is the authoritative development guide for this repository: product vision, architecture, boundaries, decisions, and commands. This file only repeats the essentials for agents that do not load `CLAUDE.md`.

- Product: **Sentinel**, a local-first multi-camera property security brain (`src/sentinel/`). A drone is a future mobile camera behind an operator-approval gate (`src/sentinel/drone/`).
- Legacy and frozen: `src/senior_safety/` and `webapp/` (former CameraAI senior-safety project). Keep their tests passing and do not extend them.
- Tests: `PYTHONPATH=src python -m unittest discover -s tests`. All tests must pass before a commit.
- Demo: `PYTHONPATH=src python -m sentinel serve` serves simulated cameras and a dashboard at http://127.0.0.1:8080.
- Setup: cameras, zones, masks, map, connections, and categories are edited in the dashboard (Setup tab) through `/api/v1/config/*` and `/api/v1/onboarding/*`. The network troubleshooter (`src/sentinel/netdiag/`) is read-only: never change router, firewall, computer or device settings, and never probe public addresses. See `docs/camera-setup.md`.
- Detection: YOLOX via ONNX Runtime (`detector.model: auto`), EfficientDet fallback; category filters in `categories`. See `docs/environmental-intelligence.md`.
- Never: return or log stream URLs or passwords, claim intent or identity in alert text, merge uncertain cross-camera identities, expose camera streams publicly, commit secrets or media, enable physical drone flight commands, or add AGPL dependencies (for example Ultralytics YOLO).
- Reliability: a camera is reported as monitoring only while changing frames reach a working detector (`health.py`). Workers are supervised (`supervisor.py`), disk space is guarded (`storage_guard.py`), backups and startup recovery live in `backup.py`, and background running in `daemon.py` (`sentinel run`, `sentinel service`). Health events never open incidents. See `docs/reliability.md`.
- Plan of record: `ROADMAP.md`. Architecture: `docs/architecture.md`. API: `docs/api.md`. Drone: `docs/drone-integration.md`.
