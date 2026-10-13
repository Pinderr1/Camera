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
  grid.innerHTML = cameras
    .map(
      (camera) => `
      <article class="card camera" data-camera="${esc(camera.id)}">
        <img src="${API}/cameras/${encodeURIComponent(camera.id)}/stream.mjpg" alt="${esc(camera.name || camera.id)} live view" loading="lazy">
        <div class="meta"><strong>${esc(camera.name || camera.id)}</strong><span class="status"></span></div>
      </article>`,
    )
    .join("");
  updateCameraStatus(cameras.map((camera) => camera.status || {}));
}

function updateCameraStatus(statuses) {
  for (const status of statuses) {
    const node = document.querySelector(`[data-camera="${CSS.escape(status.camera_id || "")}"] .status`);
    if (!node) continue;
    node.className = `status ${status.connected ? "status-ok" : "status-bad"}`;
    node.textContent = status.connected ? `${status.fps || 0} fps` : status.error || "offline";
  }
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

async function loadIncidents() {
  state.incidents = await api("/incidents?limit=100");
  renderIncidents();
}

function renderIncidents() {
  const includeClosed = $("#show-closed").checked;
  const rows = state.incidents.filter((incident) => includeClosed || incident.status === "open");
  const open = state.incidents.filter((incident) => incident.status === "open" && !incident.acknowledged_ms).length;
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
        <span class="small muted">${incident.resolution ? esc(incident.resolution.replace("_", " ")) : incident.acknowledged_ms ? "acknowledged" : esc(incident.status)}</span>
      </li>`,
      )
      .join("") || '<li class="muted">No incidents yet. Ordinary activity is logged below without alerting.</li>';
  document.querySelectorAll("[data-incident]").forEach((node) => node.addEventListener("click", () => openIncident(node.dataset.incident)));
}

async function openIncident(incidentId) {
  state.selectedIncident = incidentId;
  renderIncidents();
  const incident = await api(`/incidents/${encodeURIComponent(incidentId)}`);
  const events = [...incident.events].sort((a, b) => a.timestamp_ms - b.timestamp_ms);
  const snapshots = events.filter((event) => event.snapshot_path).slice(0, 4);
  const clips = incident.clip_paths || [];
  $("#incident-detail").innerHTML = `
    <div class="row-between"><h3>${esc(incident.title)}</h3><span class="sev ${esc(incident.severity)}">${esc(incident.severity)}</span></div>
    <div class="small muted">Opened ${dateTime(incident.opened_ms)} · ${esc(incident.cameras.join(", "))}</div>
    <div class="media">
      ${clips.map((clip) => `<video controls preload="metadata" src="${API}/media/${clip.split("/").map(encodeURIComponent).join("/")}"></video>`).join("")}
      ${snapshots.map((event) => `<img alt="${esc(event.summary)}" src="${API}/media/${event.snapshot_path.split("/").map(encodeURIComponent).join("/")}">`).join("")}
    </div>
    ${clips.length ? `<div class="small muted">Clip will not play? <a href="${API}/media/${clips[0].split("/").map(encodeURIComponent).join("/")}" download>Download it</a> (this OpenCV build may lack H.264).</div>` : ""}
    <ul class="list compact">${events
      .map(
        (event) => `<li class="event-row"><span class="muted">${time(event.timestamp_ms)}</span>
        <span><span class="sev ${esc(event.severity)}">${esc(event.severity)}</span> ${esc(event.summary)}
        <span class="muted">· ${Math.round(event.confidence * 100)}% · ${esc(event.reason_codes.join(", "))}</span>
        ${event.details && event.details.identity_note ? `<div class="small muted">${esc(event.details.identity_note)}</div>` : ""}
        ${event.details && event.details.correlation_note ? `<div class="small muted">${esc(event.details.correlation_note)}</div>` : ""}</span></li>`,
      )
      .join("")}</ul>
    <p class="small muted">Sentinel describes observed activity. It does not determine identity or intent.</p>
    <div class="button-row">
      <button id="ack" ${incident.acknowledged_ms ? "disabled" : ""}>Acknowledge</button>
      <button data-outcome="expected_activity">Expected activity</button>
      <button data-outcome="false_alarm">False alarm</button>
      <button data-outcome="real_concern" class="danger">Real concern</button>
      ${state.site.drone_enabled && incident.zones.length ? `<button id="inspect-incident">Request drone inspection</button>` : ""}
    </div>
    ${incident.resolution ? `<div class="small muted">Resolved as ${esc(incident.resolution)} by ${esc(incident.resolved_by)}</div>` : ""}`;
  $("#ack").addEventListener("click", () => incidentAction(incidentId, "acknowledge"));
  document.querySelectorAll("[data-outcome]").forEach((button) =>
    button.addEventListener("click", () => incidentAction(incidentId, "resolve", { outcome: button.dataset.outcome })),
  );
  const inspect = $("#inspect-incident");
  if (inspect) {
    const zoneId = [...incident.zones].reverse().find((id) => findZone(id)?.zone.map_polygon);
    inspect.disabled = !zoneId;
    inspect.addEventListener("click", () => requestInspection({ zone_id: zoneId, incident_id: incidentId }));
  }
}

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

async function requestInspection(body) {
  try {
    await api("/drone/inspections", { method: "POST", body: JSON.stringify(body) });
    toast("Inspection requested", "Approve it on the Drone tab to fly.", "medium");
    renderDrone(await api("/drone"));
  } catch (error) {
    toast("Inspection not requested", error.message);
  }
}

function renderDrone(status) {
  state.drone = status;
  const t = status.telemetry;
  $("#drone-mode-notice").textContent = status.physical
    ? "Physical drone connected in TELEMETRY-ONLY mode. Flight commands are disabled in this build."
    : "SIMULATION: no real aircraft is connected. Commands drive the simulator only.";
  const tile = (label, value) => `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`;
  $("#drone-tiles").innerHTML = [
    tile("Mode", t.flight_mode),
    tile("Battery", t.battery_pct == null ? "?" : `${Math.round(t.battery_pct)}%`),
    tile("Altitude", `${t.alt_m.toFixed(1)} m`),
    tile("Speed", `${t.ground_speed_mps.toFixed(1)} m/s`),
    tile("GPS", t.gps_fix >= 3 ? `3D · ${t.satellites} sats` : "no fix"),
    tile("Link", t.link_ok ? "ok" : "LOST"),
    tile("Dock", t.docked ? (t.charging ? "charging" : "docked") : "away"),
    tile("Safety", status.safety_state),
  ].join("");
  $("#drone-blockers").textContent = status.preflight_blockers.length ? `Launch blocked: ${status.preflight_blockers.join(", ")}` : "Preflight checks pass.";
  $("#drone-requests").innerHTML =
    status.requests
      .map(
        (request) => `<li>
        <div><strong>${esc(request.target_label || "Custom point")}</strong> <span class="muted small">${esc(request.status.replace("_", " "))}${request.phase ? ` · ${esc(request.phase)}` : ""}</span></div>
        <div class="small muted">${dateTime(request.created_ms)} · by ${esc(request.requested_by)}${request.incident_id ? ` · ${esc(request.incident_id)}` : ""}</div>
        ${request.status === "pending_approval" ? `<div class="button-row"><button class="primary" data-approve="${esc(request.request_id)}">Approve flight</button><button data-deny="${esc(request.request_id)}">Deny</button></div>` : ""}
      </li>`,
      )
      .join("") || '<li class="muted">No inspection requests.</li>';
  document.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", () => droneDecision(b.dataset.approve, "approve")));
  document.querySelectorAll("[data-deny]").forEach((b) => b.addEventListener("click", () => droneDecision(b.dataset.deny, "deny")));
  if (document.querySelector("#tab-map.active")) renderMap();
}

async function droneDecision(requestId, decision) {
  if (decision === "approve" && !confirm("Approve this inspection flight? The drone will take off.")) return;
  try {
    await api(`/drone/inspections/${encodeURIComponent(requestId)}/${decision}`, { method: "POST", body: JSON.stringify({}) });
    renderDrone(await api("/drone"));
  } catch (error) {
    toast(`Could not ${decision}`, error.message);
  }
}

document.querySelectorAll("[data-cmd]").forEach((button) =>
  button.addEventListener("click", async () => {
    const action = button.dataset.cmd;
    if (action === "takeoff" && !confirm("Take off now?")) return;
    try {
      renderDrone(await api("/drone/commands", { method: "POST", body: JSON.stringify({ action, params: {} }) }));
    } catch (error) {
      toast("Command rejected", error.message);
    }
  }),
);

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
    renderIncidents();
  });
  source.addEventListener("notification", (message) => {
    const notification = JSON.parse(message.data);
    toast(notification.title, notification.message.split("\n")[0], notification.severity);
  });
  source.addEventListener("settings", (message) => renderSettings(JSON.parse(message.data)));
  source.addEventListener("drone", (message) => renderDrone(JSON.parse(message.data)));
  source.addEventListener("config", () => refreshAfterConfigChange());
}

setInterval(async () => {
  const cutoff = Date.now() - 60_000;
  for (const [zone, seen] of state.activeZones) if (seen < cutoff) state.activeZones.delete(zone);
  try {
    const cameras = await api("/cameras");
    updateCameraStatus(cameras.map((camera) => camera.status));
  } catch {
    /* connection indicator covers this */
  }
}, 5000);

async function refreshAfterConfigChange() {
  try {
    state.site = await api("/site");
    renderCameras(await api("/cameras"));
    renderStatic();
    renderMap();
    if (typeof onSetupConfigChanged === "function") onSetupConfigChanged();
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
  await Promise.all([loadIncidents(), loadActivity()]);
  if (state.site.drone_enabled) renderDrone(await api("/drone"));
  else document.querySelector('[data-tab="drone"]').hidden = true;
  renderMap();
  connectStream();
}

boot();
