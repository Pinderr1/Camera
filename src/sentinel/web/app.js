"use strict";

const API = "/api/v1";
const OPERATOR_KEY = "sentinel.operator";
const state = {
  site: null,
  settings: {},
  incidents: [],
  activity: [],
  selectedIncident: null,
  selectedZone: null,
  activeZones: new Map(),
  drone: null,
};

const $ = (selector) => document.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const time = (ms) => (ms ? new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
const dateTime = (ms) => (ms ? new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");
const operator = () => {
  try { return localStorage.getItem(OPERATOR_KEY) || "dashboard"; } catch { return "dashboard"; }
};

async function api(path, options = {}) {
  const response = await fetch(API + path, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", "X-Sentinel-Operator": operator(), ...(options.headers || {}) },
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("authentication required");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || response.statusText);
  }
  return response.json();
}

function toast(title, body = "", severity = "medium") {
  const node = document.createElement("div");
  node.className = `toast ${severity}`;
  node.innerHTML = `<strong>${esc(title)}</strong>${body ? `<div class="small muted">${esc(body)}</div>` : ""}`;
  $("#toasts").append(node);
  setTimeout(() => node.remove(), 8000);
}

// ---- auth ------------------------------------------------------------------

function showLogin() {
  const dialog = $("#login");
  if (!dialog.open) dialog.showModal();
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/session", { method: "POST", body: JSON.stringify({ token: $("#token").value }) });
    $("#login").close();
    boot();
  } catch (error) {
    $("#login-error").textContent = error.message;
  }
});

// ---- tabs and header -------------------------------------------------------

document.querySelectorAll(".tabs button").forEach((button) =>
  button.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === button));
    document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.id === `tab-${button.dataset.tab}`));
    if (button.dataset.tab === "settings") loadAudit();
    if (button.dataset.tab === "system") loadSystem();
  }),
);

document.querySelectorAll(".arm button").forEach((button) =>
  button.addEventListener("click", async () => {
    try {
      renderSettings(await api("/settings", { method: "PATCH", body: JSON.stringify({ arm_mode: button.dataset.arm }) }));
    } catch (error) {
      toast("Could not change arm mode", error.message);
    }
  }),
);

$("#sensitivity").addEventListener("change", (event) => patchSettings({ sensitivity: event.target.value }));
$("#notifications").addEventListener("change", (event) => patchSettings({ notifications_enabled: event.target.checked }));
$("#show-closed").addEventListener("change", renderIncidents);

async function patchSettings(changes) {
  try {
    renderSettings(await api("/settings", { method: "PATCH", body: JSON.stringify(changes) }));
  } catch (error) {
    toast("Could not save setting", error.message);
  }
}

function renderSettings(settings) {
  state.settings = settings;
  document.querySelectorAll(".arm button").forEach((b) => b.classList.toggle("active", b.dataset.arm === settings.arm_mode));
  $("#sensitivity").value = settings.sensitivity;
  $("#notifications").checked = !!settings.notifications_enabled;
}

// ---- live cameras ----------------------------------------------------------

function renderCameras(cameras) {
  const grid = $("#camera-grid");
  if (!cameras.length) {
    grid.innerHTML = `<article class="card empty-state"><h3>No cameras yet</h3><p class="muted">This installation has no fixed cameras, so there is no continuous video monitoring.
      Add cameras, a drone or sensors from the Install tab.</p><button type="button" class="primary" onclick="document.querySelector('[data-tab=install]').click()">Open Install</button></article>`;
    return;
  }
  grid.innerHTML = cameras
    .map(
      (camera) => `
      <article class="card camera" data-camera="${esc(camera.id)}">
        <img src="${API}/cameras/${encodeURIComponent(camera.id)}/stream.mjpg" alt="${esc(camera.name || camera.id)} live view" loading="lazy">
        <div class="camera-overlay" hidden><div class="overlay-title">NOT MONITORING</div><div class="small overlay-reason"></div></div>
        <div class="meta"><strong>${esc(camera.name || camera.id)}</strong><span class="status"></span></div>
      </article>`,
    )
    .join("");
  updateCameraStatus(cameras.map((camera) => camera.status || {}));
}

// A tile only looks live while the camera is really being monitored: otherwise the
// last frame stays visible under an overlay that says why, so a frozen picture is
// never mistaken for a working camera.
function updateCameraStatus(statuses) {
  for (const status of statuses) {
    const tile = document.querySelector(`[data-camera="${CSS.escape(status.camera_id || "")}"]`);
    if (!tile) continue;
    const node = tile.querySelector(".status");
    const health = status.health || {};
    const monitoring = health.monitoring === true;
    const starting = health.state === "starting" || health.state === "not_running" || !health.state;
    node.className = `status ${monitoring ? "status-ok" : starting ? "muted" : "status-bad"}`;
    const warnings = (health.warnings || []).map((w) => w.replace(/_/g, " "));
    node.textContent = health.state === "unverified" ? "Unverified: still picture" : monitoring ? `${status.fps || 0} fps${warnings.length ? ` · ${warnings.join(", ")}` : ""}` : health.label || "starting";
    if (health.state === "unverified") node.className = "status status-warn";
    const overlay = tile.querySelector(".camera-overlay");
    overlay.hidden = monitoring || (health.state || "starting") === "starting";
    overlay.querySelector(".overlay-title").textContent = `NOT MONITORING: ${(health.label || "").toUpperCase()}`;
    overlay.querySelector(".overlay-reason").textContent = [health.reason, health.since_ms ? `since ${time(health.since_ms)}` : ""].filter(Boolean).join(" · ");
  }
}

function renderCoverage(coverage) {
  if (!coverage) return;
  const node = $("#coverage");
  const text = {
    full: `Monitoring ${coverage.monitoring}/${coverage.total}`,
    unverified: `Monitoring ${coverage.monitoring}/${coverage.total} · ${coverage.unverified} unverified`,
    degraded: `Coverage degraded ${coverage.monitoring}/${coverage.total}`,
    none: "NOT MONITORING",
    not_running: "Cameras not running",
    no_cameras: "No cameras",
  }[coverage.state] || coverage.state;
  node.className = `coverage ${coverage.state}`;
  node.textContent = text;
  const problems = [...coverage.not_monitoring, ...(coverage.unverified_cameras || [])].filter((camera) => camera.state !== "starting").map((camera) => `${camera.name}: ${camera.label}`);
  const components = Object.values(coverage.components || {}).filter((c) => c.state !== "ok").map((c) => `${c.label}: ${c.detail}`);
  node.title = [...problems, ...components].join("\n") || "All cameras are being monitored";
  $("#system-badge").hidden = ["full", "not_running", "no_cameras"].includes(coverage.state);
}

