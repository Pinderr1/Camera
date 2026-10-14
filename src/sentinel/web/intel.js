"use strict";
// Security intelligence in the dashboard: the rule builder and rule tester
// (Setup → Rules), incident investigation (explanation, timeline, replay,
// feedback, notes, export), missed-event reports, activity insights and alert
// performance. Uses $, esc, api, toast, time, dateTime, state, loadIncidents,
// incidentAction, requestInspection, findZone and OUTCOME_LABELS from app.js.

const SEV_COLOR = { info: "var(--info)", low: "var(--low)", medium: "var(--medium)", high: "var(--high)" };
const mediaUrl = (path) => `${API}/media/${path.split("/").map(encodeURIComponent).join("/")}`;
const pct = (value) => (value == null ? "–" : `${Math.round(value * 100)}%`);
const localInput = (ms) => {
  const d = new Date(ms - new Date(ms).getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
};

// ---- incident search and missed events -------------------------------------------

$("#incident-search").addEventListener("submit", (event) => {
  event.preventDefault();
  loadIncidents().catch((error) => toast("Search failed", error.message));
});
$("#incident-search").addEventListener("reset", () => setTimeout(() => loadIncidents().catch(() => {}), 0));

function fillSearchOptions() {
  if (!state.site) return;
  const form = $("#incident-search");
  const keep = (select) => select.value;
  const camera = form.elements.camera_id;
  const zone = form.elements.zone_id;
  const [cameraValue, zoneValue] = [keep(camera), keep(zone)];
  camera.innerHTML = '<option value="">Any camera</option>' + state.site.cameras.map((c) => `<option value="${esc(c.id)}">${esc(c.name || c.id)}</option>`).join("");
  zone.innerHTML = '<option value="">Any zone</option>' + state.site.cameras.flatMap((c) => (c.zones || []).map((z) => `<option value="${esc(z.id)}">${esc(z.name)} (${esc(c.name || c.id)})</option>`)).join("");
  camera.value = cameraValue;
  zone.value = zoneValue;
  const rules = form.elements.rule_id;
  const ruleValue = rules.value;
  rules.innerHTML = '<option value="">Any rule</option>' + (state.site.rules || []).map((r) => `<option value="${esc(r.id)}">${esc(r.name || r.id)}</option>`).join("");
  rules.value = ruleValue;
}

$("#report-missed").addEventListener("click", () => {
  const form = $("#missed-form");
  form.elements.when.value = localInput(Date.now());
  form.elements.camera_id.innerHTML = state.site.cameras.map((c) => `<option value="${esc(c.id)}">${esc(c.name || c.id)}</option>`).join("");
  const fillZones = () => {
    const camera = state.site.cameras.find((c) => c.id === form.elements.camera_id.value);
    form.elements.zone_id.innerHTML = '<option value="">Not sure</option>' + ((camera && camera.zones) || []).map((z) => `<option value="${esc(z.id)}">${esc(z.name)}</option>`).join("");
  };
  form.elements.camera_id.onchange = fillZones;
  fillZones();
  $("#missed-dialog").showModal();
});
$("#missed-close").addEventListener("click", () => $("#missed-dialog").close());
$("#missed-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  try {
    const report = await api("/missed", {
      method: "POST",
      body: JSON.stringify({
        occurred_ms: new Date(form.elements.when.value).getTime(), camera_id: form.elements.camera_id.value, zone_id: form.elements.zone_id.value,
        category: form.elements.category.value, description: form.elements.description.value,
      }),
    });
    $("#missed-dialog").close();
    toast("Missed event recorded", `Sentinel had ${report.sentinel_recorded.length} event(s) on that camera within 5 minutes. Used for evaluation only.`, "info");
    if (document.querySelector("#isub-performance.active")) loadPerformance();
  } catch (error) {
    toast("Not saved", error.message);
  }
});

// ---- incident investigation -------------------------------------------------------

async function openIncident(incidentId) {
  state.selectedIncident = incidentId;
  renderIncidents();
  let incident, explanation;
  try {
    [incident, explanation] = await Promise.all([api(`/incidents/${encodeURIComponent(incidentId)}`), api(`/incidents/${encodeURIComponent(incidentId)}/explanation`)]);
  } catch (error) {
    toast("Could not open incident", error.message);
    return;
  }
  const clips = incident.clip_paths || [];
  const snapshots = explanation.evidence.filter((item) => item.type === "snapshot");
  const outcome = incident.resolution;
  $("#incident-detail").innerHTML = `
    <div class="row-between"><h3>${esc(incident.title)}</h3><span class="sev ${esc(incident.severity)}">${esc(incident.severity)}</span></div>
    <div class="small muted">Opened ${dateTime(incident.opened_ms)} · ${esc(incident.status)}${incident.acknowledged_ms ? ` · acknowledged by ${esc(incident.acknowledged_by)}` : ""}${outcome ? ` · ${esc(OUTCOME_LABELS[outcome] || outcome)}` : ""}</div>
    <p class="explain-headline">${esc(explanation.headline)}</p>
    <div id="incident-timeline"></div>
    <div class="small muted">Solid markers are direct observations; hollow, dashed markers are Sentinel's inferences (links between cameras, related visits). Click a marker to replay that moment.</div>
    ${clips.length ? `<label class="inline small">Recording <select id="clip-select">${clips.map((clip, index) => `<option value="${index}">${esc(explanationClipLabel(explanation, clip))}</option>`).join("")}</select></label>
      <video id="incident-video" controls preload="metadata" class="preview" src="${mediaUrl(clips[0])}"></video>
      <div class="small muted">Clip will not play? <a id="clip-download" href="${mediaUrl(clips[0])}" download>Download it</a> (this OpenCV build may lack H.264).</div>` : '<p class="muted small">No recording was kept for this incident.</p>'}
    ${snapshots.length ? `<div class="media">${snapshots.slice(0, 6).map((item) => `<img alt="Snapshot from ${esc(item.camera)} at ${time(item.time_ms)}" src="${mediaUrl(item.path)}">`).join("")}</div>` : ""}
    <div class="explain">${explanationHtml(explanation)}</div>
    <h4>Your review</h4>
    <div class="button-row feedback">
      <button id="ack" ${incident.acknowledged_ms ? "disabled" : ""}>Acknowledge</button>
      ${Object.entries(OUTCOME_LABELS).map(([value, label]) => `<button data-outcome="${value}" class="${outcome === value ? "chosen" : ""} ${value === "real_concern" ? "danger" : ""}">${esc(label)}</button>`).join("")}
      ${state.site.drone_enabled && incident.zones.length ? `<button id="inspect-incident">Request drone inspection</button>` : ""}
    </div>
    <p class="muted small">Your feedback is stored to measure how well rules work. It never retrains detection or changes rules on its own.</p>
    <ul class="list compact" id="note-list">${(incident.notes || []).map((note) => `<li>${dateTime(note.ms)} · <strong>${esc(note.by)}</strong>: ${esc(note.text)}</li>`).join("") || '<li class="muted">No notes.</li>'}</ul>
    <form class="search-form" id="note-form"><input type="text" name="text" maxlength="2000" placeholder="Add a note (what you found, who it was if known to you)" aria-label="Note" style="flex:1"><button type="submit">Add note</button></form>
    <label class="field checkbox small"><input type="checkbox" id="protect" ${incident.protected ? "checked" : ""}> Keep these recordings (excluded from automatic deletion and disk-space clean-up)</label>
    <div class="button-row"><a class="button-link" href="${API}/incidents/${encodeURIComponent(incidentId)}/export" download>Export report</a>
      <a class="button-link" href="${API}/incidents/${encodeURIComponent(incidentId)}/export?include_media=true" download>Export with recordings</a></div>
    <p class="small muted">${esc(explanation.statement)}</p>`;
  renderIncidentTimeline(explanation, incident);
  $("#ack").addEventListener("click", () => incidentAction(incidentId, "acknowledge"));
  $("#protect").addEventListener("change", (event) => incidentAction(incidentId, "protect", { protected: event.target.checked }));
  document.querySelectorAll("#incident-detail [data-outcome]").forEach((button) =>
    button.addEventListener("click", () => incidentAction(incidentId, "resolve", { outcome: button.dataset.outcome })),
  );
  $("#note-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = event.target.elements.text.value.trim();
    if (!text) return;
    try {
      await api(`/incidents/${encodeURIComponent(incidentId)}/notes`, { method: "POST", body: JSON.stringify({ text }) });
      openIncident(incidentId);
    } catch (error) {
      toast("Note not saved", error.message);
    }
  });
  const select = $("#clip-select");
  if (select) {
    select.addEventListener("change", () => {
      const clip = clips[Number(select.value)];
      $("#incident-video").src = mediaUrl(clip);
      $("#clip-download").href = mediaUrl(clip);
    });
    $("#incident-video").addEventListener("timeupdate", () => moveTimelineCursor(incident, clips[Number(select.value)]));
  }
  const inspect = $("#inspect-incident");
  if (inspect) {
    const zoneId = [...incident.zones].reverse().find((id) => findZone(id)?.zone.map_polygon);
    inspect.disabled = !zoneId;
    inspect.addEventListener("click", () => requestInspection({ zone_id: zoneId, incident_id: incidentId }));
  }
}

