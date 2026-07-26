import { DEFAULTS, loadConfig, saveConfig, configToHash, generateTopic } from "./config.js";
import { BedWatchEngine, PostureEngine } from "./engine.js";
import { openCamera, createLandmarker, extractFeatures, sampleLuma } from "./pose.js";
import { Notifier, startHeartbeat } from "./alerts.js";
import * as ui from "./ui.js";

const cfg = loadConfig();
const video = document.getElementById("camera");
const overlay = document.getElementById("overlay");
const el = (id) => document.getElementById(id);

let engine = null;
let notifier = new Notifier(cfg);
let landmarker = null;
let stream = null;
let wakeLock = null;
let loopTimer = null;
let videoFrameCallbackId = null;
let detectionLoopRunning = false;
let lastInferenceMs = -Infinity;
let busy = false;
let lastState = "arming";
let lastLandmarks = null;
let cameraLostSince = null;
let cameraBlindNotified = false;
let lastFrameMs = null;
let cameraStalledNotified = false;

notifier.onLog = ui.log;

const zoneEditor = new ui.ZoneEditor(overlay, (points) => {
  ui.drawOverlay(overlay, video, null, points, false);
  el("zone-count").textContent = `${points.length} corner${points.length === 1 ? "" : "s"}`;
  updateSetupReady();
});

function updateSetupReady() {
  const zoneOk = cfg.mode === "zone" ? zoneEditor.points.length >= 3 : true;
  el("setup-start").disabled = !(stream && zoneOk && el("topic-input").value.trim());
}

function statusDetail() {
  if (!engine || !engine.debug) return "";
  const d = engine.debug;
  const angle = d.angle === null || d.angle === undefined ? "-" : Math.round(d.angle) + "°";
  const luma = d.luma === null || d.luma === undefined ? "-" : Math.round(d.luma);
  return `pose ${d.present ? "yes" : "no"} · in bed ${d.hipInBed ? "yes" : "no"} · torso ${angle} · light ${luma}`;
}

function refreshBanner() {
  const state = notifier.paused ? "paused" : lastState;
  ui.updateBanner(state, engine ? engine.stateSince && performanceToWall(engine.stateSince) : null, statusDetail());
}

// Engine timestamps are performance.now(); convert for wall-clock elapsed display.
function performanceToWall(perfMs) {
  return Date.now() - (performance.now() - perfMs);
}

function handleEvents(events) {
  for (const event of events) {
    if (event.name === "state_change") {
      ui.log(`state -> ${event.value} (${event.reason})`);
      continue;
    }
    ui.log(`event: ${event.name} (${event.reason})`);
    const where = event.name === "person_missing"
      ? "out of the camera view"
      : event.name === "sitting_up" ? "sitting up"
      : event.name.startsWith("bed_exit_no_return") ? "out of bed" : "up";
    const body = event.elapsed_s != null
      ? `She's still ${where} - ${event.elapsed_s} seconds. Please check now.`
      : event.minutes != null
        ? `She's still ${where} and hasn't settled - ${event.minutes} min. Please check.`
        : undefined;
    // The notifier is the single source of truth for which detector events
    // produce pushes. Unknown informational events are ignored there. This
    // avoids a second allow-list silently dropping a valid new alert event.
    notifier.notify(event.name, body).catch((err) => {
      ui.log(`ntfy ${event.name}: unexpected error (${err.message})`);
    });
  }
}

function tick(timestampMs = performance.now()) {
  if (busy || !landmarker || !video.videoWidth) return;
  busy = true;
  try {
    const ts = timestampMs;
    const result = landmarker.detectForVideo(video, ts);
    lastLandmarks = result.landmarks?.[0] ?? null;
    const features = extractFeatures(lastLandmarks, ts);
    features.luma = sampleLuma(video);
    const { state, events } = engine.update(features);
    lastState = state;
    handleEvents(events);
    ui.drawOverlay(overlay, video, lastLandmarks, cfg.mode === "zone" ? cfg.bed_polygon : null, cfg.privacy_mode);
    lastFrameMs = Date.now();
    if (cameraStalledNotified) {
      cameraStalledNotified = false;
      ui.log("camera recovered (frames resumed)");
    }
  } finally {
    busy = false;
  }
}

function stopDetectionLoop() {
  detectionLoopRunning = false;
  if (loopTimer !== null) clearInterval(loopTimer);
  loopTimer = null;
  if (videoFrameCallbackId !== null && video.cancelVideoFrameCallback) {
    video.cancelVideoFrameCallback(videoFrameCallbackId);
  }
  videoFrameCallbackId = null;
}

