"use strict";

// Flame Mode: the drone's optional agricultural configuration. Fields, the
// removable accessory, burn operations, thermal watch and reports. Monitoring
// only: nothing on this tab controls ignition, and nothing here approves a burn.

const flame = { data: null, operation: null, draft: null, placing: null, timer: null };
const SIDES = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];
const STATE_PILL = { pass: "ok", na: "", warn: "warn", fail: "bad", missing: "bad" };
const ZONE_CLASS = { outside: "hot-outside", edge: "hot-edge", interior: "hot-inside", "": "hot-inside" };

const flameTab = document.querySelector('[data-tab="flame"]');
flameTab.addEventListener("click", () => loadFlame());

async function flameApi(path, method = "GET", body) {
  return api(`/flame${path}`, method === "GET" ? {} : { method, body: JSON.stringify(body ?? {}) });
}

async function flameAction(promise, success) {
  try {
    const result = await promise;
    if (success) toast(success);
    return result;
  } catch (error) {
    toast("Not done", error.message, "high");
    return null;
  }
}

async function loadFlame(keepOperation = true) {
  try {
    flame.data = await flameApi("");
  } catch {
    return;
  }
  flameTab.hidden = !flame.data.enabled && !flameTab.classList.contains("active");
  const open = flame.data.operations.find((op) => op.status !== "closed");
  const wanted = keepOperation && flame.operation ? flame.operation.operation_id : open?.operation_id;
  flame.operation = wanted ? await flameApi(`/operations/${encodeURIComponent(wanted)}`).catch(() => null) : null;
  renderFlame();
  clearInterval(flame.timer);
  flame.timer = setInterval(() => {
    if (document.querySelector("#tab-flame.active") && flame.operation && flame.operation.status !== "closed") refreshOperation();
  }, 3000);
}

async function refreshOperation() {
  try {
    flame.operation = await flameApi(`/operations/${encodeURIComponent(flame.operation.operation_id)}`);
    // Polling updates the watch and warnings, but must not erase a crew member's
    // unfinished weather, limits, crew or observation entry every three seconds.
    const box = $("#flame-operation");
    const focused = document.activeElement;
    const focus = focused?.closest("form[data-flame-dirty]") ? {
      form: focused.form.id, name: focused.name,
      start: focused.selectionStart, end: focused.selectionEnd,
    } : null;
    const drafts = [...box.querySelectorAll("form[data-flame-dirty]")].map((form) => ({
      id: form.id,
      values: [...form.elements].filter((el) => el.name).map((el) => ({ name: el.name, value: el.value, checked: el.checked })),
    }));
    renderOperation();
    for (const draft of drafts) {
      const form = document.getElementById(draft.id);
      if (!form) continue; // a closed operation no longer accepts these entries
      form.dataset.flameDirty = "true";
      for (const value of draft.values) {
        const el = form.elements.namedItem(value.name);
        if (!el) continue;
        el.value = value.value;
        if (typeof value.checked === "boolean") el.checked = value.checked;
      }
    }
    if (focus) {
      const el = document.getElementById(focus.form)?.elements.namedItem(focus.name);
      el?.focus({ preventScroll: true });
      if (Number.isInteger(focus.start)) el?.setSelectionRange(focus.start, focus.end);
    }
    renderFlameMap();
  } catch {
    /* next poll retries */
  }
}

$("#flame-operation").addEventListener("input", (event) => {
  const form = event.target.closest("form");
  if (form) form.dataset.flameDirty = "true";
});

function pill(text, kind = "") {
  return `<span class="pill ${kind}">${esc(text)}</span>`;
}

function renderFlame() {
  const d = flame.data;
  $("#flame-disclaimer").textContent = d.disclaimer;
  if (d.recording?.history_incomplete) $("#flame-disclaimer").textContent += " Some operation records could not be saved; history may be incomplete. Check System health.";
  $("#flame-enabled").checked = d.enabled;
  renderConfigurations();
  renderAccessories();
  renderFields();
  renderRecords();
  renderOperation();
  renderFlameMap();
}

// ---- drone configuration ----------------------------------------------------------------

