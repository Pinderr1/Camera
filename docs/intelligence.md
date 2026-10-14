# Security intelligence: rules, explanations, activity insights, investigation

This document covers how Sentinel decides what matters and explains it:
- the rule language and the dashboard rule builder;
- testing rules before they are switched on;
- incident explanations and the incident timeline;
- statistics about ordinary property activity;
- investigation tools, feedback, and evaluation reports.

Everything runs locally, uses only the standard library in its logic modules, and keeps the product boundaries in `CLAUDE.md`. Sentinel describes behaviour; it never asserts intent, criminality, or identity. A statistically unusual event is never treated as a threat.

## Rules

Rules live in `site.json` (`rules[]`) and are evaluated by `engine.TemporalEventEngine` against site tracks: camera tracks linked across cameras only when timing, topology and (optionally) appearance support it.

| Type | Fires when |
| --- | --- |
| `zone_entry` | A subject enters a matching zone (once per visit) |
| `dwell` | A subject stays in a zone for `min_dwell_s` (`max_roam` limits wandering; `require_arrival` ignores things already parked) |
| `repeated_presence` | `count` separate visits to a zone within `window_s` (by the same or different people) |
| `sequence` | One linked subject visits `zones` in order within `within_s` |
| `multi_camera` | One linked subject is seen by `min_cameras` cameras within `window_s` |
| `combination` | At least `min_matches` of the listed `rules` match within `within_s`: for one linked subject (`scope: "subject"`) or anywhere on the property (`scope: "site"`, explicitly noted as possibly different people) |