// ---- map -------------------------------------------------------------------

function geoToMap(lat, lon) {
  const [lat0, lon0] = state.site.site.map.origin_geo;
  const metres = 111320;
  return [(lon - lon0) * metres * Math.cos((lat0 * Math.PI) / 180), -(lat - lat0) * metres];
}

function renderMap() {
  const map = state.site.site.map;
  const svg = $("#site-map");
  svg.setAttribute("viewBox", `-1 -1 ${map.width_m + 2} ${map.height_m + 2}`);
  const points = (polygon) => polygon.map((p) => p.join(",")).join(" ");
  const parts = [];
  for (const building of map.buildings || []) parts.push(`<polygon class="building" points="${points(building.polygon)}"><title>${esc(building.name)}</title></polygon>`);
  for (const camera of state.site.cameras) {
    for (const zone of camera.zones || []) {
      if (!zone.map_polygon) continue;
      const [cx, cy] = centroid(zone.map_polygon);
      const classes = ["zone", state.activeZones.has(zone.id) ? "active" : "", state.selectedZone === zone.id ? "selected" : ""].join(" ");
      parts.push(`<polygon class="${classes}" data-zone="${esc(zone.id)}" style="fill: var(--zone-${esc(zone.type)}, var(--zone-street))" points="${points(zone.map_polygon)}"><title>${esc(zone.name)}</title></polygon>`);
      parts.push(`<text class="zone-label" x="${cx}" y="${cy}" text-anchor="middle">${esc(zone.name)}</text>`);
    }
  }
  for (const camera of state.site.cameras) {
    if (!camera.map) continue;
    const { x, y, heading_deg: heading = 0, fov_deg: fov = 90 } = camera.map;
    const reach = 9;
    const toRad = (deg) => ((deg - 90) * Math.PI) / 180;
    const a = toRad(heading - fov / 2);
    const b = toRad(heading + fov / 2);
    parts.push(`<path class="fov" d="M${x},${y} L${x + reach * Math.cos(a)},${y + reach * Math.sin(a)} A${reach},${reach} 0 0 1 ${x + reach * Math.cos(b)},${y + reach * Math.sin(b)} Z"/>`);
    parts.push(`<circle class="camera-icon" cx="${x}" cy="${y}" r="0.7"><title>${esc(camera.name)}</title></circle>`);
  }
  for (const point of state.site.drone_points || []) {
    parts.push(`<rect class="drone-point" x="${point.map[0] - 0.5}" y="${point.map[1] - 0.5}" width="1" height="1"><title>Drone inspection location: ${esc(point.name)}</title></rect>`);
  }
  if (state.drone) {
    const fence = state.drone.geofence.polygon.map(([lat, lon]) => geoToMap(lat, lon));
    parts.push(`<polygon class="geofence" points="${points(fence)}"/>`);
    const t = state.drone.telemetry;
    if (t.lat != null) {
      const [dx, dy] = geoToMap(t.lat, t.lon);
      parts.push(`<circle class="drone-icon" cx="${dx}" cy="${dy}" r="0.9"><title>Drone (${esc(t.flight_mode)})</title></circle>`);
    }
  }
  svg.innerHTML = parts.join("");
  svg.querySelectorAll("[data-zone]").forEach((node) => node.addEventListener("click", () => selectZone(node.dataset.zone)));
}

function centroid(polygon) {
  return [polygon.reduce((s, p) => s + p[0], 0) / polygon.length, polygon.reduce((s, p) => s + p[1], 0) / polygon.length];
}

function findZone(zoneId) {
  for (const camera of state.site.cameras) for (const zone of camera.zones || []) if (zone.id === zoneId) return { zone, camera };
  return null;
}

function selectZone(zoneId) {
  state.selectedZone = zoneId;
  const found = findZone(zoneId);
  if (!found) return;
  const { zone, camera } = found;
  const recent = state.activity.filter((event) => event.zone_id === zoneId).slice(0, 5);
  $("#zone-panel").innerHTML = `
    <h3>${esc(zone.name)}</h3>
    <dl class="kv"><dt>Type</dt><dd>${esc(zone.type)}</dd><dt>Camera</dt><dd>${esc(camera.name)}</dd></dl>
    ${state.site.drone_enabled ? `<div class="button-row"><button class="primary" id="inspect-zone">Request drone inspection</button></div>` : ""}
    <h3>Recent events</h3>
    <ul class="list compact">${recent.map((e) => `<li>${time(e.timestamp_ms)} · ${esc(e.summary)}</li>`).join("") || '<li class="muted">None recently</li>'}</ul>`;
  const button = $("#inspect-zone");
  if (button) button.addEventListener("click", () => requestInspection({ zone_id: zoneId }));
  renderMap();
}

// ---- timeline --------------------------------------------------------------

const OUTCOME_LABELS = { expected_activity: "Expected activity", false_alarm: "False alarm", real_concern: "Genuine concern", unclear: "Uncertain" };

function incidentQuery() {
  const form = new FormData($("#incident-search"));
  const params = new URLSearchParams({ limit: "200" });
  for (const [key, value] of form.entries()) {
    if (!value) continue;
    if (key === "from") params.set("since_ms", String(new Date(`${value}T00:00:00`).getTime()));
    else if (key === "to") params.set("until_ms", String(new Date(`${value}T23:59:59`).getTime()));
    else params.set(key, value);
  }
  return params.toString();
}

async function loadIncidents() {
  const [found, open] = await Promise.all([api(`/search/incidents?${incidentQuery()}`), api("/incidents?status=open&limit=200")]);
  state.incidents = found;
  state.openIncidents = open;
  renderIncidents();
}