function startDetectionLoop() {
  stopDetectionLoop();
  detectionLoopRunning = true;
  lastInferenceMs = -Infinity;
  const intervalMs = 1000 / cfg.fps;

  // Frame callbacks react as soon as the camera produces a fresh frame. Keep
  // the timer fallback for Safari versions without this API.
  if (video.requestVideoFrameCallback) {
    const onFrame = (now) => {
      if (!detectionLoopRunning) return;
      videoFrameCallbackId = video.requestVideoFrameCallback(onFrame);
      if (now - lastInferenceMs + 1 < intervalMs) return;
      lastInferenceMs = now;
      tick(now);
    };
    videoFrameCallbackId = video.requestVideoFrameCallback(onFrame);
  } else {
    loopTimer = setInterval(() => tick(), intervalMs);
  }
}

// Watchdog: tick stops advancing lastFrameMs if the video freezes, loses its
// dimensions, or detectForVideo throws. Warn once; the paused/blind paths have
// their own handling, so skip when paused or before the first frame.
function checkCameraHealth() {
  if (!engine || !detectionLoopRunning || notifier.paused || lastFrameMs === null) return;
  if (Date.now() - lastFrameMs < cfg.camera_stall_s * 1000) return;
  lastState = "offline_or_blind";
  if (!cameraStalledNotified) {
    cameraStalledNotified = true;
    notifier.notify("camera_stopped");
  }
}

async function acquireWakeLock() {
  if (!navigator.wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => {
      if (document.visibilityState === "visible") acquireWakeLock();
    });
  } catch {
    // Denied (e.g. Low Power Mode); Auto-Lock Never is the documented fallback.
  }
}

async function startCamera() {
  stream = await openCamera(video);
  cameraLostSince = null;
  cameraBlindNotified = false;
  for (const track of stream.getVideoTracks()) {
    track.addEventListener("ended", onCameraLost);
  }
}

function onCameraLost() {
  if (cameraLostSince === null) cameraLostSince = Date.now();
  ui.log("camera stream lost, retrying");
  retryCamera();
}

async function retryCamera() {
  try {
    await startCamera();
    ui.log("camera recovered");
  } catch {
    lastState = "offline_or_blind";
    if (!cameraBlindNotified && Date.now() - cameraLostSince > 60_000) {
      cameraBlindNotified = true;
      notifier.notify("camera_blind", "Camera stream lost on bedside phone.");
    }
    setTimeout(retryCamera, 5000);
  }
}

async function startMonitoring() {
  el("start-overlay").classList.add("hidden");
  if (!stream) await startCamera();
  if (!landmarker) {
    el("model-progress").textContent = "Loading pose model...";
    landmarker = await createLandmarker(cfg.model, (msg) => {
      el("model-progress").textContent = msg;
    });
    el("model-progress").textContent = "";
  }
  engine = cfg.mode === "zone" ? new BedWatchEngine(cfg) : new PostureEngine(cfg);
  lastState = "arming";
  zoneEditor.stop();
  await acquireWakeLock();
  startHeartbeat(cfg);
  lastFrameMs = Date.now();
  cameraStalledNotified = false;
  startDetectionLoop();
  ui.log("monitoring started");
}

// ---------------------------------------------------------------- setup screen

el("enable-camera").addEventListener("click", async () => {
  try {
    await startCamera();
    el("enable-camera").classList.add("hidden");
    if (cfg.mode === "zone") {
      el("zone-controls").classList.remove("hidden");
      zoneEditor.start(cfg.bed_polygon);
    } else {
      el("posture-tip").classList.remove("hidden");
    }
  } catch (err) {
    alert("Camera access failed: " + err.message + "\nAllow camera for this site in Safari settings.");
  }
  updateSetupReady();
});

el("zone-undo").addEventListener("click", () => zoneEditor.undo());
el("zone-clear").addEventListener("click", () => zoneEditor.clear());

el("topic-generate").addEventListener("click", () => {
  el("topic-input").value = generateTopic();
  el("topic-input").dispatchEvent(new Event("input"));
});

el("topic-input").addEventListener("input", () => {
  const topic = el("topic-input").value.trim();
  el("subscribe-link").textContent = topic ? `ntfy.sh/${topic}` : "";
  updateSetupReady();
});

el("topic-copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText("https://ntfy.sh/" + el("topic-input").value.trim());
  el("topic-copy").textContent = "Copied";
  setTimeout(() => (el("topic-copy").textContent = "Copy link"), 1500);
});

el("topic-test").addEventListener("click", () => {
  notifier.cfg.topic = el("topic-input").value.trim();
  notifier.notify("test");
});

el("setup-start").addEventListener("click", async () => {
  if (cfg.mode === "zone") cfg.bed_polygon = [...zoneEditor.points];
  cfg.topic = el("topic-input").value.trim();
  saveConfig(cfg);
  notifier = new Notifier(cfg);
  notifier.onLog = ui.log;
  ui.showScreen("monitor");
  await startMonitoring();
});

