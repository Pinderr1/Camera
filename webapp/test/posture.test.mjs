import test from "node:test";
import assert from "node:assert/strict";
import { PostureEngine } from "../js/engine.js";
import { DEFAULTS } from "../js/config.js";

const STEP_MS = 200;

function makeCfg(overrides = {}) {
  return { ...DEFAULTS, mode: "posture", arming_s: 1, ...overrides };
}

const frame = (ts, hip, shoulderMid, luma = 100) => ({
  timestampMs: ts,
  personPresent: Boolean(hip),
  hip,
  shoulderMid,
  coreConfidence: hip ? 0.9 : 0,
  luma,
});

const lyingCouch = (ts) => frame(ts, [0.5, 0.6], [0.65, 0.6]);
const sittingCouch = (ts) => frame(ts, [0.5, 0.6], [0.5, 0.45]);
const standingUp = (ts) => frame(ts, [0.5, 0.35], [0.5, 0.2]);
const walkedAway = (ts) => frame(ts, [0.8, 0.55], [0.8, 0.4]);
const seatedElsewhere = (ts) => frame(ts, [0.85, 0.6], [0.85, 0.45]);
const onFloor = (ts) => frame(ts, [0.7, 0.8], [0.55, 0.8]);
const absent = (ts) => frame(ts, null, null);

function run(engine, clock, seconds, factory) {
  const events = [];
  const frames = Math.round((seconds * 1000) / STEP_MS);
  for (let i = 0; i < frames; i++) {
    clock.now += STEP_MS;
    const result = engine.update(factory(clock.now));
    events.push(...result.events);
  }
  return events;
}

const names = (events) => events.map((event) => event.name);

function lyingEngine(overrides = {}) {
  const engine = new PostureEngine(makeCfg(overrides));
  const clock = { now: 0 };
  run(engine, clock, 1.4, lyingCouch);
  assert.equal(engine.state, "lying");
  return { engine, clock };
}

test("arming classifies upright person as sitting without alert", () => {
  const engine = new PostureEngine(makeCfg());
  const clock = { now: 0 };
  const events = run(engine, clock, 1.4, sittingCouch);
  assert.equal(engine.state, "sitting");
  assert.deepEqual(names(events), ["state_change"]);
});

test("lying to sitting fires sitting_up early warning", () => {
  const { engine, clock } = lyingEngine();
  const events = run(engine, clock, 1.2, sittingCouch);
  assert.equal(engine.state, "sitting");
  assert.ok(names(events).includes("sitting_up"));
  assert.equal(engine.update(sittingCouch(clock.now + STEP_MS)).state, "sitting_up");
});

test("standing up from sitting fires got_up", () => {
  const { engine, clock } = lyingEngine();
  run(engine, clock, 1.2, sittingCouch);
  const events = run(engine, clock, 1.4, standingUp);
  assert.equal(engine.state, "up");
  const alert = events.find((event) => event.name === "got_up");
  assert.equal(alert.reason, "stood_up");
});

test("walking away from rest spot fires got_up", () => {
  const { engine, clock } = lyingEngine();
  run(engine, clock, 1.2, sittingCouch);
  const events = run(engine, clock, 1.4, walkedAway);
  assert.equal(engine.state, "up");
  assert.ok(names(events).includes("got_up"));
});

test("sitting quietly on the couch for a long time never alerts", () => {
  const engine = new PostureEngine(makeCfg());
  const clock = { now: 0 };
  run(engine, clock, 1.4, sittingCouch);
  const events = run(engine, clock, 600, sittingCouch);
  assert.equal(engine.state, "sitting");
  assert.deepEqual(names(events), []);
});

test("pose lost while lying turns red and alerts every missing interval", () => {
  const { engine, clock } = lyingEngine({
    person_missing_debounce_s: 2,
    person_missing_repeat_s: 3,
  });
  const events = run(engine, clock, 12, absent);
  assert.equal(engine.state, "person_missing");
  assert.ok(names(events).filter((name) => name === "person_missing").length >= 4);
});

test("lie back down from sitting is silent", () => {
  const { engine, clock } = lyingEngine();
  run(engine, clock, 3, sittingCouch);
  const events = run(engine, clock, 7, lyingCouch);
  assert.equal(engine.state, "lying");
  assert.ok(names(events).includes("lie_down"));
  assert.ok(!names(events).includes("got_up"));
});

test("sitting-up warning repeats every configured interval", () => {
  const { engine, clock } = lyingEngine({ sit_up_repeat_s: 5 });
  const initial = run(engine, clock, 3, sittingCouch);
  assert.equal(names(initial).filter((name) => name === "sitting_up").length, 1);
  const reminders = run(engine, clock, 16, sittingCouch);
  assert.equal(names(reminders).filter((name) => name === "sitting_up").length, 3);
});