function explanationClipLabel(explanation, clip) {
  const item = explanation.evidence.find((entry) => entry.type === "clip" && entry.path === clip);
  return item ? `${item.camera || "camera"}${item.started_ms ? ` from ${time(item.started_ms)}` : ""}` : clip.split("/").pop();
}

function explanationHtml(x) {
  const list = (items) => `<ul>${items.join("") || '<li class="muted">None</li>'}</ul>`;
  return `
    <h4>1 · What was detected</h4>
    ${list(x.objects.map((o) => `<li>${esc(o.category)}${o.kinds.length ? ` (${esc(o.kinds.join(", "))})` : ""}: detector confidence ${pct(o.confidence_min)}–${pct(o.confidence_max)}${o.detections ? `, tracked over ${o.detections} detections` : ""}</li>`))}
    <h4>2 · Cameras</h4>
    ${list(x.cameras.map((c) => `<li>${esc(c.name)}: ${time(c.first_ms)}–${time(c.last_ms)}</li>`))}
    <h4>3 · Zones entered</h4>
    ${list(x.zones.map((z) => `<li>${esc(z.name)} <span class="muted">(${esc(z.type)})</span> from ${time(z.first_ms)}</li>`))}
    <h4>4 · Sequence</h4>
    <div class="scroll"><table class="table" id="explain-sequence"><tr><th>Time</th><th>Camera</th><th>Observation</th><th>Confidence</th><th>Basis</th></tr>
      ${x.sequence.map((s) => `<tr><td>${time(s.time_ms)}</td><td>${esc(s.camera)}</td><td class="wrap"><span class="sev ${esc(s.severity)}">${esc(s.severity)}</span> ${esc(s.summary)}</td><td>${pct(s.confidence)}</td>
        <td class="wrap"><span class="basis ${s.basis}">${s.basis === "observed" ? "Observed" : "Inferred"}</span>${s.note ? ` <span class="small muted">${esc(s.note)}</span>` : ""}</td></tr>`).join("")}</table></div>
    <h4>5 · Rules that matched</h4>
    ${list(x.rules.map((r) => `<li><strong>${esc(r.name)}</strong> <span class="muted">(${esc(r.type)})</span>: ${esc(r.description.what)}${r.description.when ? `; ${esc(r.description.when)}` : ""}.
      ${r.matched.map((m) => `<div class="small muted">${time(m.time_ms)} · ${esc(m.summary)}${Object.keys(m.details).length ? ` · ${esc(Object.entries(m.details).map(([k, v]) => `${k.replace(/_/g, " ")}: ${Array.isArray(v) ? v.join(" → ") : v}`).join(", "))}` : ""}</div>`).join("")}
      ${r.downgraded.map((d) => `<div class="small muted">${esc(d)}</div>`).join("")}</li>`))}
    <h4>6 · Confidence and uncertainty</h4>
    ${list([`<li>Detection confidence: ${pct(x.confidence.detection_min)} to ${pct(x.confidence.detection_max)}</li>`,
      ...x.confidence.cross_camera_links.map((l) => `<li>Inferred link ${esc(l.from)} → ${esc(l.to)} at ${time(l.time_ms)}: ${pct(l.confidence)} (timing and camera connections, not recognition)</li>`),
      ...x.confidence.uncertainty.map((u) => `<li>${esc(u)}</li>`)])}
    <h4>7 · Why this severity</h4>
    ${list(x.severity.reasons.map((r) => `<li>${esc(r)}</li>`))}
    <h4>8 · Supporting recordings</h4>
    ${list(x.evidence.map((e) => `<li>${e.type === "clip" ? "Clip" : e.type === "drone_snapshot" ? "Drone snapshot" : "Snapshot"} · ${esc(e.camera)} · ${time(e.started_ms || e.time_ms)} · <a href="${mediaUrl(e.path)}" target="_blank" rel="noopener">open</a></li>`))}
    ${(x.drone_inspections || []).length ? `<h4>9 · Drone inspection</h4>
      <p class="small muted">Reported separately from the fixed cameras: a moving camera has no zones, and the drone cannot tell whether it sees the same person.</p>
      ${x.drone_inspections.map((d) => `<div class="card inner" data-drone-inspection="${esc(d.request_id)}">
        <div><strong>${esc(d.target_label || "Inspection")}</strong> <span class="muted small">${esc(d.status.replace(/_/g, " "))} · ${esc(d.mode === "command" ? "flown by Sentinel (simulator)" : "flown by the pilot")}</span></div>
        <div class="small muted">Requested by ${esc(d.requested_by)}${d.reason ? ` because: ${esc(d.reason)}` : ""} · approved by ${esc(d.decided_by || "nobody")} ${d.decided_ms ? time(d.decided_ms) : ""}${d.note ? ` · ${esc(d.note)}` : ""}</div>
        ${list(((d.observations || {}).text || ["No drone video was analysed."]).map((line) => `<li>${esc(line)}</li>`))}
      </div>`).join("")}` : ""}`;
}

