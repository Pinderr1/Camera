"use strict";
// Setup: camera onboarding wizard, zone/mask editor, property map, camera
// connections and category filters. Uses $, esc, api, toast, state from app.js.

const setup = {
  config: null,
  statuses: {},
  zoneCamera: null,
  zones: [],
  masks: [],
  selected: null, // {kind: "zone"|"mask", index}
  drafting: null, // {kind, points: []}
  drag: null,
  zonesDirty: false,
  map: { cameras: {}, footprints: {}, drafting: null, drag: null, dirty: false },
  links: [],
};

const ZONE_HELP = {
  driveway: "vehicles arrive and park",
  entrance: "doors, porches, gates people use",
  yard: "private open ground",
  sidewalk: "public path: activity there never counts as on-property",
  street: "public road: ignored by default rules",
  perimeter: "fence lines, side gates",
  restricted: "nobody should be here",
  ignore: "detections here are dropped (trees, TV screens, neighbours)",
  general: "anything else",
};

// ---- loading and navigation --------------------------------------------------

document.querySelector('[data-tab="setup"]').addEventListener("click", () => loadSetup());
document.querySelectorAll(".subtabs button").forEach((button) =>
  button.addEventListener("click", () => {
    document.querySelectorAll(".subtabs button").forEach((b) => b.classList.toggle("active", b === button));
    document.querySelectorAll(".sub").forEach((panel) => panel.classList.toggle("active", panel.id === `sub-${button.dataset.sub}`));
    if (button.dataset.sub === "zones") loadZoneFrame();
  }),
);

function showSub(name) {
  document.querySelector(`.subtabs button[data-sub="${name}"]`).click();
}

async function loadSetup() {
  try {
    setup.config = await api("/config");
    const cameras = await api("/cameras");
    setup.statuses = Object.fromEntries(cameras.map((camera) => [camera.id, camera.status || {}]));
  } catch (error) {
    toast("Could not load setup", error.message);
    return;
  }
  renderCameraList();
  fillCameraSelects();
  if (!setup.zonesDirty) loadZonesFromConfig();
  if (!setup.map.dirty) loadMapFromConfig();
  setup.links = structuredClone(setup.config.topology?.links || []);
  renderLinks();
  renderCategories();
  loadDecodeInfo();
}

function onSetupConfigChanged() {
  if (document.querySelector("#tab-setup.active")) loadSetup();
}

function cameraById(id) {
  return setup.config.cameras.find((camera) => camera.id === id);
}

// ---- camera list ---------------------------------------------------------------

function sourceSummary(source) {
  if (source.type === "rtsp" || source.type === "http") {
    const template = source.template && source.template !== "custom" ? setup.config.catalog.templates[source.template] : "custom path";
    const channel = source.channel ? ` · channel ${source.channel}` : "";
    return `${source.type.toUpperCase()} ${source.host || ""}${source.port ? ":" + source.port : ""} · ${template || ""}${channel}`;
  }
  if (source.type === "usb" || source.type === "capture") return `Local device #${source.index}`;
  if (source.type === "file") return `Recording ${source.file}`;
  if (source.type === "simulated") return `Simulated (${source.scenario})`;
  return source.type;
}

function renderCameraList() {
  const cameras = setup.config.cameras;
  $("#setup-camera-list").innerHTML = cameras
    .map((camera, index) => {
      const status = setup.statuses[camera.id] || {};
      const pills = [
        camera.enabled === false ? `<span class="pill">disabled</span>` : status.connected ? `<span class="pill ok">online</span>` : `<span class="pill bad">${esc(status.error_kind || "offline")}</span>`,
        status.fps ? `<span class="pill">${status.fps} fps processed</span>` : "",
        status.resolution ? `<span class="pill">${status.resolution.join("×")}</span>` : "",
        status.stream && status.stream !== "main" ? `<span class="pill">${esc(status.stream)} stream</span>` : "",
        status.hw_decode_active ? `<span class="pill">GPU decode</span>` : "",
        status.reconnects ? `<span class="pill">${status.reconnects} reconnects</span>` : "",
        ...(status.scene || []).map((name) => `<span class="pill">${esc(name.replaceAll("_", " "))}</span>`),
      ].join("");
      return `<li class="cam-row">
        <div><strong>${esc(camera.name)}</strong> <span class="muted small">${esc(camera.id)}</span>
          <div class="small muted">${esc(sourceSummary(camera.source))}</div><div>${pills}</div>
          ${status.error ? `<div class="small" style="color: var(--high)">${esc(status.error)}</div>` : ""}</div>
        <div class="button-row tight">
          <button data-move="${index}" data-dir="-1" title="Move up" ${index === 0 ? "disabled" : ""}>↑</button>
          <button data-move="${index}" data-dir="1" title="Move down" ${index === cameras.length - 1 ? "disabled" : ""}>↓</button>
          <button data-zones="${esc(camera.id)}">Zones</button>
          <button data-edit="${esc(camera.id)}">Edit</button>
          <button data-delete="${esc(camera.id)}" class="danger">Delete</button>
        </div></li>`;
    })
    .join("");
  document.querySelectorAll("[data-move]").forEach((button) =>
    button.addEventListener("click", async () => {
      const order = cameras.map((camera) => camera.id);
      const from = Number(button.dataset.move);
      const to = from + Number(button.dataset.dir);
      [order[from], order[to]] = [order[to], order[from]];
      await saveConfig("/config/camera-order", { order }, "Order saved");
    }),
  );
  document.querySelectorAll("[data-edit]").forEach((button) => button.addEventListener("click", () => openWizard(cameraById(button.dataset.edit))));
  document.querySelectorAll("[data-zones]").forEach((button) =>
    button.addEventListener("click", () => {
      selectZoneCamera(button.dataset.zones);
      showSub("zones");
    }),
  );
  document.querySelectorAll("[data-delete]").forEach((button) =>
    button.addEventListener("click", async () => {
      if (!confirm(`Delete camera ${button.dataset.delete}? Its zones, stored password and connections are removed. Recorded history is kept.`)) return;
      try {
        const result = await api(`/config/cameras/${encodeURIComponent(button.dataset.delete)}`, { method: "DELETE" });
        toast("Camera deleted", (result.notes || []).join(" "), "medium");
        await loadSetup();
      } catch (error) {
        toast("Could not delete", error.message);
      }
    }),
  );
}

async function saveConfig(path, body, message) {
  try {
    const result = await api(path, { method: "PUT", body: JSON.stringify(body) });
    toast(message, (result && result.notes || []).join(" "), "medium");
    await loadSetup();
    return result;
  } catch (error) {
    toast("Not saved", error.message);
    throw error;
  }
}

async function loadDecodeInfo() {
  try {
    const info = await api("/onboarding/decode");
    $("#decode-info").textContent = info.hw_decode_api
      ? `Hardware video decoding available (${info.hw_decode_api}); each camera tries it first and falls back to software. ${info.ffmpeg_backend ? "" : "FFmpeg backend missing."}`
      : "No hardware decoding in this OpenCV build: streams are decoded on the CPU. Prefer camera sub-streams.";
  } catch {
    $("#decode-info").textContent = "Decoder information unavailable.";
  }
}