function renderConfigurations() {
  const drones = flame.data.drones;
  $("#flame-drones").innerHTML = drones.length ? drones.map((d) => `
    <li>
      <div class="row-between"><strong>${esc(d.name)}</strong>${pill(d.label, d.configuration === "agricultural_monitoring" ? "warn" : "ok")}</div>
      <div class="small muted">${esc(d.model)}${d.simulated ? " · simulated" : ""}${d.since_ms ? ` · since ${dateTime(d.since_ms)} by ${esc(d.by)}` : ""}</div>
      <ul class="small">${d.effects.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
      ${d.switch_blockers.length ? `<div class="small">Cannot switch to ${esc(d.switch_to.replace("_", " "))} now:</div>
        <ul class="errors">${d.switch_blockers.map((b) => `<li>${esc(b.detail)}</li>`).join("")}</ul>` : ""}
      <button type="button" data-config="${esc(d.drone_id)}" data-target="${esc(d.switch_to)}" ${d.switch_blockers.length ? "disabled" : ""}>
        Switch to ${d.switch_to === "security" ? "security" : "agricultural monitoring"}</button>
    </li>`).join("") : '<li class="muted">No operable drone. Register one on the Install tab.</li>';
  document.querySelectorAll("[data-config]").forEach((button) => button.addEventListener("click", async () => {
    const result = await flameAction(flameApi(`/drones/${encodeURIComponent(button.dataset.config)}/configuration`, "PUT", { configuration: button.dataset.target }),
      "Configuration changed");
    if (result) loadFlame();
  }));
}

// ---- accessories --------------------------------------------------------------------------

function renderAccessories() {
  const list = flame.data.accessories;
  $("#flame-accessories").innerHTML = list.length ? list.map(accessoryCard).join("") :
    `<p class="muted small">No accessory registered. Register one on the Install tab (category: drone accessory) and choose the drone it is fitted to.
     Ignition payloads cannot be registered: Sentinel never controls ignition or release hardware.</p>`;
  for (const a of list) bindAccessory(a);
}

function accessoryCard(a) {
  const c = a.compatibility || { checks: [], verdict: "", statement: "" };
  const h = a.health || {};
  const mass = c.mass || {};
  const endurance = mass.endurance || {};
  return `<div class="card inner" data-accessory="${esc(a.id)}">
    <div class="row-between"><strong>${esc(a.name)}</strong><span>${pill(a.support)} ${pill(a.attachment, a.attachment === "attached" ? "ok" : "bad")}
      ${a.faults.map((f) => pill(f, "bad")).join("")}</span></div>
    <div class="small muted">${esc(a.profile_name)} on ${esc(a.drone_model_name)}${a.serial ? ` · serial ${esc(a.serial)}` : ""} · ${esc(a.attachment_detail)}</div>
    ${a.reporting ? `<dl class="kv"><dt>Latch</dt><dd>${h.latch_closed ? "closed" : "OPEN"}</dd><dt>Identity</dt><dd>${h.identity_ok ? "matches" : "MISMATCH"}</dd>
      <dt>Heat shield</dt><dd>${esc(h.shield_temperature_c)} °C</dd><dt>Payload bay</dt><dd>${esc(h.bay_temperature_c)} °C</dd>
      <dt>Last report</dt><dd>${esc(a.last_report_age_s)} s ago</dd></dl>` : ""}
    <h3 class="small">Compatibility: ${esc(c.verdict.replace("_", " "))}</h3>
    <p class="small">${esc(c.statement)}${mass.takeoff_g ? ` Take-off about ${(mass.takeoff_g / 1000).toFixed(2)} kg of ${(mass.limit_g / 1000).toFixed(1)} kg (${Math.round(mass.fraction_of_limit * 100)}%).` : ""}
      ${endurance.known ? ` ${esc(endurance.detail)}.` : ""} Mass basis: ${esc(a.mass_basis)}.</p>
    <div class="scroll"><table class="table"><tbody>${c.checks.map((k) => `<tr><td>${esc(k.label)}</td><td>${pill(k.state, STATE_PILL[k.state] ?? "warn")}</td><td class="wrap">${esc(k.detail)}</td></tr>`).join("")}</tbody></table></div>
    <div class="small">${a.commissioned ? `Commissioned ${dateTime(a.commissioning.recorded_ms)} by ${esc(a.commissioning.by)}, measured ${esc(a.commissioning.measured_mass_g)} g.` : "Not commissioned."}
      ${a.inspection_due ? `<span class="pill warn">inspection due: ${esc(a.inspection_reasons.join("; "))}</span>` : a.last_inspection_ms ? `Last inspection ${dateTime(a.last_inspection_ms)}.` : ""}</div>
    <details><summary class="small">Record inspection, maintenance or commissioning</summary>
      <form class="flame-record" data-id="${esc(a.id)}">
        <div class="grid2">
          <label class="field">Kind <select name="kind"><option value="inspection">Inspection</option><option value="maintenance">Maintenance</option>
            <option value="repair">Repair</option><option value="commissioning">Commissioning</option><option value="note">Note</option></select></label>
          <label class="field">Note <input type="text" name="note" maxlength="500"></label>
          <label class="field">Measured mass, g (commissioning) <input type="number" name="measured_mass_g" min="1" max="5000"></label>
          <label class="field">Hover throttle % (optional) <input type="number" name="hover_throttle_pct" min="10" max="90"></label>
        </div>
        <label class="field checkbox"><input type="checkbox" name="cg_checked"> Centre of gravity checked with the accessory and a full battery</label>
        <button type="submit">Save record</button>
      </form></details>
    ${a.support === "simulated" ? `<div class="button-row small">Simulate: ${["attach", "detach", "latch_open", "identity_mismatch", "overheat", "silent", "clear"]
      .map((x) => `<button type="button" data-sim-acc="${esc(a.id)}" data-action="${x}">${x.replace("_", " ")}</button>`).join("")}</div>` : ""}
    <details><summary class="small">Records (${a.records.length})</summary><ul class="list compact">${a.records.map((r) =>
      `<li>${dateTime(r.recorded_ms)} · ${esc(r.kind)} · ${esc(r.by)}${r.note ? ` · ${esc(r.note)}` : ""}</li>`).join("") || '<li class="muted">None</li>'}</ul></details>
  </div>`;
}

