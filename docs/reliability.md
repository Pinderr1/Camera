# Running Sentinel 24/7: reliability

Sentinel is meant to run for months without anyone watching it. This document covers how it notices that it has stopped protecting the property, how it recovers by itself where that is safe, and how it tells a person when it cannot.

The rule behind every design choice: **Sentinel never reports a camera as monitoring unless new, changing frames are reaching the detector and the detector is working.** Everything else is shown as a specific "not monitoring" state, recorded as an outage, and pushed to people when it lasts.

## Camera health states

Every second, `HealthMonitor` (`health.py`) turns each camera's raw signals into one state:

| State | Monitoring? | Meaning | Detected from |
| --- | --- | --- | --- |
| `monitoring` | yes | Frames arrive, change, and are analysed | All checks below pass |
| `starting` | no | Worker (re)started, waiting for the first frame | No frame since `worker_started_ms`, within `startup_grace_s` |
| `offline` | no | No video | No processed frame for `frame_timeout_s` (the source's error is the reason, for example `auth_failed`, `unreachable`) |
| `frozen` | no | Frames arrive but the picture is identical | Pixel samples (not averages, so sensor noise survives) unchanged for `frozen_after_s` |
| `ai_failed` | no | Video arrives but the detector cannot run | Repeated inference errors with no working fallback |
| `stalled` / `restarting` | no | Processing thread crashed or hung; restart scheduled | Supervisor (below) |
| `failed` | no | Restart budget used up; automatic restarts paused | Supervisor circuit breaker |
| `not_running` | no | Cameras are disabled in this session (`serve --no-cameras`) | No supervisor |

A monitoring camera can carry **warnings** that do not stop protection: `cpu_fallback` (GPU inference failed, running on CPU), `slow_processing` (below `slow_ratio` of the target fps for `slow_after_s`), and `recent_restarts`.

Site **coverage** is `full`, `degraded`, `none`, or `not_running`. A failed system component (storage, database, process) makes coverage `degraded` even when every camera is live.

Where it shows:
- **Live tab:** a tile that is not monitoring keeps its last frame under a "NOT MONITORING: <state>" overlay with the reason and time. A frozen picture is never presented as live video.
- **Header:** the coverage badge (for example "Coverage degraded 2/3").
- **System tab:** per-camera state, processed fps, detection and capture-to-decision p95 latency, skipped frames, reconnects, restarts, detector provider, the reason, and a Restart button. Also CPU, memory, GPU (NVIDIA via `nvidia-smi`), disk, database, backups, and the outage history.
- **API:** `GET /api/v1/system/health`, `GET /api/v1/system/outages`, `coverage` in `GET /api/v1/state`, an SSE `health` topic, and `X-Sentinel-Monitoring` / `X-Frame-Age-Ms` headers on snapshots.

## Outages and notifications

- An **outage** opens when a camera leaves `monitoring` for anything but `starting`, and closes when it returns. State changes inside one outage (offline → restarting → failed) extend it. System components (storage, database, notifications, process) and Sentinel's own downtime are outages too. All are stored in the `outages` table and listed on the System tab.
- Timeline events (`camera_offline`, `camera_frozen`, `camera_ai_failed`, `camera_stalled`, `camera_restarting`, `camera_failed`, `camera_online`, `system_health`) are written for every transition. **They never open security incidents**, so coverage problems cannot trigger or suppress security alerts.
- A **notification** is sent once per outage after `notify_after_s` (120 s by default; short drop-outs are not pushed). Outages that become due together are grouped ("2 cameras not monitoring"). A recovery message follows when a notified outage ends. A camera that exhausts its restart budget after its outage was reported sends one more, high-priority message. Health messages have their own rate limit (`max_notifications_per_hour`); held-back messages are counted in the next one.
- Health notifications ignore the arm mode: a blind camera matters whether the house is armed or not. They still respect the global "notifications enabled" setting and end with the standard disclaimer.
- **Sentinel's own downtime:** `runstate.json` holds a heartbeat (every 10 s) and whether the last stop was clean. On start, a clean stop is recorded as a `service_stopped` outage (not pushed); an unclean one (crash, power cut, forced kill) as `service_crash`, pushed as "Sentinel was not monitoring for N min".
- **Whole-computer failure** (power loss, dead disk) cannot be reported by Sentinel itself. Configure `health.heartbeat.url` with a hosted dead-man's switch (healthchecks.io style): Sentinel pings it every `interval_s` while coverage is full (and `fail_url`, if set, while degraded). The service alerts people when pings stop. Pings carry no data. This is off by default because it contacts an outside service.

## Automatic recovery

### Camera workers (`supervisor.py`, `pipeline.py`)
- Each `CameraWorker` advances a `heartbeat` every loop iteration, sets `phase = "running"` once the detector and source are up, and records `failure` when it ends on an exception (including detector or source start-up errors).
- `WorkerSupervisor.check()` runs every second:
  - **Crashed** (thread ended with a failure): restart after `backoff_initial_s`, doubling to `backoff_max_s`.
  - **Stalled** (no heartbeat for `stall_after_s`, or not running after `startup_timeout_s`, which allows for a first model download): Python threads cannot be killed, so the worker is told to stop and **abandoned**; a fresh worker takes over. A stopped worker never publishes again, so a hung call that returns late cannot overwrite its replacement's state.
  - **Restart budget:** at most `max_restarts` per `window_s`. Then the camera is `failed` and gets one trial restart per `cooldown_s` (a circuit breaker), never a tight loop. An operator Restart resets the budget.
  - **Stuck threads:** if more than `max_stuck_threads` abandoned threads are still alive, the supervisor reports a fatal condition. Under `sentinel run` (or `serve --exit-on-fatal`) the process exits with code 3 and is restarted fresh.
- **Inference failures:** after `failures_before_fallback` consecutive errors, a GPU detector is replaced by the CPU profile (`cpu_fallback: true`, the default); the camera stays `monitoring` with a `cpu_fallback` warning. Without a working fallback the camera is `ai_failed`; the detector is reloaded every `retry_s` and the camera only counts as monitoring again after an inference on a real frame succeeds.
- Sources already reconnect with exponential backoff (1 s to 30 s) and classify failures; that is unchanged.

### The process (`daemon.py`)
`python -m sentinel run` starts a tiny supervisor process that runs `serve` as a child:
- A crash (non-zero exit) restarts the child with backoff (5 s doubling to 300 s), at most 10 times per hour, then one attempt every 30 minutes (`health.process`).
- Exit 0 (clean stop) ends supervision. Exit 78 (configuration error, refusing an unsafe bind, another instance already running) is not retried: restarting cannot fix it.
- Only one instance may use a storage folder: a second one refuses to start while the first one's heartbeat is fresh.

### Graceful shutdown
`python -m sentinel service stop` writes `<storage>/control/stop`. The server notices within a second, stops camera workers (finishing clip files), closes open outages, flushes the database, writes a clean `runstate.json`, and exits 0; the supervisor then exits too. Ctrl+C and SIGTERM do the same. If the server does not stop within 45 s, the supervisor terminates it (recorded as an unclean stop).

## Windows background start

```powershell
python -m sentinel service install --config config/site.local.json --env-file C:\ProgramData\Sentinel\sentinel.env
python -m sentinel service start
python -m sentinel service status
python -m sentinel service stop
python -m sentinel service uninstall
```

- This registers a **Task Scheduler** task named `Sentinel` that runs `scripts/sentinel_launcher.py run` with `pythonw` (no console window). By default it starts at boot, before anyone logs in (`--trigger startup`; schtasks prompts for your Windows password and stores it in Task Scheduler, never on a command line). Use `--trigger logon` to start at your logon without storing a password. Creating a boot task usually needs an elevated prompt.
- It runs as **your account**, not LocalSystem, because camera passwords are DPAPI-encrypted to the account that saved them.
- Task settings: no time limit, runs on battery, one instance, restart if launching fails. Crash restarts are handled by `sentinel run`.
- `--env-file` passes secrets such as `SENTINEL_API_TOKEN` and `SENTINEL_NTFY_TOPIC` (one `KEY=VALUE` per line). Keep it outside the repository and readable only by your account.
- `--dry-run` prints the task XML and the schtasks command without installing.
- **Linux:** `python -m sentinel service unit --config ... --env-file /etc/sentinel.env` prints a systemd unit (`Restart=on-failure`, start-limit burst, no restart on exit 78, `--exit-on-fatal`).

## Storage protection (`storage_guard.py`)

Every `check_interval_s` (60 s):
1. If `max_media_gb` is set and media exceeds it, delete the oldest unprotected media until it is under 90% of the cap.
2. If free space is below `max(min_free_mb, min_free_pct% of the disk)`, delete the oldest unprotected clips and snapshots until there is `resume_margin`× that free.
3. If that is not enough, **pause recording**: clips and snapshots stop, while detection, events, and alerts continue. The storage component turns `failed` and people are notified.
4. Recording resumes once free space exceeds the threshold × `resume_margin` (hysteresis, so it does not flap).

**Protected recordings** are never deleted for space: media of open incidents, incidents marked "Keep these recordings" (`POST /incidents/{id}/protect`), and incidents resolved as `real_concern`. They follow `privacy.retention_days.protected` (default 90 days) instead of the normal media retention, so protection is long but not indefinite.

Write failures are contained too. A clip or snapshot that cannot be written (disk full, permissions) is reported as a degraded storage component; the event and its alert still go out. Database writes that fail (for example "database or disk is full") are counted, the database component turns `degraded`, and alerts are still published and pushed.

**Logs** rotate by size: `<storage>/logs/sentinel.log` (server) and `supervisor.log` (process supervisor), `logging.max_mb` (10) × `logging.backups` (5) each.

## Backups and recovery (`backup.py`)

A backup is a zip in `<storage>/backups/` with a `manifest.json` (SHA-256 per file) and:
- `site.json`, the editable configuration, with `${ENV}` placeholders unresolved;
- `settings.json` (arm mode, sensitivity, notifications);
- `sentinel.db`, a consistent online copy made with SQLite's backup API while cameras keep writing;
- the property-map image;
- `credentials.json` only with `storage.backups.include_credentials` or `--include-credentials`. It stays DPAPI-encrypted, so it restores only for the same Windows account.

Media is not backed up: it is large and short-lived by design.

- **Automatic:** daily (`storage.backups.interval_h`), keeping the newest `keep` (7) automatic backups. Skipped while recording is paused for disk space.
- **Manual:** System tab → "Back up now", `POST /api/v1/system/backups`, or `python -m sentinel backup create`.
- **Verify:** checks the zip, every checksum, SQLite `integrity_check`, and config validation (`backup verify NAME`, or the Verify button).
- **Restore while running** (dashboard or `POST /system/backups/{name}/restore`): a `pre-restore` backup is written first. The configuration is applied immediately through the normal validated save, keeping this machine's storage location. Settings are applied immediately. The database is staged in `restore_pending/` and swapped in at the next start, because camera threads are writing to it.
- **Restore offline:** `python -m sentinel backup restore NAME --parts config,settings,database,assets` (refuses while Sentinel is running).
- Backups never leave the computer through the API: the config inside contains stream addresses, which the API treats as write-only.

**Startup recovery:**
- **Damaged database:** on every start Sentinel runs `PRAGMA quick_check`. A file that fails is moved to `<storage>/recovery/` (kept for inspection), and the newest backup that verifies is restored. Without one, a new, empty database is started. Either way an event, an audit entry, and a notification say what happened and what was lost.
- **Damaged `site.json`** (for example a torn write): it is moved to `recovery/`, and the newest valid copy from `config_history/` is used, or the start config if none is valid. A notification asks the person to check the Setup tab.

## Configuration reference

```json
"health": {
  "frame_timeout_s": 10, "frozen_after_s": 60, "startup_grace_s": 30, "notify_after_s": 120,
  "notifications": true, "notify_recovery": true, "max_notifications_per_hour": 6,
  "slow_ratio": 0.5, "slow_after_s": 60,
  "supervisor": {"stall_after_s": 30, "startup_timeout_s": 300, "backoff_initial_s": 5, "backoff_max_s": 300,
                 "max_restarts": 5, "window_s": 3600, "cooldown_s": 1800, "max_stuck_threads": 3},
  "process": {"backoff_initial_s": 5, "backoff_max_s": 300, "max_restarts": 10, "window_s": 3600, "cooldown_s": 1800},
  "heartbeat": {"url": "${SENTINEL_HEARTBEAT_URL}", "fail_url": "", "interval_s": 300}
},
"storage": {
  "dir": "data/sentinel", "min_free_mb": 2048, "min_free_pct": 5, "resume_margin": 1.5, "max_media_gb": null, "check_interval_s": 60,
  "backups": {"enabled": true, "interval_h": 24, "keep": 7, "include_credentials": false}
},
"logging": {"level": "INFO", "max_mb": 10, "backups": 5},
"privacy": {"retention_days": {"events": 30, "media": 7, "protected": 90}},
"detector": {"cpu_fallback": true, "failures_before_fallback": 3, "retry_s": 30}
```

The old `health.camera_offline_after_s` key is still read as `notify_after_s`.

## Testing failures

Simulated cameras accept scheduled faults, `source.faults: [{"type", "at_s", "duration_s"}]`, on a per-camera clock that survives worker restarts:

| Fault | Simulates |
| --- | --- |
| `disconnect` | The camera or network drops: no frames, `unreachable` |
| `freeze` | Frames keep arriving with an identical picture |
| `crash` | The capture call raises (worker crash) |
| `stall` | The capture call hangs and ignores stop requests (stuck decoder or driver) |
| `detector_error` | Inference raises on every frame |

Validation only accepts `faults` on simulated cameras.

- **Smoke test** (about 35 s, no hardware): `python scripts/reliability_smoke.py`. Five cameras fail in different ways, the disk fills and frees, the service restarts, and the database is damaged and restored. It prints a timeline and PASS/FAIL per check.
- **Automated tests:**
  - `tests/test_sentinel_health.py`: health states, outages, notification grouping and rate limits, the supervisor on a fake clock, the storage guard, run state, install helpers, and config validation.
  - `tests/test_sentinel_reliability.py`: every failure mode against a real service with simulated cameras, plus the system API, backups, and recovery.
  - `tests/test_sentinel_service_process.py`: the process supervisor with real child processes, including killing a live server and checking it comes back and stops cleanly.
  - `tests/test_sentinel_ui.py`: the Live overlay and the System tab in Edge.

## Limitations

- **Frozen detection** needs pixel-identical frames. A recorder that freezes the picture but keeps updating an on-screen clock, or that shows its own "No video" screen, still looks live. Such a camera keeps "monitoring" with nothing to detect. Not yet observed on real hardware.
- **GPU telemetry** is NVIDIA-only (`nvidia-smi`). DirectML exposes no utilisation counters; on other GPUs, inference health comes from detector latency and errors.
- **A stuck thread cannot be killed** inside the process. Recovery is a replacement worker, then a process restart once too many threads are stuck. A hang inside the shared GPU session lock would stall every camera until that restart.
- The Windows background start is a Task Scheduler task, not a Service Control Manager service. It starts at boot and is supervised, but it does not appear in `services.msc`. `service install` has not been run on this development machine; only the generated task XML and commands are tested.
- Disk-full behaviour is tested with a simulated disk and injected write errors, not by filling a real volume.
- Without the external heartbeat, nobody is told when the whole computer is off. Sentinel can only report the gap after it comes back.