$("#add-camera").addEventListener("click", () => openWizard(null));

// ---- onboarding wizard -----------------------------------------------------

const SOURCE_KINDS = [
  { id: "rtsp", title: "IP camera (RTSP)", text: "Most network cameras. Pick the brand to get the right stream paths." },
  { id: "dvr", title: "DVR / NVR channel", text: "A recorder serving several cameras: brand and channel number." },
  { id: "onvif", title: "Find on network (ONVIF)", text: "Scan the local network and read stream settings automatically." },
  { id: "http", title: "HTTP snapshot or MJPEG", text: "For devices without RTSP: a JPEG snapshot URL or an MJPEG stream." },
  { id: "usb", title: "USB webcam or capture device", text: "Includes analog-to-USB capture cards for older camera systems." },
  { id: "file", title: "Recorded footage", text: "A video file on the Sentinel computer." },
  { id: "simulated", title: "Simulated camera", text: "Scripted scenario for testing without hardware." },
];

const wizard = { step: 1, kind: null, camera: null, values: {}, result: null, draftId: null, onvif: { devices: [], inspected: null } };

function openWizard(camera) {
  Object.assign(wizard, { step: camera ? 2 : 1, camera, result: null, draftId: null, onvif: { devices: [], inspected: null } });
  wizard.kind = camera ? kindFromSource(camera.source) : null;
  wizard.values = camera ? valuesFromSource(camera) : { port: 554, channel: 1, prefer_substream: true, hw_decode: "auto", transport: "tcp", template: "hikvision", mode: "snapshot", fps: 5, enabled: true };
  $("#wizard-title").textContent = camera ? `Edit ${camera.name}` : "Add camera";
  renderWizard();
  $("#wizard").showModal();
}

function kindFromSource(source) {
  if (source.type === "rtsp") return source.channel && source.channel > 1 ? "dvr" : "rtsp";
  if (source.type === "capture") return "usb";
  return source.type;
}

function valuesFromSource(camera) {
  const s = camera.source;
  return {
    name: camera.name, fps: camera.fps || 5, enabled: camera.enabled !== false,
    host: s.host, port: s.port, template: s.template || "custom", channel: s.channel || 1, username: s.username,
    prefer_substream: s.prefer_substream !== false, hw_decode: s.hw_decode || "auto", transport: "tcp",
    index: s.index ?? 0, backend: s.backend || "auto", mode: s.mode || "snapshot", file: "", scenario: "",
    resolution: Array.isArray(s.resolution) ? s.resolution.join("x") : "",
    keepAddress: true,
  };
}

$("#wizard-close").addEventListener("click", () => $("#wizard").close());
$("#wizard-back").addEventListener("click", () => {
  collectStep();
  wizard.step = Math.max(wizard.camera ? 2 : 1, wizard.step - 1);
  renderWizard();
});
$("#wizard-next").addEventListener("click", () => wizardNext());

function renderWizard() {
  $("#wizard-error").textContent = "";
  document.querySelectorAll("#wizard-steps li").forEach((li) => {
    const step = Number(li.dataset.step);
    li.classList.toggle("done", step < wizard.step);
    li.classList.toggle("current", step === wizard.step);
  });
  $("#wizard-back").disabled = wizard.step === (wizard.camera ? 2 : 1);
  $("#wizard-next").textContent = wizard.step === 3 ? (wizard.result?.ok ? "Next" : "Save anyway…") : wizard.step === 4 ? "Save camera" : "Next";
  const body = $("#wizard-body");
  if (wizard.step === 1) body.innerHTML = renderKindChoice();
  if (wizard.step === 2) body.innerHTML = renderConnectionForm();
  if (wizard.step === 3) body.innerHTML = renderTestResult();
  if (wizard.step === 4) body.innerHTML = renderSaveForm();
  bindWizardStep();
}

function renderKindChoice() {
  return `<div class="choice-grid">${SOURCE_KINDS.map(
    (kind) => `<button type="button" class="choice ${wizard.kind === kind.id ? "selected" : ""}" data-kind="${kind.id}"><strong>${esc(kind.title)}</strong><span class="small muted">${esc(kind.text)}</span></button>`,
  ).join("")}</div>
  <p class="small muted">Not every DVR offers RTSP or ONVIF. If yours does not, try an HTTP snapshot URL, or connect its video output to a USB capture device.</p>
  <p class="small">Don't know the address, or can't reach the recorder? <button type="button" id="wizard-open-ts">Open the network troubleshooter</button></p>`;
}

function field(label, name, type = "text", extra = "") {
  const value = wizard.values[name] ?? "";
  if (type === "checkbox") return `<label class="field checkbox"><input type="checkbox" name="${name}" ${value ? "checked" : ""}> ${esc(label)}</label>`;
  return `<label class="field">${esc(label)} <input type="${type}" name="${name}" value="${esc(value)}" ${extra}></label>`;
}

function select(label, name, options) {
  const value = wizard.values[name];
  return `<label class="field">${esc(label)} <select name="${name}">${options
    .map(([key, text]) => `<option value="${esc(key)}" ${String(key) === String(value) ? "selected" : ""}>${esc(text)}</option>`)
    .join("")}</select></label>`;
}

