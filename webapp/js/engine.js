// Pure detection logic, no DOM. Ports point_in_polygon, torso_angle_deg, EMA
// smoothing with hip-glitch rejection, plus strict missing-person handling
// for the bedside alert behavior.

export function pointInPolygon(x, y, polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

// 0 = upright, 90 = horizontal.
export function torsoAngleDeg(shoulderMid, hip) {
  const dx = Math.abs(shoulderMid[0] - hip[0]);
  const dy = Math.abs(shoulderMid[1] - hip[1]);
  return (Math.atan2(dx, dy) * 180) / Math.PI;
}

// Sustained-condition timer with a short gap tolerance so a single dropped
// pose frame does not reset an almost-elapsed debounce.
class Sustained {
  constructor(holdS, gapToleranceS = 0.6) {
    this.holdS = holdS;
    this.gapS = gapToleranceS;
    this.since = null;
    this.lastTrue = null;
  }

  update(cond, now) {
    if (cond) {
      if (this.since === null) this.since = now;
      this.lastTrue = now;
    } else if (this.since !== null && now - this.lastTrue > this.gapS * 1000) {
      this.since = null;
    }
    return cond && this.since !== null && now - this.since >= this.holdS * 1000;
  }

  reset() {
    this.since = null;
    this.lastTrue = null;
  }
}

export const STATES = [
  "arming",
  "in_bed",
  "sitting_up",
  "bed_exit",
  "person_missing",
  "possible_fall",
  "offline_or_blind",
];

export class BedWatchEngine {
  constructor(cfg) {
    this.cfg = cfg;
    this.state = "arming";
    this.stateSince = null;
    this.blind = false;
    this.lastInBedMs = null;
    this.lastPresentMs = null;
    this.lastPresentInBed = false;
    this.episodeStartMs = null;
    this.lastRepeatMs = null;
    this.sitUpStartedMs = null;
    this.lastSitUpRepeatMs = null;
    this.missingStartedMs = null;
    this.lastMissingRepeatMs = null;
    this.debug = {};
    this._smoothedHip = null;
    this._smoothedShoulder = null;
    this._lastSmoothMs = null;
    this._glitchStreak = 0;
    this._torsoHistory = [];
    this.conds = {
      sitUp: new Sustained(cfg.sit_up_debounce_s),
      exit: new Sustained(cfg.bed_exit_debounce_s),
      untrackedExit: new Sustained(cfg.untracked_exit_debounce_s),
      missing: new Sustained(cfg.person_missing_debounce_s),
      lieBack: new Sustained(cfg.lie_back_debounce_s),
      bedReturn: new Sustained(cfg.bed_return_debounce_s, 1.5),
      floor: new Sustained(cfg.floor_level_min_s),
      floorClear: new Sustained(cfg.floor_clear_s),
      dark: new Sustained(cfg.dark_debounce_s, 0),
      darkRecover: new Sustained(cfg.dark_debounce_s, 0),
    };
  }

  update(features) {
    const events = [];
    const now = features.timestampMs;
    if (this.stateSince === null) this.stateSince = now;

    this._updateDarkness(features, now, events);
    if (this.blind) {
      return { state: "offline_or_blind", events };
    }

    const { hip, shoulderMid } = this._smooth(features, now);
    const present = Boolean(features.personPresent && hip);
    const confident =
      present && features.coreConfidence >= this.cfg.core_confidence_min;
    const hipInBed =
      present && pointInPolygon(hip[0], hip[1], this.cfg.bed_polygon);
    const angle =
      present && shoulderMid ? torsoAngleDeg(shoulderMid, hip) : null;
    const upright = angle !== null && angle <= this.cfg.sit_up_torso_deg;
    const flat = angle !== null && angle >= this.cfg.floor_level_torso_angle_deg;
    const onFloor = present && flat && hip[1] >= this.cfg.floor_line_y;

    if (present) {
      this.lastPresentMs = now;
      this.lastPresentInBed = hipInBed;
    }
    if (hipInBed) this.lastInBedMs = now;
    const trackedRecently =
      this.lastInBedMs !== null &&
      now - this.lastInBedMs <= this.cfg.track_link_s * 1000;

    this.debug = { present, confident, hipInBed, angle, luma: features.luma };

    const visible = confident && shoulderMid !== null;
    if (this.state !== "arming" && this.state !== "person_missing") {
      if (this.conds.missing.update(!visible, now)) {
        this._enterMissing(now, events);
        return { state: this.state, events };
      }
    } else if (this.state === "person_missing") {
      if (!visible) {
        this._maybeMissingRepeat(now, events);
        return { state: this.state, events };
      }

      if (onFloor) {
        this._recoverFromMissing("possible_fall", now, events, "visible_on_floor");
        events.push({ name: "possible_fall", reason: "visible_on_floor" });
      } else if (hipInBed && upright) {
        this._recoverFromMissing("sitting_up", now, events, "visible_sitting_in_bed");
        this._startSitUp(now);
        events.push({ name: "sitting_up", reason: "visible_sitting_in_bed" });
      } else if (hipInBed) {
        this._recoverFromMissing("in_bed", now, events, "visible_in_bed");
      } else {
        this._recoverFromMissing("bed_exit", now, events, "visible_outside_bed");
        events.push({ name: "bed_exit", reason: "visible_outside_bed" });
      }
      return { state: this.state, events };
    }

    switch (this.state) {
      case "arming":
        if (now - this.stateSince >= this.cfg.arming_s * 1000) {
          this._setState("in_bed", now, events, "armed");
        }
        break;

      case "in_bed":
        if (this.conds.sitUp.update(present && confident && hipInBed && upright, now)) {
          this._setState("sitting_up", now, events, "sit_up_in_bed");
          this._startSitUp(now);
          events.push({ name: "sitting_up", reason: "sit_up_in_bed" });
        } else if (this.conds.exit.update(present && !hipInBed && trackedRecently, now)) {
          this._startEpisode(now);
          this._setState("bed_exit", now, events, "outside_bed");
          events.push({ name: "bed_exit", reason: "outside_bed" });
        } else if (this.conds.untrackedExit.update(present && !hipInBed && !trackedRecently, now)) {
          this._startEpisode(now);
          this._setState("bed_exit", now, events, "person_outside_bed_untracked");
          events.push({ name: "bed_exit", reason: "person_outside_bed_untracked" });
        }
        break;

      case "sitting_up":
        if (this.conds.exit.update(present && !hipInBed, now)) {
          this._clearSitUp();
          this._startEpisode(now);
          this._setState("bed_exit", now, events, "outside_bed");
          events.push({ name: "bed_exit", reason: "outside_bed" });
        } else if (this.conds.lieBack.update(present && hipInBed && flat, now)) {
          this._clearSitUp();
          this._setState("in_bed", now, events, "lie_back");
          events.push({ name: "lie_back", reason: "lie_back" });
        } else {
          this._maybeSitUpRepeat(now, events);
        }
        break;

      case "bed_exit":
        if (this.conds.bedReturn.update(present && hipInBed, now)) {
          this._setState("in_bed", now, events, "back_in_bed");
          events.push({ name: "returned_to_bed", reason: "back_in_bed" });
          this.episodeStartMs = null;
        } else if (this.conds.floor.update(present && confident && onFloor, now)) {
          this._setState("possible_fall", now, events, "floor_level_posture");
          events.push({ name: "possible_fall", reason: "floor_level_posture" });
        } else {
          this._maybeRepeat(now, events, "bed_exit_no_return", "bed_exit_no_return_urgent");
        }
        break;

      case "possible_fall":
        // Only visible evidence clears the floor latch; a lost pose is not
        // evidence of recovery.
        if (this.conds.floorClear.update(present && !onFloor, now)) {
          this._setState("bed_exit", now, events, "floor_level_cleared");
        }
        break;
    }

    return { state: this.state, events };
  }

  _setState(state, now, events, reason) {
    this.state = state;
    this.stateSince = now;
    // Darkness is camera-health state, not posture state. Keep its debounce
    // running across posture changes (including a person disappearing), or a
    // state transition can postpone the blind-camera warning indefinitely.
    for (const [name, cond] of Object.entries(this.conds)) {
      if (name !== "dark" && name !== "darkRecover") cond.reset();
    }
    events.push({ name: "state_change", value: state, reason });
  }

  _startEpisode(now) {
    if (this.episodeStartMs === null) {
      this.episodeStartMs = now;
      this.lastRepeatMs = null;
    }
  }

  _startSitUp(now) {
    this.sitUpStartedMs = now;
    this.lastSitUpRepeatMs = now;
  }

  _clearSitUp() {
    this.sitUpStartedMs = null;
    this.lastSitUpRepeatMs = null;
  }

  _maybeSitUpRepeat(now, events) {
    if (this.sitUpStartedMs === null || this.lastSitUpRepeatMs === null) return;
    if (now - this.lastSitUpRepeatMs < this.cfg.sit_up_repeat_s * 1000) return;
    this.lastSitUpRepeatMs = now;
    events.push({
      name: "sitting_up",
      reason: "still_sitting_up",
      elapsed_s: Math.round((now - this.sitUpStartedMs) / 1000),
    });
  }

  _enterMissing(now, events) {
    const reason = `person_not_visible_from_${this.state}`;
    this._clearSitUp();
    this._startEpisode(now);
    this.missingStartedMs = now;
    this.lastMissingRepeatMs = now;
    this._setState("person_missing", now, events, reason);
    events.push({ name: "person_missing", reason });
  }

  _recoverFromMissing(state, now, events, reason) {
    this._setState(state, now, events, reason);
    this.missingStartedMs = null;
    this.lastMissingRepeatMs = null;
    if (["in_bed", "lying", "sitting"].includes(state)) {
      this.episodeStartMs = null;
      this.lastRepeatMs = null;
    }
  }

  _maybeMissingRepeat(now, events) {
    if (this.missingStartedMs === null || this.lastMissingRepeatMs === null) return;
    if (now - this.lastMissingRepeatMs < this.cfg.person_missing_repeat_s * 1000) return;
    this.lastMissingRepeatMs = now;
    events.push({
      name: "person_missing",
      reason: "still_not_visible",
      elapsed_s: Math.round((now - this.missingStartedMs) / 1000),
    });
  }

  // While she stays up/out of bed, re-emit a reminder every up_repeat_s and
  // escalate to the urgent variant once she has been up past up_urgent_after_s.
  // The cadence lives here; the alert stages use cooldown_s: 0.
  _maybeRepeat(now, events, name, urgentName) {
    if (this.episodeStartMs === null) return;
    const elapsed = now - this.episodeStartMs;
    if (elapsed < this.cfg.up_repeat_s * 1000) return;
    if (this.lastRepeatMs !== null && now - this.lastRepeatMs < this.cfg.up_repeat_s * 1000) {
      return;
    }
    this.lastRepeatMs = now;
    const urgent = elapsed >= this.cfg.up_urgent_after_s * 1000;
    events.push({
      name: urgent ? urgentName : name,
      reason: urgent ? "still_up_urgent" : "still_up_repeat",
      minutes: Math.round(elapsed / 60000),
    });
  }

  _updateDarkness(features, now, events) {
    if (features.luma === null || features.luma === undefined) return;
    if (!this.blind) {
      if (this.conds.dark.update(features.luma < this.cfg.dark_luma, now)) {
        this.blind = true;
        this.conds.darkRecover.reset();
        events.push({ name: "camera_blind", reason: "too_dark" });
      }
    } else if (this.conds.darkRecover.update(features.luma >= this.cfg.dark_luma_recover, now)) {
      this.blind = false;
      for (const cond of Object.values(this.conds)) cond.reset();
      events.push({ name: "camera_ok", reason: "light_recovered" });
    }
  }

  _smooth(features, now) {
    if (!features.personPresent || !features.hip) {
      return { hip: null, shoulderMid: null };
    }
    const gapS =
      this._lastSmoothMs === null ? null : (now - this._lastSmoothMs) / 1000;
    this._lastSmoothMs = now;
    if (gapS === null || gapS <= 0 || gapS > 1.0 || this._smoothedHip === null) {
      this._resetTrack(features);
      return { hip: this._smoothedHip, shoulderMid: this._smoothedShoulder };
    }

    const alpha = 1 - Math.exp(-gapS / Math.max(this.cfg.smoothing_tau_s, 1e-3));
    const raw = features.hip;
    const prev = this._smoothedHip;
    const jump = Math.hypot(raw[0] - prev[0], raw[1] - prev[1]);
    let hip;
    if (jump > this.cfg.outlier_jump_bl * this._bodyScale()) {
      this._glitchStreak += 1;
      if (this._glitchStreak < this.cfg.outlier_accept_frames) {
        // Nonphysical teleport (landmark mistrack): hold the last believable
        // position unless it persists.
        hip = prev;
      } else {
        this._resetTrack(features);
        return { hip: this._smoothedHip, shoulderMid: this._smoothedShoulder };
      }
    } else {
      this._glitchStreak = 0;
      hip = [
        prev[0] + alpha * (raw[0] - prev[0]),
        prev[1] + alpha * (raw[1] - prev[1]),
      ];
    }
    this._smoothedHip = hip;

    let shoulderMid = features.shoulderMid;
    if (shoulderMid && this._smoothedShoulder) {
      shoulderMid = [
        this._smoothedShoulder[0] + alpha * (shoulderMid[0] - this._smoothedShoulder[0]),
        this._smoothedShoulder[1] + alpha * (shoulderMid[1] - this._smoothedShoulder[1]),
      ];
    }
    if (shoulderMid) this._smoothedShoulder = shoulderMid;

    this._pushTorso(hip, this._smoothedShoulder, now);
    return { hip, shoulderMid: this._smoothedShoulder };
  }

  _resetTrack(features) {
    this._smoothedHip = features.hip;
    this._smoothedShoulder = features.shoulderMid;
    this._glitchStreak = 0;
    this._pushTorso(features.hip, features.shoulderMid, this._lastSmoothMs);
  }

  _pushTorso(hip, shoulderMid, now) {
    if (!hip || !shoulderMid || now === null) return;
    this._torsoHistory.push([
      now,
      Math.hypot(shoulderMid[0] - hip[0], shoulderMid[1] - hip[1]),
    ]);
    while (this._torsoHistory.length && now - this._torsoHistory[0][0] > 2000) {
      this._torsoHistory.shift();
    }
  }

  _bodyScale() {
    if (this._torsoHistory.length) {
      const values = this._torsoHistory.map((item) => item[1]).sort((a, b) => a - b);
      return Math.max(0.05, values[Math.floor(values.length / 2)]);
    }
    return 0.25;
  }
}

// Zone-free mode: instead of a bed polygon, watch posture transitions wherever
// the camera points (couch or bed). "Standing up" is judged against a slow
// rest-position baseline: hips rising or moving sideways by body-lengths, so
// it self-calibrates to any camera placement.
export class PostureEngine extends BedWatchEngine {
  constructor(cfg) {
    super(cfg);
    this.restBaseline = null;
    this._baseLastMs = null;
    this._settleAnchor = null;
    this.sittingFromLyingMs = null;
    this.conds = {
      sitUp: new Sustained(cfg.sit_up_debounce_s),
      standUp: new Sustained(cfg.stand_debounce_s),
      lieDown: new Sustained(cfg.lie_back_debounce_s),
      missing: new Sustained(cfg.person_missing_debounce_s),
      settleFlat: new Sustained(cfg.settle_flat_s),
      floor: new Sustained(cfg.floor_level_min_s),
      floorClear: new Sustained(cfg.floor_clear_s),
      dark: new Sustained(cfg.dark_debounce_s, 0),
      darkRecover: new Sustained(cfg.dark_debounce_s, 0),
    };
  }

  update(features) {
    const events = [];
    const now = features.timestampMs;
    if (this.stateSince === null) this.stateSince = now;

    this._updateDarkness(features, now, events);
    if (this.blind) {
      return { state: "offline_or_blind", events };
    }

    const { hip, shoulderMid } = this._smooth(features, now);
    const present = Boolean(features.personPresent && hip);
    const confident =
      present && features.coreConfidence >= this.cfg.core_confidence_min;
    const angle =
      present && shoulderMid ? torsoAngleDeg(shoulderMid, hip) : null;
    const upright = angle !== null && angle <= this.cfg.sit_up_torso_deg;
    const flat = angle !== null && angle >= this.cfg.floor_level_torso_angle_deg;
    const onFloor = present && flat && hip[1] >= this.cfg.floor_line_y;
    if (present) this.lastPresentMs = now;

    const standing = this._standingSignal(present, upright, hip);
    this.debug = { present, confident, angle, standing, luma: features.luma };

    const visible = confident && shoulderMid !== null;
    if (this.state !== "arming" && this.state !== "person_missing") {
      if (this.conds.missing.update(!visible, now)) {
        this._enterMissing(now, events);
        return { state: this.state, events };
      }
    } else if (this.state === "person_missing") {
      if (!visible) {
        this._maybeMissingRepeat(now, events);
        return { state: this.state, events };
      }

      if (onFloor) {
        this._recoverFromMissing("possible_fall", now, events, "visible_on_floor");
        events.push({ name: "possible_fall", reason: "visible_on_floor" });
      } else if (flat) {
        this._recoverFromMissing("lying", now, events, "visible_lying");
        this.restBaseline = [...hip];
      } else if (standing) {
        this._recoverFromMissing("up", now, events, "visible_and_up");
      } else {
        this._recoverFromMissing("sitting", now, events, "visible_sitting");
        this.restBaseline = [...hip];
      }
      return { state: this._displayState(), events };
    }

    switch (this.state) {
      case "arming":
        if (now - this.stateSince >= this.cfg.arming_s * 1000) {
          const initial = present && upright ? "sitting" : "lying";
          this._setState(initial, now, events, "armed");
          if (present) this.restBaseline = [...hip];
        }
        break;

      case "lying":
        this._updateBaseline(present, hip, now);
        if (this.conds.standUp.update(present && confident && standing, now)) {
          this._startEpisode(now);
          this._setState("up", now, events, "stood_up_from_lying");
          events.push({ name: "got_up", reason: "stood_up_from_lying" });
        } else if (this.conds.sitUp.update(present && confident && upright && !standing, now)) {
          this._setState("sitting", now, events, "sit_up");
          this.sittingFromLyingMs = now;
          this._startSitUp(now);
          events.push({ name: "sitting_up", reason: "sit_up" });
        }
        break;

      case "sitting":
        this._updateBaseline(present, hip, now);
        if (this.conds.standUp.update(present && confident && standing, now)) {
          this._clearSitUp();
          this._startEpisode(now);
          this._setState("up", now, events, "stood_up");
          events.push({ name: "got_up", reason: "stood_up" });
        } else if (this.conds.lieDown.update(present && flat, now)) {
          this._clearSitUp();
          this._setState("lying", now, events, "lie_down");
          this.sittingFromLyingMs = null;
          events.push({ name: "lie_down", reason: "lie_down" });
        } else {
          this._maybeSitUpRepeat(now, events);
        }
        break;

      case "up":
        if (this.conds.floor.update(present && confident && onFloor, now)) {
          this._setState("possible_fall", now, events, "floor_level_posture");
          events.push({ name: "possible_fall", reason: "floor_level_posture" });
        } else if (this.conds.settleFlat.update(present && flat && !onFloor, now)) {
          this._settle("lying", hip, now, events);
        } else if (this._settledUpright(present, upright, hip, now)) {
          this._settle("sitting", hip, now, events);
        } else {
          this._maybeRepeat(now, events, "still_up", "still_up_urgent");
        }
        break;

      case "possible_fall":
        if (this.conds.floorClear.update(present && !onFloor, now)) {
          this._setState("up", now, events, "floor_level_cleared");
        }
        break;
    }

    return { state: this._displayState(), events };
  }

  _displayState() {
    if (this.state === "sitting" && this.sittingFromLyingMs !== null) {
      return "sitting_up";
    }
    return this.state;
  }

  _standingSignal(present, upright, hip) {
    if (!present || !upright || !this.restBaseline) return false;
    const scale = this._bodyScale();
    const rise = this.restBaseline[1] - hip[1];
    const walked = Math.abs(hip[0] - this.restBaseline[0]);
    return (
      rise >= this.cfg.stand_rise_bl * scale ||
      walked >= this.cfg.walk_dist_bl * scale
    );
  }

  // Slow EMA of the resting hip position, updated only while resting so a
  // stand-up is measured against where she was resting, not where she is now.
  _updateBaseline(present, hip, now) {
    if (!present) {
      this._baseLastMs = null;
      return;
    }
    if (this.restBaseline === null || this._baseLastMs === null) {
      if (this.restBaseline === null) this.restBaseline = [...hip];
      this._baseLastMs = now;
      return;
    }
    const dtS = (now - this._baseLastMs) / 1000;
    this._baseLastMs = now;
    const alpha = 1 - Math.exp(-dtS / Math.max(this.cfg.baseline_tau_s, 1e-3));
    this.restBaseline = [
      this.restBaseline[0] + alpha * (hip[0] - this.restBaseline[0]),
      this.restBaseline[1] + alpha * (hip[1] - this.restBaseline[1]),
    ];
  }

  // Upright but stationary for a long hold means she settled somewhere new
  // (couch, chair). Anchor resets whenever the hip drifts, so walking or
  // pacing never counts as settled.
  _settledUpright(present, upright, hip, now) {
    if (!present || !upright) {
      this._settleAnchor = null;
      return false;
    }
    const scale = this._bodyScale();
    if (
      !this._settleAnchor ||
      Math.hypot(hip[0] - this._settleAnchor.pos[0], hip[1] - this._settleAnchor.pos[1]) >
        this.cfg.settle_move_bl * scale
    ) {
      this._settleAnchor = { pos: [...hip], since: now };
      return false;
    }
    return now - this._settleAnchor.since >= this.cfg.settle_upright_s * 1000;
  }

  _settle(state, hip, now, events) {
    this._setState(state, now, events, "settled");
    this.sittingFromLyingMs = null;
    this._settleAnchor = null;
    if (hip) this.restBaseline = [...hip];
    this._baseLastMs = null;
    events.push({ name: "settled", reason: "settled_" + state });
    this.episodeStartMs = null;
  }
}