Conditions and options on every rule:
- **Who:** `classes` (`person`, `vehicle`, `bicycle`, `animal`, `bag`, `unknown_motion`).
- **Where:** `zones` (ids) or `zone_types`; neither means any on-property zone.
- **Modes:** `arm_modes` from `home`, `away`, `night`, `vacation`; none means every armed mode. `disarmed` never notifies.
- **Time:** `active_hours` `{start, end}` (may wrap midnight) and `active_days` (`mon`…`sun`; an overnight window belongs to the day it starts). A zone entry is judged at the moment of entry, so arming later never fires for someone already inside.
- **Observations:** `min_observations` (detections of the subject before the rule may fire; entries wait up to 15 s for them) and `min_confidence`.
- **Alerts:** `severity`, `notify` (`auto` follows the mode's level; `always` notifies in any armed mode; `never` records without notifying), and `cooldown_s`. Matches during a cooldown are still recorded, as `info` with reason `rule_cooldown`, so they stay visible but never alert.
- **Labels:** `name` and `description` for people.

Mode notification levels (`alerts.notify_min_severity`): disarmed none, home high, away medium, night medium, vacation low.

### Rule builder (Setup → Rules)
- **Rule list:** every rule in plain language (what, when, what then), with an on/off switch, Edit and Copy.
- **New rule:** starts from a template (residential and small-business: restricted area, lingering at doors or gates, vehicle arriving while away, long-parked vehicle, repeated door visits, gate-then-shed after midnight, walking around the house, night yard activity, shop entrance after hours, weekend loading bay, storefront lingering, two concerns within ten minutes) or from a blank rule.
- **Editing:** the form adapts to the rule type. Sequence steps are ordered zone pickers; combinations pick other rules. Every change is validated server-side (`POST /api/v1/rules/check`, the same `validate_config` as saving) and re-described in plain language.
- **Testing first:** "Save and turn on" stays disabled until the current version has been tested; any edit makes the test stale. "Save, keep off" is always possible for a valid rule. Turning on an untested rule from the list asks for confirmation.
- **Saving:** goes through `SiteConfigManager.save_rule`: whole-config validation, atomic write with history, an audit entry, then a live update of the running engine. A rule used by a combination cannot be deleted.

### Testing a rule (`rule_lab.py`)
Every test runs the candidate (plus the live rules) through the real tracker, correlator, engine, incident manager and alert policy, in virtual time, on a throwaway database. The live configuration is never touched.
- **Simulated walk:** a subject stands in chosen zones for chosen times and moves on. It works on any site. Cross-camera steps link only if a camera connection allows that travel time; otherwise the result says the subject was not linked.
- **Built-in scenario:** `config/scenarios/*.json` (example-site camera ids).
- **Recorded activity:** the last N days of stored zone visits are rebuilt as subjects standing inside each zone for the recorded times. Zones and times are real, positions are not stored, so roaming limits and appearance links are estimated. The result says so.

The result lists matches, the incidents and notifications that would follow, other rules that matched, and notes.

## Explanations (`explain.py`)

`GET /api/v1/incidents/{id}/explanation` builds a structured account from stored events and the configuration:
1. **Objects detected:** category, specific kind, detector confidence range, number of detections.
2. **Cameras:** observed directly, or known only through a cross-camera link (`basis: inferred`).
3. **Zones entered,** with times.
4. **Sequence:** each event is marked **observed** (a camera saw it) or **inferred**. Inferred covers events that rely on linking sightings across cameras, repeated visits, and site-wide combinations. The subject's earlier observations from just before the incident are included.
5. **Rules that matched:** plain-language description and the matched measurements (dwell, elapsed time, visit count), plus any matches recorded only as information (cooldown, category without alerts).
6. **Confidence and uncertainty:** detection confidence and each cross-camera link with its score, explicitly "not a recognition of who the person is". Refused ambiguous links and multiple subjects are also listed.
7. **Why this severity:** the highest rule severity, any one-level raise for several kinds of behaviour, whether and at what level a notification was sent, and the mode levels.
8. **Supporting recordings:** clips (with start times) and snapshots.

The dashboard incident view shows this with a visual timeline: one lane per camera, solid markers for observations, hollow dashed markers for inferences, dashed connectors for cross-camera links, and recording spans. Clicking a marker seeks the recording to that moment; playback moves a cursor along the timeline. Notifications carry a one-line reason ("Why: rule 'Side gate then shed after midnight' matched.") before the standard disclaimer.

## Activity insights (`insights.py`)

The engine logs every **completed zone visit** to the SQLite `visits` table: category, specific kind, camera, zone, start, end, dwell, and whether the subject arrived. It logs a **trip** to `trips` when a subject leaves the property: cameras, zone route, and how confidently its sightings were linked. No images, positions, appearance data or identities are stored. Retention is `activity.retention_days` (90).

The Insights tab (`GET /api/v1/insights?days=`) shows:
- activity by hour per category;
- vehicle arrivals and departures, with typical hours;
- entrances used and common routes;
- delivery-like visits: short daytime entrance visits, labelled as a pattern, not a confirmed delivery;
- animals by kind and zone;
- passers-by seen only on the street or sidewalk;
- environmental motion by camera;
- typical and long stays per zone;
- changes over time (the last 7 days against the weeks before).

**Evidence before patterns:** nothing is called typical until there is activity on `min_days` (7) days and `min_samples` (10) observations.

**Unusual observations** are information only and never alerts. Each is explained with its numbers:
- a time of day rarely seen: at most `rarity` (5%) of the observed days had this category in this zone within ±1 hour;
- a stay far longer than the 95th percentile;
- a route never seen before.

**Suggestions** need the owner's approval. A rule suggestion (for example, "vehicles arriving outside their usual hours" while away) opens prefilled in the rule builder, where it still has to be tested and saved. Review suggestions point at noisy rules, delivery-heavy entrances, or repeated environmental motion. Nothing changes automatically.

## Investigation and feedback

- **Search** (`GET /api/v1/search/incidents`): date range, camera, zone, object category, minimum severity, review outcome (including not reviewed), rule, and text.
- **Review:** Acknowledge; outcome *Expected activity*, *False alarm*, *Genuine concern* or *Uncertain* (stored as `expected_activity`, `false_alarm`, `real_concern`, `unclear`); append-only notes; "Keep these recordings". Every action is audited.
- **Missed events** (`POST /api/v1/missed`): the owner reports activity Sentinel did not report. The report stores what Sentinel did record on that camera within ±5 minutes, and the camera's health state at the time of reporting.
- **Alert performance** (Insights → Alert performance, `GET /api/v1/reports/performance?days=`): per rule and per camera. It shows matches, matches recorded only as information, incidents, notifications, outcomes, and the **nuisance rate**: notifications later marked expected activity or false alarm, out of the reviewed notifications. Missed-event counts are listed per camera.
- Feedback is used for evaluation only. It never retrains a model or changes a rule by itself.

### Evidence integrity and export
- Each finished clip and snapshot is fingerprinted (SHA-256) in the `evidence` table. The first hash wins.
- `GET /api/v1/incidents/{id}/export[?include_media=true]` returns a zip:
  - `report.html` (readable);
  - `report.json` (incident, explanation, notes, outcome, events);
  - optionally `evidence/` with the recordings;
  - `manifest.json` with a SHA-256 for every file.
- Each piece of evidence is reported as *intact*, *CHANGED since it was recorded*, *missing (deleted by retention…)*, or *not fingerprinted*.
- Exports never contain camera addresses, credentials, map coordinates or other incidents. Each export is audited with the actor.

## Configuration

```json
"activity": {"enabled": true, "retention_days": 90, "window_days": 30, "min_days": 7, "min_samples": 10, "rarity": 0.05, "recent_hours": 48}
```

## Tests

- `tests/test_sentinel_intelligence.py`:
  - the rule language (days, overnight windows, cooldowns, notification preferences, minimum observations, arming after entry, combinations);
  - the "side gate after midnight, then the shed" scenario (`config/scenarios/gate_then_shed.json`): one coherent incident and one notification at night, nothing from that rule by day;
  - explanations (observed versus inferred, no intent words);
  - the rule tester (walk, unlinkable walk, invalid rule, recorded activity);
  - insights (patterns, unusual items, suggestions, not enough data);
  - the rule API lifecycle, search, notes, feedback reports, missed events, exports, and activity retention.
- `tests/test_sentinel_intelligence_ui.py` (Edge): building, testing and enabling a rule, validation feedback, the investigation view (explanation, timeline marker, note, feedback, search, export), missed-event reports and alert performance, insights and suggestions, Night/Vacation modes, and phone width.

## Limitations

- All rule, explanation and insight behaviour is verified on simulated scenarios and synthetic history, not on weeks of real residential activity.
- Recorded-activity tests are approximations: visits keep zones and times, not paths.
- Delivery-like visits are a heuristic (short daytime entrance visits); couriers who linger and residents who pop out look different from what the label suggests.
- Cross-camera linking is only as good as the configured camera connections and travel times; with weak or missing connections, sequence and combination rules across cameras do not fire, by design.
- Statistics use the server's local time zone and do not model holidays or seasons.