function bindAccessory(a) {
  document.querySelectorAll(`[data-sim-acc="${CSS.escape(a.id)}"]`).forEach((button) => button.addEventListener("click", async () => {
    if (await flameAction(flameApi(`/accessories/${encodeURIComponent(a.id)}/simulate`, "POST", { action: button.dataset.action }))) loadFlame();
  }));
  const form = document.querySelector(`.flame-record[data-id="${CSS.escape(a.id)}"]`);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const f = form.elements;
    const body = { kind: f.kind.value, note: f.note.value };
    if (f.measured_mass_g.value) body.measured_mass_g = Number(f.measured_mass_g.value);
    if (f.hover_throttle_pct.value) body.hover_throttle_pct = Number(f.hover_throttle_pct.value);
    if (f.cg_checked.checked) body.cg_checked = true;
    if (await flameAction(flameApi(`/accessories/${encodeURIComponent(a.id)}/records`, "POST", body), "Record saved")) loadFlame();
  });
}

// ---- map ----------------------------------------------------------------------------------

function flameBounds() {
  const map = state.site?.site.map || { width_m: 100, height_m: 100 };
  let [x0, y0, x1, y1] = [0, 0, map.width_m, map.height_m];
  const grow = ([x, y]) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); };
  for (const f of flame.data?.fields || []) {
    (f.boundary_map || []).forEach(grow);
    (f.sensitive_map || []).forEach((p) => grow(p.point_map));
  }
  (flame.draft?.points || []).forEach(grow);
  const pad = Math.max(x1 - x0, y1 - y0) * 0.05 + 5;
  return [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
}

