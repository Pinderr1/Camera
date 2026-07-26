import test from "node:test";
import assert from "node:assert/strict";
import { BedWatchEngine, pointInPolygon, torsoAngleDeg } from "../js/engine.js";
import { DEFAULTS } from "../js/config.js";

const BED = [[0, 0.3], [0.5, 0.3], [0.5, 0.9], [0, 0.9]];
const STEP_MS = 200;

function makeCfg(overrides = {}) {
  return { ...DEFAULTS, bed_polygon: BED, arming_s: 1, ...overrides };
}

const frame = (ts, hip, shoulderMid, luma = 100) => ({
  timestampMs: ts,
  personPresent: Boolean(hip),
  hip,
  shoulderMid,
  coreConfidence: hip ? 0.9 : 0,
  luma,
});

const lyingInBed = (ts) => frame(ts, [0.2, 0.6], [0.35, 0.6]);
const sittingInBed = (ts) => frame(ts, [0.2, 0.6], [0.2, 0.45]);
const standingOutside = (ts) => frame(ts, [0.7, 0.5], [0.7, 0.35]);
const onFloor = (ts) => frame(ts, [0.7, 0.8], [0.55, 0.8]);
const absent = (ts) => frame(ts, null, null);
const darkFrame = (ts) => frame(ts, null, null, 5);

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

function armedEngine(overrides = {}) {
  const engine = new BedWatchEngine(makeCfg(overrides));
  const clock = { now: 0 };
  run(engine, clock, 1.4, lyingInBed);
  assert.equal(engine.state, "in_bed");
  return { engine, clock };
}

const names = (events) => events.map((event) => event.name);

test("geometry ports", () => {
  assert.equal(pointInPolygon(0.2, 0.6, BED), true);
  assert.equal(pointInPolygon(0.7, 0.6, BED), false);
  assert.equal(Math.round(torsoAngleDeg([0.2, 0.45], [0.2, 0.6])), 0);
  assert.equal(Math.round(torsoAngleDeg([0.35, 0.6], [0.2, 0.6])), 90);
});

test("arming suppresses alerts then lands in in_bed", () => {
  const engine = new BedWatchEngine(makeCfg());
  const clock = { now: 0 };
  const events = run(engine, clock, 1.4, standingOutside);
  assert.equal(engine.state, "in_bed");
  assert.ok(!names(events).includes("bed_exit"));
});

test("sit-up fires after debounce", () => {
  const { engine, clock } = armedEngine();
  const events = run(engine, clock, 1.2, sittingInBed);
  assert.equal(engine.state, "sitting_up");
  assert.ok(names(events).includes("sitting_up"));
});

test("pose lost in bed turns red and repeats missing-person alerts", () => {
  const { engine, clock } = armedEngine();
  const events = run(engine, clock, 8, absent);
  assert.equal(engine.state, "person_missing");
  assert.equal(names(events).filter((name) => name === "person_missing").length, 4);
});

test("tracked bed exit alerts with outside_bed reason", () => {
  const { engine, clock } = armedEngine();
  const events = run(engine, clock, 1.4, standingOutside);
  assert.equal(engine.state, "bed_exit");
  const exit = events.find((event) => event.name === "bed_exit");
  assert.equal(exit.reason, "outside_bed");
});

test("untracked person outside bed uses longer debounce", () => {
  const engine = new BedWatchEngine(makeCfg());
  const clock = { now: 0 };
  run(engine, clock, 1.4, absent);
  assert.equal(engine.state, "in_bed");
  const early = run(engine, clock, 0.8, standingOutside);
  assert.ok(!names(early).includes("bed_exit"));
  const late = run(engine, clock, 1, standingOutside);
  const exit = late.find((event) => event.name === "bed_exit");
  assert.equal(exit.reason, "person_outside_bed_untracked");
});

test("brief hip glitch out of bed does not exit", () => {
  const { engine, clock } = armedEngine();
  run(engine, clock, 0.6, standingOutside);
  const events = run(engine, clock, 2, lyingInBed);
  assert.equal(engine.state, "in_bed");
  assert.ok(!names(events).includes("bed_exit"));
});