test("settles flat again and sends settled once", () => {
  const { engine, clock } = lyingEngine();
  run(engine, clock, 3, sittingCouch);
  run(engine, clock, 4, standingUp);
  assert.equal(engine.state, "up");
  const events = run(engine, clock, 13, lyingCouch);
  assert.equal(engine.state, "lying");
  assert.equal(names(events).filter((name) => name === "settled").length, 1);
});

test("settles seated somewhere new after long stationary hold", () => {
  const { engine, clock } = lyingEngine();
  run(engine, clock, 3, sittingCouch);
  run(engine, clock, 4, walkedAway);
  assert.equal(engine.state, "up");
  const events = run(engine, clock, 63, seatedElsewhere);
  assert.equal(engine.state, "sitting");
  assert.ok(names(events).includes("settled"));
});

test("pacing never counts as settled", () => {
  const { engine, clock } = lyingEngine();
  run(engine, clock, 3, sittingCouch);
  run(engine, clock, 4, walkedAway);
  const pacing = (ts) => {
    const x = 0.4 + 0.3 * Math.abs(Math.sin(ts / 4000));
    return frame(ts, [x, 0.5], [x, 0.35]);
  };
  run(engine, clock, 90, pacing);
  assert.equal(engine.state, "up");
});

test("still_up reminder repeats every interval while up", () => {
  const { engine, clock } = lyingEngine({ up_repeat_s: 5, up_urgent_after_s: 1000 });
  run(engine, clock, 3, sittingCouch);
  run(engine, clock, 4, walkedAway);
  const events = run(engine, clock, 22, walkedAway);
  assert.ok(names(events).filter((name) => name === "still_up").length >= 3);
  assert.ok(!names(events).includes("still_up_urgent"));
});

test("still_up escalates to still_up_urgent after urgent threshold", () => {
  const { engine, clock } = lyingEngine({ up_repeat_s: 5, up_urgent_after_s: 12 });
  run(engine, clock, 3, sittingCouch);
  run(engine, clock, 4, walkedAway);
  const events = run(engine, clock, 20, walkedAway);
  assert.ok(names(events).includes("still_up"));
  assert.ok(names(events).includes("still_up_urgent"));
});

test("repeats stop once she settles", () => {
  const { engine, clock } = lyingEngine({ up_repeat_s: 5, up_urgent_after_s: 1000 });
  run(engine, clock, 3, sittingCouch);
  run(engine, clock, 4, walkedAway);
  run(engine, clock, 13, lyingCouch);
  assert.equal(engine.state, "lying");
  const events = run(engine, clock, 30, lyingCouch);
  assert.ok(!names(events).some((name) => name.startsWith("still_up")));
});

test("floor-level posture while up becomes possible_fall", () => {
  const { engine, clock } = lyingEngine();
  run(engine, clock, 3, sittingCouch);
  run(engine, clock, 4, walkedAway);
  const events = run(engine, clock, 4, onFloor);
  assert.equal(engine.state, "possible_fall");
  assert.ok(names(events).includes("possible_fall"));
});

test("pose lost while up becomes person_missing and repeats", () => {
  const { engine, clock } = lyingEngine({
    person_missing_debounce_s: 2,
    person_missing_repeat_s: 3,
  });
  run(engine, clock, 3, sittingCouch);
  run(engine, clock, 4, walkedAway);
  const events = run(engine, clock, 12, absent);
  assert.equal(engine.state, "person_missing");
  assert.ok(names(events).filter((name) => name === "person_missing").length >= 4);
});

test("person reappearing safely clears person_missing", () => {
  const { engine, clock } = lyingEngine({ person_missing_debounce_s: 2 });
  run(engine, clock, 4, absent);
  assert.equal(engine.state, "person_missing");
  run(engine, clock, 1, lyingCouch);
  assert.equal(engine.state, "lying");
});

test("missing from lying then reappearing sitting restarts sit-up alerts", () => {
  const { engine, clock } = lyingEngine({
    person_missing_debounce_s: 1,
    sit_up_repeat_s: 5,
  });
  run(engine, clock, 2, absent);
  assert.equal(engine.state, "person_missing");

  const returned = run(engine, clock, 1, sittingCouch);
  assert.equal(engine.state, "sitting");
  assert.equal(engine.update(sittingCouch(clock.now + STEP_MS)).state, "sitting_up");
  assert.ok(names(returned).includes("sitting_up"));

  const repeats = run(engine, clock, 11, sittingCouch);
  assert.equal(names(repeats).filter((name) => name === "sitting_up").length, 2);
});

test("missing person reappearing lying emits the settled all-clear", () => {
  const { engine, clock } = lyingEngine({ person_missing_debounce_s: 1 });
  run(engine, clock, 2, absent);
  const events = run(engine, clock, 1, lyingCouch);
  assert.equal(engine.state, "lying");
  assert.ok(names(events).includes("settled"));
});
