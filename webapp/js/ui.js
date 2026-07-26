import { SKELETON_CONNECTIONS, VISIBILITY_MIN } from "./pose.js";

const BANNER = {
  arming: { label: "STARTING UP", cls: "arming" },
  in_bed: { label: "IN BED", cls: "ok" },
  lying: { label: "LYING DOWN", cls: "ok" },
  sitting: { label: "SITTING", cls: "ok" },
  sitting_up: { label: "SITTING UP", cls: "warn" },
  bed_exit: { label: "OUT OF BED", cls: "alert" },
  up: { label: "UP - MOVING", cls: "alert" },
  possible_fall: { label: "POSSIBLE FALL", cls: "alert" },
  person_missing: { label: "PERSON NOT VISIBLE", cls: "alert" },
  offline_or_blind: { label: "CAMERA CAN'T SEE", cls: "blind" },
  paused: { label: "PAUSED", cls: "paused" },
};

const LOG_KEY = "bedwatch_log";
const LOG_LIMIT = 200;

export function showScreen(name) {
  document.body.dataset.screen = name;
}

export function updateBanner(state, sinceMs, detail) {
  const banner = document.getElementById("banner");
  const info = BANNER[state] || { label: state, cls: "arming" };
  banner.className = "banner " + info.cls;
  banner.querySelector(".banner-label").textContent = info.label;
  const elapsed = sinceMs === null ? "" : formatElapsed(Date.now() - sinceMs);
  banner.querySelector(".banner-since").textContent = elapsed;
  document.getElementById("status-detail").textContent = detail || "";
  const dot = document.getElementById("dim-dot");
  if (dot) dot.className = "dim-dot " + info.cls;
}

function formatElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function loadLog() {
  try {
    return JSON.parse(localStorage.getItem(LOG_KEY)) || [];
  } catch {
    return [];
  }
}

export function log(message) {
  const entries = loadLog();
  entries.unshift({ t: new Date().toISOString(), m: message });
  entries.length = Math.min(entries.length, LOG_LIMIT);
  localStorage.setItem(LOG_KEY, JSON.stringify(entries));
  renderLog(entries);
}

export function renderLog(entries = loadLog()) {
  const list = document.getElementById("event-log");
  if (!list) return;
  list.innerHTML = "";
  for (const entry of entries.slice(0, 50)) {
    const item = document.createElement("li");
    const time = entry.t.slice(11, 19);
    item.textContent = `${time}  ${entry.m}`;
    list.appendChild(item);
  }
}

export async function copyLog() {
  await navigator.clipboard.writeText(JSON.stringify(loadLog(), null, 1));
}

// Bed-zone editor: taps on the overlay canvas add normalized polygon corners.
export class ZoneEditor {
  constructor(canvas, onChange) {
    this.canvas = canvas;
    this.onChange = onChange;
    this.points = [];
    this.active = false;
    canvas.addEventListener("pointerdown", (event) => {
      if (!this.active) return;
      const rect = canvas.getBoundingClientRect();
      this.points.push([
        (event.clientX - rect.left) / rect.width,
        (event.clientY - rect.top) / rect.height,
      ]);
      this.onChange(this.points);
    });
  }

  start(points) {
    this.points = [...(points || [])];
    this.active = true;
    this.onChange(this.points);
  }

  undo() {
    this.points.pop();
    this.onChange(this.points);
  }

  clear() {
    this.points = [];
    this.onChange(this.points);
  }

  stop() {
    this.active = false;
  }
}

export function drawOverlay(canvas, videoEl, landmarks, polygon, privacyMode) {
  if (videoEl.videoWidth && canvas.width !== videoEl.videoWidth) {
    canvas.width = videoEl.videoWidth;
    canvas.height = videoEl.videoHeight;
  }
  const ctx = canvas.getContext("2d");
  const { width, height } = canvas;
  ctx.clearRect(0, 0, width, height);
  if (privacyMode) {
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, width, height);
  }

  if (polygon && polygon.length) {
    ctx.beginPath();
    polygon.forEach(([x, y], i) => {
      if (i === 0) ctx.moveTo(x * width, y * height);
      else ctx.lineTo(x * width, y * height);
    });
    if (polygon.length > 2) ctx.closePath();
    ctx.strokeStyle = "#2ecc71";
    ctx.lineWidth = 3;
    ctx.stroke();
    ctx.fillStyle = "rgba(46, 204, 113, 0.12)";
    ctx.fill();
    ctx.fillStyle = "#2ecc71";
    for (const [x, y] of polygon) {
      ctx.beginPath();
      ctx.arc(x * width, y * height, 6, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  if (landmarks) {
    ctx.strokeStyle = "#4aa3ff";
    ctx.lineWidth = 3;
    for (const [a, b] of SKELETON_CONNECTIONS) {
      const pa = landmarks[a];
      const pb = landmarks[b];
      if ((pa?.visibility ?? 0) < VISIBILITY_MIN || (pb?.visibility ?? 0) < VISIBILITY_MIN) continue;
      ctx.beginPath();
      ctx.moveTo(pa.x * width, pa.y * height);
      ctx.lineTo(pb.x * width, pb.y * height);
      ctx.stroke();
    }
  }
}

export function setDim(on) {
  document.getElementById("dim-overlay").classList.toggle("visible", on);
}
