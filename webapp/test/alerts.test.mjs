import test from "node:test";
import assert from "node:assert/strict";
import { Notifier } from "../js/alerts.js";
import { DEFAULTS, loadConfig } from "../js/config.js";

test("v1 browser settings migrate to the 30-second away cadence", () => {
  const originalStorage = globalThis.localStorage;
  globalThis.localStorage = {
    getItem: () => JSON.stringify({ up_repeat_s: 60, topic: "saved-topic" }),
  };

  try {
    const cfg = loadConfig();
    assert.equal(cfg.config_version, 2);
    assert.equal(cfg.up_repeat_s, 30);
    assert.equal(cfg.sit_up_repeat_s, 60);
    assert.equal(cfg.person_missing_repeat_s, 30);
    assert.equal(cfg.topic, "saved-topic");
  } finally {
    globalThis.localStorage = originalStorage;
  }
});

test("recurring sitting and missing alerts are not dropped by notifier cooldowns", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(options.headers.Title);
    return { ok: true };
  };

  try {
    const notifier = new Notifier({
      ...DEFAULTS,
      topic: "test-topic",
      alerts: { ...DEFAULTS.alerts },
    });
    await notifier.notify("sitting_up");
    await notifier.notify("sitting_up");
    await notifier.notify("person_missing");
    await notifier.notify("person_missing");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(requests, [
    "Grandma is sitting up",
    "Grandma is sitting up",
    "Grandma is NOT VISIBLE",
    "Grandma is NOT VISIBLE",
  ]);
});