function renderConnectionForm() {
  const editing = Boolean(wizard.camera);
  const keep = editing ? ` placeholder="leave blank to keep the stored value"` : "";
  const templates = Object.entries(setup.config.catalog.templates);
  const kind = wizard.kind;
  if (kind === "rtsp" || kind === "dvr") {
    const custom = wizard.values.template === "custom";
    return `<div class="grid2">${field("Camera / recorder IP address or host name", "host", "text", 'required autocomplete="off"')}${field("RTSP port", "port", "number")}</div>
      <div class="grid2">${select("Brand (stream path template)", "template", templates)}${field("Channel", "channel", "number", 'min="1" max="64"')}</div>
      ${custom ? `<div class="grid2">${field("Main stream path, e.g. /stream1", "path", "text", keep)}${field("Sub-stream path (optional)", "sub_path", "text", keep)}</div>` : ""}
      <div class="grid2">${field("Username", "username", "text", 'autocomplete="off"')}${field("Password", "password", "password", `autocomplete="new-password"${keep}`)}</div>
      <div class="grid2">${select("Transport", "transport", [["tcp", "TCP (reliable)"], ["udp", "UDP (lower latency)"]])}${select("Hardware decoding", "hw_decode", [["auto", "Auto (try GPU)"], ["off", "Off (CPU)"]])}</div>
      ${field("Use the sub-stream for AI processing when available (recommended)", "prefer_substream", "checkbox")}
      <p class="small muted">The password is stored encrypted on the Sentinel computer (${esc(setup.config.credential_backend)}) and is never shown again.</p>`;
  }
  if (kind === "onvif") {
    const devices = wizard.onvif.devices;
    const inspected = wizard.onvif.inspected;
    return `<div class="button-row"><button type="button" id="onvif-scan">Scan local network</button><span class="small muted" id="onvif-scan-status"></span></div>
      ${devices.length ? `<ul class="list compact">${devices.map((d, i) => `<li><label><input type="radio" name="onvif_pick" value="${i}" ${wizard.values.xaddr === d.xaddr ? "checked" : ""}> ${esc(d.name || "ONVIF device")} <span class="muted">${esc(d.hardware || "")} · ${esc(d.host || "")}</span></label></li>`).join("")}</ul>` : ""}
      ${field("Device service address (from scan, or http://IP/onvif/device_service)", "xaddr")}
      <div class="grid2">${field("Username", "username", "text", 'autocomplete="off"')}${field("Password", "password", "password", 'autocomplete="new-password"')}</div>
      <div class="button-row"><button type="button" id="onvif-inspect">Read stream profiles</button></div>
      ${inspected ? `<div class="small">${inspected.ok
        ? `<strong>${esc(inspected.device.Manufacturer)} ${esc(inspected.device.Model)}</strong> · firmware ${esc(inspected.device.FirmwareVersion)}<br>${Object.entries(inspected.streams).map(([which, p]) => `${which}: ${p.width}×${p.height} ${esc(p.encoding)} ${p.fps ? p.fps + " fps" : ""}`).join("<br>")}`
        : `<span style="color: var(--high)">${esc(inspected.message)}</span>`}</div>` : ""}
      <p class="small muted">Discovery only finds ONVIF devices on this network segment. Many DVRs do not support ONVIF; use a brand template instead.</p>`;
  }
  if (kind === "http") {
    return `<div class="grid2">${field("Host or IP", "host")}${field("Port", "port", "number")}</div>
      ${field("Path, e.g. /cgi-bin/snapshot.cgi or /video.mjpg", "path", "text", keep)}
      ${select("Type", "mode", [["snapshot", "JPEG snapshot (polled at the camera fps)"], ["stream", "MJPEG stream"]])}
      <div class="grid2">${field("Username", "username", "text", 'autocomplete="off"')}${field("Password", "password", "password", `autocomplete="new-password"${keep}`)}</div>`;
  }
  if (kind === "usb") {
    return `<div class="button-row"><button type="button" id="usb-scan">Find connected devices</button><span class="small muted" id="usb-status">Opening a device may briefly turn on its light.</span></div>
      <div id="usb-devices"></div>
      <div class="grid2">${field("Device index", "index", "number", 'min="0" max="20"')}${select("Requested resolution", "resolution", [["", "Device default"], ["640x480", "640×480"], ["1280x720", "1280×720"], ["1920x1080", "1920×1080"]])}</div>
      ${select("Capture API", "backend", [["auto", "Automatic"], ["dshow", "DirectShow (Windows, most capture cards)"], ["msmf", "Media Foundation (Windows)"], ["v4l2", "Video4Linux (Linux)"]])}`;
  }
  if (kind === "file") return field("Video file path on the Sentinel computer", "file", "text", editing ? keep : "");
  if (kind === "simulated") return field("Scenario file (relative to the config folder)", "scenario", "text", editing ? keep : 'placeholder="scenarios/demo_loop.json"');
  return "";
}

// Shared with the troubleshooter (troubleshoot.js).
const NETWORK_FAILURES = ["unreachable", "timeout", "port_closed", "dns_failed", "not_rtsp"];