function renderIncidents() {
  const includeClosed = $("#show-closed").checked;
  const rows = state.incidents.filter((incident) => includeClosed || incident.status === "open");
  const open = (state.openIncidents || state.incidents).filter((incident) => incident.status === "open" && !incident.acknowledged_ms).length;
  const badge = $("#open-count");
  badge.hidden = open === 0;
  badge.textContent = open;
  $("#incident-list").innerHTML =
    rows
      .map(
        (incident) => `
      <li class="incident ${incident.incident_id === state.selectedIncident ? "selected" : ""}" data-incident="${esc(incident.incident_id)}">
        <span class="sev ${esc(incident.severity)}">${esc(incident.severity)}</span>
        <div class="what"><div>${esc(incident.title)}</div>
          <div class="small muted">${dateTime(incident.opened_ms)} · ${esc(incident.cameras.join(", "))} · ${esc(incident.event_types.join(", "))}</div></div>
        <span class="small muted">${incident.resolution ? esc(OUTCOME_LABELS[incident.resolution] || incident.resolution) : incident.acknowledged_ms ? "acknowledged" : esc(incident.status)}</span>
      </li>`,
      )
      .join("") || '<li class="muted">No incidents match. Ordinary activity is logged below without alerting.</li>';
  document.querySelectorAll("[data-incident]").forEach((node) => node.addEventListener("click", () => openIncident(node.dataset.incident)));
}

// openIncident (explanation, timeline, replay, notes, export) lives in intel.js.

async function incidentAction(incidentId, action, body = {}) {
  try {
    await api(`/incidents/${encodeURIComponent(incidentId)}/${action}`, { method: "POST", body: JSON.stringify(body) });
    await loadIncidents();
    openIncident(incidentId);
  } catch (error) {
    toast("Action failed", error.message);
  }
}

async function loadActivity() {
  state.activity = await api("/events?limit=60");
  renderActivity();
}

function renderActivity() {
  $("#activity-list").innerHTML =
    state.activity
      .slice(0, 40)
      .map((event) => `<li><span class="sev ${esc(event.severity)}">${esc(event.severity)}</span> ${time(event.timestamp_ms)} · ${esc(event.summary)} <span class="muted">(${esc(event.camera_id)})</span></li>`)
      .join("") || '<li class="muted">No activity recorded yet.</li>';
}

// ---- drone -----------------------------------------------------------------
// The tab is laid out once (forms keep their input across live updates); the
// SSE "drone" topic refreshes the dynamic parts every 2 s.

async function requestInspection(body) {
  try {
    await api("/drone/inspections", { method: "POST", body: JSON.stringify(body) });
    toast("Inspection requested", "Review and approve it on the Drone tab.", "medium");
    renderDrone(await api("/drone"));
  } catch (error) {
    toast("Inspection not requested", error.message);
  }
}

const DRONE_STATE_LABELS = { offline: "Offline", link_lost: "LINK LOST", docked: "Docked", on_ground: "On the ground", armed: "Armed",
  taking_off: "Taking off", airborne: "Airborne", returning: "Returning home", landing: "Landing" };

const DRONE_CAPABILITY_LABELS = {
  telemetry: "Telemetry", gps: "GPS", dual_gps: "Redundant GPS", battery_monitoring: "Battery monitoring", manual_control: "Independent pilot radio",
  fc_failsafes: "Failsafes", parameter_audit: "Settings audit", command_lockout: "Hub command lockout", video_stream: "Live video",
  stabilized_video: "Stabilised camera", gimbal_attitude: "Gimbal angle", optical_zoom: "Optical zoom", thermal: "Thermal",
  night_inspection: "Night inspections", obstacle_sensing: "Obstacle sensing", companion_computer: "Companion computer",
};
const DRONE_VALUE_MARKS = { yes: "✓", optional: "Option", limited: "Limited", no: "–" };

// What each Sentinel drone can do, side by side. Shared by the Drone and Install tabs.
function droneComparisonHtml(table, current) {
  const models = table.models;
  const head = `<tr><th></th>${models.map((m) => `<th class="${m.id === current ? "current" : ""}">${esc(m.name)}<div class="small muted">${esc(m.cost_label)}</div></th>`).join("")}</tr>`;
  const facts = table.facts.map((row) => `<tr><td>${esc(row.label)}</td>${models.map((m) => `<td class="small wrap">${esc(row.values[m.id])}</td>`).join("")}</tr>`).join("");
  const rows = table.rows.map((row) => `<tr title="${esc(row.detail)}"><td>${esc(row.label)}</td>${models.map((m) => {
    const value = row.values[m.id];
    return `<td class="cap cap-${esc(value)}" data-model="${esc(m.id)}" data-capability="${esc(row.id)}">${esc(DRONE_VALUE_MARKS[value] || value)}</td>`;
  }).join("")}</tr>`).join("");
  return `<div class="drone-compare card inner">
    <h3>Drone models</h3>
    <div class="grid3">${models.map((m) => `<div><strong>${esc(m.name)}</strong><p class="small">${esc(m.summary)}</p><p class="small muted">Best for: ${esc(m.best_for)}</p></div>`).join("")}</div>
    <div class="scroll"><table class="table compare-table">${head}${facts}${rows}</table></div>
    <p class="small"><strong>Shared by all three:</strong> ${table.shared.map(esc).join("; ")}.</p>
    <p class="small muted">${esc(table.support_note)} Hover times and wind limits are estimates until flight tests measure them.</p></div>`;
}

function droneDiagnosisHtml(report) {
  return `<div class="card inner drone-diagnosis"><p><strong>${esc(report.summary)}</strong></p>
    ${report.findings.map((f) => `<details ${f.blocks_approval ? "open" : ""} data-finding="${esc(f.check)}"><summary>${f.blocks_approval ? "✗" : "!"} ${esc(f.label)} <span class="small muted">${esc(f.detail)}</span></summary>
      ${f.causes.length ? `<div class="small">Likely cause: ${f.causes.map(esc).join(" ")}</div>` : ""}
      ${f.steps.length ? `<ol class="small">${f.steps.map((step) => `<li>${esc(step)}</li>`).join("")}</ol>` : ""}</details>`).join("")}
    <p class="small muted">${esc(report.retest)}</p></div>`;
}