function renderFlameMap() {
  const svg = $("#flame-map");
  if (!flame.data) return;
  const [x, y, w, h] = flameBounds();
  svg.setAttribute("viewBox", `${x} ${y} ${w} ${h}`);
  const pts = (list) => list.map((p) => p.join(",")).join(" ");
  const r = Math.max(w, h) / 160;
  const parts = [];
  for (const b of state.site?.site.map.buildings || []) parts.push(`<polygon class="building" points="${pts(b.polygon)}"><title>${esc(b.name)}</title></polygon>`);
  if (state.drone?.geofence) parts.push(`<polygon class="flame-fence" points="${pts(state.drone.geofence.polygon.map(([la, lo]) => geoToMap(la, lo)))}"><title>Drone geofence</title></polygon>`);
  const current = flame.operation?.field_id;
  for (const f of flame.data.fields) {
    if (!f.boundary_map) continue;
    parts.push(`<polygon class="flame-field ${f.id === current ? "current" : ""}" points="${pts(f.boundary_map)}"><title>${esc(f.name)} · ${f.area_ha} ha</title></polygon>`);
    const [cx, cy] = centroid(f.boundary_map);
    parts.push(`<text class="flame-label" x="${cx}" y="${cy}" font-size="${r * 3}" text-anchor="middle">${esc(f.name)}</text>`);
    for (const p of f.sensitive_map || []) {
      parts.push(`<rect class="flame-sensitive" x="${p.point_map[0] - r}" y="${p.point_map[1] - r}" width="${2 * r}" height="${2 * r}"><title>${esc(p.label)} (${esc(p.kind)})</title></rect>`);
    }
  }
  if (flame.draft?.points.length) {
    parts.push(`<polyline class="flame-draft" points="${pts(flame.draft.points)}"/>`);
    flame.draft.points.forEach(([px, py], i) => parts.push(`<circle class="flame-corner" cx="${px}" cy="${py}" r="${r}"><title>Corner ${i + 1}</title></circle>`));
  }
  for (const s of flame.operation?.hotspots || []) {
    if (!s.map) continue;
    parts.push(`<circle class="hot ${ZONE_CLASS[s.zone] || "hot-inside"} ${s.kind === "residual_heat" ? "hot-residual" : ""}" cx="${s.map[0]}" cy="${s.map[1]}" r="${r * 1.2}">
      <title>${esc(s.kind.replaceAll("_", " "))}: ${esc(s.max_c)} °C ±${esc(s.uncertainty_m ?? "?")} m at ${time(s.recorded_ms)}</title></circle>`);
  }
  const t = state.drone?.telemetry;
  if (t?.lat != null) {
    const [dx, dy] = geoToMap(t.lat, t.lon);
    parts.push(`<circle class="drone-icon flame-drone" cx="${dx}" cy="${dy}" r="${r * 1.4}"><title>Drone · ${esc(t.flight_mode)} · ${Math.round(t.alt_m)} m</title></circle>`);
  }
  svg.innerHTML = parts.join("");
}

$("#flame-map").addEventListener("click", (event) => {
  if (!flame.draft && !flame.placing) return;
  const svg = event.currentTarget;
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const p = point.matrixTransform(svg.getScreenCTM().inverse());
  const xy = [Math.round(p.x * 10) / 10, Math.round(p.y * 10) / 10];
  if (flame.placing) {
    flame.draft = flame.draft || { points: [], sensitive: [] };
    flame.draft.sensitive.push({ kind: flame.placing.kind, label: flame.placing.label, point_map: xy });
    flame.placing = null;
    $("#flame-draw-hint").textContent = "Sensitive place added. Save the field to keep it.";
  } else {
    flame.draft.points.push(xy);
    $("#flame-draw-hint").textContent = `${flame.draft.points.length} corner(s). Click the next corner, in order around the field.`;
  }
  renderFlameMap();
});

// ---- fields -------------------------------------------------------------------------------

function renderFields() {
  const fields = flame.data.fields;
  $("#flame-fields").innerHTML = fields.map((f) => `<li>
      <div class="row-between"><strong>${esc(f.name)}</strong><span class="small">${f.area_ha} ha · ${f.perimeter_m} m</span></div>
      <div class="small muted">${esc(f.crop || "crop not recorded")} · fireguard ${f.fireguard_m} m · watch strip ${f.watch_buffer_m} m · ${f.sensitive.length} sensitive place(s)</div>
      <div class="small">${pill(f.geofence.state === "pass" ? "inside geofence" : f.geofence.detail, STATE_PILL[f.geofence.state])}</div>
      <ul class="small">${f.monitored_areas.map((m) => `<li><b>${esc(m.label)}</b>: ${esc(m.detail)}</li>`).join("")}</ul>
      <div class="button-row tight"><button type="button" data-plan="${esc(f.id)}">Plan a burn</button><button type="button" class="danger" data-del-field="${esc(f.id)}">Delete</button></div>
    </li>`).join("") || '<li class="muted">No field mapped yet. Draw one on the map, or mark corners with the drone.</li>';
  document.querySelectorAll("[data-plan]").forEach((b) => b.addEventListener("click", async () => {
    const op = await flameAction(flameApi("/operations", "POST", { field_id: b.dataset.plan }), "Operation created");
    if (op) { flame.operation = op; loadFlame(true); }
  }));
  document.querySelectorAll("[data-del-field]").forEach((b) => b.addEventListener("click", async () => {
    if (confirm("Delete this field? Closed operations keep their own copy.") && await flameAction(flameApi(`/fields/${encodeURIComponent(b.dataset.delField)}`, "DELETE"), "Field deleted")) loadFlame();
  }));
}

