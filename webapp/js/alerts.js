// ntfy delivery. Priorities/cooldowns follow the plan table; cooldowns are
// per-stage, mirroring the (state, severity) cooldown keying in
// src/senior_safety/state_machine.py.

const STAGES = {
  sitting_up: {
    title: "Grandma is sitting up",
    body: "Sitting up in bed - head over now.",
    priority: 4,
    tags: "warning,bed",
    // The engine owns the 60-second repeat cadence. A notification-layer
    // cooldown here would silently discard those reminders.
    cooldown_s: 0,
    toggle: "sitting_up",
    opensEpisode: true,
  },
  bed_exit: {
    title: "Grandma is OUT OF BED",
    body: "Left the bed zone - go now.",
    priority: 5,
    tags: "rotating_light",
    cooldown_s: 300,
    opensEpisode: true,
  },
  possible_fall: {
    title: "Possible fall - check immediately",
    body: "Low on the floor after leaving bed.",
    priority: 5,
    tags: "rotating_light,sos",
    cooldown_s: 300,
    toggle: "possible_fall",
    opensEpisode: true,
  },
  got_up: {
    title: "Grandma is UP",
    body: "She just got up - go now.",
    priority: 5,
    tags: "rotating_light",
    cooldown_s: 300,
    opensEpisode: true,
  },
  person_missing: {
    title: "Grandma is NOT VISIBLE",
    body: "She is out of the camera view. Please check now.",
    priority: 5,
    tags: "rotating_light,eyes",
    cooldown_s: 0,
    toggle: "no_return_reminder",
    opensEpisode: true,
  },
  settled: {
    title: "Settled again",
    body: "Sitting or lying down again on her own.",
    priority: 2,
    tags: "white_check_mark",
    cooldown_s: 0,
    toggle: "back_in_bed",
    onlyIfEpisodeAlerted: true,
    closesEpisode: true,
  },
  still_up: {
    title: "Still up - not settled",
    body: "She's still up and hasn't settled. Please check.",
    priority: 4,
    tags: "hourglass,warning",
    cooldown_s: 0,
    toggle: "no_return_reminder",
  },
  still_up_urgent: {
    title: "URGENT: still up, not settled",
    body: "She's been up a while and still hasn't settled. Check now.",
    priority: 5,
    tags: "rotating_light,sos",
    cooldown_s: 0,
    toggle: "no_return_reminder",
  },
  bed_exit_no_return: {
    title: "Still out of bed - not back",
    body: "She's still out of bed and hasn't returned. Please check.",
    priority: 4,
    tags: "hourglass,warning",
    cooldown_s: 0,
    toggle: "no_return_reminder",
  },
  bed_exit_no_return_urgent: {
    title: "URGENT: still out of bed",
    body: "She's been out of bed a while and hasn't returned. Check now.",
    priority: 5,
    tags: "rotating_light,sos",
    cooldown_s: 0,
    toggle: "no_return_reminder",
  },
  returned_to_bed: {
    title: "Back in bed",
    body: "Returned to bed on her own.",
    priority: 2,
    tags: "white_check_mark",
    cooldown_s: 0,
    toggle: "back_in_bed",
    onlyIfEpisodeAlerted: true,
    closesEpisode: true,
  },
  camera_blind: {
    title: "Bedside camera can't see",
    body: "Too dark or camera blocked. Check the night light and phone.",
    priority: 3,
    tags: "flashlight",
    cooldown_s: 1800,
  },
  camera_stopped: {
    title: "Camera stopped working",
    body: "The bedside phone stopped sending video - check the phone.",
    priority: 4,
    tags: "warning,camera",
    cooldown_s: 900,
  },
  paused: {
    title: "Monitoring paused",
    body: "Bed alerts are OFF until resumed.",
    priority: 3,
    tags: "pause_button",
    cooldown_s: 0,
    alwaysSend: true,
  },
  resumed: {
    title: "Monitoring resumed",
    body: "Bed alerts are back ON.",
    priority: 3,
    tags: "arrow_forward",
    cooldown_s: 0,
    alwaysSend: true,
  },
  test: {
    title: "Test alert from bedside phone",
    body: "If you can read this, alerts are working.",
    priority: 3,
    tags: "bell",
    cooldown_s: 0,
    alwaysSend: true,
  },
};

export class Notifier {
  constructor(cfg) {
    this.cfg = cfg;
    this.lastSent = {};
    this.pausedUntil = 0;
    this.episodeAlerted = false;
    this.onLog = null;
  }

  get paused() {
    return this.pausedUntil === -1 || Date.now() < this.pausedUntil;
  }

  pause(minutes) {
    this.pausedUntil = minutes === null ? -1 : Date.now() + minutes * 60_000;
    const label = minutes === null ? "until resumed" : `for ${minutes} min`;
    this.notify("paused", `Bed alerts are OFF ${label}.`);
  }

  resume() {
    this.pausedUntil = 0;
    this.notify("resumed");
  }

  async notify(name, bodyOverride) {
    const stage = STAGES[name];
    if (!stage) return;
    if (stage.toggle && this.cfg.alerts[stage.toggle] === false) {
      this._log(name, "skipped (disabled)");
      return;
    }
    if (this.paused && !stage.alwaysSend) {
      this._log(name, "suppressed (paused)");
      return;
    }
    if (stage.onlyIfEpisodeAlerted && !this.episodeAlerted) {
      this._log(name, "skipped (no alert this episode)");
      return;
    }
    const now = Date.now();
    if (stage.cooldown_s && this.lastSent[name] && now - this.lastSent[name] < stage.cooldown_s * 1000) {
      this._log(name, "suppressed (cooldown)");
      return;
    }
    this.lastSent[name] = now;
    if (stage.opensEpisode) this.episodeAlerted = true;
    if (stage.closesEpisode) this.episodeAlerted = false;
    await this._post(stage, bodyOverride || stage.body, name);
  }

  async _post(stage, body, name) {
    if (!this.cfg.topic) {
      this._log(name, "not sent (no topic configured)");
      return;
    }
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const response = await fetch(`https://ntfy.sh/${this.cfg.topic}`, {
          method: "POST",
          headers: {
            Title: stage.title,
            Priority: String(stage.priority),
            Tags: stage.tags,
          },
          body,
        });
        if (response.ok) {
          this._log(name, "sent");
          return;
        }
      } catch {
        // Retry below.
      }
      await new Promise((resolve) => setTimeout(resolve, 1000 * 2 ** attempt));
    }
    this._log(name, "SEND FAILED after 3 tries");
  }

  _log(name, outcome) {
    this.onLog?.(`ntfy ${name}: ${outcome}`);
  }
}

export function startHeartbeat(cfg) {
  if (!cfg.heartbeat_url) return null;
  const ping = () => fetch(cfg.heartbeat_url, { mode: "no-cors", cache: "no-store" }).catch(() => {});
  ping();
  return setInterval(ping, 60_000);
}