let timelineScale = null;

function renderIncidentTimeline(x, incident) {
  const holder = $("#incident-timeline");
  const lanes = [...new Set([...x.cameras.map((c) => c.name), ...x.evidence.filter((e) => e.camera).map((e) => e.camera)])];
  const times = x.sequence.flatMap((s) => [s.started_ms, s.time_ms]).concat(Object.values(incident.clip_starts || {}).map((c) => c.started_ms)).filter(Boolean);
  if (!times.length) {
    holder.innerHTML = "";
    return;
  }
  const t0 = Math.min(...times) - 2000;
  const t1 = Math.max(...times) + 4000;
  const width = 800, left = 130, right = 20, laneH = 34, top = 18;
  const height = top + lanes.length * laneH + 22;
  const x_ = (t) => left + ((t - t0) / (t1 - t0)) * (width - left - right);
  const laneY = (camera) => top + lanes.indexOf(camera) * laneH + laneH / 2;
  timelineScale = { x: x_, top, bottom: top + lanes.length * laneH };
  const parts = [`<svg class="timeline-svg" id="timeline-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Incident timeline">`];
  lanes.forEach((camera, index) => {
    parts.push(`<rect class="lane" x="${left}" y="${top + index * laneH + 3}" width="${width - left - right}" height="${laneH - 6}" rx="4"/>`);
    parts.push(`<text x="8" y="${top + index * laneH + laneH / 2 + 4}">${esc(camera)}</text>`);
  });
  for (const [path, clip] of Object.entries(incident.clip_starts || {})) {
    const camera = x.evidence.find((e) => e.path === path)?.camera;
    if (camera && lanes.includes(camera)) parts.push(`<rect x="${x_(clip.started_ms)}" y="${laneY(camera) + 9}" width="${Math.max(4, x_(Math.min(t1, clip.started_ms + 60000)) - x_(clip.started_ms))}" height="3" fill="var(--muted)" opacity="0.5"><title>Recording</title></rect>`);
  }
  for (const link of x.confidence.cross_camera_links) {
    if (!lanes.includes(link.from) || !lanes.includes(link.to)) continue;
    const before = x.sequence.filter((s) => s.camera === link.from && s.time_ms <= link.time_ms).pop();
    const fromX = before ? x_(before.time_ms) : x_(link.time_ms) - 10;
    parts.push(`<path class="link" d="M${fromX},${laneY(link.from)} L${x_(link.time_ms)},${laneY(link.to)}"><title>Inferred link ${esc(link.from)} → ${esc(link.to)} (${pct(link.confidence)})</title></path>`);
  }
  const placed = {};
  x.sequence.forEach((s, index) => {
    const fill = SEV_COLOR[s.severity] || "var(--info)";
    // Events at (nearly) the same moment on one camera are stacked so each marker stays clickable.
    const cx = x_(s.time_ms);
    const near = (placed[s.camera] ||= []).filter((other) => Math.abs(other - cx) < 12).length;
    placed[s.camera].push(cx);
    const cy = laneY(s.camera) + (near ? (near % 2 ? -1 : 1) * 8 * Math.ceil(near / 2) : 0);
    parts.push(`<circle class="marker ${s.basis}" data-step="${index}" cx="${cx}" cy="${cy}" r="${near ? 5 : 7}" fill="${fill}" stroke="${s.basis === "inferred" ? fill : ""}">
      <title>${esc(time(s.time_ms))} ${esc(s.summary)} (${s.basis})</title></circle>`);
  });
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const t = t0 + ((t1 - t0) * i) / ticks;
    parts.push(`<text x="${x_(t)}" y="${height - 6}" text-anchor="middle">${esc(time(t))}</text>`);
  }
  parts.push(`<line class="cursor" id="timeline-cursor" x1="-10" x2="-10" y1="${top}" y2="${top + lanes.length * laneH}"/></svg>`);
  holder.innerHTML = parts.join("");
  holder.querySelectorAll("[data-step]").forEach((node) => node.addEventListener("click", () => seekTo(x.sequence[Number(node.dataset.step)], incident)));
}

function moveTimelineCursor(incident, clip) {
  const start = (incident.clip_starts || {})[clip]?.started_ms;
  const video = $("#incident-video");
  const cursor = $("#timeline-cursor");
  if (!start || !timelineScale || !video || !cursor) return;
  const x = timelineScale.x(start + video.currentTime * 1000);
  cursor.setAttribute("x1", x);
  cursor.setAttribute("x2", x);
}

function seekTo(step, incident) {
  const select = $("#clip-select");
  const clips = incident.clip_paths || [];
  const index = clips.findIndex((clip) => {
    const meta = (incident.clip_starts || {})[clip];
    return meta && meta.camera_id === step.camera_id && meta.started_ms <= step.time_ms && step.time_ms <= meta.started_ms + 60000;
  });
  document.querySelectorAll("#explain-sequence tr").forEach((row, rowIndex) => row.classList.toggle("selected", rowIndex - 1 === incidentStepIndex(step)));
  if (index < 0 || !select) {
    toast(step.summary, `${time(step.time_ms)} · no recording covers this moment on ${step.camera}`, "info");
    return;
  }
  select.value = String(index);
  const video = $("#incident-video");
  const clip = clips[index];
  if (!video.src.endsWith(mediaUrl(clip))) video.src = mediaUrl(clip);
  const seek = () => {
    video.currentTime = Math.max(0, (step.time_ms - incident.clip_starts[clip].started_ms) / 1000 - 1);
  };
  if (video.readyState >= 1) seek();
  else video.addEventListener("loadedmetadata", seek, { once: true });
}

function incidentStepIndex(step) {
  return [...document.querySelectorAll("#timeline-svg [data-step]")].findIndex((node) => node.querySelector("title")?.textContent.includes(step.summary));
}

// ---- rule builder -----------------------------------------------------------------