$("#flame-draw").addEventListener("click", () => {
  flame.draft = { points: [], sensitive: [] };
  $("#flame-draw-hint").textContent = "Click the field's corners on the map, in order around the field.";
  $("#flame-field-form").hidden = false;
  renderFlameMap();
});

$("#flame-corner-drone").addEventListener("click", async () => {
  const drone = flame.data.drones.find((d) => d.primary) || flame.data.drones[0];
  if (!drone) return toast("No operable drone", "", "high");
  const position = await flameAction(flameApi(`/drones/${encodeURIComponent(drone.drone_id)}/position`));
  if (!position) return;
  flame.draft = flame.draft || { points: [], sensitive: [] };
  flame.draft.points.push(geoToMap(position.lat, position.lon).map((v) => Math.round(v * 10) / 10));
  $("#flame-field-form").hidden = false;
  $("#flame-draw-hint").textContent = `${flame.draft.points.length} corner(s) from the drone's GPS (${position.satellites} satellites). ${position.note}.`;
  renderFlameMap();
});

$("#flame-add-sensitive").addEventListener("click", () => {
  const f = $("#flame-field-form").elements;
  flame.placing = { kind: f.sensitive_kind.value, label: f.sensitive_label.value || f.sensitive_kind.selectedOptions[0].text };
  $("#flame-draw-hint").textContent = `Click on the map where ${flame.placing.label} is.`;
});

$("#flame-undo").addEventListener("click", () => {
  flame.draft?.points.pop();
  renderFlameMap();
});

$("#flame-field-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const f = event.target.elements;
  const body = { name: f.name.value, crop: f.crop.value, fireguard_m: Number(f.fireguard_m.value || 0), watch_buffer_m: Number(f.watch_buffer_m.value || 100),
    boundary_map: flame.draft?.points || [], sensitive: flame.draft?.sensitive || [] };
  if (await flameAction(flameApi("/fields", "POST", body), "Field saved")) {
    flame.draft = null;
    event.target.hidden = true;
    event.target.reset();
    $("#flame-draw-hint").textContent = "";
    loadFlame();
  }
});

$("#flame-enabled").addEventListener("change", async (event) => {
  if (await flameAction(flameApi("/enabled", "PUT", { enabled: event.target.checked }))) loadFlame();
});

// ---- qualifications and authorizations -----------------------------------------------------

function renderRecords() {
  const d = flame.data;
  const sel = (kinds) => Object.entries(kinds).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  $("#flame-qual-kind").innerHTML = sel(d.qualification_kinds);
  $("#flame-auth-kind").innerHTML = sel(d.authorization_kinds);
  $("#flame-auth-field").innerHTML = `<option value="">Any field</option>` + d.fields.map((f) => `<option value="${esc(f.id)}">${esc(f.name)}</option>`).join("");
  $("#flame-sensitive-kind").innerHTML = sel(d.sensitive_kinds);
  const withdraw = (r) => `<button type="button" class="small" data-withdraw="${esc(r.record_id)}">Withdraw</button>`;
  $("#flame-quals").innerHTML = d.qualifications.map((q) => `<li>${esc(q.person)} · ${esc(d.qualification_kinds[q.qualification])}${q.reference ? ` · ${esc(q.reference)}` : ""}
    ${q.expires ? ` · expires ${esc(q.expires)}` : ""} <span class="muted">recorded by ${esc(q.recorded_by)}</span> ${withdraw(q)}</li>`).join("") || '<li class="muted">None recorded</li>';
  $("#flame-auths").innerHTML = d.authorizations.map((a) => `<li>${esc(d.authorization_kinds[a.authorization].split(" (")[0])}${a.reference ? ` · ${esc(a.reference)}` : ""}
    · ${esc(a.valid_from || "")} to ${esc(a.valid_until || "open")} <span class="muted">recorded by ${esc(a.recorded_by)}</span> ${withdraw(a)}</li>`).join("") || '<li class="muted">None recorded</li>';
  document.querySelectorAll("[data-withdraw]").forEach((b) => b.addEventListener("click", async () => {
    if (await flameAction(flameApi(`/records/${encodeURIComponent(b.dataset.withdraw)}`, "DELETE"), "Withdrawn")) loadFlame();
  }));
}

function formBody(form) {
  const body = {};
  for (const [key, value] of new FormData(form).entries()) if (value !== "") body[key] = value;
  return body;
}