function droneLayout() {
  if ($("#drone-readiness")) return;
  $("#tab-drone").innerHTML = `
    <div class="notice" id="drone-mode-notice"></div>
    <div class="notice" id="drone-advisories" hidden></div>
    <div class="tiles" id="drone-tiles"></div>
    <div class="drone-layout">
      <div>
        <div class="card"><h3>Inspection requests</h3>
          <p class="muted small">Sentinel proposes inspections of places; only an operator approves, and the pilot in command decides how to fly.</p>
          <ul class="list" id="drone-requests"></ul></div>
        <div class="card" id="drone-video-card" hidden><h3>Drone camera</h3>
          <p class="muted small" id="drone-video-detail"></p><img id="drone-video" alt="Drone camera during the inspection" style="width:100%;border-radius:8px"></div>
        <div class="card"><h3>Flight log</h3><div class="scroll"><table class="table" id="drone-flights"></table></div></div>
      </div>
      <div>
        <div class="card" id="drone-aircraft"><h3>Aircraft</h3><div id="drone-model"></div>
          <div class="button-row"><button type="button" id="drone-diagnose">Troubleshoot</button><button type="button" id="drone-compare">Compare drone models</button></div>
          <div id="drone-diagnosis"></div></div>
        <div class="card"><h3>Readiness</h3><ul class="list compact" id="drone-readiness"></ul>
          <details><summary class="small">Flight-controller settings audit</summary><div class="scroll"><table class="table" id="drone-audit"></table></div>
            <p class="small muted">Sentinel only reads these values. Change them with the ground station, then record commissioning below.</p></details></div>
        <div class="card"><h3>Weather and conditions</h3>
          <p class="muted small">Confirm on site before each flight session (valid 1 hour).</p>
          <form id="drone-conditions" class="form-grid">
            <label><input type="checkbox" name="precipitation"> Rain, snow or fog</label>
            <label><input type="checkbox" name="visibility_ok" checked> Visibility good</label>
            <label>Wind (m/s) <input type="number" name="wind_mps" min="0" max="60" step="0.5"></label>
            <label>Temperature (°C) <input type="number" name="temperature_c" min="-60" max="60" step="1"></label>
            <label>Light <select name="daylight"><option value="true">Daylight</option><option value="false">Dark (dusk or night)</option></select></label>
            <label><input type="checkbox" name="lights_ok"> Anti-collision lights on</label>
            <button class="primary" type="submit">Confirm conditions</button>
          </form><div class="small muted" id="drone-conditions-state"></div></div>
        <div class="card" id="drone-commands-card"><h3>Simulator commands</h3>
          <p class="muted small">Only for simulators. Real aircraft are flown by the pilot; Sentinel refuses flight commands for them.</p>
          <div class="button-row"><button data-cmd="hold">Hold</button><button data-cmd="return_home">Return home</button><button data-cmd="land" class="danger">Land now</button></div></div>
        <div class="card"><h3>Companion computer</h3><div id="drone-companion"></div>
          <div class="button-row"><button id="drone-pair">Show pairing code</button></div><div class="small" id="drone-pair-code"></div></div>
        <div class="card"><h3>Maintenance</h3><div class="small" id="drone-maintenance"></div>
          <form id="drone-maintenance-form" class="form-grid">
            <label>Record <select name="kind"><option value="inspection">Pre-flight inspection</option><option value="propellers">Propellers replaced</option>
              <option value="battery">Battery service</option><option value="repair">Repair</option><option value="firmware">Firmware update</option>
              <option value="calibration">Calibration</option><option value="commissioning">Commissioning (save FC settings as reference)</option><option value="note">Note</option></select></label>
            <label>Note <input name="note" maxlength="1000"></label><button type="submit">Add record</button></form></div>
        <div class="card"><h3>Inspection locations</h3>
          <p class="muted small">Places a drone may be asked to look at. Each request still needs an operator's approval.</p><ul class="list compact" id="drone-points"></ul></div>
      </div>
    </div>`;
  $("#drone-conditions").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const number = (key) => (form.get(key) === "" ? null : Number(form.get(key)));
    try {
      await api("/drone/conditions", { method: "POST", body: JSON.stringify({ precipitation: form.has("precipitation"), visibility_ok: form.has("visibility_ok"),
        wind_mps: number("wind_mps"), temperature_c: number("temperature_c"), daylight: form.get("daylight") === "true", lights_ok: form.has("lights_ok") }) });
      renderDrone(await api("/drone"));
    } catch (error) { toast("Conditions not saved", error.message); }
  });
  $("#drone-maintenance-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    try {
      await api("/drone/maintenance", { method: "POST", body: JSON.stringify({ kind: form.get("kind"), note: form.get("note") }) });
      event.target.reset();
      toast("Maintenance recorded", "", "low");
      renderDrone(await api("/drone"));
    } catch (error) { toast("Not recorded", error.message); }
  });
  $("#drone-diagnose").addEventListener("click", async () => {
    try { $("#drone-diagnosis").innerHTML = droneDiagnosisHtml(await api("/drone/diagnosis")); }
    catch (error) { toast("No diagnosis", error.message); }
  });
  $("#drone-compare").addEventListener("click", async () => {
    const box = $("#drone-diagnosis");
    if (box.querySelector(".drone-compare")) { box.innerHTML = ""; return; }
    try { box.innerHTML = droneComparisonHtml(await api("/drone/models"), state.drone?.model?.id); }
    catch (error) { toast("Comparison unavailable", error.message); }
  });
  $("#drone-pair").addEventListener("click", async () => {
    try {
      const code = await api("/drone/companions/code", { method: "POST", body: JSON.stringify({}) });
      $("#drone-pair-code").innerHTML = `On the companion run <code>python -m sentinel.companion pair --hub ${esc(location.origin)} --code ${esc(code.code)}</code>
        <div class="muted">Valid once, until ${time(code.expires_ms)}.</div>`;
    } catch (error) { toast("No pairing code", error.message); }
  });
  document.querySelectorAll("#drone-commands-card [data-cmd]").forEach((button) => button.addEventListener("click", async () => {
    try { renderDrone(await api("/drone/commands", { method: "POST", body: JSON.stringify({ action: button.dataset.cmd, params: {} }) })); }
    catch (error) { toast("Command rejected", error.message); }
  }));
  loadDroneLogs();
}