// -------------------------------------------------------------- monitor screen

el("start-monitoring").addEventListener("click", () => startMonitoring());

el("pause-15").addEventListener("click", () => notifier.pause(15));
el("pause-60").addEventListener("click", () => notifier.pause(60));
el("pause-hold").addEventListener("click", () => notifier.pause(null));
el("resume").addEventListener("click", () => notifier.resume());
el("dim").addEventListener("click", () => ui.setDim(true));
el("monitor-test").addEventListener("click", () => notifier.notify("test"));
el("dim-overlay").addEventListener("click", () => ui.setDim(false));
el("open-settings").addEventListener("click", () => {
  fillSettings();
  ui.showScreen("settings");
});

// ------------------------------------------------------------- settings screen

function fillSettings() {
  el("set-topic").value = cfg.topic;
  el("set-mode").value = cfg.mode;
  el("set-situp-toggle").checked = cfg.alerts.sitting_up;
  el("set-back-toggle").checked = cfg.alerts.back_in_bed;
  el("set-reminder-toggle").checked = cfg.alerts.no_return_reminder;
  el("set-fall-toggle").checked = cfg.alerts.possible_fall;
  el("set-situp-deg").value = cfg.sit_up_torso_deg;
  el("set-situp-deb").value = cfg.sit_up_debounce_s;
  el("set-exit-deb").value = cfg.bed_exit_debounce_s;
  el("set-fps").value = cfg.fps;
  el("set-dark").value = cfg.dark_luma;
  el("set-model").value = cfg.model;
  el("set-privacy").checked = cfg.privacy_mode;
  el("set-heartbeat").value = cfg.heartbeat_url;
}

el("settings-save").addEventListener("click", () => {
  cfg.topic = el("set-topic").value.trim();
  cfg.mode = el("set-mode").value;
  cfg.alerts.sitting_up = el("set-situp-toggle").checked;
  cfg.alerts.back_in_bed = el("set-back-toggle").checked;
  cfg.alerts.no_return_reminder = el("set-reminder-toggle").checked;
  cfg.alerts.possible_fall = el("set-fall-toggle").checked;
  cfg.sit_up_torso_deg = Number(el("set-situp-deg").value) || 40;
  cfg.sit_up_debounce_s = Number(el("set-situp-deb").value) || DEFAULTS.sit_up_debounce_s;
  cfg.bed_exit_debounce_s = Number(el("set-exit-deb").value) || DEFAULTS.bed_exit_debounce_s;
  cfg.fps = Math.min(10, Math.max(3, Number(el("set-fps").value) || DEFAULTS.fps));
  cfg.dark_luma = Number(el("set-dark").value) || 25;
  cfg.dark_luma_recover = cfg.dark_luma + 10;
  cfg.model = el("set-model").value;
  cfg.privacy_mode = el("set-privacy").checked;
  cfg.heartbeat_url = el("set-heartbeat").value.trim();
  saveConfig(cfg);
  location.reload();
});

el("settings-cancel").addEventListener("click", () => ui.showScreen("monitor"));

el("redraw-zone").addEventListener("click", () => {
  stopDetectionLoop();
  ui.showScreen("setup");
  el("enable-camera").classList.toggle("hidden", Boolean(stream));
  el("zone-controls").classList.toggle("hidden", !stream);
  if (stream) zoneEditor.start(cfg.bed_polygon);
  el("topic-input").value = cfg.topic;
  el("topic-input").dispatchEvent(new Event("input"));
});

el("backup-copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText(configToHash(cfg));
  el("backup-copy").textContent = "Copied";
  setTimeout(() => (el("backup-copy").textContent = "Copy backup link"), 1500);
});

el("settings-test").addEventListener("click", () => notifier.notify("test"));
el("log-copy").addEventListener("click", () => ui.copyLog());

// ----------------------------------------------------------------------- boot

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    acquireWakeLock();
    if (video.paused && video.srcObject) video.play();
    // Backgrounding throttles timers on iOS; don't count that gap as a stall.
    if (lastFrameMs !== null) lastFrameMs = Date.now();
  }
});

setInterval(() => {
  refreshBanner();
  checkCameraHealth();
}, 1000);

if (cfg.topic && (cfg.mode !== "zone" || cfg.bed_polygon.length >= 3)) {
  saveConfig(cfg);
  ui.showScreen("monitor");
  el("start-overlay").classList.remove("hidden");
} else {
  ui.showScreen("setup");
  el("topic-input").value = cfg.topic || generateTopic();
  el("topic-input").dispatchEvent(new Event("input"));
}
ui.renderLog();