$("#flame-qual-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (await flameAction(flameApi("/qualifications", "POST", formBody(event.target)), "Qualification recorded")) { event.target.reset(); loadFlame(); }
});

$("#flame-auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (await flameAction(flameApi("/authorizations", "POST", formBody(event.target)), "Authorization recorded")) { event.target.reset(); loadFlame(); }
});

// ---- operation ----------------------------------------------------------------------------

function renderOperation() {
  const op = flame.operation;
  const box = $("#flame-operation");
  const closed = flame.data.operations.filter((o) => o.status === "closed").slice(0, 8);
  $("#flame-history").innerHTML = closed.map((o) => `<li>${dateTime(o.created_ms)} · ${esc(o.field_name)}${o.simulated ? " (simulated)" : ""}
    <a href="${API}/flame/operations/${encodeURIComponent(o.operation_id)}/report?format=html" target="_blank" rel="noopener">report</a></li>`).join("") || '<li class="muted">None yet</li>';
  if (!op) {
    box.innerHTML = '<p class="muted">No open operation. Choose <b>Plan a burn</b> on a mapped field.</p>';
    return;
  }
  const r = op.readiness || { items: [], statement: "" };
  const active = ["monitoring", "post_burn"].includes(op.status);
  const sides = (name) => `<select name="${name}">${["centre", ...SIDES].map((s) => `<option>${s}</option>`).join("")}</select>`;
  const p = op.prescription;
  box.innerHTML = `
    <div class="row-between"><h3>${esc(op.name)}</h3><span>${pill(op.status_label, active ? "warn" : "")} ${op.simulated ? pill("SIMULATED", "warn") : ""}</span></div>
    <p class="notice">${esc(r.statement || op.disclaimer)}</p>
    <div class="tiles">
      <div class="tile"><div class="label">Thermal watch</div><div class="value small">${esc(op.thermal.state)}</div><div class="small muted">${esc(op.thermal.detail)}</div></div>
      <div class="tile"><div class="label">Boundary edge seen (${op.coverage.window_min} min)</div><div class="value">${op.coverage.available ? `${op.coverage.edge_pct}%` : "–"}</div></div>
      <div class="tile"><div class="label">Watch strip seen</div><div class="value">${op.coverage.available ? `${op.coverage.strip_pct}%` : "–"}</div></div>
      <div class="tile"><div class="label">Heat outside / at edge</div><div class="value">${op.counts.hotspots_outside} / ${op.counts.hotspots_edge}</div></div>
      <div class="tile"><div class="label">Notifications sent</div><div class="value">${op.counts.notifications}</div></div>
    </div>
    <div class="button-row">
      ${["planning", "stood_down"].includes(op.status) ? '<button type="button" class="primary" id="op-start">Start monitoring</button>' : ""}
      ${op.status === "monitoring" ? `<label class="small">Crew lit the field: ${sides("ignition_side")}</label><button type="button" id="op-ignition">Record ignition report</button>` : ""}
      ${op.status !== "closed" ? `<label class="small">Watch flight over: ${sides("watch_side")}</label><button type="button" id="op-watch">Request watch flight</button>` : ""}
      ${["monitoring", "stood_down"].includes(op.status) ? '<button type="button" id="op-postburn">Start post-burn inspection</button>' : ""}
      ${op.status !== "stood_down" ? '<button type="button" class="danger" id="op-standdown">Stand down (emergency)</button>' : ""}
      <button type="button" class="danger" id="op-alert">Send emergency message</button>
      <button type="button" id="op-close">Close operation</button>
      <a class="button" href="${API}/flame/operations/${encodeURIComponent(op.operation_id)}/report?format=html" target="_blank" rel="noopener">Report</a>
    </div>
    ${op.watch_requests.length ? `<h3>Watch flights</h3><ul class="list compact">${op.watch_requests.map((q) => `<li>${esc(q.target_label)} · ${pill(q.status)}
      ${q.status === "pending_approval" ? `<button type="button" data-approve="${esc(q.request_id)}" class="primary">Approve flight</button> <button type="button" data-deny="${esc(q.request_id)}">Deny</button>` : ""}
      </li>`).join("")}</ul><p class="small muted">Approval runs the same preflight checks as any inspection. The pilot remains in command.</p>` : ""}
    ${op.simulation ? `<details open><summary>Simulation controls</summary><div class="button-row small">
      Burnt ${Math.round(op.simulation.burnt_share * 100)}% · flaming outside ${op.simulation.flaming_m2_outside} m² ·
      <label>Spot fire outside: <select id="sim-side">${SIDES.map((s) => `<option>${s}</option>`).join("")}</select></label><button type="button" id="sim-spot">Start spot fire</button>
      <button type="button" id="sim-fault">Toggle thermal fault</button><button type="button" id="sim-advance">Advance 5 min</button></div></details>` : ""}
    <h3>Checklist</h3>
    <div class="scroll"><table class="table"><tbody>${r.items.map((i) => `<tr><td class="small muted">${esc(i.group)}</td><td class="wrap">${esc(i.label)}</td>
      <td>${pill(i.state, STATE_PILL[i.state])}</td><td class="wrap small">${esc(i.detail || "")}${i.by && !i.auto ? ` · ${esc(i.by)}` : ""}</td>
      <td>${i.auto ? '<span class="small muted">live data</span>' : `<button type="button" data-check="${esc(i.id)}" data-status="${i.state === "pass" ? "" : "done"}">${i.state === "pass" ? "Undo" : "Done"}</button>
        <button type="button" data-check="${esc(i.id)}" data-status="not_applicable">N/A</button>`}</td></tr>`).join("")}</tbody></table></div>
    <div class="grid2">
      <form id="op-prescription" class="card inner"><h3>Weather limits (burn boss)</h3>
        <p class="small muted">${p.source === "example" ? "Example values: set your own from the permit's conditions." : `Set by ${esc(p.set_by)}.`}</p>
        <div class="grid2">${[["min_wind_mps", "Lowest wind m/s"], ["max_wind_mps", "Highest wind m/s"], ["max_gust_mps", "Highest gust m/s"], ["min_humidity_pct", "Lowest humidity %"],
          ["max_temperature_c", "Highest temperature °C"], ["smoke_check_km", "Smoke check distance km"]].map(([k, l]) => `<label class="field">${l}<input type="number" step="0.1" name="${k}" value="${esc(p[k])}"></label>`).join("")}</div>
        <button type="submit">Save limits</button></form>
      <form id="op-weather" class="card inner"><h3>Weather observation</h3>
        <div class="grid2"><label class="field">Wind m/s<input type="number" step="0.1" name="wind_mps" required></label><label class="field">Gust m/s<input type="number" step="0.1" name="gust_mps"></label>
        <label class="field">Wind from (°)<input type="number" name="wind_from_deg" min="0" max="360"></label><label class="field">Humidity %<input type="number" name="humidity_pct" min="0" max="100"></label>
        <label class="field">Temperature °C<input type="number" step="0.1" name="temperature_c"></label><label class="field">Note<input type="text" name="note" maxlength="200"></label></div>
        <button type="submit">Record weather</button></form>
    </div>
    <div class="grid2">
      <div class="card inner"><h3>Crew</h3><ul class="list compact" id="op-crew">${op.crew.map((m, i) => `<li>${esc(m.name)} · ${esc(flame.data.roles[m.role])} <button type="button" data-crew-del="${i}">Remove</button></li>`).join("") || '<li class="muted">No crew named</li>'}</ul>
        <form id="op-crew-form" class="button-row"><input type="text" name="name" placeholder="Name" maxlength="60" required>
          <select name="role">${Object.entries(flame.data.roles).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select><button type="submit">Add</button></form></div>
      <div class="card inner"><h3>Equipment</h3><ul class="list compact">${op.equipment.map((e) => `<li><label class="field checkbox"><input type="checkbox" data-equip="${esc(e.id)}" ${e.checked ? "checked" : ""}> ${esc(e.label)}
        ${e.by ? `<span class="small muted">· ${esc(e.by)}</span>` : ""}</label></li>`).join("")}</ul></div>
    </div>
    <form id="op-note" class="button-row"><input type="text" name="text" placeholder="Crew observation (e.g. smoke drifting toward the road)" maxlength="500" required style="flex:1"><button type="submit">Add observation</button></form>
    <h3>Log</h3>
    <ul class="list compact flame-log">${op.observations.slice().reverse().slice(0, 60).map((o) => `<li class="sev-${esc(o.severity)}">${time(o.recorded_ms)} · ${esc(o.text)}</li>`).join("")}</ul>`;
  bindOperation(op);
}