test("sitting_up escalates to bed_exit", () => {
  const { engine, clock } = armedEngine();
  run(engine, clock, 3, sittingInBed);
  assert.equal(engine.state, "sitting_up");
  const events = run(engine, clock, 4.5, standingOutside);
  assert.equal(engine.state, "bed_exit");
  assert.ok(names(events).includes("bed_exit"));
});

test("lie back returns to in_bed without alert", () => {
  const { engine, clock } = armedEngine();
  run(engine, clock, 3, sittingInBed);
  const events = run(engine, clock, 7, lyingInBed);
  assert.equal(engine.state, "in_bed");
  assert.ok(names(events).includes("lie_back"));
  assert.ok(!names(events).includes("bed_exit"));
});

test("floor-level posture after exit becomes possible_fall", () => {
  const { engine, clock } = armedEngine();
  run(engine, clock, 4.5, standingOutside);
  assert.equal(engine.state, "bed_exit");
  const events = run(engine, clock, 4, onFloor);
  assert.equal(engine.state, "possible_fall");
  assert.ok(names(events).includes("possible_fall"));
});

test("pose lost during possible_fall becomes an explicit missing-person alert", () => {
  const { engine, clock } = armedEngine();
  run(engine, clock, 4.5, standingOutside);
  run(engine, clock, 4, onFloor);
  run(engine, clock, 30, absent);
  assert.equal(engine.state, "person_missing");
});

test("return to bed sends returned_to_bed after 15s", () => {
  const { engine, clock } = armedEngine();
  run(engine, clock, 4.5, standingOutside);
  const early = run(engine, clock, 10, lyingInBed);
  assert.ok(!names(early).includes("returned_to_bed"));
  const late = run(engine, clock, 8, lyingInBed);
  assert.equal(engine.state, "in_bed");
  assert.ok(names(late).includes("returned_to_bed"));
});

test("pose lost during a return remains a missing-person concern", () => {
  const { engine, clock } = armedEngine();
  run(engine, clock, 4.5, standingOutside);
  run(engine, clock, 5, lyingInBed);
  assert.equal(engine.state, "bed_exit");
  const events = run(engine, clock, 32, absent);
  assert.equal(engine.state, "person_missing");
  assert.ok(names(events).includes("person_missing"));
  assert.ok(!names(events).includes("returned_to_bed"));
});

test("pose lost while outside bed becomes person_missing", () => {
  const { engine, clock } = armedEngine();
  run(engine, clock, 4.5, standingOutside);
  assert.equal(engine.state, "bed_exit");
  run(engine, clock, 60, absent);
  assert.equal(engine.state, "person_missing");
});

test("no-return reminder repeats while out of bed", () => {
  const { engine, clock } = armedEngine({ up_repeat_s: 5, up_urgent_after_s: 1000 });
  run(engine, clock, 4.5, standingOutside);
  const events = run(engine, clock, 22, standingOutside);
  assert.ok(names(events).filter((name) => name === "bed_exit_no_return").length >= 3);
  assert.ok(!names(events).includes("bed_exit_no_return_urgent"));
});

test("no-return reminder escalates to urgent after threshold", () => {
  const { engine, clock } = armedEngine({ up_repeat_s: 5, up_urgent_after_s: 12 });
  run(engine, clock, 4.5, standingOutside);
  const events = run(engine, clock, 20, standingOutside);
  assert.ok(names(events).includes("bed_exit_no_return"));
  assert.ok(names(events).includes("bed_exit_no_return_urgent"));
});

test("darkness blinds and recovers with events", () => {
  const { engine, clock } = armedEngine();
  const darkEvents = run(engine, clock, 11, darkFrame);
  assert.ok(names(darkEvents).includes("camera_blind"));
  assert.equal(engine.update(darkFrame(clock.now + STEP_MS)).state, "offline_or_blind");
  const lightEvents = run(engine, clock, 12, absent);
  assert.ok(names(lightEvents).includes("camera_ok"));
  assert.equal(engine.state, "person_missing");
});

test("missing person reappearing in bed emits returned_to_bed", () => {
  const { engine, clock } = armedEngine({ person_missing_debounce_s: 1 });
  run(engine, clock, 2, absent);
  assert.equal(engine.state, "person_missing");
  const events = run(engine, clock, 1, lyingInBed);
  assert.equal(engine.state, "in_bed");
  assert.ok(names(events).includes("returned_to_bed"));
});