function testResultHtml(r) {
  const streams = Object.entries(r.streams || {});
  return `<p><strong style="color: ${r.ok ? "var(--low)" : "var(--high)"}">${r.ok ? "Connected" : "Connection failed"}</strong> ${esc(r.ok ? "" : r.message)}</p>
    ${streams.length ? `<div class="scroll"><table class="table"><tr><th>Stream</th><th>Result</th><th>Resolution</th><th>Measured fps</th><th>Codec</th><th>Decoding</th></tr>${streams
      .map(([which, s]) => `<tr><td>${which}${r.ai_stream === which ? " (AI)" : ""}</td><td>${esc(s.ok ? "ok" : s.kind)}</td><td>${s.resolution ? s.resolution.join("×") : "–"}</td><td>${s.measured_fps ?? "–"}</td><td>${esc(s.codec || "–")}</td><td>${s.hw_decode_active ? "GPU" : s.ok ? "CPU" : "–"}</td></tr>`)
      .join("")}</table></div>` : ""}
    ${(r.diagnostics || []).length ? `<ul class="small">${r.diagnostics.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
    ${r.snapshot_jpeg_b64 ? `<img class="preview" alt="Preview" src="data:image/jpeg;base64,${r.snapshot_jpeg_b64}">` : ""}`;
}

function renderTestResult() {
  const r = wizard.result;
  if (!r) return `<p>Testing connection…</p>`;
  const failedKinds = [r.kind, ...Object.values(r.streams || {}).map((s) => s.kind)];
  const networkProblem = !r.ok && wizard.values.host && failedKinds.some((kind) => NETWORK_FAILURES.includes(kind));
  return `${testResultHtml(r)}
    <div class="button-row"><button type="button" id="wizard-retest">Test again</button>
      ${networkProblem ? `<button type="button" id="wizard-troubleshoot">Troubleshoot this connection</button>` : ""}</div>`;
}

function renderSaveForm() {
  return `${field("Camera name", "name", "text", 'maxlength="60" required')}
    <div class="grid2">${field("Frames analysed per second (1–15)", "fps", "number", 'min="1" max="15"')}${field("Enabled", "enabled", "checkbox")}</div>
    <p class="small muted">5 fps is enough for people and vehicles and keeps CPU/GPU load manageable with several cameras.</p>`;
}

function bindWizardStep() {
  document.querySelectorAll("[data-kind]").forEach((button) =>
    button.addEventListener("click", () => {
      wizard.kind = button.dataset.kind;
      if (wizard.kind === "dvr") wizard.values.template = wizard.values.template || "hikvision";
      if (wizard.kind === "http") wizard.values.port = 80;
      if (wizard.kind === "rtsp" || wizard.kind === "dvr") wizard.values.port = wizard.values.port === 80 ? 554 : wizard.values.port;
      renderWizard();
    }),
  );
  const template = document.querySelector('#wizard-body select[name="template"]');
  if (template) template.addEventListener("change", () => { collectStep(); renderWizard(); });
  const retest = $("#wizard-retest");
  if (retest) retest.addEventListener("click", () => runTest());
  const troubleshoot = $("#wizard-troubleshoot");
  if (troubleshoot) troubleshoot.addEventListener("click", () => {
    $("#wizard").close();
    openTroubleshooter({ host: wizard.values.host, template: wizard.values.template, port: wizard.values.port, autorun: true });
  });
  const openTs = $("#wizard-open-ts");
  if (openTs) openTs.addEventListener("click", () => { $("#wizard").close(); openTroubleshooter({}); });
  const scan = $("#onvif-scan");
  if (scan) scan.addEventListener("click", async () => {
    $("#onvif-scan-status").textContent = "Scanning for 3 seconds…";
    try {
      collectStep();
      wizard.onvif.devices = await api("/onboarding/onvif/discover", { method: "POST", body: "{}" });
      renderWizard();
      if (!wizard.onvif.devices.length) $("#onvif-scan-status").textContent = "No ONVIF devices answered. Enter the address manually or use a brand template.";
    } catch (error) {
      $("#onvif-scan-status").textContent = error.message;
    }
  });
  document.querySelectorAll('input[name="onvif_pick"]').forEach((radio) =>
    radio.addEventListener("change", () => {
      collectStep();
      wizard.values.xaddr = wizard.onvif.devices[Number(radio.value)].xaddr;
      renderWizard();
    }),
  );
  const inspect = $("#onvif-inspect");
  if (inspect) inspect.addEventListener("click", async () => {
    collectStep();
    try {
      wizard.onvif.inspected = await api("/onboarding/onvif/inspect", {
        method: "POST", body: JSON.stringify({ xaddr: wizard.values.xaddr, username: wizard.values.username || "", password: wizard.values.password || "" }),
      });
      wizard.draftId = wizard.onvif.inspected.ok ? wizard.onvif.inspected.draft_id : null;
    } catch (error) {
      wizard.onvif.inspected = { ok: false, message: error.message };
    }
    renderWizard();
  });
  const usb = $("#usb-scan");
  if (usb) usb.addEventListener("click", async () => {
    $("#usb-status").textContent = "Looking for devices…";
    try {
      const devices = await api("/onboarding/local-devices", { method: "POST", body: "{}" });
      $("#usb-devices").innerHTML = devices.length
        ? `<ul class="list compact">${devices.map((d) => `<li>Device #${d.index}: ${d.resolution.join("×")} <button type="button" data-use-device="${d.index}">Use</button></li>`).join("")}</ul>`
        : "<p class='small muted'>No devices found.</p>";
      $("#usb-status").textContent = "";
      document.querySelectorAll("[data-use-device]").forEach((b) => b.addEventListener("click", () => {
        document.querySelector('#wizard-body input[name="index"]').value = b.dataset.useDevice;
      }));
    } catch (error) {
      $("#usb-status").textContent = error.message;
    }
  });
}

function collectStep() {
  document.querySelectorAll("#wizard-body [name]").forEach((input) => {
    if (input.name === "onvif_pick") return;
    wizard.values[input.name] = input.type === "checkbox" ? input.checked : input.type === "number" ? (input.value === "" ? "" : Number(input.value)) : input.value.trim();
  });
}

function buildSource() {
  const v = wizard.values;
  const kind = wizard.kind;
  if (kind === "rtsp" || kind === "dvr") {
    const source = { type: "rtsp", host: v.host, port: v.port || 554, template: v.template, channel: v.channel || 1, username: v.username, prefer_substream: v.prefer_substream, transport: v.transport, hw_decode: v.hw_decode };
    if (v.template === "custom") Object.assign(source, v.path ? { path: v.path } : {}, v.sub_path ? { sub_path: v.sub_path } : {});
    if (v.password) source.password = v.password;
    return source;
  }
  if (kind === "http") {
    const source = { type: "http", host: v.host, port: v.port || 80, template: "custom", mode: v.mode, username: v.username, hw_decode: v.hw_decode || "auto" };
    if (v.path) source.path = v.path;
    if (v.password) source.password = v.password;
    return source;
  }
  if (kind === "usb") {
    const source = { type: "usb", index: Number(v.index) || 0, backend: v.backend || "auto" };
    if (v.resolution) source.resolution = v.resolution.split("x").map(Number);
    return source;
  }
  if (kind === "file") return { type: "file", path: v.file, loop: true };
  if (kind === "simulated") return { type: "simulated", scenario: v.scenario || "scenarios/demo_loop.json" };
  return null;
}

function validateStep() {
  const v = wizard.values;
  if (wizard.step === 1 && !wizard.kind) return "Choose how the camera connects.";
  if (wizard.step === 2) {
    if ((wizard.kind === "rtsp" || wizard.kind === "dvr" || wizard.kind === "http") && !v.host) return "Enter the camera's IP address or host name.";
    if (wizard.kind === "onvif" && !wizard.draftId) return "Read the stream profiles first.";
    if (wizard.kind === "file" && !v.file && !wizard.camera) return "Enter the video file path.";
  }
  if (wizard.step === 4 && !v.name) return "Give the camera a name.";
  return "";
}

async function runTest() {
  wizard.result = null;
  renderWizard();
  const body = wizard.kind === "onvif" ? { draft_id: wizard.draftId } : { source: buildSource() };
  if (wizard.camera) body.camera_id = wizard.camera.id;
  const keepsAddress = wizard.camera && ((wizard.kind === "file" && !wizard.values.file) || (wizard.kind === "simulated" && !wizard.values.scenario));
  try {
    wizard.result = keepsAddress
      ? await api("/onboarding/test", { method: "POST", body: JSON.stringify({ camera_id: wizard.camera.id }) })
      : await api("/onboarding/test", { method: "POST", body: JSON.stringify(body) });
    if (wizard.result.draft_id) wizard.draftId = wizard.result.draft_id;
  } catch (error) {
    wizard.result = { ok: false, message: error.message, streams: {} };
  }
  if (wizard.step === 3) renderWizard();
}

async function wizardNext() {
  collectStep();
  const problem = validateStep();
  if (problem) {
    $("#wizard-error").textContent = problem;
    return;
  }
  if (wizard.step === 2) {
    wizard.step = 3;
    renderWizard();
    await runTest();
    return;
  }
  if (wizard.step === 3) {
    if (!wizard.result) return;
    if (!wizard.result.ok && !confirm("The camera did not connect. Save it anyway? Sentinel will keep retrying and show it as offline.")) return;
    if (!wizard.values.name) wizard.values.name = wizard.camera ? wizard.camera.name : "";
    wizard.step = 4;
    renderWizard();
    return;
  }
  if (wizard.step === 4) {
    const payload = { name: wizard.values.name, fps: Math.max(1, Math.min(15, Number(wizard.values.fps) || 5)), enabled: Boolean(wizard.values.enabled) };
    if (wizard.draftId) payload.draft_id = wizard.draftId;
    try {
      const path = wizard.camera ? `/config/cameras/${encodeURIComponent(wizard.camera.id)}` : "/config/cameras";
      const result = await api(path, { method: wizard.camera ? "PUT" : "POST", body: JSON.stringify(payload) });
      $("#wizard").close();
      toast(wizard.camera ? "Camera updated" : "Camera added", "Draw its zones next.", "medium");
      await loadSetup();
      selectZoneCamera(result.id);
      showSub("zones");
    } catch (error) {
      $("#wizard-error").textContent = error.message;
    }
    return;
  }
  wizard.step += 1;
  renderWizard();
}

// ---- zone and privacy-mask editor -----------------------------------------

function fillCameraSelects() {
  const options = setup.config.cameras.map((camera) => `<option value="${esc(camera.id)}">${esc(camera.name)}</option>`).join("");
  const zoneSelect = $("#zone-camera");
  const current = setup.zoneCamera || setup.config.cameras[0]?.id;
  zoneSelect.innerHTML = options;
  if (current) zoneSelect.value = current;
  setup.zoneCamera = zoneSelect.value;
  $("#category-scope").innerHTML = `<option value="">All cameras (site default)</option>${options}`;
}

$("#zone-camera").addEventListener("change", (event) => {
  if (setup.zonesDirty && !confirm("Discard unsaved zone changes?")) {
    event.target.value = setup.zoneCamera;
    return;
  }
  selectZoneCamera(event.target.value);
});

function selectZoneCamera(id) {
  setup.zoneCamera = id;
  $("#zone-camera").value = id;
  setup.zonesDirty = false;
  loadZonesFromConfig();
  loadZoneFrame();
}

function loadZonesFromConfig() {
  const camera = setup.zoneCamera && cameraById(setup.zoneCamera);
  setup.zones = camera ? structuredClone(camera.zones || []) : [];
  setup.masks = camera ? structuredClone(camera.privacy_masks || []) : [];
  setup.selected = null;
  setup.drafting = null;
  renderZoneEditor();
}

function loadZoneFrame() {
  if (!setup.zoneCamera) return;
  const img = $("#zone-frame");
  img.onerror = () => { $("#zone-status").textContent = "No frame from this camera yet (offline or starting). Zones can still be drawn on a blank canvas."; img.removeAttribute("src"); };
  img.onload = () => { $("#zone-status").textContent = ""; };
  img.src = `${API}/cameras/${encodeURIComponent(setup.zoneCamera)}/frame.jpg?t=${Date.now()}`;
}

$("#zone-refresh").addEventListener("click", loadZoneFrame);
$("#draw-zone").addEventListener("click", () => startDrafting("zone"));
$("#draw-mask").addEventListener("click", () => startDrafting("mask"));
$("#zone-revert").addEventListener("click", () => { setup.zonesDirty = false; loadZonesFromConfig(); });
$("#zone-save").addEventListener("click", async () => {
  try {
    const result = await saveConfig(`/config/cameras/${encodeURIComponent(setup.zoneCamera)}/zones`, {
      zones: setup.zones.map(({ id, name, type, polygon }) => ({ id, name, type, polygon })),
      privacy_masks: setup.masks,
    }, "Zones saved and applied");
    setup.zonesDirty = false;
    $("#zone-status").textContent = (result.notes || []).join(" ");
    loadZonesFromConfig();
  } catch {
    /* toast already shown */
  }
});

function startDrafting(kind) {
  setup.drafting = { kind, points: [] };
  setup.selected = null;
  $("#zone-help").textContent = `Drawing a ${kind === "zone" ? "zone" : "privacy mask"}: click to add points; click the first point or press Enter to finish; Esc cancels.`;
  renderZoneEditor();
}

function finishDrafting() {
  const draft = setup.drafting;
  setup.drafting = null;
  $("#zone-help").textContent = "Click points to draw; click the first point or press Enter to finish. Drag corners to reshape, drag inside to move, Delete removes the selection.";
  if (!draft || draft.points.length < 3) return renderZoneEditor();
  if (draft.kind === "zone") {
    setup.zones.push({ id: "", name: `Zone ${setup.zones.length + 1}`, type: "yard", polygon: draft.points });
    setup.selected = { kind: "zone", index: setup.zones.length - 1 };
  } else {
    setup.masks.push(draft.points);
    setup.selected = { kind: "mask", index: setup.masks.length - 1 };
  }
  markZonesDirty();
}

function markZonesDirty() {
  setup.zonesDirty = true;
  $("#zone-status").textContent = "Unsaved changes.";
  renderZoneEditor();
}

function svgPoint(event, svg) {
  const rect = svg.getBoundingClientRect();
  const clamp = (v) => Math.round(Math.min(1, Math.max(0, v)) * 10000) / 10000;
  return [clamp((event.clientX - rect.left) / rect.width), clamp((event.clientY - rect.top) / rect.height)];
}

function polygonOf(selection) {
  return selection.kind === "zone" ? setup.zones[selection.index].polygon : setup.masks[selection.index];
}

function renderZoneEditor() {
  const svg = $("#zone-svg");
  const isSel = (kind, index) => setup.selected && setup.selected.kind === kind && setup.selected.index === index;
  const pts = (polygon) => polygon.map((p) => p.join(",")).join(" ");
  const parts = [];
  setup.masks.forEach((mask, index) => parts.push(`<polygon class="poly mask ${isSel("mask", index) ? "selected" : ""}" data-kind="mask" data-index="${index}" points="${pts(mask)}"/>`));
  setup.zones.forEach((zone, index) => parts.push(`<polygon class="poly zone ${esc(zone.type)} ${isSel("zone", index) ? "selected" : ""}" data-kind="zone" data-index="${index}" points="${pts(zone.polygon)}"/>`));
  if (setup.selected) {
    const polygon = polygonOf(setup.selected);
    polygon.forEach((p, i) => {
      const q = polygon[(i + 1) % polygon.length];
      parts.push(`<circle class="handle" data-mid="${i}" cx="${(p[0] + q[0]) / 2}" cy="${(p[1] + q[1]) / 2}" r="0.006" opacity="0.6"/>`);
    });
    polygon.forEach((p, i) => parts.push(`<circle class="handle" data-vertex="${i}" cx="${p[0]}" cy="${p[1]}" r="0.011"/>`));
  }
  if (setup.drafting) {
    const d = setup.drafting.points;
    if (d.length) parts.push(`<polyline class="poly drafting" points="${pts(d)}" fill="none"/>`);
    d.forEach((p, i) => parts.push(`<circle class="handle" cx="${p[0]}" cy="${p[1]}" r="${i === 0 ? 0.014 : 0.008}"/>`));
  }
  svg.innerHTML = parts.join("");
  renderZoneLists();
  renderZoneProps();
}

function renderZoneLists() {
  $("#zone-list").innerHTML = setup.zones
    .map((zone, index) => `<li data-pick-zone="${index}" style="cursor:pointer"><strong>${esc(zone.name)}</strong> <span class="muted">${esc(zone.type)}${zone.id ? "" : " · new"}</span></li>`)
    .join("") || "<li class='muted'>No zones yet. Detections outside zones are still tracked, but zone rules cannot fire.</li>";
  $("#mask-list").innerHTML = setup.masks.map((_, index) => `<li data-pick-mask="${index}" style="cursor:pointer">Mask ${index + 1}</li>`).join("") || "<li class='muted'>None</li>";
  document.querySelectorAll("[data-pick-zone]").forEach((li) => li.addEventListener("click", () => { setup.selected = { kind: "zone", index: Number(li.dataset.pickZone) }; renderZoneEditor(); }));
  document.querySelectorAll("[data-pick-mask]").forEach((li) => li.addEventListener("click", () => { setup.selected = { kind: "mask", index: Number(li.dataset.pickMask) }; renderZoneEditor(); }));
}

function renderZoneProps() {
  const panel = $("#zone-props");
  if (!setup.selected) {
    panel.innerHTML = "Nothing selected.";
    return;
  }
  if (setup.selected.kind === "mask") {
    panel.innerHTML = `<p>Privacy mask ${setup.selected.index + 1}</p><button type="button" class="danger" id="delete-selected">Delete mask</button>`;
  } else {
    const zone = setup.zones[setup.selected.index];
    panel.innerHTML = `<label class="field">Name <input type="text" id="zone-name" maxlength="60" value="${esc(zone.name)}"></label>
      <label class="field">Type <select id="zone-type">${setup.config.catalog.zone_types.map((t) => `<option value="${t}" ${t === zone.type ? "selected" : ""}>${t} – ${esc(ZONE_HELP[t] || "")}</option>`).join("")}</select></label>
      <p class="small muted">${zone.id ? `id ${esc(zone.id)}` : "new zone (id assigned on save)"}</p>
      <button type="button" class="danger" id="delete-selected">Delete zone</button>`;
    $("#zone-name").addEventListener("input", (event) => { zone.name = event.target.value; setup.zonesDirty = true; renderZoneLists(); $("#zone-status").textContent = "Unsaved changes."; });
    $("#zone-type").addEventListener("change", (event) => { zone.type = event.target.value; markZonesDirty(); });
  }
  $("#delete-selected").addEventListener("click", deleteSelected);
}

function deleteSelected() {
  if (!setup.selected) return;
  if (setup.selected.kind === "zone") setup.zones.splice(setup.selected.index, 1);
  else setup.masks.splice(setup.selected.index, 1);
  setup.selected = null;
  markZonesDirty();
}

const zoneSvg = $("#zone-svg");
zoneSvg.addEventListener("pointerdown", (event) => {
  const point = svgPoint(event, zoneSvg);
  if (setup.drafting) {
    const first = setup.drafting.points[0];
    if (first && setup.drafting.points.length >= 3 && Math.hypot(first[0] - point[0], first[1] - point[1]) < 0.02) return finishDrafting();
    setup.drafting.points.push(point);
    return renderZoneEditor();
  }
  const target = event.target;
  if (target.dataset.vertex !== undefined) {
    setup.drag = { mode: "vertex", index: Number(target.dataset.vertex) };
  } else if (target.dataset.mid !== undefined) {
    const polygon = polygonOf(setup.selected);
    const index = Number(target.dataset.mid) + 1;
    polygon.splice(index, 0, point);
    setup.drag = { mode: "vertex", index };
  } else if (target.dataset.kind) {
    setup.selected = { kind: target.dataset.kind, index: Number(target.dataset.index) };
    setup.drag = { mode: "move", last: point };
  } else {
    setup.selected = null;
    renderZoneEditor();
    return;
  }
  zoneSvg.setPointerCapture(event.pointerId);
  renderZoneEditor();
});
zoneSvg.addEventListener("pointermove", (event) => {
  if (!setup.drag || !setup.selected) return;
  const point = svgPoint(event, zoneSvg);
  const polygon = polygonOf(setup.selected);
  if (setup.drag.mode === "vertex") {
    polygon[setup.drag.index] = point;
  } else {
    const dx = point[0] - setup.drag.last[0];
    const dy = point[1] - setup.drag.last[1];
    const inside = polygon.every(([x, y]) => x + dx >= 0 && x + dx <= 1 && y + dy >= 0 && y + dy <= 1);
    if (inside) polygon.forEach((p) => { p[0] = Math.round((p[0] + dx) * 10000) / 10000; p[1] = Math.round((p[1] + dy) * 10000) / 10000; });
    setup.drag.last = point;
  }
  setup.zonesDirty = true;
  renderZoneEditor();
});
zoneSvg.addEventListener("pointerup", () => {
  if (setup.drag) markZonesDirty();
  setup.drag = null;
});
zoneSvg.addEventListener("contextmenu", (event) => {
  if (event.target.dataset.vertex === undefined || !setup.selected) return;
  event.preventDefault();
  const polygon = polygonOf(setup.selected);
  if (polygon.length > 3) {
    polygon.splice(Number(event.target.dataset.vertex), 1);
    markZonesDirty();
  }
});
document.addEventListener("keydown", (event) => {
  if (!document.querySelector("#tab-setup.active") || ["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
  if (setup.drafting && event.key === "Enter") finishDrafting();
  else if (setup.drafting && event.key === "Escape") { setup.drafting = null; renderZoneEditor(); }
  else if (setup.map.drafting && event.key === "Enter") finishFootprint();
  else if (setup.map.drafting && event.key === "Escape") { setup.map.drafting = null; renderPropMap(); }
  else if ((event.key === "Delete" || event.key === "Backspace") && setup.selected && document.querySelector("#sub-zones.active")) deleteSelected();
});

// ---- property map ------------------------------------------------------------

function loadMapFromConfig() {
  const map = setup.config.site.map || {};
  $("#map-width").value = map.width_m || 40;
  $("#map-height").value = map.height_m || 30;
  setup.map.cameras = {};
  setup.config.cameras.forEach((camera, index) => {
    setup.map.cameras[camera.id] = camera.map
      ? { ...camera.map, placed: true }
      : { x: (map.width_m || 40) * (0.2 + 0.15 * index), y: (map.height_m || 30) * 0.5, heading_deg: 0, fov_deg: 90, placed: false };
  });
  setup.map.footprints = {};
  setup.config.cameras.forEach((camera) => (camera.zones || []).forEach((zone) => { setup.map.footprints[zone.id] = zone.map_polygon || null; }));
  $("#footprint-zone").innerHTML = setup.config.cameras
    .flatMap((camera) => (camera.zones || []).map((zone) => `<option value="${esc(zone.id)}">${esc(camera.name)} – ${esc(zone.name)}</option>`))
    .join("");
  setup.map.dirty = false;
  renderPropMap();
}

function mapSize() {
  return [Number($("#map-width").value) || 40, Number($("#map-height").value) || 30];
}

function renderPropMap() {
  const [w, h] = mapSize();
  const svg = $("#prop-svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const parts = [];
  const site = setup.config.site.map || {};
  if (site.image_url) parts.push(`<image href="${site.image_url}?t=${setup.map.imageVersion || 0}" x="0" y="0" width="${w}" height="${h}" preserveAspectRatio="none"/>`);
  else {
    for (let x = 0; x <= w; x += 5) parts.push(`<line x1="${x}" y1="0" x2="${x}" y2="${h}" stroke="var(--border)" stroke-width="0.05"/>`);
    for (let y = 0; y <= h; y += 5) parts.push(`<line x1="0" y1="${y}" x2="${w}" y2="${y}" stroke="var(--border)" stroke-width="0.05"/>`);
  }
  const unit = Math.max(w, h) / 100;
  Object.entries(setup.map.footprints).forEach(([zoneId, polygon]) => {
    if (!polygon) return;
    parts.push(`<polygon points="${polygon.map((p) => p.join(",")).join(" ")}" fill="rgba(0,200,255,0.2)" stroke="#00c8ff" stroke-width="${unit * 0.2}"/>`);
    const [cx, cy] = [polygon.reduce((s, p) => s + p[0], 0) / polygon.length, polygon.reduce((s, p) => s + p[1], 0) / polygon.length];
    parts.push(`<text x="${cx}" y="${cy}" font-size="${unit * 2.2}" text-anchor="middle" fill="var(--text)">${esc(zoneId)}</text>`);
  });
  setup.links.forEach((link) => {
    const a = setup.map.cameras[link.from];
    const b = setup.map.cameras[link.to];
    if (a && b) parts.push(`<line class="map-link" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke-width="${unit * 0.3}"/>`);
  });
  Object.entries(setup.map.cameras).forEach(([id, cam]) => {
    const reach = Math.max(w, h) * 0.18;
    const rad = (deg) => ((deg - 90) * Math.PI) / 180;
    const a = rad(cam.heading_deg - cam.fov_deg / 2);
    const b = rad(cam.heading_deg + cam.fov_deg / 2);
    parts.push(`<path class="map-fov" d="M${cam.x},${cam.y} L${cam.x + reach * Math.cos(a)},${cam.y + reach * Math.sin(a)} A${reach},${reach} 0 0 1 ${cam.x + reach * Math.cos(b)},${cam.y + reach * Math.sin(b)} Z"/>`);
    const hx = cam.x + reach * 0.6 * Math.cos(rad(cam.heading_deg));
    const hy = cam.y + reach * 0.6 * Math.sin(rad(cam.heading_deg));
    parts.push(`<line x1="${cam.x}" y1="${cam.y}" x2="${hx}" y2="${hy}" stroke="#ffd400" stroke-width="${unit * 0.2}"/>`);
    parts.push(`<circle class="map-cam-handle" data-aim="${esc(id)}" cx="${hx}" cy="${hy}" r="${unit * 0.9}" stroke-width="${unit * 0.1}"/>`);
    parts.push(`<circle class="map-cam" data-cam="${esc(id)}" cx="${cam.x}" cy="${cam.y}" r="${unit * 1.5}" opacity="${cam.placed ? 1 : 0.45}"/>`);
    parts.push(`<text x="${cam.x}" y="${cam.y - unit * 2.2}" font-size="${unit * 2.4}" text-anchor="middle" fill="var(--text)">${esc(cameraById(id)?.name || id)}</text>`);
  });
  if (setup.map.drafting) {
    const d = setup.map.drafting.points;
    if (d.length) parts.push(`<polyline points="${d.map((p) => p.join(",")).join(" ")}" fill="none" stroke="#ffd400" stroke-width="${unit * 0.3}"/>`);
    d.forEach((p) => parts.push(`<circle cx="${p[0]}" cy="${p[1]}" r="${unit * 0.8}" fill="#ffd400"/>`));
  }
  svg.innerHTML = parts.join("");
  $("#map-camera-list").innerHTML = Object.entries(setup.map.cameras)
    .map(([id, cam]) => `<li>${esc(cameraById(id)?.name || id)} ${cam.placed ? "" : "<span class='muted'>(not placed)</span>"}
      <label class="inline small">heading <input type="number" min="0" max="359" data-heading="${esc(id)}" value="${Math.round(cam.heading_deg)}" style="width:60px"></label>
      <label class="inline small">view angle <input type="number" min="10" max="360" data-fov="${esc(id)}" value="${Math.round(cam.fov_deg)}" style="width:60px"></label></li>`)
    .join("");
  document.querySelectorAll("[data-heading]").forEach((input) => input.addEventListener("change", () => { const cam = setup.map.cameras[input.dataset.heading]; cam.heading_deg = Number(input.value) % 360; cam.placed = true; setup.map.dirty = true; renderPropMap(); }));
  document.querySelectorAll("[data-fov]").forEach((input) => input.addEventListener("change", () => { const cam = setup.map.cameras[input.dataset.fov]; cam.fov_deg = Math.max(10, Math.min(360, Number(input.value))); cam.placed = true; setup.map.dirty = true; renderPropMap(); }));
}

function mapPoint(event) {
  const svg = $("#prop-svg");
  const rect = svg.getBoundingClientRect();
  const [w, h] = mapSize();
  return [Math.round(((event.clientX - rect.left) / rect.width) * w * 100) / 100, Math.round(((event.clientY - rect.top) / rect.height) * h * 100) / 100];
}

const propSvg = $("#prop-svg");
propSvg.addEventListener("pointerdown", (event) => {
  const point = mapPoint(event);
  if (setup.map.drafting) {
    const d = setup.map.drafting.points;
    const [w, h] = mapSize();
    if (d.length >= 3 && Math.hypot(d[0][0] - point[0], d[0][1] - point[1]) < Math.max(w, h) * 0.02) return finishFootprint();
    d.push(point);
    return renderPropMap();
  }
  if (event.target.dataset.cam) setup.map.drag = { mode: "move", id: event.target.dataset.cam };
  else if (event.target.dataset.aim) setup.map.drag = { mode: "aim", id: event.target.dataset.aim };
  else return;
  propSvg.setPointerCapture(event.pointerId);
});
propSvg.addEventListener("pointermove", (event) => {
  if (!setup.map.drag) return;
  const [x, y] = mapPoint(event);
  const [w, h] = mapSize();
  const cam = setup.map.cameras[setup.map.drag.id];
  if (setup.map.drag.mode === "move") Object.assign(cam, { x: Math.min(w, Math.max(0, x)), y: Math.min(h, Math.max(0, y)) });
  else cam.heading_deg = (Math.round((Math.atan2(y - cam.y, x - cam.x) * 180) / Math.PI + 90) + 360) % 360;
  cam.placed = true;
  setup.map.dirty = true;
  renderPropMap();
});
propSvg.addEventListener("pointerup", () => { setup.map.drag = null; });

function finishFootprint() {
  const draft = setup.map.drafting;
  setup.map.drafting = null;
  if (draft && draft.points.length >= 3) {
    setup.map.footprints[draft.zone] = draft.points;
    setup.map.dirty = true;
  }
  renderPropMap();
}

$("#footprint-draw").addEventListener("click", () => {
  const zone = $("#footprint-zone").value;
  if (!zone) return toast("No zones yet", "Draw zones on a camera first.");
  setup.map.drafting = { zone, points: [] };
  $("#map-status").textContent = "Click points on the map; click the first point or press Enter to finish.";
  renderPropMap();
});
$("#footprint-clear").addEventListener("click", () => {
  setup.map.footprints[$("#footprint-zone").value] = null;
  setup.map.dirty = true;
  renderPropMap();
});
["#map-width", "#map-height"].forEach((id) => $(id).addEventListener("change", () => { setup.map.dirty = true; renderPropMap(); }));
$("#map-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    const response = await fetch(`${API}/config/map/image`, { method: "PUT", credentials: "same-origin", headers: { "Content-Type": file.type, "X-Sentinel-Operator": operator() }, body: file });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || response.statusText);
    setup.map.imageVersion = Date.now();
    toast("Map image uploaded", "", "medium");
    await loadSetup();
  } catch (error) {
    toast("Upload failed", error.message);
  }
  event.target.value = "";
});
$("#map-save").addEventListener("click", async () => {
  const [width_m, height_m] = mapSize();
  const body = {
    width_m, height_m,
    cameras: Object.entries(setup.map.cameras).filter(([, cam]) => cam.placed).map(([id, cam]) => ({ id, x: cam.x, y: cam.y, heading_deg: cam.heading_deg, fov_deg: cam.fov_deg })),
    zones: Object.entries(setup.map.footprints).map(([id, map_polygon]) => ({ id, map_polygon })),
  };
  try {
    await saveConfig("/config/map", body, "Property map saved");
    setup.map.dirty = false;
    $("#map-status").textContent = "";
    loadMapFromConfig();
  } catch {
    /* toast shown */
  }
});

// ---- connections -------------------------------------------------------------

function zoneOptions(cameraId, selected) {
  const camera = cameraById(cameraId);
  return (camera?.zones || []).map((zone) => `<option value="${esc(zone.id)}" ${selected.includes(zone.id) ? "selected" : ""}>${esc(zone.name)}</option>`).join("");
}

function renderLinks() {
  const cameraOptions = (selected) => setup.config.cameras.map((c) => `<option value="${esc(c.id)}" ${c.id === selected ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  $("#link-table").innerHTML = `<tr><th>From</th><th>To</th><th>Min s</th><th>Max s</th><th>Overlapping views</th><th>Both ways</th><th>Leaves via zones</th><th>Arrives in zones</th><th></th></tr>${setup.links
    .map((link, i) => `<tr data-link="${i}">
      <td><select data-f="from">${cameraOptions(link.from)}</select></td><td><select data-f="to">${cameraOptions(link.to)}</select></td>
      <td><input type="number" data-f="min_transit_s" min="0" step="1" value="${link.min_transit_s ?? 0}"></td>
      <td><input type="number" data-f="max_transit_s" min="1" max="600" step="1" value="${link.max_transit_s}"></td>
      <td><input type="checkbox" data-f="overlap" ${link.overlap ? "checked" : ""}></td>
      <td><input type="checkbox" data-f="bidirectional" ${link.bidirectional !== false ? "checked" : ""}></td>
      <td><select multiple size="2" data-f="from_zones">${zoneOptions(link.from, link.from_zones || [])}</select></td>
      <td><select multiple size="2" data-f="to_zones">${zoneOptions(link.to, link.to_zones || [])}</select></td>
      <td><button type="button" class="danger" data-remove-link="${i}">Remove</button></td></tr>`)
    .join("")}`;
  document.querySelectorAll("[data-link] [data-f]").forEach((input) =>
    input.addEventListener("change", () => {
      const link = setup.links[Number(input.closest("tr").dataset.link)];
      const key = input.dataset.f;
      if (input.type === "checkbox") link[key] = input.checked;
      else if (input.multiple) link[key] = [...input.selectedOptions].map((o) => o.value);
      else if (input.type === "number") link[key] = Number(input.value);
      else {
        link[key] = input.value;
        if (key === "from") link.from_zones = [];
        if (key === "to") link.to_zones = [];
        renderLinks();
      }
    }),
  );
  document.querySelectorAll("[data-remove-link]").forEach((button) => button.addEventListener("click", () => { setup.links.splice(Number(button.dataset.removeLink), 1); renderLinks(); }));
}

$("#link-add").addEventListener("click", () => {
  const cams = setup.config.cameras;
  if (cams.length < 2) return toast("Need two cameras", "Add another camera first.");
  setup.links.push({ from: cams[0].id, to: cams[1].id, min_transit_s: 1, max_transit_s: 30, bidirectional: true, overlap: false });
  renderLinks();
});
$("#link-save").addEventListener("click", async () => {
  const links = setup.links.map((l) => Object.fromEntries(Object.entries(l).filter(([k, v]) => !(Array.isArray(v) && !v.length))));
  try {
    await saveConfig("/config/topology", { links }, "Connections saved");
  } catch {
    /* toast shown */
  }
});

// ---- categories ----------------------------------------------------------------

const CATEGORY_NOTES = {
  person: "Recognised by the detector.",
  vehicle: "car, truck, bus, motorcycle",
  bicycle: "Parked or ridden bicycles (riders are also detected as people).",
  animal: "Dogs, cats, birds and other animals.",
  bag: "Backpacks, handbags, suitcases. Delivered boxes are not recognised yet.",
  unknown_motion: "Moving things the detector cannot name (from motion analysis). Off by default.",
};

function effectiveCategories(cameraId) {
  const catalog = setup.config.catalog;
  const merged = structuredClone(catalog.category_defaults);
  const layers = [setup.config.categories || {}, cameraId ? cameraById(cameraId)?.categories || {} : {}];
  layers.forEach((layer) => Object.entries(layer).forEach(([name, values]) => Object.assign(merged[name] ||= {}, values)));
  return merged;
}

function renderCategories() {
  const scope = $("#category-scope").value;
  const values = effectiveCategories(scope);
  const labels = setup.config.catalog.category_labels;
  $("#category-table").innerHTML = `<tr><th>Category</th><th>Detect</th><th>Alerts</th><th>Covers</th></tr>${setup.config.catalog.categories
    .map((name) => `<tr><td>${esc(name.replaceAll("_", " "))}</td>
      <td><input type="checkbox" data-cat="${name}" data-key="detect" ${values[name]?.detect ? "checked" : ""}></td>
      <td><input type="checkbox" data-cat="${name}" data-key="alerts" ${values[name]?.alerts ? "checked" : ""}></td>
      <td class="small muted" style="white-space:normal">${esc(CATEGORY_NOTES[name] || "")}${labels[name]?.length ? ` <span>(${esc(labels[name].join(", "))})</span>` : ""}</td></tr>`)
    .join("")}`;
}
$("#category-scope").addEventListener("change", renderCategories);
$("#category-save").addEventListener("click", async () => {
  const categories = {};
  document.querySelectorAll("[data-cat]").forEach((input) => { (categories[input.dataset.cat] ||= {})[input.dataset.key] = input.checked; });
  Object.values(categories).forEach((values) => { if (!values.detect) values.alerts = false; });
  const scope = $("#category-scope").value;
  try {
    await saveConfig(scope ? `/config/cameras/${encodeURIComponent(scope)}/categories` : "/config/categories", { categories }, "Categories saved and applied");
    $("#category-scope").value = scope;
    renderCategories();
  } catch {
    /* toast shown */
  }
});