function bindOperation(op) {
  const id = encodeURIComponent(op.operation_id);
  const on = (selector, handler) => { const node = $(selector); if (node) node.addEventListener("click", handler); };
  const post = async (path, body, message) => {
    const result = await flameAction(flameApi(`/operations/${id}${path}`, "POST", body), message);
    if (result) loadFlame(true);
    return result;
  };
  on("#op-start", async () => {
    const open = op.readiness.incomplete.length;
    if (open && !confirm(`${open} checklist item(s) are open. Start monitoring anyway? This is recorded in the report.`)) return;
    post("/start", { acknowledge_incomplete: open > 0 }, "Monitoring started");
  });
  on("#op-ignition", () => post("/ignition", { side: $('[name="ignition_side"]').value === "centre" ? "north" : $('[name="ignition_side"]').value }, "Ignition report recorded"));
  on("#op-watch", () => post("/watch", { side: $('[name="watch_side"]').value }, "Watch flight requested: approve it after the preflight checks"));
  on("#op-postburn", () => post("/post-burn", {}, "Post-burn inspection started"));
  on("#op-standdown", () => { const reason = prompt("Why is the operation being stood down? Everyone on the notification channels is told."); if (reason !== null) post("/stand-down", { reason }, "Stood down"); });
  on("#op-alert", () => { const text = prompt("Emergency message to everyone on the notification channels:"); if (text) post("/alert", { text }, "Message sent"); });
  on("#op-close", () => {
    const result = op.status === "planning" ? null : prompt("Post-burn result: no_heat_observed, heat_remains or not_inspected", "heat_remains");
    if (op.status !== "planning" && !result) return;
    post("/close", result ? { result: result.trim() } : {}, "Operation closed");
  });
  on("#sim-spot", () => post("/simulate", { action: "spot_fire", side: $("#sim-side").value, distance_m: 25 }));
  on("#sim-fault", () => post("/simulate", { action: "thermal_fault", on: op.thermal.state !== "fault" }));
  on("#sim-advance", () => post("/simulate", { action: "advance", seconds: 300 }));
  document.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", async () => {
    if (await flameAction(api(`/drone/inspections/${encodeURIComponent(b.dataset.approve)}/approve`, { method: "POST" }), "Flight approved")) loadFlame(true);
  }));
  document.querySelectorAll("[data-deny]").forEach((b) => b.addEventListener("click", async () => {
    if (await flameAction(api(`/drone/inspections/${encodeURIComponent(b.dataset.deny)}/deny`, { method: "POST", body: JSON.stringify({ note: "" }) }), "Denied")) loadFlame(true);
  }));
  const patch = async (body) => {
    const result = await flameAction(flameApi(`/operations/${id}`, "PATCH", body));
    if (result) { flame.operation = result; renderOperation(); }
  };
  document.querySelectorAll("[data-check]").forEach((b) => b.addEventListener("click", () => {
    const note = b.dataset.status === "not_applicable" ? prompt("Why does this not apply?") : "";
    if (note === null) return;
    patch({ checklist: { [b.dataset.check]: { status: b.dataset.status, note } } });
  }));
  document.querySelectorAll("[data-equip]").forEach((box) => box.addEventListener("change", () => patch({ equipment: [{ id: box.dataset.equip, checked: box.checked }] })));
  document.querySelectorAll("[data-crew-del]").forEach((b) => b.addEventListener("click", () => patch({ crew: op.crew.filter((_, i) => i !== Number(b.dataset.crewDel)) })));
  $("#op-crew-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const f = event.target.elements;
    patch({ crew: [...op.crew, { name: f.name.value, role: f.role.value }] });
  });
  $("#op-prescription").addEventListener("submit", (event) => {
    event.preventDefault();
    const body = {};
    for (const [k, v] of new FormData(event.target).entries()) if (v !== "") body[k] = Number(v);
    patch({ prescription: body });
  });
  $("#op-weather").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {};
    for (const [k, v] of new FormData(event.target).entries()) if (v !== "") body[k] = k === "note" ? v : Number(v);
    if (await flameAction(flameApi(`/operations/${id}/weather`, "POST", body), "Weather recorded")) refreshOperation();
  });
  $("#op-note").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (await flameAction(flameApi(`/operations/${id}/observations`, "POST", { text: event.target.elements.text.value }), "Observation recorded")) refreshOperation();
  });
}

// The tab is shown once Flame Mode is in use (a field, or an accessory registered).
(async () => {
  try {
    const data = await api("/flame");
    flameTab.hidden = !data.enabled;
  } catch {
    flameTab.hidden = true;
  }
})();
