# Optional accessories and FLAME Mode

FLAME is the existing Sentinel drone's agricultural **monitoring** configuration.
It shares Sentinel's device registry, drone manager, operator approval, database,
notification channels, dashboard and AI Guide. No new aircraft or separate app
is required. There are no ignition, fuel-delivery or release controls.

## What is implemented

- Optional, removable accessories with compatibility records, estimated payload
  effects, commissioning, inspections, maintenance and simulated health faults.
- Ground-only switching between security and agricultural monitoring. Switching
  checks the aircraft, open requests, operations, accessory identity, attachment
  and inspection. Agricultural configuration refuses security inspection
  requests; security configuration refuses agricultural watch requests. Fixed
  camera monitoring continues in either configuration.
- Fields drawn on the map or recorded from the drone's current position,
  boundaries, watch strips, fireguards and sensitive places. Coverage geometry
  assumes flat ground and is approximate.
- Saved operator checklists, crew and equipment records, authorizations and
  qualifications, weather observations against operator-entered limits, notes,
  thermal observations, boundary heat and hotspot maps, and post-operation reports.
- Notifications to configured human recipients for observed heat, equipment
  faults, lost thermal watch and drone safety changes. Emergency messages are
  operator-entered; Sentinel does not contact emergency services automatically.
- JSON/printable HTML reports with a fingerprint, recorded evidence and explicit
  coverage/uncertainty. Closing an operation records the person's post-operation
  assessment; it never establishes that a fire is out.

## Hardware and simulation limits

The built-in farm example (`config/sentinel.farm.example.json`) has **simulated**
accessory reports, fire spread and thermal data. Compatibility results are
published specifications or estimates; they are not measured validation.
Physical accessory profiles remain registration/planning only: no live accessory
transport or real thermal-camera integration has been verified. No real aircraft,
fire, latch, temperature sensor or ignition hardware was tested.

SITL runs real ArduCopter firmware, while aerial images, fire and thermal signals
are simulated. A thermal observation describes only the camera footprint at that
time, with positional uncertainty. It cannot establish whole-property coverage.
Permits, crew qualifications, checklist items and weather entered by a person are
records, not independently verified authorization or advice to conduct a burn.

## Persistence and recovery

Fields, configuration selections and settings live in `<storage>/flame.json`.
Accessory/crew records, operations and observations use the existing SQLite
database. Sentinel backups include `flame.json` with configuration and include
the operation tables in the database. Removing a drone removes its registered
accessories; historical records remain.

Malformed FLAME settings are moved to `recovery/flame.<id>.damaged.json` with a
SHA-256 sidecar before defaults can be saved, and a degraded health component
reports the problem. Unreadable files raise an error rather than being silently
overwritten. Startup retains open operations and records an interrupted watch.

Drone readiness and FLAME hooks read the service's applied configuration
snapshot, without acquiring the site-editor lock while holding fleet/manager
locks. Regression tests cover concurrent API reads, site edits, fleet rebuilds,
hook execution, and accessory edit/removal without a fleet rebuild.

## Dashboard, API and Guide

Use Install to register an accessory on its drone. On the FLAME tab, record
commissioning and inspection, select the drone configuration, map a field and
create an operation. A watch flight still requires the existing operator
approval and every applicable drone safety check. Configuration can return to
security when its switching checks allow it.

All FLAME endpoints use Sentinel's existing authentication under `/api/v1/flame`.
See [API reference](api.md). The AI Guide exposes read tools for the overview,
sensor health, checklist, hotspots and reports. Field and configuration changes
are proposals requiring confirmation through the existing backend. The same
registered tools are available to the optional DeepSeek and Claude providers;
this is not evidence of a live external-provider or hardware trial.

## Hardware compatibility

Payload margin, endurance, IGNIS III Mini findings, regulation notes and the engineer's validation list are in [FLAME hardware evaluation](flame-hardware-evaluation.md).

## Validation

`tests/test_sentinel_flame.py` covers compatibility, records, switching, fields,
checklists, observations, notifications, persistence, reports, API and Guide.
`tests/test_sentinel_flame_ui.py` exercises real browser commissioning, inspection,
configuration switching, accessory faults, field drawing and return to Live.
`tests/test_sentinel_lock_order.py` covers the configuration lock boundary.
`tests/test_sentinel_flame_sitl.py` is a separate opt-in firmware integration
scenario; a default skipped result never counts as a SITL pass. It passed on
2026-10-15 against ArduCopter 4.7.2 with `config/drone/sentinel-thermal-sitl.parm`
and the simulated SF45/B (`SENTINEL_SITL=flame`, about 1 minute).

For current firmware results and outstanding limitations, see
[drone reliability](drone-reliability.md).