async function loadDroneLogs() {
  try {
    const flights = await api("/drone/flights?limit=10");
    $("#drone-flights").innerHTML = "<tr><th>Started</th><th>Duration</th><th>Max alt.</th><th>Distance</th><th>Battery</th><th>Inspection</th></tr>" +
      (flights.map((f) => `<tr><td>${dateTime(f.started_ms)}${f.simulated ? ' <span class="muted small">sim</span>' : ""}${f.interrupted ? ' <span class="muted small">interrupted</span>' : ""}</td>
        <td>${f.duration_s == null ? "in flight" : `${Math.round(f.duration_s)} s`}</td><td>${f.max_alt_m} m</td><td>${f.distance_m} m</td>
        <td>${f.battery_start_pct ?? "?"}% → ${f.battery_end_pct ?? "?"}%</td><td class="small">${esc(f.request_id || "")}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">No flights yet.</td></tr>');
  } catch { /* the tab still works without the log */ }
}

function renderDrone(status) {
  state.drone = status;
  droneLayout();
  const t = status.telemetry;
  $("#drone-mode-notice").textContent = status.mode === "command"
    ? `SIMULATION (${status.adapter === "sitl" ? "ArduPilot SITL" : "built-in simulator"}): approved inspections are flown by Sentinel in the simulator.`
    : `${status.physical ? "Real or unverified aircraft" : "Simulator in pilot mode"}: approving an inspection exports a route for the pilot's ground station. Sentinel sends no flight commands (${status.mode_reason}).`;
  const advisories = status.advisories || [];
  $("#drone-advisories").hidden = !advisories.length || !["airborne", "returning", "landing", "link_lost"].includes(status.flight_state);
  $("#drone-advisories").textContent = advisories.length ? `${time(advisories.at(-1).ms)}: ${advisories.at(-1).text}` : "";
  const tile = (label, value) => `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`;
  const companion = status.companion;
  $("#drone-tiles").innerHTML = [
    tile("State", DRONE_STATE_LABELS[status.flight_state] || status.flight_state),
    tile("Flight mode", t.flight_mode),
    tile("Battery", t.battery_pct == null ? "?" : `${Math.round(t.battery_pct)}%`),
    tile("Altitude", `${t.alt_m.toFixed(1)} m`),
    tile("Speed", `${t.ground_speed_mps.toFixed(1)} m/s`),
    tile("GPS", t.gps_fix >= 3 ? `${t.satellites} sats${t.hdop != null ? ` · HDOP ${t.hdop}` : ""}` : "no fix"),
    tile("Link", t.link_ok ? "ok" : "LOST"),
    tile("Wind (FC estimate)", t.wind_mps == null ? "–" : `${t.wind_mps} m/s`),
    tile("Companion", companion ? companion.state : "none"),
    tile("Safety", status.safety_state),
  ].join("");
  const model = status.model || {};
  const offered = Object.entries(model.capabilities || {}).filter(([, value]) => value === "yes").map(([key]) => key);
  $("#drone-model").innerHTML = `<div><strong>${esc(model.label || model.name || "Drone")}</strong> <span class="small muted">${esc(model.camera || "")}</span></div>
    ${model.status === "reference_design" ? '<p class="small muted">Reference design: no Sentinel drone has been built or flown. Physical aircraft are telemetry-only.</p>' : ""}
    <div class="chips small">${offered.map((key) => `<span class="chip">${esc((DRONE_CAPABILITY_LABELS[key] || key))}</span>`).join("")}</div>
    ${(model.limitations || []).length ? `<details class="small"><summary>Limits of this model</summary><ul>${model.limitations.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></details>` : ""}`;
  const icon = { pass: "✓", fail: "✗", warn: "!", na: "–" };
  $("#drone-readiness").innerHTML = status.readiness.checks.map((c) => `<li class="row-between" data-check="${esc(c.id)}" data-state="${esc(c.state)}"><span>${icon[c.state]} ${esc(c.label)}</span>
    <span class="small muted wrap">${esc(c.detail)}</span></li>`).join("") +
    `<li><strong>${status.readiness.ready ? "Ready for an operator-approved inspection" : "Not ready"}</strong></li>`;
  const audit = status.parameter_audit;
  $("#drone-audit").innerHTML = audit ? "<tr><th>Setting</th><th>Now</th><th>Needed</th></tr>" + audit.findings.map((f) =>
    `<tr><td>${f.ok ? "✓" : f.blocking ? "✗" : "!"} ${esc(f.param)}</td><td class="wrap">${esc(f.actual)}</td><td class="wrap small">${esc(f.expected)}<div class="muted">${esc(f.why)}</div></td></tr>`).join("")
    : '<tr><td class="muted">Not applicable for the built-in simulator.</td></tr>';
  const conditions = status.conditions;
  $("#drone-conditions-state").textContent = conditions ? `Last confirmed ${time(conditions.recorded_ms)} by ${conditions.by}` : "Not confirmed yet.";
  $("#drone-commands-card").hidden = status.mode !== "command";
  const report = (companion && companion.report) || {};
  $("#drone-companion").innerHTML = companion
    ? `<div><strong>${esc(companion.name)}</strong> <span class="muted small">${esc(companion.state)}${companion.last_report_age_s != null ? ` · ${companion.last_report_age_s} s ago` : ""}</span></div>
       <div class="small muted">${esc((companion.hardware || {}).model || "")} · CPU ${(report.health || {}).cpu_temp_c ?? "?"} °C ·
       ${(report.relay || {}).hub_frames_dropped ?? 0} Hub message(s) blocked by the relay</div>
       <button class="danger small" data-revoke="${esc(companion.companion_id)}">Unpair</button>`
    : '<p class="small muted">No companion paired. Pairing gives the companion its own identity; its health then appears here.</p>';
  document.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Unpair this companion computer? It will stop reporting until paired again.")) return;
    try { await api(`/drone/companions/${encodeURIComponent(b.dataset.revoke)}`, { method: "DELETE" }); renderDrone(await api("/drone")); }
    catch (error) { toast("Not unpaired", error.message); }
  }));
  const maintenance = status.maintenance;
  $("#drone-maintenance").innerHTML = `${maintenance.flights} flight(s), ${Math.round(maintenance.flight_time_s / 60)} min in total ·
    ${maintenance.flights_since_inspection} since the last inspection${maintenance.inspection_due ? " <strong>(inspection due)</strong>" : ""} ·
    ${maintenance.commissioned ? "commissioned" : "<strong>not commissioned</strong>"}`;
  const video = status.video;
  $("#drone-video-card").hidden = !video;
  if (video) {
    $("#drone-video-detail").textContent = video.detail || video.state;
    const img = $("#drone-video");
    const live = video.state === "streaming";
    if (live && !img.getAttribute("src")) img.src = `${API}/drone/video.mjpg`;
    if (!live && img.getAttribute("src")) img.removeAttribute("src");
  }
  $("#drone-requests").innerHTML = status.requests.map((r) => {
    const obs = r.observations || {};
    const id = encodeURIComponent(r.request_id);
    return `<li data-request="${esc(r.request_id)}">
      <div><strong>${esc(r.target_label || "Custom point")}</strong> <span class="muted small">${esc(r.status.replace(/_/g, " "))}${r.phase && r.phase !== "done" ? ` · ${esc(r.phase.replace(/_/g, " "))}` : ""}</span></div>
      <div class="small muted">${dateTime(r.created_ms)} · by ${esc(r.requested_by)}${r.reason ? ` · ${esc(r.reason)}` : ""}${r.incident_id ? ` · ${esc(r.incident_id)}` : ""}${r.aircraft ? ` · ${esc(r.aircraft)}` : ""}</div>
      ${r.flight_estimate && r.flight_estimate.detail ? `<div class="small muted">Endurance: ${esc(r.flight_estimate.detail)}</div>` : ""}
      ${r.note ? `<div class="small">${esc(r.note)}</div>` : ""}
      <div class="small"><a href="${API}/drone/inspections/${id}/mission" download>Route (.plan)</a> · <a href="${API}/drone/inspections/${id}/mission?format=waypoints" download>.waypoints</a></div>
      ${r.status === "approved" ? `<div class="small"><strong>Approved:</strong> load the route in the ground station and fly it before ${time(r.approved_until_ms)}. Sentinel follows the flight from telemetry.</div>` : ""}
      ${(obs.text || []).length ? `<ul class="small">${obs.text.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : ""}
      ${r.status === "pending_approval" ? `<div class="button-row"><button class="primary" data-approve="${esc(r.request_id)}">Approve</button><button data-deny="${esc(r.request_id)}">Deny</button></div>` : ""}
    </li>`;
  }).join("") || '<li class="muted">No inspection requests.</li>';
  $("#drone-points").innerHTML = (state.site.drone_points || []).map((point) => `<li class="row-between"><span>${esc(point.name)}</span>
      <button data-inspect-point="${esc(point.id)}">Request inspection</button></li>`).join("") || '<li class="muted">None. Add them in Install → Zones and privacy.</li>';
  document.querySelectorAll("[data-inspect-point]").forEach((b) => b.addEventListener("click", () => requestInspection({ point_id: b.dataset.inspectPoint })));
  document.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", () => droneDecision(b.dataset.approve, "approve")));
  document.querySelectorAll("[data-deny]").forEach((b) => b.addEventListener("click", () => droneDecision(b.dataset.deny, "deny")));
  if (document.querySelector("#tab-map.active")) renderMap();
}

async function droneDecision(requestId, decision) {
  const command = state.drone && state.drone.mode === "command";
  if (decision === "approve" && !confirm(command ? "Approve this inspection? Sentinel will fly it in the simulator."
    : "Approve this inspection? The route is exported for the pilot, who decides whether and how to fly it.")) return;
  try {
    await api(`/drone/inspections/${encodeURIComponent(requestId)}/${decision}`, { method: "POST", body: JSON.stringify({}) });
    renderDrone(await api("/drone"));
    loadDroneLogs();
  } catch (error) {
    toast(`Could not ${decision}`, error.message);
  }
}

// ---- settings --------------------------------------------------------------

function renderStatic() {
  const privacy = state.site.privacy || {};
  const retention = privacy.retention_days || {};
  $("#privacy-info").innerHTML = `
    <dt>Clips</dt><dd>${privacy.store_clips === false ? "not stored" : "stored locally"}</dd>
    <dt>Snapshots</dt><dd>${privacy.store_snapshots === false ? "not stored" : "stored locally"}</dd>
    <dt>Event history</dt><dd>${esc(retention.events ?? 30)} days</dd>
    <dt>Media</dt><dd>${esc(retention.media ?? 7)} days</dd>
    <dt>Privacy masks</dt><dd>${state.site.cameras.reduce((n, c) => n + (c.privacy_masks || []).length, 0)} region(s)</dd>`;
  $("#rule-list").innerHTML = state.site.rules
    .map((rule) => `<li><span class="sev ${esc(rule.severity)}">${esc(rule.severity)}</span> <strong>${esc(rule.id)}</strong> <span class="muted">${esc(rule.type)}${rule.arm_modes ? ` · ${esc(rule.arm_modes.join("/"))} only` : ""}${rule.active_hours ? ` · ${esc(rule.active_hours.start)}–${esc(rule.active_hours.end)}` : ""}</span></li>`)
    .join("");
}

async function loadAudit() {
  try {
    const rows = await api("/audit?limit=50");
    $("#audit-list").innerHTML = rows.map((row) => `<li>${dateTime(row.timestamp_ms)} · <strong>${esc(row.actor)}</strong> ${esc(row.action)}</li>`).join("") || '<li class="muted">Empty</li>';
  } catch {
    /* shown on next load */
  }
}

// ---- system health ---------------------------------------------------------

const ago = (ms) => {
  if (!ms) return "";
  const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000));
  return seconds < 90 ? `${seconds} s` : seconds < 5400 ? `${Math.round(seconds / 60)} min` : `${(seconds / 3600).toFixed(1)} h`;
};
const duration = (seconds) => (seconds < 90 ? `${Math.round(seconds)} s` : seconds < 5400 ? `${Math.round(seconds / 60)} min` : `${(seconds / 3600).toFixed(1)} h`);
const num = (value, unit = "") => (value == null ? "–" : `${value}${unit}`);
let systemLoading = false;

async function loadSystem() {
  if (systemLoading) return;
  systemLoading = true;
  try {
    const [report, outages, backups] = await Promise.all([api("/system/health"), api("/system/outages?limit=100"), api("/system/backups")]);
    renderSystem(report, outages, backups);
  } catch (error) {
    $("#coverage-banner").textContent = `Could not load system health: ${error.message}`;
  } finally {
    systemLoading = false;
  }
}

// Each liveness signal is shown on its own, so a frozen picture behind a working
// decoder, or a stopped detector behind a live picture, is visible at a glance.
function signalBadges(signals) {
  if (!signals) return "";
  const names = { frames: "frames", decoder: "decoder", stream_clock: "stream clock", content: "picture", inference: "AI" };
  return Object.entries(names)
    .filter(([key]) => signals[key])
    .map(([key, label]) => {
      const ok = signals[key].ok;
      return `<span class="signal ${ok === true ? "ok" : ok === false ? "bad" : ""}" title="${esc(label)}: ${ok === null ? "not available" : ok ? "ok" : "check"}">${esc(label)}</span>`;
    })
    .join("");
}

function renderSystem(report, outages, backups) {
  const coverage = report.coverage;
  renderCoverage(coverage);
  const banner = $("#coverage-banner");
  banner.className = `banner ${coverage.state === "full" ? "good" : ["not_running", "no_cameras"].includes(coverage.state) ? "" : "bad"}`;
  const issues = [
    ...coverage.not_monitoring.map((camera) => `${camera.name}: ${camera.label}`),
    ...(coverage.unverified_cameras || []).map((camera) => `${camera.name}: ${camera.label} (check the picture)`),
    ...Object.values(coverage.components).filter((c) => c.state !== "ok").map((c) => `${c.label}: ${c.detail}`),
  ];
  banner.innerHTML = `<strong>${esc({ full: "All cameras are being monitored.", unverified: "Monitoring, but a still picture cannot be confirmed as live on some cameras.", degraded: "Security coverage is degraded.", none: "No camera is being monitored.", not_running: "Camera processing is not running in this session.", no_cameras: "There are no fixed cameras to monitor. Add them from the Install tab." }[coverage.state] || coverage.state)}</strong>
    ${issues.length ? `<ul class="small">${issues.map((issue) => `<li>${esc(issue)}</li>`).join("")}</ul>` : ""}`;
  $("#health-updated").textContent = `Updated ${time(report.time_ms)}`;
  $("#health-cameras").innerHTML = `<tr><th>Camera</th><th>State</th><th>Processed fps</th><th>Detect p95</th><th>Delay p95</th><th>Skipped frames</th><th>Reconnects</th><th>Restarts</th><th>AI</th><th>Signals</th><th>Issue</th><th></th></tr>` +
    report.cameras.map((camera) => {
      const health = camera.health || {};
      const metrics = camera.metrics || {};
      const issue = [health.reason, ...(health.warnings || []).map((w) => w.replace(/_/g, " ")), camera.worker_state === "restarting" && camera.next_restart_ms ? `retry ${time(camera.next_restart_ms)}` : ""].filter(Boolean).join(" · ");
      return `<tr data-health-camera="${esc(camera.camera_id)}">
        <td><strong>${esc(camera.name)}</strong></td>
        <td><span class="state ${esc(health.state || "starting")}">${esc(health.label || "Starting")}</span> <span class="small muted">${health.since_ms ? `for ${ago(health.since_ms)}` : ""}</span></td>
        <td>${num(camera.fps)} / ${num(camera.target_fps)}</td>
        <td>${num(metrics.detect_ms_p95, " ms")}</td>
        <td>${num(metrics.latency_ms_p95, " ms")}</td>
        <td>${metrics.frames_skipped == null ? "–" : `${metrics.frames_skipped} of ${metrics.frames_skipped + metrics.frames_processed}`}</td>
        <td>${num(camera.reconnects)}</td>
        <td>${num(camera.restarts_total)}</td>
        <td>${esc(camera.detector_provider || "–")}${camera.detector_state && camera.detector_state !== "ok" ? ` (${esc(camera.detector_state)})` : ""}</td>
        <td>${signalBadges(health.signals)}</td>
        <td class="wrap small">${esc(issue)}</td>
        <td>${camera.worker_state ? `<button data-restart="${esc(camera.camera_id)}">Restart</button>` : ""}</td></tr>`;
    }).join("");
  document.querySelectorAll("[data-restart]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/cameras/${encodeURIComponent(button.dataset.restart)}/restart`, { method: "POST", body: "{}" });
      toast("Camera restarting", "", "info");
      loadSystem();
    } catch (error) {
      toast("Restart failed", error.message);
    }
  }));
  const r = report.resources || {};
  const memory = r.system_memory || {};
  const disk = r.disk || {};
  $("#health-resources").innerHTML = `
    <dt>CPU (whole computer)</dt><dd>${num(r.system_cpu_pct, "%")}</dd>
    <dt>CPU (Sentinel)</dt><dd>${num(r.process_cpu_pct, "%")} of ${num(r.cpu_count)} threads</dd>
    <dt>Memory</dt><dd>${memory.total_mb ? `${num(memory.used_pct, "%")} used · ${Math.round(memory.available_mb / 1024 * 10) / 10} GB free` : "–"}</dd>
    <dt>Sentinel memory</dt><dd>${num(r.process_rss_mb, " MB")}</dd>
    <dt>GPU</dt><dd>${r.gpu ? `${esc(r.gpu.name)} · ${r.gpu.utilization_pct}% · ${Math.round(r.gpu.memory_used_mb)} / ${Math.round(r.gpu.memory_total_mb)} MB · ${r.gpu.temperature_c} °C` : "no counters (detector latency shows inference health)"}</dd>
    <dt>Running since</dt><dd>${dateTime(report.service.started_ms)} (${ago(report.service.started_ms)})</dd>
    ${report.service.stuck_threads ? `<dt>Stuck threads</dt><dd class="status-bad">${report.service.stuck_threads}</dd>` : ""}`;
  const storage = report.storage || {};
  const database = report.database || {};
  $("#health-storage").innerHTML = `
    <dt>Free disk</dt><dd class="${storage.low ? "status-bad" : ""}">${storage.free_mb != null ? `${Math.round(storage.free_mb / 102.4) / 10} GB (${storage.free_pct}%)` : disk.free_mb != null ? `${Math.round(disk.free_mb / 102.4) / 10} GB` : "–"}</dd>
    <dt>Keep free</dt><dd>${storage.threshold_mb != null ? `${Math.round(storage.threshold_mb / 102.4) / 10} GB` : "–"}</dd>
    <dt>Recordings</dt><dd>${storage.media_mb != null ? `${Math.round(storage.media_mb)} MB` : "–"} · ${storage.recording_paused ? '<span class="status-bad">recording paused (disk full)</span>' : "recording"}</dd>
    <dt>Freed for space</dt><dd>${num(storage.purged_total)} file(s)</dd>
    <dt>Event database</dt><dd class="${database.ok ? "" : "status-bad"}">${database.ok ? "saving normally" : `writes failing: ${esc(database.last_error)}`}</dd>
    ${database.recovery ? `<dt>Last repair</dt><dd>${esc(database.recovery.action.replace(/_/g, " "))}</dd>` : ""}`;
  $("#backup-list").innerHTML = backups.slice(0, 10).map((item) => `<li class="row-between"><span>${item.valid ? `${dateTime(item.created_ms)} · ${esc(item.label)} · ${Math.round(item.size_bytes / 1024)} KB` : `${esc(item.name)} (unreadable)`}</span>
      ${item.valid ? `<span class="button-row tight"><button data-verify="${esc(item.name)}">Verify</button><button data-restore="${esc(item.name)}">Restore</button></span>` : ""}</li>`).join("") ||
    '<li class="muted">No backups yet. One is made automatically every day.</li>';
  document.querySelectorAll("[data-verify]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const result = await api(`/system/backups/${encodeURIComponent(button.dataset.verify)}/verify`, { method: "POST", body: "{}" });
      toast(result.ok ? "Backup verified" : "Backup is damaged", result.problems.join("; "), result.ok ? "info" : "high");
    } catch (error) {
      toast("Verify failed", error.message);
    }
  }));
  document.querySelectorAll("[data-restore]").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("Restore this backup? The configuration and settings change now; event history is replaced at the next start. The current state is backed up first.")) return;
    try {
      const result = await api(`/system/backups/${encodeURIComponent(button.dataset.restore)}/restore`, { method: "POST", body: JSON.stringify({ parts: ["config", "settings", "database"] }) });
      toast("Backup restored", `Applied: ${result.applied.join(", ") || "nothing"}. ${result.staged.length ? "Event history is restored at the next start." : ""}`, "info");
      loadSystem();
    } catch (error) {
      toast("Restore failed", error.message);
    }
  }));
  $("#outage-table").innerHTML = `<tr><th>Started</th><th>What</th><th>State</th><th>Duration</th><th>Reason</th></tr>` +
    (outages.map((outage) => `<tr><td>${dateTime(outage.started_ms)}</td>
      <td>${esc(outage.camera_id ? (state.site.cameras.find((c) => c.id === outage.camera_id) || {}).name || outage.camera_id : outage.scope)}</td>
      <td>${esc(outage.states.join(" → ").replace(/_/g, " "))}</td>
      <td>${outage.ended_ms ? duration(outage.duration_s) : `<span class="status-bad">ongoing · ${duration(outage.duration_s)}</span>`}</td>
      <td class="wrap small">${esc(outage.reason)}${outage.notified ? " · notified" : ""}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">No outages recorded.</td></tr>');
}

$("#backup-now").addEventListener("click", async () => {
  try {
    const info = await api("/system/backups", { method: "POST", body: JSON.stringify({ label: "manual" }) });
    toast("Backup written", info.name, "info");
    loadSystem();
  } catch (error) {
    toast("Backup failed", error.message);
  }
});

// ---- live stream -----------------------------------------------------------

let source = null;
function connectStream() {
  if (source) source.close();
  source = new EventSource(`${API}/stream`);
  source.onopen = () => {
    $("#live-dot").classList.add("on");
    $("#live-dot").textContent = "live";
  };
  source.onerror = () => {
    $("#live-dot").classList.remove("on");
    $("#live-dot").textContent = "reconnecting";
  };
  source.addEventListener("event", (message) => {
    const event = JSON.parse(message.data);
    state.activity.unshift(event);
    state.activity.length = Math.min(state.activity.length, 200);
    if (event.zone_id) {
      state.activeZones.set(event.zone_id, Date.now());
      if (document.querySelector("#tab-map.active")) renderMap();
    }
    renderActivity();
  });
  source.addEventListener("incident", (message) => {
    const incident = JSON.parse(message.data);
    const index = state.incidents.findIndex((item) => item.incident_id === incident.incident_id);
    if (index >= 0) state.incidents[index] = incident;
    else state.incidents.unshift(incident);
    const openIndex = (state.openIncidents || []).findIndex((item) => item.incident_id === incident.incident_id);
    if (openIndex >= 0) state.openIncidents[openIndex] = incident;
    else if (state.openIncidents) state.openIncidents.unshift(incident);
    renderIncidents();
  });
  source.addEventListener("notification", (message) => {
    const notification = JSON.parse(message.data);
    toast(notification.title, notification.message.split("\n")[0], notification.severity);
  });
  source.addEventListener("settings", (message) => renderSettings(JSON.parse(message.data)));
  source.addEventListener("drone", (message) => renderDrone(JSON.parse(message.data)));
  source.addEventListener("drone_inspection", (message) => {
    const request = JSON.parse(message.data);
    toast(`Drone inspection ${request.status.replace(/_/g, " ")}`, request.target_label || "", "low");
    loadDroneLogs();
  });
  source.addEventListener("config", () => refreshAfterConfigChange());
  source.addEventListener("health", (message) => renderCoverage(JSON.parse(message.data)));
}

setInterval(async () => {
  const cutoff = Date.now() - 60_000;
  for (const [zone, seen] of state.activeZones) if (seen < cutoff) state.activeZones.delete(zone);
  try {
    const [cameras, live] = await Promise.all([api("/cameras"), api("/state")]);
    updateCameraStatus(cameras.map((camera) => camera.status));
    renderCoverage(live.coverage);
    if (document.querySelector("#tab-system.active")) loadSystem();
  } catch {
    /* connection indicator covers this */
  }
}, 5000);

async function refreshAfterConfigChange() {
  try {
    state.site = await api("/site");
    renderCameras(await api("/cameras"));
    renderStatic();
    const droneTab = document.querySelector('[data-tab="drone"]');
    droneTab.hidden = !state.site.drone_enabled;
    if (state.site.drone_enabled) renderDrone(await api("/drone"));
    else state.drone = null;
    renderMap();
    if (typeof onSetupConfigChanged === "function") onSetupConfigChanged();
    if (typeof fillSearchOptions === "function") fillSearchOptions();
  } catch {
    /* next event or reload will retry */
  }
}

async function boot() {
  try {
    state.site = await api("/site");
  } catch {
    return;
  }
  $("#site-name").textContent = state.site.site.name || state.site.site.id;
  renderSettings(await api("/settings"));
  renderCameras(await api("/cameras"));
  renderStatic();
  renderCoverage((await api("/state")).coverage);
  if (typeof fillSearchOptions === "function") fillSearchOptions();
  await Promise.all([loadIncidents(), loadActivity()]);
  if (state.site.drone_enabled) renderDrone(await api("/drone"));
  else document.querySelector('[data-tab="drone"]').hidden = true;
  renderMap();
  connectStream();
}

boot();