const rulesUI = { data: null, draft: null, isNew: false, tested: false, valid: false, checkTimer: null, testedIds: new Set(), walkEdited: false };
const MODE_TEXT = { home: "Home", away: "Away", night: "Night", vacation: "Vacation" };
const DAY_TEXT = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const CLASS_TEXT = { person: "People", vehicle: "Vehicles", bicycle: "Bicycles", animal: "Animals", bag: "Bags", unknown_motion: "Unidentified motion" };
const NOTIFY_TEXT = { auto: "Follow the mode's alert level", always: "Always notify (any armed mode)", never: "Record only, never notify" };

document.querySelector('[data-tab="setup"]').addEventListener("click", () => loadRules());
document.querySelector('.subtabs button[data-sub="rules"]').addEventListener("click", () => loadRules());

async function loadRules() {
  try {
    rulesUI.data = await api("/rules");
  } catch (error) {
    toast("Could not load rules", error.message);
    return;
  }
  renderRuleList();
}

function renderRuleList() {
  const rules = rulesUI.data.rules;
  $("#rule-list-setup").innerHTML = rules.map((rule) => `
    <li class="rule-row ${rule.enabled === false ? "off" : ""}" data-rule-row="${esc(rule.id)}">
      <div class="what"><strong>${esc(rule.display_name)}</strong> <span class="sev ${esc(rule.severity)}">${esc(rule.severity)}</span>
        <div class="small">${esc(rule.plain.what)}</div><div class="small muted">${esc(rule.plain.when)} · ${esc(rule.plain.then)}</div></div>
      <div class="button-row tight">
        <label class="switch"><input type="checkbox" data-rule-toggle="${esc(rule.id)}" ${rule.enabled === false ? "" : "checked"}> On</label>
        <button data-rule-edit="${esc(rule.id)}">Edit</button>
        <button data-rule-copy="${esc(rule.id)}" title="Duplicate">Copy</button>
      </div>
    </li>`).join("") || '<li class="muted">No rules yet. Start with a template.</li>';
  document.querySelectorAll("[data-rule-edit]").forEach((b) => b.addEventListener("click", () => editRule(rulesUI.data.rules.find((r) => r.id === b.dataset.ruleEdit), false)));
  document.querySelectorAll("[data-rule-copy]").forEach((b) => b.addEventListener("click", () => {
    const rule = structuredClone(rulesUI.data.rules.find((r) => r.id === b.dataset.ruleCopy));
    delete rule.id;
    rule.name = `${rule.display_name} (copy)`;
    editRule(rule, true);
  }));
  document.querySelectorAll("[data-rule-toggle]").forEach((box) => box.addEventListener("change", async () => {
    const id = box.dataset.ruleToggle;
    if (box.checked && !rulesUI.testedIds.has(id) && !confirm("This rule has not been tested in this session. Turn it on anyway?")) {
      box.checked = false;
      return;
    }
    try {
      await api(`/rules/${encodeURIComponent(id)}/enabled`, { method: "PUT", body: JSON.stringify({ enabled: box.checked }) });
      toast(box.checked ? "Rule on" : "Rule off", "", "info");
      await loadRules();
    } catch (error) {
      box.checked = !box.checked;
      toast("Not changed", error.message);
    }
  }));
}

$("#rule-new").addEventListener("click", () => {
  if (!rulesUI.data) return;
  $("#rule-editor").innerHTML = `
    <h3>Start from a template</h3>
    <p class="muted small">Templates use area types (entrances, driveway, restricted…) so they fit any property; you can pick exact zones next.</p>
    <div class="template-grid">${rulesUI.data.templates.map((t) => `<button data-template="${esc(t.key)}"><strong>${esc(t.title)}</strong><span class="small muted">${esc(t.summary)}</span></button>`).join("")}
      <button data-template=""><strong>Blank rule</strong><span class="small muted">Build every condition yourself.</span></button></div>`;
  document.querySelectorAll("[data-template]").forEach((b) => b.addEventListener("click", () => {
    const template = rulesUI.data.templates.find((t) => t.key === b.dataset.template);
    editRule(template ? { ...structuredClone(template.rule), name: template.title, description: template.summary } : { type: "zone_entry", classes: ["person"], severity: "medium", name: "New rule" }, true);
  }));
});

function editRule(rule, isNew) {
  rulesUI.draft = structuredClone(rule);
  rulesUI.isNew = isNew;
  rulesUI.tested = false;
  rulesUI.walkEdited = false;
  renderRuleEditor();
  scheduleRuleCheck();
}

function whereMode(rule) {
  if (rule.type === "sequence") return "sequence";
  if (rule.zones && rule.zones.length) return "zones";
  if (rule.zone_types && rule.zone_types.length) return "types";
  return "any";
}

