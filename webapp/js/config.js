// Threshold names match config/zones.example.json so tuning stays comparable
// with the Python lab.
export const DEFAULTS = {
  smoothing_tau_s: 0.15,
  outlier_jump_bl: 1.5,
  outlier_accept_frames: 5,
  floor_level_torso_angle_deg: 55,
  floor_line_y: 0.65,

  sit_up_torso_deg: 40,
  sit_up_debounce_s: 1.5,
  bed_exit_debounce_s: 2.0,
  untracked_exit_debounce_s: 5.0,
  track_link_s: 10,
  lie_back_debounce_s: 5.0,
  pose_lost_in_bed_s: 30,
  bed_return_debounce_s: 15,
  floor_level_min_s: 2.0,
  floor_clear_s: 3.0,
  arming_s: 60,
  no_return_reminder_s: 900,
  up_repeat_s: 60,
  up_urgent_after_s: 300,
  camera_stall_s: 60,
  core_confidence_min: 0.5,
  dark_luma: 25,
  dark_luma_recover: 35,
  dark_debounce_s: 10,

  mode: "posture",
  stand_debounce_s: 2.0,
  stand_rise_bl: 0.6,
  walk_dist_bl: 1.5,
  baseline_tau_s: 20,
  settle_flat_s: 10,
  settle_upright_s: 60,
  settle_move_bl: 0.5,

  fps: 5,
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
  const cfg = { ...DEFAULTS, ...(stored || {}) };
  cfg.alerts = { ...DEFAULTS.alerts, ...((stored && stored.alerts) || {}) };
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
