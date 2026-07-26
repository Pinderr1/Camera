// Threshold names match config/zones.example.json so tuning stays comparable
// with the Python lab.
export const DEFAULTS = {
  config_version: 4,
  // Fast-response profile: at 10 FPS these holds confirm a real transition in
  // roughly half a second while still rejecting a single bad pose frame.
  smoothing_tau_s: 0.08,
  outlier_jump_bl: 1.5,
  outlier_accept_frames: 2,
  floor_level_torso_angle_deg: 55,
  floor_line_y: 0.65,

  sit_up_torso_deg: 40,
  sit_up_debounce_s: 0.4,
  sit_up_repeat_s: 5,
  bed_exit_debounce_s: 0.5,
  untracked_exit_debounce_s: 1.0,
  person_missing_debounce_s: 1.0,
  person_missing_repeat_s: 2,
  track_link_s: 10,
  lie_back_debounce_s: 5.0,
  bed_return_debounce_s: 15,
  floor_level_min_s: 2.0,
  floor_clear_s: 3.0,
  arming_s: 60,
  no_return_reminder_s: 900,
  up_repeat_s: 30,
  up_urgent_after_s: 300,
  camera_stall_s: 60,
  core_confidence_min: 0.5,
  dark_luma: 25,
  dark_luma_recover: 35,
  dark_debounce_s: 10,

  mode: "posture",
  stand_debounce_s: 0.5,
  stand_rise_bl: 0.4,
  walk_dist_bl: 0.9,
  baseline_tau_s: 20,
  settle_flat_s: 10,
  settle_upright_s: 60,
  settle_move_bl: 0.5,

  fps: 10,
  model: "lite",
  topic: "",
  heartbeat_url: "",
  privacy_mode: false,
  bed_polygon: [],
  alerts: {
    sitting_up: true,
    back_in_bed: true,
    no_return_reminder: true,
    possible_fall: true,
  },
};

const STORAGE_KEY = "bedwatch_config";

export function loadConfig() {
  let stored = null;
  try {
    stored = JSON.parse(localStorage.getItem(STORAGE_KEY));
  } catch {
    stored = null;
  }
  if (!stored) stored = configFromHash();
  const storedVersion = Number(stored?.config_version || 1);
  const cfg = { ...DEFAULTS, ...(stored || {}) };
  cfg.alerts = { ...DEFAULTS.alerts, ...((stored && stored.alerts) || {}) };
  if (storedVersion < 2) {
    // v2 makes away alerts deliberately persistent. Existing phones saved
    // the old 60-second default, so a plain defaults merge would leave them
    // on the old cadence forever.
    cfg.up_repeat_s = 30;
  }
  if (storedVersion < 3) {
    // Upgrade already-configured bedside phones to the fast-response profile;
    // otherwise localStorage would keep the old 1.5-2 second holds and 5 FPS.
    cfg.smoothing_tau_s = DEFAULTS.smoothing_tau_s;
    cfg.outlier_accept_frames = DEFAULTS.outlier_accept_frames;
    cfg.sit_up_debounce_s = DEFAULTS.sit_up_debounce_s;
    cfg.bed_exit_debounce_s = DEFAULTS.bed_exit_debounce_s;
    cfg.untracked_exit_debounce_s = DEFAULTS.untracked_exit_debounce_s;
    cfg.person_missing_debounce_s = DEFAULTS.person_missing_debounce_s;
    cfg.stand_debounce_s = DEFAULTS.stand_debounce_s;
    cfg.stand_rise_bl = DEFAULTS.stand_rise_bl;
    cfg.walk_dist_bl = DEFAULTS.walk_dist_bl;
    cfg.fps = DEFAULTS.fps;
  }
  if (storedVersion < 4) {
    // v4 applies the requested repeat cadence to already-configured phones.
    // These values were not editable in the UI, so preserving old values
    // would leave an upgraded phone on the obsolete cadence.
    cfg.sit_up_repeat_s = DEFAULTS.sit_up_repeat_s;
    cfg.person_missing_repeat_s = DEFAULTS.person_missing_repeat_s;
  }
  cfg.config_version = DEFAULTS.config_version;
  return cfg;
}

export function saveConfig(cfg) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
}

// Safari can evict script-writable storage after 7 days of no use; the hash
// backup link survives as a bookmark.
export function configToHash(cfg) {
  const url = new URL(location.href);
  url.hash = "cfg=" + encodeURIComponent(JSON.stringify(cfg));
  return url.toString();
}

export function configFromHash() {
  const match = location.hash.match(/^#cfg=(.+)$/);
  if (!match) return null;
  try {
    return JSON.parse(decodeURIComponent(match[1]));
  } catch {
    return null;
  }
}

export function generateTopic() {
  const chars = "abcdefghjkmnpqrstuvwxyz23456789";
  let suffix = "";
  const random = new Uint8Array(8);
  crypto.getRandomValues(random);
  for (const byte of random) suffix += chars[byte % chars.length];
  return "bedwatch-" + suffix;
}