function renderRuleEditor() {
  const r = rulesUI.draft;
  const cat = rulesUI.data.catalog;
  const zonesByCamera = {};
  for (const zone of cat.zones) (zonesByCamera[zone.camera_name] ||= []).push(zone);
  const mode = whereMode(r);
  const num = (field, label, value, attrs = "") => `<label class="field">${label} <input type="number" data-f="${field}" value="${value ?? ""}" ${attrs}></label>`;
  const check = (group, value, label, checked) => `<label><input type="checkbox" data-${group}="${esc(value)}" ${checked ? "checked" : ""}> ${esc(label)}</label>`;
  const typeFields = {
    dwell: `<div class="grid2">${num("min_dwell_s", "Stays at least (seconds)", r.min_dwell_s ?? 60, 'min="1"')}${num("max_roam", "Staying within (body heights, optional)", r.max_roam, 'min="0.5" step="0.5"')}</div>
      <label class="field checkbox small"><input type="checkbox" data-f="require_arrival" ${r.require_arrival ? "checked" : ""}> Only after arriving (ignore things already there, such as parked cars)</label>`,
    repeated_presence: `<div class="grid2">${num("count", "Number of separate visits", r.count ?? 3, 'min="2"')}${num("window_s", "Within (seconds)", r.window_s ?? 900, 'min="10"')}</div>`,
    sequence: num("within_s", "All steps within (seconds)", r.within_s ?? 180, 'min="5"'),
    multi_camera: `<div class="grid2">${num("min_cameras", "Seen by at least (cameras)", r.min_cameras ?? 2, 'min="2"')}${num("window_s", "Within (seconds)", r.window_s ?? 240, 'min="10"')}</div>`,
    combination: `<fieldset><legend>Observations that must happen together</legend>
        <div class="chips">${rulesUI.data.rules.filter((other) => other.type !== "combination" && other.id !== r.id).map((other) => check("component", other.id, other.display_name, (r.rules || []).includes(other.id))).join("") || '<span class="muted small">Create the individual rules first.</span>'}</div>
        <div class="grid2">${num("min_matches", "At least this many of them", r.min_matches ?? 2, 'min="1"')}${num("within_s", "Within (seconds)", r.within_s ?? 600, 'min="5"')}</div>
        <label class="field">Linked how <select data-f="scope"><option value="subject" ${r.scope !== "site" ? "selected" : ""}>Same subject (only when Sentinel could link the sightings)</option><option value="site" ${r.scope === "site" ? "selected" : ""}>Anywhere on the property (may be different people)</option></select></label>
        <p class="muted small">Tip: set the individual rules to "Record only" so only this combination notifies.</p></fieldset>`,
  };
  const zoneChoices = Object.entries(zonesByCamera).map(([camera, zones]) => `<div class="small"><strong>${esc(camera)}</strong> <span class="chips">${zones.map((z) => check("zone", z.id, `${z.name} (${z.type})`, (r.zones || []).includes(z.id))).join("")}</span></div>`).join("");
  const sequenceSteps = (r.zones || []).map((zoneId, index) => `<li>${index + 1}. <select data-seq="${index}">${cat.zones.map((z) => `<option value="${esc(z.id)}" ${z.id === zoneId ? "selected" : ""}>${esc(z.name)} · ${esc(z.camera_name)}</option>`).join("")}</select>
      <button type="button" data-seq-remove="${index}" aria-label="Remove step">✕</button></li>`).join("");
  $("#rule-editor").innerHTML = `
    <div class="row-between"><h3>${rulesUI.isNew ? "New rule" : "Edit rule"}</h3>${rulesUI.isNew ? "" : '<button class="danger" id="rule-delete">Delete</button>'}</div>
    <form id="rule-form" novalidate>
      <label class="field">Name <input type="text" data-f="name" maxlength="80" value="${esc(r.name || r.display_name || "")}"></label>
      <label class="field">Description (optional) <input type="text" data-f="description" maxlength="300" value="${esc(r.description || "")}"></label>
      <label class="field">What should Sentinel look for? <select data-f="type">${Object.entries(cat.types).map(([value, label]) => `<option value="${value}" ${r.type === value ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>
      ${r.type !== "combination" ? `<fieldset><legend>Who</legend><div class="chips">${cat.classes.map((c) => check("class", c, CLASS_TEXT[c] || c, (r.classes || ["person"]).includes(c))).join("")}</div></fieldset>` : ""}
      ${r.type === "combination" || r.type === "multi_camera" ? "" : `<fieldset><legend>Where</legend>
        ${r.type === "sequence" ? `<p class="muted small">Areas in order. Other areas may be visited in between. Steps on different cameras need a camera connection (Setup → Connections).</p>
          <ol class="steps-editor list">${sequenceSteps}</ol><button type="button" id="seq-add">Add step</button>` : `
        <div class="chips"><label><input type="radio" name="where" value="any" ${mode === "any" ? "checked" : ""}> Anywhere on the property</label>
          <label><input type="radio" name="where" value="types" ${mode === "types" ? "checked" : ""}> Types of area</label>
          <label><input type="radio" name="where" value="zones" ${mode === "zones" ? "checked" : ""}> Specific zones</label></div>
        ${mode === "types" ? `<div class="chips">${cat.zone_types.map((t) => check("ztype", t, t, (r.zone_types || []).includes(t))).join("")}</div>` : ""}
        ${mode === "zones" ? zoneChoices : ""}`}</fieldset>`}
      ${typeFields[r.type] || ""}
      <fieldset><legend>When</legend>
        <div class="small muted">Modes (none ticked = every armed mode)</div>
        <div class="chips">${cat.modes.map((m) => check("mode", m, MODE_TEXT[m] || m, (r.arm_modes || []).includes(m))).join("")}</div>
        <label class="field checkbox small"><input type="checkbox" id="use-hours" ${r.active_hours ? "checked" : ""}> Only between</label>
        <div class="grid2"><label class="field">From <input type="time" data-hours="start" value="${esc(r.active_hours?.start || "22:00")}" ${r.active_hours ? "" : "disabled"}></label>
          <label class="field">To <input type="time" data-hours="end" value="${esc(r.active_hours?.end || "06:00")}" ${r.active_hours ? "" : "disabled"}></label></div>
        <div class="small muted">Days (none ticked = every day; overnight times count for the day they start)</div>
        <div class="chips">${cat.days.map((d) => check("day", d, DAY_TEXT[d], (r.active_days || []).includes(d))).join("")}</div>
      </fieldset>
      <fieldset><legend>Alert</legend>
        <div class="grid2"><label class="field">Severity <select data-f="severity">${cat.severities.map((s) => `<option value="${s}" ${r.severity === s ? "selected" : ""}>${s}</option>`).join("")}</select></label>
          <label class="field">Notifications <select data-f="notify">${Object.entries(NOTIFY_TEXT).map(([v, l]) => `<option value="${v}" ${(r.notify || "auto") === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
          ${num("cooldown_s", "Cooldown after an alert (seconds)", r.cooldown_s ?? "", 'min="0" max="86400"')}
          ${r.type !== "combination" ? num("min_observations", "Detections needed before it counts", r.min_observations ?? "", 'min="1" max="100"') : ""}</div>
        <p class="muted small">Mode alert levels: ${esc(Object.entries(cat.notify_thresholds).map(([m, l]) => `${m} ${l}`).join(", "))}.</p>
      </fieldset>
    </form>
    <div class="plain" id="rule-plain">…</div>
    <ul class="errors" id="rule-errors"></ul>
    <h3>Test before turning it on</h3>
    <div class="grid2">
      <label class="field">Activity <select id="test-source"><option value="walk">Simulated walk through zones</option><option value="history">Recorded activity on this property</option>${cat.scenarios.length ? '<option value="scenario">Built-in scenario (example site)</option>' : ""}</select></label>
      <label class="field">Mode <select id="test-mode">${cat.modes.map((m) => `<option value="${m}" ${m === "away" ? "selected" : ""}>${MODE_TEXT[m] || m}</option>`).join("")}</select></label>
      <label class="field" id="test-start-field">Start time <input type="datetime-local" id="test-start" value="${localInput(Date.now())}"></label>
      <label class="field" id="test-days-field" hidden>Days of recorded activity <input type="number" id="test-days" value="7" min="1" max="14"></label>
      <label class="field" id="test-scenario-field" hidden>Scenario <select id="test-scenario">${cat.scenarios.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}</select></label>
      <label class="field" id="test-label-field">Subject <select id="test-label"><option value="person">Person</option><option value="vehicle">Vehicle</option><option value="animal">Animal</option></select></label>
    </div>
    <div id="test-walk"><div class="small muted">Walk: stand in each zone, then move on (time to reach the next zone).</div><ol class="steps-editor list" id="walk-steps"></ol>
      <button type="button" id="walk-add">Add zone to walk</button></div>
    <div class="button-row"><button id="rule-test" class="primary">Run test</button></div>
    <div id="rule-test-result"></div>
    <div class="button-row"><button id="rule-save-off">Save, keep off</button><button id="rule-save-on" class="primary" disabled title="Run a test first">Save and turn on</button></div>`;
  bindRuleEditor();
  initWalkSteps();
}

function readRuleForm() {
  const r = structuredClone(rulesUI.draft);
  const form = $("#rule-form");
  form.querySelectorAll("[data-f]").forEach((input) => {
    const key = input.dataset.f;
    if (input.type === "checkbox") r[key] = input.checked;
    else if (input.type === "number") {
      if (input.value === "") delete r[key];
      else r[key] = Number(input.value);
    } else r[key] = input.value;
  });
  if (!r.require_arrival) delete r.require_arrival;
  const picked = (attr) => [...form.querySelectorAll(`[data-${attr}]`)].filter((box) => box.checked).map((box) => box.dataset[attr]);
  if (form.querySelector("[data-class]")) r.classes = picked("class");
  r.arm_modes = picked("mode");
  r.active_days = picked("day");
  if (form.querySelector("[data-component]")) r.rules = picked("component");
  const where = form.querySelector('[name="where"]:checked');
  if (r.type === "sequence") r.zones = [...form.querySelectorAll("[data-seq]")].map((select) => select.value);
  else if (where) {
    if (where.value === "any") { delete r.zones; delete r.zone_types; }
    if (where.value === "types") { delete r.zones; if (form.querySelector("[data-ztype]")) r.zone_types = picked("ztype"); }
    if (where.value === "zones") { delete r.zone_types; if (form.querySelector("[data-zone]")) r.zones = picked("zone"); }
  }
  if ($("#use-hours").checked) r.active_hours = { start: form.querySelector('[data-hours="start"]').value, end: form.querySelector('[data-hours="end"]').value };
  else delete r.active_hours;
  return r;
}

function bindRuleEditor() {
  const form = $("#rule-form");
  form.addEventListener("input", (event) => {
    const structural = event.target.dataset.f === "type" || event.target.name === "where";
    rulesUI.draft = readRuleForm();
    rulesUI.tested = false;
    updateSaveButtons();
    if (structural) {
      if (rulesUI.draft.type === "sequence" && !(rulesUI.draft.zones || []).length) rulesUI.draft.zones = [];
      renderRuleEditor();
    } else if (!rulesUI.walkEdited) initWalkSteps(); // the test walk follows the rule until the user edits it
    scheduleRuleCheck();
  });
  $("#use-hours").addEventListener("change", (event) => {
    form.querySelectorAll("[data-hours]").forEach((input) => (input.disabled = !event.target.checked));
  });
  const addStep = $("#seq-add");
  if (addStep) addStep.addEventListener("click", () => {
    rulesUI.draft = readRuleForm();
    rulesUI.draft.zones = [...(rulesUI.draft.zones || []), rulesUI.data.catalog.zones[0]?.id].filter(Boolean);
    rulesUI.tested = false;
    renderRuleEditor();
    scheduleRuleCheck();
  });
  form.querySelectorAll("[data-seq-remove]").forEach((button) => button.addEventListener("click", () => {
    rulesUI.draft = readRuleForm();
    rulesUI.draft.zones.splice(Number(button.dataset.seqRemove), 1);
    renderRuleEditor();
    scheduleRuleCheck();
  }));
  const remove = $("#rule-delete");
  if (remove) remove.addEventListener("click", async () => {
    if (!confirm(`Delete the rule "${rulesUI.draft.name || rulesUI.draft.id}"?`)) return;
    try {
      await api(`/rules/${encodeURIComponent(rulesUI.draft.id)}`, { method: "DELETE" });
      $("#rule-editor").innerHTML = '<p class="muted">Rule deleted.</p>';
      await loadRules();
    } catch (error) {
      toast("Not deleted", error.message);
    }
  });
  $("#test-source").addEventListener("change", updateTestFields);
  $("#rule-test").addEventListener("click", runRuleTest);
  $("#rule-save-off").addEventListener("click", () => saveRule(false));
  $("#rule-save-on").addEventListener("click", () => saveRule(true));
  $("#walk-add").addEventListener("click", () => { rulesUI.walkEdited = true; addWalkStep(); });
  $("#walk-steps").addEventListener("input", () => (rulesUI.walkEdited = true));
  updateTestFields();
}

function updateTestFields() {
  const source = $("#test-source").value;
  $("#test-walk").hidden = source !== "walk";
  $("#test-label-field").hidden = source !== "walk";
  $("#test-start-field").hidden = source === "history";
  $("#test-days-field").hidden = source !== "history";
  $("#test-scenario-field").hidden = source !== "scenario";
}

function initWalkSteps() {
  if (!$("#walk-steps")) return;
  const r = rulesUI.draft;
  const zones = rulesUI.data.catalog.zones;
  let ids = r.type === "sequence" ? r.zones || [] : r.zones || zones.filter((z) => (r.zone_types || []).includes(z.type)).map((z) => z.id).slice(0, 1);
  if (!ids.length && zones.length) ids = [zones[0].id];
  const stay = r.type === "dwell" ? (r.min_dwell_s || 60) + 15 : 10;
  $("#walk-steps").innerHTML = "";
  ids.forEach((id) => addWalkStep(id, stay));
  if (r.active_hours) {
    const [h, m] = r.active_hours.start.split(":").map(Number);
    const start = new Date();
    start.setHours(h, m + 10, 0, 0);
    $("#test-start").value = localInput(start.getTime());
  }
  if (r.classes && r.classes[0] && ["person", "vehicle", "animal"].includes(r.classes[0])) $("#test-label").value = r.classes[0];
}

function addWalkStep(zoneId, stay = 10) {
  const zones = rulesUI.data.catalog.zones;
  const li = document.createElement("li");
  li.innerHTML = `<select data-walk-zone aria-label="Zone">${zones.map((z) => `<option value="${esc(z.id)}" ${z.id === zoneId ? "selected" : ""}>${esc(z.name)} · ${esc(z.camera_name)}</option>`).join("")}</select>
    <label class="small">stay <input type="number" data-walk-stay value="${stay}" min="1" max="3600"> s</label>
    <label class="small">then <input type="number" data-walk-gap value="5" min="0.5" max="600"> s to next</label>
    <button type="button" aria-label="Remove">✕</button>`;
  li.querySelector("button").addEventListener("click", () => {
    rulesUI.walkEdited = true;
    li.remove();
  });
  $("#walk-steps").append(li);
}

function scheduleRuleCheck() {
  clearTimeout(rulesUI.checkTimer);
  rulesUI.checkTimer = setTimeout(async () => {
    try {
      const result = await api("/rules/check", { method: "POST", body: JSON.stringify(rulesUI.draft) });
      rulesUI.valid = result.valid;
      $("#rule-plain").innerHTML = result.plain ? `<strong>${esc(result.plain.what)}</strong><br>${esc(result.plain.when)}.<br>${esc(result.plain.then)}.` : "";
      $("#rule-errors").innerHTML = result.errors.map((error) => `<li>${esc(error.replace(/^rules\[\d+\]\.?/, ""))}</li>`).join("");
      updateSaveButtons();
    } catch (error) {
      $("#rule-errors").innerHTML = `<li>${esc(error.message)}</li>`;
    }
  }, 250);
}

function updateSaveButtons() {
  const on = $("#rule-save-on");
  const off = $("#rule-save-off");
  if (!on) return;
  off.disabled = !rulesUI.valid;
  on.disabled = !(rulesUI.valid && rulesUI.tested);
  on.title = !rulesUI.valid ? "Fix the problems above first" : rulesUI.tested ? "" : "Run a test first";
}

async function runRuleTest() {
  rulesUI.draft = readRuleForm();
  const source = $("#test-source").value;
  const body = {
    rule: rulesUI.draft, source, arm_mode: $("#test-mode").value, label: $("#test-label").value,
    start: source === "history" ? null : $("#test-start").value + ":00", days: Number($("#test-days").value || 7), scenario: $("#test-scenario")?.value || null,
    steps: [...document.querySelectorAll("#walk-steps li")].map((li) => ({
      zone_id: li.querySelector("[data-walk-zone]").value, stay_s: Number(li.querySelector("[data-walk-stay]").value), gap_s: Number(li.querySelector("[data-walk-gap]").value),
    })),
  };
  $("#rule-test-result").innerHTML = '<p class="muted small">Running the rule through the real engine…</p>';
  try {
    const result = await api("/rules/test", { method: "POST", body: JSON.stringify(body) });
    rulesUI.tested = true;
    if (rulesUI.draft.id) rulesUI.testedIds.add(rulesUI.draft.id);
    $("#rule-test-result").innerHTML = `<div class="test-result" id="test-verdict"><strong>${esc(result.verdict)}</strong>
      ${result.fired.length ? `<ul>${result.fired.slice(0, 10).map((e) => `<li>${dateTime(e.timestamp_ms)} · <span class="sev ${esc(e.severity)}">${esc(e.severity)}</span> ${esc(e.summary)} (${pct(e.confidence)})</li>`).join("")}</ul>` : ""}
      ${result.notifications.length ? `<div>Would notify: ${result.notifications.map((n) => esc(n.title)).join("; ")}</div>` : ""}
      ${Object.keys(result.other_rules_fired).length ? `<div class="muted">Other rules that matched: ${esc(Object.entries(result.other_rules_fired).map(([id, n]) => `${id} ×${n}`).join(", "))}</div>` : ""}
      ${result.notes.map((note) => `<div class="muted">${esc(note)}</div>`).join("")}</div>`;
  } catch (error) {
    rulesUI.tested = false;
    $("#rule-test-result").innerHTML = `<div class="test-result errors">${esc(error.message)}</div>`;
  }
  updateSaveButtons();
}

async function saveRule(enable) {
  rulesUI.draft = readRuleForm();
  const body = { ...rulesUI.draft, enabled: enable };
  delete body.plain;
  delete body.display_name;
  try {
    const saved = rulesUI.isNew
      ? await api("/rules", { method: "POST", body: JSON.stringify(body) })
      : await api(`/rules/${encodeURIComponent(rulesUI.draft.id)}`, { method: "PUT", body: JSON.stringify(body) });
    if (rulesUI.tested) rulesUI.testedIds.add(saved.id);
    rulesUI.draft = saved;
    rulesUI.isNew = false;
    toast(enable ? "Rule saved and on" : "Rule saved (off)", saved.name || saved.id, "info");
    await loadRules();
    renderRuleEditor();
    scheduleRuleCheck();
  } catch (error) {
    toast("Rule not saved", error.message);
  }
}

// ---- insights and alert performance ---------------------------------------------------

document.querySelectorAll(".subtabs-alt button").forEach((button) =>
  button.addEventListener("click", () => {
    document.querySelectorAll(".subtabs-alt button").forEach((b) => b.classList.toggle("active", b === button));
    document.querySelectorAll(".isub").forEach((panel) => panel.classList.toggle("active", panel.id === `isub-${button.dataset.isub}`));
    if (button.dataset.isub === "performance") loadPerformance();
    else loadInsights();
  }),
);
document.querySelector('[data-tab="insights"]').addEventListener("click", () => loadInsights());
document.querySelector('[data-tab="timeline"]').addEventListener("click", () => fillSearchOptions());
$("#insights-days").addEventListener("change", () => loadInsights());
$("#performance-days").addEventListener("change", () => loadPerformance());

function bars(counts) {
  const max = Math.max(1, ...counts);
  return `<div class="bars" role="img" aria-label="Activity by hour">${counts.map((value, hour) => `<span style="height:${Math.round((value / max) * 100)}%" title="${String(hour).padStart(2, "0")}:00 · ${value}"></span>`).join("")}</div>
    <div class="bars-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div>`;
}

async function loadInsights() {
  let data;
  try {
    data = await api(`/insights?days=${$("#insights-days").value}`);
  } catch (error) {
    $("#insights-status").textContent = `Could not load insights: ${error.message}`;
    return;
  }
  const w = data.window;
  $("#insights-status").className = `banner ${w.enough_data ? "good" : ""}`;
  $("#insights-status").innerHTML = `${esc(data.notes[0])}<br><span class="small">${w.days_observed} day(s) with activity in the last ${w.days} days${w.enough_data ? "." : ` · patterns appear after ${w.min_days} days.`}</span>`;
  const card = (title, body) => `<div class="card"><h3>${esc(title)}</h3>${body}</div>`;
  const totals = Object.entries(data.totals).map(([label, count]) => `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${count}</div></div>`).join("") || '<p class="muted small">No completed activity recorded yet.</p>';
  const hourly = Object.entries(data.hourly).map(([label, counts]) => `<div class="small"><strong>${esc(label)}</strong>${bars(counts)}</div>`).join("") || '<p class="muted small">Nothing yet.</p>';
  $("#insights-body").innerHTML = `
    <div class="tiles">${totals}</div>
    <div class="health-layout">
      ${card("When there is activity", hourly)}
      ${card("Vehicles", `<div class="small">Arrivals ${data.vehicles.arrivals}${data.vehicles.typical_arrival_hours ? ` · usually ${esc(data.vehicles.typical_arrival_hours)}` : ""}${data.vehicles.parked_median ? ` · typical long stay ${esc(data.vehicles.parked_median)}` : ""}</div>${bars(data.vehicles.arrivals_by_hour)}<div class="small muted">Departures</div>${bars(data.vehicles.departures_by_hour)}`)}
      ${card("Entrances used by people", data.entrances.map((e) => `<div class="small">${esc(e.name)} <span class="muted">(${esc(e.type)})</span> · ${e.visits}<div class="meter"><span style="width:${Math.round(e.share * 100)}%"></span></div></div>`).join("") || '<p class="muted small">No entrance visits yet.</p>')}
      ${card("Common routes", `<ul class="list compact">${data.pathways.map((p) => `<li>${esc(p.path.join(" → "))} · ${p.count}</li>`).join("") || '<li class="muted">None yet.</li>'}</ul>`)}
      ${card("Delivery-like visits", `<div class="small">${data.deliveries.count} visit(s)${data.deliveries.typical_hours ? ` · mostly ${esc(data.deliveries.typical_hours)}` : ""}${data.deliveries.median_visit ? ` · typical length ${esc(data.deliveries.median_visit)}` : ""}</div>${bars(data.deliveries.by_hour)}<p class="muted small">${esc(data.deliveries.note)}</p>`)}
      ${card("Animals", `<div class="small">${data.animals.count} sighting(s) ${Object.entries(data.animals.kinds).map(([k, n]) => `· ${esc(k)} ${n}`).join(" ")}</div>${bars(data.animals.by_hour)}<div class="small muted">${data.animals.zones.map((z) => `${esc(z.name)} ${z.visits}`).join(" · ")}</div>`)}
      ${card("Passers-by", `<div class="small">${data.pedestrians.count} on the street or sidewalk only</div>${bars(data.pedestrians.by_hour)}<p class="muted small">${esc(data.pedestrians.note)}</p>`)}
      ${card("Environmental motion", `<ul class="list compact">${data.environment.map((e) => `<li>${esc(e.camera)} · ${esc(e.condition)} · ${e.count}× on ${e.days} day(s)</li>`).join("") || '<li class="muted">None recorded.</li>'}</ul>`)}
    </div>
    ${card("Typical stays by zone", `<div class="scroll"><table class="table"><tr><th>Zone</th><th>What</th><th>Visits</th><th>Typical stay</th><th>Long stay (90%)</th></tr>${data.zones.map((z) => `<tr><td>${esc(z.zone)}</td><td>${esc(z.category)}</td><td>${z.visits}</td><td>${esc(z.typical_stay)}</td><td>${esc(z.long_stay_p90 || "needs more data")}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">No completed visits yet.</td></tr>'}</table></div>`)}
    ${card("Changes over time", `<ul class="list compact">${data.changes.map((c) => `<li>${esc(c.text)}</li>`).join("") || '<li class="muted">Not enough history to compare yet.</li>'}</ul>`)}
    ${card("Unusual observations", `<p class="muted small">Information only: unusual is not the same as suspicious. These never raise alerts or change rules.</p>
      <ul class="list compact" id="unusual-list">${data.unusual.map((u) => `<li>${esc(u.text)}</li>`).join("") || '<li class="muted">Nothing unusual by these measures.</li>'}</ul>`)}
    ${card("Suggestions to review", `<p class="muted small">Nothing changes unless you save it yourself.</p>
      <ul class="list" id="suggestion-list">${data.suggestions.map((s, i) => `<li><strong>${esc(s.title)}</strong><div class="small">${esc(s.text)}</div>${s.rule ? `<button data-suggest="${i}">Open in rule builder</button>` : ""}</li>`).join("") || '<li class="muted">No suggestions.</li>'}</ul>`)}`;
  document.querySelectorAll("[data-suggest]").forEach((button) => button.addEventListener("click", async () => {
    document.querySelector('nav.tabs [data-tab="setup"]').click();
    showSub("rules");
    await loadRules();
    editRule(structuredClone(data.suggestions[Number(button.dataset.suggest)].rule), true);
  }));
}

async function loadPerformance() {
  let data;
  try {
    data = await api(`/reports/performance?days=${$("#performance-days").value}`);
  } catch (error) {
    $("#performance-body").textContent = error.message;
    return;
  }
  $("#performance-note").textContent = data.note;
  const t = data.totals;
  const rate = (value) => (value == null ? "–" : `${Math.round(value * 100)}%`);
  $("#performance-body").innerHTML = `
    <div class="tiles">
      <div class="tile"><div class="label">Incidents</div><div class="value">${t.incidents}</div></div>
      <div class="tile"><div class="label">Notified</div><div class="value">${t.notified}</div></div>
      <div class="tile"><div class="label">Reviewed</div><div class="value">${t.reviewed}</div></div>
      <div class="tile"><div class="label">Missed reports</div><div class="value">${t.missed_reports}</div></div>
    </div>
    <div class="card"><h3>By rule</h3><div class="scroll"><table class="table" id="performance-rules"><tr><th>Rule</th><th>Matches</th><th>Recorded only</th><th>Incidents</th><th>Notified</th><th>Nuisance rate</th><th>Genuine</th><th>Reviews</th></tr>
      ${data.rules.map((r) => `<tr><td>${esc(r.name)}${r.deleted ? ' <span class="muted">(deleted)</span>' : r.enabled === false ? ' <span class="muted">(off)</span>' : ""}</td><td>${r.matches}</td><td>${r.recorded_only}</td><td>${r.incidents}</td><td>${r.notified}</td>
        <td>${rate(r.nuisance_rate)}</td><td>${rate(r.genuine_rate)}</td><td class="wrap small">${esc(Object.entries(r.outcomes).filter(([, n]) => n).map(([k, n]) => `${k} ${n}`).join(", "))}</td></tr>`).join("")}</table></div></div>
    <div class="card"><h3>By camera</h3><div class="scroll"><table class="table"><tr><th>Camera</th><th>Incidents</th><th>Notified</th><th>Nuisance rate</th><th>Missed reports</th></tr>
      ${data.cameras.map((c) => `<tr><td>${esc(c.name)}</td><td>${c.incidents}</td><td>${c.notified}</td><td>${rate(c.nuisance_rate)}</td><td>${c.missed_reports}</td></tr>`).join("")}</table></div></div>
    <div class="card"><h3>Missed events you reported</h3><ul class="list compact">${data.missed.map((m) => `<li>${dateTime(m.occurred_ms)} · ${esc(m.camera)} · ${esc(m.category || "")} ${esc(m.description || "")}
      <div class="small muted">Sentinel recorded ${m.sentinel_recorded.length} event(s) on that camera within 5 minutes${m.sentinel_recorded.length ? `: ${esc(m.sentinel_recorded.slice(0, 3).map((e) => e.summary).join("; "))}` : ""}.</div></li>`).join("") || '<li class="muted">None reported.</li>'}</ul></div>`;
}
