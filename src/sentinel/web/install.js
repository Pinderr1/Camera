"use strict";
// Installation: the four installation paths as one guided journey, the device
// registry, the property planner and the readiness summary. Uses $, esc, api,
// toast and state from app.js, and the camera wizard / troubleshooter / zone
// editor from setup.js and troubleshoot.js.
//
// Wording rule: never say a property is "protected". Sentinel states what it
// has verified: devices connected, video available, AI monitoring verified.

const inst = {
  view: null,
  catalog: null,
  step: null,
  plan: { items: [] },
  planDirty: false,
  selected: null,
  tool: "select",
  drafting: null,
  drag: null,
  coverage: null,
  showCoverage: true,
  form: null, // open registration form: {category}
  loading: false,
};

const PATH_CARDS = {
  existing_cameras: ["Connect existing cameras", "You already have IP cameras, a DVR/NVR recorder, analog cameras through a recorder or capture device, or a USB camera."],
  new_cameras: ["Install new cameras", "You are starting from scratch: plan how many cameras you need, where they go, and the power and network they need."],
  drone_first: ["Drone-first installation", "Start with a Sentinel hub and a drone (or sensors). Fixed cameras can be added at any time. Flight stays simulated in this build."],
  complete_package: ["Complete Sentinel package", "Cameras, drone, dock and sensors managed together, added in any order as your system grows."],
};
const SUPPORT_LABELS = { verified: "Verified", simulated: "Simulated", expected: "Untested", limited: "Limited", planned: "Planned", unsupported: "Unsupported" };
const LEVEL_ORDER = ["registered", "connected", "video", "ai_verified"];
const CHECK_ICON = { pass: "✓", warn: "!", fail: "✕", pending: "…" };
const TOOL_GROUPS = [
  ["Property", ["building", "driveway", "garage", "yard", "parking", "fence", "obstruction"]],
  ["Entrances", ["entrance", "gate", "garage_door"]],
  ["Privacy", ["public_area", "privacy_area"]],
  ["Indoor", ["room"]],
  ["Equipment", ["camera", "sensor", "network", "drone_dock", "drone_point"]],
];
const PLAN_KIND_LABELS = {
  building: "Building", driveway: "Driveway", garage: "Garage", yard: "Yard", parking: "Parking", fence: "Fence / wall", obstruction: "Obstruction",
  entrance: "Door", gate: "Gate", garage_door: "Garage door", public_area: "Public area", privacy_area: "Privacy area", room: "Indoor room",
  camera: "Camera", sensor: "Sensor", network: "Network", drone_dock: "Drone dock", drone_point: "Drone point",
};
const AREA = new Set(["building", "driveway", "garage", "yard", "parking", "public_area", "privacy_area", "room", "obstruction"]);
const LINE = new Set(["fence"]);

// ---- loading and navigation ----------------------------------------------------------

document.querySelector('[data-tab="install"]').addEventListener("click", () => loadInstall(true));
$("#install-report").addEventListener("click", () => window.open(`${API}/installation/report.html`, "_blank", "noopener"));
$("#install-reset").addEventListener("click", async () => {
  if (!confirm("Start the installation journey again? Your cameras, devices and settings are kept; only the progress marks are reset.")) return;
  inst.view = await api("/installation/reset", { method: "POST", body: JSON.stringify({ keep_plan: true }) });
  inst.step = "type";
  renderInstall();
});

async function loadInstall(navigate = false) {
  if (inst.loading) return;
  inst.loading = true;
  try {
    const [view, catalog, guide] = await Promise.all([api("/installation"), inst.catalog || api("/devices/catalog"),
      inst.guide || api("/onboarding/guidance/new-cameras")]);
    inst.view = view;
    inst.catalog = catalog;
    inst.guide = guide;
    if (!inst.planDirty) inst.plan = structuredClone(view.state.plan || { items: [] });
    if (navigate || !inst.step) inst.step = view.state.current_step || "type";
  } catch (error) {
    $("#install-statement").textContent = `Could not load the installation: ${error.message}`;
    return;
  } finally {
    inst.loading = false;
  }
  renderInstall();
}

function installBadge(view) {
  const badge = $("#install-badge");
  if (badge) badge.hidden = !view || !!view.state.completed_ms;
}

async function gotoStep(step) {
  if (inst.planDirty && inst.step === "plan" && step !== "plan" && !confirm("Leave the plan without saving your changes?")) return;
  if (step !== "plan") inst.planDirty = false;
  inst.step = step;
  inst.form = null;
  try {
    inst.view = await api("/installation", { method: "PUT", body: JSON.stringify({ current_step: step }) });
    if (!inst.planDirty) inst.plan = structuredClone(inst.view.state.plan);
  } catch (error) {
    toast("Could not save progress", error.message);
  }
  renderInstall();
}

async function markStep(step, status, next = true) {
  try {
    inst.view = await api("/installation", { method: "PUT", body: JSON.stringify({ steps: { [step]: status } }) });
  } catch (error) {
    return toast("Could not save progress", error.message);
  }
  const ids = inst.view.steps.map((s) => s.id);
  if (next && ids.indexOf(step) < ids.length - 1) return gotoStep(ids[ids.indexOf(step) + 1]);
  renderInstall();
}

function renderInstall() {
  const view = inst.view;
  if (!view) return;
  installBadge(view);
  const r = view.readiness;
  $("#install-path-label").textContent = view.state.path ? view.paths[view.state.path] : "No installation type chosen yet";
  $("#install-steps").innerHTML = view.steps.map((step, index) => {
    const info = r.steps[step.id];
    const mark = info.status === "done" ? "done" : info.status === "skipped" ? "skipped" : "";
    const issue = info.fails ? `<span class="pill bad" title="${info.fails} problem(s)">${info.fails}</span>` : info.warnings ? `<span class="pill warn" title="${info.warnings} warning(s)">${info.warnings}</span>` : "";
    return `<li class="${mark} ${inst.step === step.id ? "current" : ""}"><button type="button" data-istep="${esc(step.id)}">
      <span class="num">${info.status === "done" ? "✓" : index + 1}</span><span>${esc(step.label)}</span>${issue}</button></li>`;
  }).join("");
  document.querySelectorAll("[data-istep]").forEach((button) => button.addEventListener("click", () => gotoStep(button.dataset.istep)));
  const levels = r.levels;
  $("#install-progress-bar").style.width = `${Math.round((100 * levels.steps_done) / levels.steps_total)}%`;
  const statement = $("#install-statement");
  statement.className = `banner ${r.statement.state === "monitoring_verified" ? "good" : ["not_monitoring", "partial"].includes(r.statement.state) ? "bad" : ""}`;
  statement.innerHTML = `<strong>${esc(r.statement.text)}</strong>${r.statement.extras.length ? `<div class="small">${r.statement.extras.map(esc).join(" ")}</div>` : ""}
    ${view.state.completed_ms ? `<div class="small">Installation completed ${dateTime(view.state.completed_ms)}${view.state.completed_with_issues.length ? ` with ${view.state.completed_with_issues.length} open issue(s)` : ""}.</div>` : ""}
    ${view.recovered ? `<div class="small status-bad">${esc(view.recovered)}</div>` : ""}`;
  const renderers = { type: stepType, property: stepProperty, plan: stepPlan, hardware: stepHardware, verify: stepVerify, zones: stepZones,
    rules: stepRules, alerts: stepAlerts, health: stepHealth, finish: stepFinish };
  const label = view.steps.find((s) => s.id === inst.step)?.label || "";
  $("#install-step").innerHTML = `<div class="row-between"><h3>${esc(label)}</h3><span class="small muted">Step ${view.steps.findIndex((s) => s.id === inst.step) + 1} of ${view.steps.length}</span></div>${renderers[inst.step]()}`;
  bindStep();
}

function stepFooter(step, { skippable = false, nextLabel = "Continue" } = {}) {
  const ids = inst.view.steps.map((s) => s.id);
  const index = ids.indexOf(step);
  return `<div class="row-between step-footer">
    <button type="button" data-goto="${esc(ids[Math.max(0, index - 1)])}" ${index === 0 ? "disabled" : ""}>Back</button>
    <div class="button-row tight">${skippable ? `<button type="button" data-mark="${esc(step)}" data-status="skipped">Skip for now</button>` : ""}
    <button type="button" class="primary" data-mark="${esc(step)}" data-status="done">${esc(nextLabel)}</button></div></div>`;
}

function checksHtml(step, filter = () => true) {
  const checks = inst.view.readiness.checks.filter((check) => check.step === step && filter(check));
  if (!checks.length) return "";
  return `<ul class="checks">${checks.map((check) => `<li class="check-${esc(check.status)}"><span class="icon" aria-label="${esc(check.status)}">${CHECK_ICON[check.status]}</span>
    <div><div>${esc(check.title)}</div>${check.detail ? `<div class="small muted">${esc(check.detail)}</div>` : ""}</div></li>`).join("")}</ul>`;
}

// ---- step 1: installation type ----------------------------------------------------------

function stepType() {
  const current = inst.view.state.path;
  return `<p class="muted small">Choose how you are starting. You can add any kind of hardware later, whichever path you pick.</p>
    <div class="choice-grid path-grid">${Object.entries(PATH_CARDS).map(([id, [title, text]]) =>
      `<button type="button" class="choice ${current === id ? "selected" : ""}" data-path="${esc(id)}"><strong>${esc(title)}</strong><span class="small muted">${esc(text)}</span></button>`).join("")}</div>
    ${current ? stepFooter("type") : ""}`;
}

// ---- step 2: property --------------------------------------------------------------------

function stepProperty() {
  const site = inst.view.site;
  const property = inst.view.state.property || {};
  const geo = site.origin_geo || [];
  const needsLocation = ["drone_first", "complete_package"].includes(inst.view.state.path);
  return `<form id="property-form" class="install-form">
    <div class="grid2">
      <label class="field">Property name <input type="text" name="name" maxlength="80" required value="${esc(site.name || "")}"></label>
      <label class="field">Property type <select name="property_type">${Object.entries(inst.view.property_types).map(([id, label]) =>
        `<option value="${esc(id)}" ${property.property_type === id ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>
      <label class="field">Width of the area to plan (m) <input type="number" name="width_m" min="1" max="5000" step="1" required value="${esc(site.width_m ?? 40)}"></label>
      <label class="field">Depth (m) <input type="number" name="height_m" min="1" max="5000" step="1" required value="${esc(site.height_m ?? 30)}"></label>
    </div>
    <fieldset><legend>Location ${needsLocation ? "(needed for drones)" : "(optional; only needed for drones)"}</legend>
      <p class="small muted">Used only on this computer to place the drone geofence. It is never sent anywhere.</p>
      <div class="grid2">
        <label class="field">Latitude <input type="number" name="latitude" min="-90" max="90" step="0.000001" value="${esc(geo[0] ?? "")}"></label>
        <label class="field">Longitude <input type="number" name="longitude" min="-180" max="180" step="0.000001" value="${esc(geo[1] ?? "")}"></label>
      </div>
      <button type="button" id="use-test-location" class="small">Use a test location (simulation only)</button>
    </fieldset>
    <label class="field">Notes for the installer <input type="text" name="notes" maxlength="500" value="${esc(property.notes || "")}"></label>
    ${checksHtml("property")}
    <div class="row-between step-footer"><button type="button" data-goto="type">Back</button><button type="submit" class="primary">Save and continue</button></div>
  </form>`;
}

// ---- step 3: plan or discover --------------------------------------------------------------

function stepPlan() {
  const path = inst.view.state.path;
  const recommendation = inst.view.state.recommendation;
  const answers = inst.view.state.questionnaire || {};
  const intro = {
    existing_cameras: "Optional: find what is already on your network, and place your cameras on the map to see coverage gaps. Planning can be skipped.",
    new_cameras: "Answer a few questions for a starting camera count, then place cameras on the map to check what they would see before buying anything.",
    drone_first: "Mark your property, the drone dock and the places a drone should be able to look at. Fixed cameras can be planned later.",
    complete_package: "Plan cameras, sensors, the drone dock and observation points together.",
  }[path] || "Choose an installation type first.";
  const questions = ["new_cameras", "complete_package"].includes(path) || recommendation;
  const number = (name, label, max = 20) => `<label class="field">${esc(label)} <input type="number" name="${name}" min="0" max="${max}" value="${esc(answers[name] ?? 0)}"></label>`;
  const check = (name, label) => `<label class="field checkbox"><input type="checkbox" name="${name}" ${answers[name] ? "checked" : ""}> ${esc(label)}</label>`;
  return `<p class="muted small">${esc(intro)}</p>
    ${path === "existing_cameras" ? `<div class="banner">Search your network for cameras and recorders, or check one that does not answer.
      <div class="button-row"><button type="button" id="plan-find" class="primary">Find &amp; troubleshoot cameras</button></div></div>` : ""}
    ${questions ? `<details class="card inner" id="recommend-box" ${!recommendation || inst.recommendOpen ? "open" : ""}><summary><strong>How many cameras?</strong> ${recommendation ? `<span class="small muted">Suggested: ${recommendation.essential} essential, ${recommendation.total} in total</span>` : ""}</summary>
      <form id="recommend-form" class="install-form">
        <div class="grid3">${number("exterior_doors", "Exterior doors")}${number("garage_doors", "Garage doors", 10)}${number("gates", "Gates", 10)}
          ${number("yard_sides", "Sides of the building to watch", 4)}${number("loading_areas", "Loading areas (business)", 10)}${number("indoor_areas", "Indoor areas to watch")}</div>
        <div class="chips">${check("driveway", "Driveway or parking")}${check("want_sensors", "Door/window sensors")}${check("want_drone", "Drone")}</div>
        <label class="field">Wiring <select name="wired">${[["unsure", "Not sure"], ["poe", "Wired (PoE)"], ["wifi", "Wi-Fi"], ["mixed", "Mixed"]].map(([v, l]) => `<option value="${v}" ${answers.wired === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <button type="submit">Suggest cameras</button>
      </form>
      ${recommendation ? recommendationHtml(recommendation) : ""}</details>` : ""}
    <div class="planner">
      <div class="planner-tools" role="toolbar" aria-label="Plan tools">
        <button type="button" data-tool="select" class="${inst.tool === "select" ? "active" : ""}">Select / move</button>
        ${TOOL_GROUPS.map(([group, kinds]) => `<span class="tool-group"><span class="small muted">${esc(group)}</span>${kinds.map((kind) =>
          `<button type="button" data-tool="${kind}" class="${inst.tool === kind ? "active" : ""}">${esc(PLAN_KIND_LABELS[kind])}</button>`).join("")}</span>`).join("")}
      </div>
      <div class="planner-layout">
        <div class="canvas-wrap map-wrap planner-canvas"><svg id="plan-svg" aria-label="Property planner" tabindex="0"></svg></div>
        <div class="planner-side">
          <div id="plan-help" class="small muted">${planHelp()}</div>
          <div id="plan-props"></div>
          <div class="button-row">
            <button type="button" class="primary" id="plan-save">Save plan</button>
            <button type="button" id="plan-check">Check coverage</button>
            <label class="small"><input type="checkbox" id="plan-show-coverage" ${inst.showCoverage ? "checked" : ""}> Show coverage</label>
          </div>
          <div class="small" id="plan-status">${inst.planDirty ? "Unsaved changes" : ""}</div>
          <div id="plan-coverage"></div>
        </div>
      </div>
    </div>
    ${stepFooter("plan", { skippable: true })}`;
}

function recommendationHtml(r) {
  return `<table class="table"><tr><th>Where</th><th>Cameras</th><th></th><th>Why</th></tr>${r.lines.map((line) =>
    `<tr><td>${esc(line.role)}</td><td>${line.count}</td><td class="small">${line.essential ? "essential" : "optional"} · ${esc(line.lens)} lens (~${line.range_m} m)</td><td class="small wrap">${esc(line.why)}</td></tr>`).join("")}</table>
    <p class="small">PoE ports: ${r.poe_ports} · PoE budget ${r.poe_budget_w} W (15.4 W per camera when the datasheet value is unknown).
      ${r.sensors.length ? `Sensors: ${r.sensors.map((s) => `${s.count} ${esc(s.kind)}`).join(", ")}.` : ""} ${r.drone ? esc(r.drone.why) : ""}</p>
    <ul class="small">${r.notes.map((note) => `<li>${esc(note)}</li>`).join("")}</ul>
    <p class="small muted">${esc(r.disclaimer)}</p>
    <button type="button" id="recommend-place">Add suggested cameras to the plan</button>`;
}

function planHelp() {
  if (inst.drafting) return `Drawing ${esc(PLAN_KIND_LABELS[inst.drafting.kind])}: click to add corners; click the first corner, double-click or press Enter to finish (Esc cancels). <button type="button" id="plan-finish">Finish</button>`;
  if (inst.tool !== "select") return `Click on the map to place a ${esc(PLAN_KIND_LABELS[inst.tool].toLowerCase())}.`;
  return "Select an item to edit it. Drag items to move them; drag a camera's small handle to aim it. Delete removes the selection.";
}

// ---- step 4: hardware ------------------------------------------------------------------------

function stepHardware() {
  const path = inst.view.state.path;
  const cameraFirst = ["existing_cameras", "new_cameras", "complete_package"].includes(path);
  return `<p class="muted small">Registering a device records what is installed. Sentinel only reports it as connected, delivering video, or verified when it actually receives data from it.</p>
    <div class="button-row add-devices">
      ${cameraFirst ? `<button type="button" class="primary" id="hw-add-camera">Add camera</button><button type="button" id="hw-find">Find &amp; troubleshoot cameras</button><button type="button" data-register="recorder">Register recorder (DVR/NVR)</button>` : ""}
      <button type="button" ${path === "drone_first" ? 'class="primary"' : ""} data-register="drone">Register drone</button>
      <button type="button" data-register="contact_sensor">Register sensor</button>
      <button type="button" data-register="dock">Register dock</button>
      ${!cameraFirst ? `<button type="button" id="hw-add-camera">Add camera</button>` : ""}
    </div>
    ${inst.form ? registrationForm(inst.form.category) : ""}
    ${devicesHtml()}
    ${cameraFirst ? guideHtml(path === "existing_cameras") : ""}
    ${checksHtml("hardware")}
    ${stepFooter("hardware")}`;
}

function guideHtml(existing) {
  const sections = (inst.guide || []).filter((section) => !existing || section.id === "streams");
  return `<details class="card inner" id="camera-guide"><summary><strong>Camera not showing up?</strong> <span class="small muted">Common power, network and login problems</span></summary>
    ${existing ? '<p class="small">For a camera or recorder that does not answer, use "Find &amp; troubleshoot cameras": it checks your network and explains what to change.</p>' : ""}
    ${sections.map((section) => `<h4>${esc(section.title)}</h4>${section.problems.map((problem) => `<p class="small"><strong>${esc(problem.symptom)}</strong></p>
      <ul class="small">${problem.checks.map((check) => `<li>${esc(check)}</li>`).join("")}</ul>`).join("")}`).join("")}
    <p class="small muted">Sentinel only explains what to change. It never changes router, camera or computer settings itself.</p></details>`;
}

function supportBadge(support) {
  const level = inst.catalog?.support_levels?.[support] || "";
  return `<span class="support support-${esc(support)}" title="${esc(level)}">${esc(SUPPORT_LABELS[support] || support)}</span>`;
}

function levelChips(device) {
  const reached = LEVEL_ORDER.indexOf(device.level);
  const applies = device.category === "camera" || device.category === "recorder" ? LEVEL_ORDER : ["registered", "connected"];
  return `<span class="levels">${applies.map((level) => `<span class="lvl ${LEVEL_ORDER.indexOf(level) <= reached ? "on" : ""}">${esc(inst.catalog.levels[level])}</span>`).join("")}</span>`;
}

function devicesHtml() {
  const devices = inst.view.devices;
  const categories = inst.catalog.categories;
  return `<ul class="list device-list">${devices.map((device) => `<li class="device" data-device="${esc(device.id)}">
      <div class="row-between"><div><strong>${esc(device.name)}</strong> <span class="small muted">${esc(categories[device.category] || device.category)}</span> ${supportBadge(device.support)}</div>
        <div class="button-row tight">
          ${device.category.endsWith("_sensor") && device.simulated ? `<button type="button" data-device-test="${esc(device.id)}">Test</button>` : ""}
          ${device.category === "camera" && device.level !== "ai_verified" && device.state !== "not_running" ? `<button type="button" data-device-troubleshoot="${esc(device.id)}">Troubleshoot</button>` : ""}
          ${device.category === "recorder" ? `<button type="button" data-recorder-channels="${esc(device.id)}">Find channels</button>` : ""}
          ${device.removable ? `<button type="button" class="danger" data-device-remove="${esc(device.id)}">Remove</button>` : ""}
        </div></div>
      <div>${levelChips(device)}</div>
      <div class="small ${["ready", "monitoring", "reporting", "running", "connected"].includes(device.state) ? "status-ok" : "muted"}">${esc(device.detail || "")}${device.battery_pct != null ? ` · battery ${Math.round(device.battery_pct)}%` : ""}</div>
      ${(device.warnings || []).length ? `<div class="small status-warn">${device.warnings.map((w) => esc(String(w).replace(/_/g, " "))).join(" · ")}</div>` : ""}
    </li>`).join("")}</ul>`;
}

function profileOptions(category, selected) {
  return inst.catalog.profiles.filter((p) => p.category === category && p.support !== "unsupported")
    .map((p) => `<option value="${esc(p.id)}" ${p.id === selected ? "selected" : ""}>${esc(p.name)} (${esc(SUPPORT_LABELS[p.support])})</option>`).join("");
}

function registrationForm(category) {
  const planned = inst.plan.items;
  const docks = planned.filter((i) => i.kind === "drone_dock");
  const drones = inst.view.devices.filter((d) => d.category === "drone" && d.removable);
  const zones = (state.site?.cameras || []).flatMap((c) => (c.zones || []).map((z) => ({ id: z.id, name: `${c.name} – ${z.name}` })));
  const common = `<label class="field">Name <input type="text" name="name" maxlength="60" required></label>`;
  let body = "";
  if (category === "recorder") {
    body = `${common}<label class="field">Kind <select name="profile">${profileOptions("recorder", "hikvision_recorder")}</select></label>
      <div class="grid2"><label class="field">Address on your network <input type="text" name="host" maxlength="253" placeholder="e.g. 192.168.1.84"></label>
      <label class="field">Channels (cameras) <input type="number" name="channels" min="1" max="64" value="8"></label></div>
      <label class="field">Model (optional) <input type="text" name="model" maxlength="60"></label>
      <p class="small muted">No password is stored here. Find the recorder's channels next; its password is entered once for that search and kept in the credential store.</p>`;
  } else if (category === "drone") {
    body = `${common}<label class="field">Drone <select name="profile">${profileOptions("drone", "simulated_drone")}</select></label>
      <label class="field">MAVLink connection (MAVLink drones only) <input type="text" name="connection" placeholder="udp:127.0.0.1:14550"></label>
      <label class="field">Home / dock position <select name="home">${docks.map((d) => `<option value="${esc(d.point.join(","))}">${esc(d.name)} (planned dock)</option>`).join("")}<option value="">Property corner (0, 0)</option></select></label>
      <label class="field checkbox"><input type="checkbox" name="indoor"> Indoor drone</label>
      <p class="small muted">Real flight commands are disabled in this build: a MAVLink drone reports telemetry only. Every inspection needs a person's approval.</p>`;
  } else if (category === "dock") {
    body = `${common}<label class="field">Dock <select name="profile">${profileOptions("dock", "simulated_dock")}</select></label>
      <label class="field">Drone <select name="drone_id"><option value="">Primary drone</option>${drones.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("")}</select></label>`;
  } else {
    const kinds = ["contact_sensor", "motion_sensor", "environmental_sensor"];
    body = `${common}<label class="field">Type <select name="category">${kinds.map((k) => `<option value="${k}" ${k === category ? "selected" : ""}>${esc(inst.catalog.categories[k])}</option>`).join("")}</select></label>
      <label class="field">Hardware <select name="profile">${profileOptions(category, "")}</select></label>
      <label class="field">Zone (optional) <select name="zone_id"><option value="">None</option>${zones.map((z) => `<option value="${esc(z.id)}">${esc(z.name)}</option>`).join("")}</select></label>
      <p class="small muted">Sensors are logged only in this build; they do not raise incidents. Planned sensor hardware can be registered, but Sentinel cannot receive its data yet.</p>`;
  }
  return `<form id="register-form" class="card inner install-form" data-category="${esc(category)}"><h3>Register ${esc(inst.catalog.categories[category].toLowerCase())}</h3>${body}
    <div class="button-row"><button type="submit" class="primary">Register</button><button type="button" id="register-cancel">Cancel</button></div></form>`;
}

// ---- step 5: verify -----------------------------------------------------------------------

function stepVerify() {
  const cameras = inst.view.devices.filter((d) => d.category === "camera");
  const levels = inst.view.readiness.levels;
  return `<p class="muted small">Each device is checked separately. "Video available" means new frames are arriving; "AI monitoring verified" means changing frames are reaching a working detector.</p>
    <div class="tiles">
      ${tile("Devices connected", `${levels.devices_connected} / ${levels.devices_total}`)}
      ${tile("Cameras with video", `${levels.cameras_video} / ${levels.cameras_total}`)}
      ${tile("AI monitoring verified", `${levels.cameras_ai_verified} / ${levels.cameras_total}`)}
      ${tile("Drones ready", `${levels.drones_ready}`)}
      ${tile("Sensors reporting", `${levels.sensors_reporting}`)}
    </div>
    ${!inst.view.processing && cameras.length ? `<div class="banner bad">Camera processing is not running in this session, so cameras cannot be verified here.</div>` : ""}
    ${cameras.length ? `<div class="preview-grid">${cameras.map((camera) => `<figure class="preview-tile"><img alt="${esc(camera.name)} preview" src="${API}/cameras/${encodeURIComponent(camera.id)}/snapshot.jpg?t=${Date.now()}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'no-frame small muted',textContent:'No picture yet'}))">
      <figcaption class="small"><strong>${esc(camera.name)}</strong> ${levelChips(camera)}</figcaption></figure>`).join("")}</div>` : ""}
    ${checksHtml("verify")}
    <div class="button-row"><button type="button" id="verify-retest">Check again</button></div>
    ${stepFooter("verify")}`;
}

function tile(label, value) {
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`;
}

// ---- step 6: zones and privacy ------------------------------------------------------------

function stepZones() {
  const cameras = inst.view.devices.filter((d) => d.category === "camera");
  const drone = inst.view.drone;
  const plannedPoints = inst.plan.items.filter((i) => i.kind === "drone_point");
  return `<p class="muted small">Zones tell Sentinel what each part of a camera's picture is (driveway, entrance, yard, sidewalk). Privacy masks black out areas before detection, recording and streaming.</p>
    ${cameras.length ? `<table class="table"><tr><th>Camera</th><th>Zones</th><th>Privacy masks</th><th></th></tr>${cameras.map((camera) =>
      `<tr><td>${esc(camera.name)}</td><td class="${camera.zones ? "" : "status-bad"}">${camera.zones}</td><td>${camera.privacy_masks}</td>
       <td><button type="button" data-edit-zones="${esc(camera.id)}">Draw zones &amp; masks</button></td></tr>`).join("")}</table>` : `<p>No cameras yet.</p>`}
    ${drone.enabled ? `<h3>Drone inspection locations</h3>
      <ul class="list compact">${drone.inspection_points.map((p) => `<li>${esc(p.name)} <span class="small muted">(${p.map[0]}, ${p.map[1]} m)</span></li>`).join("") || '<li class="muted">None yet.</li>'}</ul>
      ${plannedPoints.length ? `<button type="button" id="zones-use-points">Use the ${plannedPoints.length} observation point(s) from the plan</button>` : `<p class="small muted">Add drone observation points on the plan (step 3), or zone footprints on the property map.</p>`}` : ""}
    ${checksHtml("zones")}
    ${stepFooter("zones")}`;
}

// ---- step 7: rules -----------------------------------------------------------------------

function stepRules() {
  const rules = (state.site?.rules || []);
  return `<p class="muted small">Sentinel always logs activity in your zones. Security rules decide what becomes an incident worth your attention. Every rule must pass a test before it can be turned on.</p>
    <ul class="list compact">${rules.map((rule) => `<li><span class="sev ${esc(rule.severity)}">${esc(rule.severity)}</span> ${esc(rule.name || rule.id.replace(/_/g, " "))}
      <span class="small muted">${rule.enabled === false ? "off" : "on"}</span></li>`).join("") || '<li class="muted">No rules yet.</li>'}</ul>
    <div class="button-row"><button type="button" class="primary" id="rules-open">Open the rule builder</button><button type="button" id="categories-open">What Sentinel looks for</button></div>
    ${checksHtml("rules")}
    ${stepFooter("rules", { skippable: true })}`;
}

// ---- step 8: alerts --------------------------------------------------------------------------

function stepAlerts() {
  const n = inst.view.notifications;
  const last = n.last_test;
  const thresholds = n.thresholds || {};
  return `<p class="muted small">Sentinel pushes a notification once per incident (again only if it gets more serious), within a rate limit. Ordinary activity is never pushed.</p>
    <form id="alerts-form" class="install-form">
      <fieldset><legend>Phone notifications (ntfy)</legend>
        <label class="field checkbox"><input type="checkbox" name="ntfy_enabled" ${n.ntfy.enabled ? "checked" : ""}> Send push notifications with ntfy</label>
        <div class="grid2"><label class="field">Server <input type="text" name="server" value="${esc(n.ntfy.server)}" maxlength="200"></label>
          <label class="field">Topic ${n.ntfy.topic_set ? '<span class="small muted">(set; leave blank to keep)</span>' : ""} <input type="password" name="topic" autocomplete="off" maxlength="64" placeholder="${n.ntfy.topic_set ? "••••••" : "a long, hard-to-guess name"}"></label></div>
        <p class="small muted">On public servers the topic works like a password: anyone who knows it can read your alerts. It is kept in the credential store, never in the configuration.</p>
        <label class="field checkbox"><input type="checkbox" name="attach_snapshot" ${n.ntfy.attach_snapshot ? "checked" : ""}> Attach a snapshot (sends images to the push service)</label>
      </fieldset>
      <label class="field checkbox"><input type="checkbox" name="mqtt_enabled" ${n.mqtt.enabled ? "checked" : ""} ${n.mqtt.broker_configured ? "" : "disabled"}> Send alerts to MQTT (Home Assistant) ${n.mqtt.broker_configured ? "" : '<span class="small muted">(configure the MQTT broker in the config file first)</span>'}</label>
      <div class="button-row"><button type="submit" class="primary">Save notification settings</button><button type="button" id="alerts-test">Send test notification</button></div>
    </form>
    ${last ? `<p class="small">Last test ${dateTime(last.ms)}: ${Object.entries(last.outcomes).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(" · ")}. Check that it arrived on the phone.</p>` : ""}
    <h3>When Sentinel pushes</h3>
    <table class="table small"><tr><th>Arm mode</th><th>Pushes incidents of</th></tr>${Object.entries(thresholds).map(([mode, minimum]) =>
      `<tr><td>${esc(mode)}</td><td>${minimum === "none" ? "nothing" : `${esc(minimum)} severity and above`}</td></tr>`).join("")}</table>
    ${checksHtml("alerts")}
    ${stepFooter("alerts", { skippable: true })}`;
}

// ---- step 9: health -------------------------------------------------------------------------

function stepHealth() {
  return `<p class="muted small">The same checks Sentinel runs around the clock: storage, database, backups and the AI pipeline.</p>
    ${checksHtml("health")}
    <div class="button-row"><button type="button" id="health-retest">Check again</button><button type="button" id="health-system">Open the System tab</button></div>
    ${stepFooter("health")}`;
}

// ---- step 10: finish -------------------------------------------------------------------------

function stepFinish() {
  const r = inst.view.readiness;
  const levels = r.levels;
  const done = inst.view.state.completed_ms;
  const issues = r.checks.filter((c) => c.status === "fail" || c.status === "warn");
  return `<div class="tiles">
      ${tile("Installation steps", `${levels.steps_done} / ${levels.steps_total}`)}
      ${tile("Devices connected", `${levels.devices_connected} / ${levels.devices_total}`)}
      ${tile("Cameras with video", `${levels.cameras_video} / ${levels.cameras_total}`)}
      ${tile("AI monitoring verified", `${levels.cameras_ai_verified} / ${levels.cameras_total}`)}
    </div>
    <p><strong>${esc(r.statement.text)}</strong></p>${r.statement.extras.map((e) => `<p class="small">${esc(e)}</p>`).join("")}
    ${r.blocking.length ? `<div class="banner bad"><strong>Installation cannot be completed yet:</strong><ul>${r.blocking.map((b) => `<li>${esc(b)}</li>`).join("")}</ul></div>` : ""}
    ${issues.length ? `<h3>Open issues (${issues.length})</h3><ul class="checks">${issues.map((c) => `<li class="check-${esc(c.status)}"><span class="icon">${CHECK_ICON[c.status]}</span><div>${esc(c.title)}${c.detail ? `<div class="small muted">${esc(c.detail)}</div>` : ""}</div></li>`).join("")}</ul>` : "<p>No open issues.</p>"}
    ${done ? `<div class="banner good">Installation completed ${dateTime(done)}. Add or replace hardware at any time from step 4; readiness updates automatically.</div>
      <div class="button-row"><button type="button" class="primary" id="finish-dashboard">Go to the dashboard</button><button type="button" id="finish-reopen">Reopen installation</button></div>`
    : `<label class="field checkbox"><input type="checkbox" id="finish-ack" ${issues.length ? "" : "checked"}> I understand the open issues above; Sentinel reports only what it has verified.</label>
      <div class="button-row"><button type="button" class="primary" id="finish-complete" ${r.can_complete ? "" : "disabled"}>Complete installation</button>
      <button type="button" id="finish-report">Installation plan (print)</button></div>`}`;
}

// ---- binding ------------------------------------------------------------------------------

function bindStep() {
  const on = (selector, event, handler) => document.querySelectorAll(selector).forEach((node) => node.addEventListener(event, handler));
  on("[data-goto]", "click", (e) => gotoStep(e.currentTarget.dataset.goto));
  on("[data-mark]", "click", (e) => markStep(e.currentTarget.dataset.mark, e.currentTarget.dataset.status));
  on("[data-path]", "click", async (e) => {
    try {
      inst.view = await api("/installation", { method: "PUT", body: JSON.stringify({ path: e.currentTarget.dataset.path }) });
      renderInstall();
    } catch (error) {
      toast("Could not save", error.message);
    }
  });
  const property = $("#property-form");
  if (property) {
    $("#use-test-location").addEventListener("click", () => { property.latitude.value = "45.0"; property.longitude.value = "-75.0"; });
    property.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(property).entries());
      const body = { name: data.name, property_type: data.property_type, width_m: Number(data.width_m), height_m: Number(data.height_m), notes: data.notes };
      if (data.latitude !== "" && data.longitude !== "") Object.assign(body, { latitude: Number(data.latitude), longitude: Number(data.longitude) });
      try {
        await api("/installation/property", { method: "POST", body: JSON.stringify(body) });
        $("#site-name").textContent = body.name;
        gotoStep("plan");
      } catch (error) {
        toast("Property not saved", error.message);
      }
    });
  }
  const recommendForm = $("#recommend-form");
  $("#recommend-box")?.addEventListener("toggle", (event) => { inst.recommendOpen = event.target.open; });
  if (recommendForm) recommendForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    inst.recommendOpen = true;
    const data = new FormData(recommendForm);
    const body = { property_type: inst.view.state.property?.property_type || "house", wired: data.get("wired") };
    for (const name of ["exterior_doors", "garage_doors", "gates", "yard_sides", "loading_areas", "indoor_areas"]) body[name] = Number(data.get(name) || 0);
    for (const name of ["driveway", "want_sensors", "want_drone"]) body[name] = data.get(name) === "on";
    try {
      await api("/installation/recommend", { method: "POST", body: JSON.stringify(body) });
      await loadInstall();
    } catch (error) {
      toast("No suggestion", error.message);
    }
  });
  on("#recommend-place", "click", placeRecommended);
  on("#plan-find, #hw-find", "click", () => openTroubleshooter({}));
  on("#hw-add-camera", "click", () => openWizard(null));
  on("[data-register]", "click", (e) => { inst.form = { category: e.currentTarget.dataset.register }; renderInstall(); $("#register-form [name=name]")?.focus(); });
  on("#register-cancel", "click", () => { inst.form = null; renderInstall(); });
  const register = $("#register-form");
  if (register) {
    const kind = register.querySelector("[name=category]");
    if (kind) kind.addEventListener("change", () => { inst.form = { category: kind.value }; renderInstall(); });
    register.addEventListener("submit", submitRegistration);
  }
  on("[data-device-remove]", "click", (e) => removeDevice(e.currentTarget.dataset.deviceRemove));
  on("[data-device-test]", "click", async (e) => {
    try {
      const event = await api(`/devices/${encodeURIComponent(e.currentTarget.dataset.deviceTest)}/test`, { method: "POST", body: "{}" });
      toast("Sensor test received", event.summary, "info");
      loadInstall();
    } catch (error) {
      toast("Test failed", error.message);
    }
  });
  on("[data-device-troubleshoot]", "click", (e) => troubleshootCamera(e.currentTarget.dataset.deviceTroubleshoot));
  on("[data-recorder-channels]", "click", (e) => {
    const recorder = inst.view.devices.find((d) => d.id === e.currentTarget.dataset.recorderChannels);
    openTroubleshooter({ host: recorder.host || undefined, template: recorder.brand_template });
    if (recorder.host) tsGo("streams");
  });
  on("#verify-retest, #health-retest", "click", () => loadInstall());
  on("[data-edit-zones]", "click", (e) => openZoneEditor(e.currentTarget.dataset.editZones));
  on("#zones-use-points", "click", usePlannedPoints);
  on("#rules-open", "click", () => openSetup("rules"));
  on("#categories-open", "click", () => openSetup("categories"));
  on("#health-system", "click", () => document.querySelector('[data-tab="system"]').click());
  const alerts = $("#alerts-form");
  if (alerts) alerts.addEventListener("submit", saveAlerts);
  on("#alerts-test", "click", sendTestNotification);
  on("#finish-complete", "click", completeInstallation);
  on("#finish-report", "click", () => $("#install-report").click());
  on("#finish-reopen", "click", async () => { inst.view = await api("/installation/reopen", { method: "POST", body: "{}" }); renderInstall(); });
  on("#finish-dashboard", "click", () => document.querySelector('[data-tab="live"]').click());
  if (inst.step === "plan") bindPlanner();
}

async function submitRegistration(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  const category = data.category || form.dataset.category;
  const profile = inst.catalog.profiles.find((p) => p.id === data.profile);
  const body = { category, name: data.name, profile: data.profile, simulated: profile?.support === "simulated" };
  if (category === "recorder") Object.assign(body, { host: data.host || null, channels: data.channels ? Number(data.channels) : null, model: data.model || null });
  if (category === "drone") {
    if (data.connection) body.connection = data.connection;
    body.indoor = data.indoor === "on";
    if (data.home) { const [x, y] = data.home.split(",").map(Number); body.map = { x, y }; }
    if (!inst.view.site.origin_geo) return toast("Set the property location first", "A drone needs the property's location for its geofence (step 2).");
  }
  if (category === "dock" && data.drone_id) body.drone_id = data.drone_id;
  if (data.zone_id) body.zone_id = data.zone_id;
  try {
    const device = await api("/devices", { method: "POST", body: JSON.stringify(body) });
    if (category === "drone") {
      const first = !inst.view.devices.some((d) => d.category === "drone" && d.removable);
      await api("/config/drone", { method: "PUT", body: JSON.stringify({ enabled: true, ...(first ? { device: device.id } : {}) }) });
    }
    if (category === "dock" && device.profile === "simulated_dock") {
      const dock = inst.plan.items.find((i) => i.kind === "drone_dock" && !i.device_id);
      if (dock && !inst.planDirty) { dock.device_id = device.id; await api("/installation/plan", { method: "PUT", body: JSON.stringify(inst.plan) }); }
    }
    toast("Registered", `${device.name}. ${profile?.support === "planned" ? "Planned hardware: Sentinel cannot connect to it yet." : ""}`, "info");
    inst.form = null;
    await loadInstall();
  } catch (error) {
    toast("Not registered", error.message);
  }
}

async function removeDevice(id) {
  const device = inst.view.devices.find((d) => d.id === id);
  if (!confirm(`Remove ${device.name}? ${device.category === "camera" ? "Its zones, stored password and connections are removed too. " : ""}Recorded history is kept.`)) return;
  try {
    const result = await api(`/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("Removed", [device.name, ...(result.notes || [])].join(". "), "info");
    inst.planDirty = false;
    await loadInstall();
  } catch (error) {
    toast("Not removed", error.message);
  }
}

async function troubleshootCamera(id) {
  try {
    const config = await api("/config");
    const camera = config.cameras.find((c) => c.id === id);
    const source = camera?.source || {};
    if (source.host) openTroubleshooter({ host: source.host, template: source.template, port: source.port, autorun: true });
    else openWizard(camera);
  } catch (error) {
    toast("Could not open the troubleshooter", error.message);
  }
}

function openSetup(sub) {
  document.querySelector('[data-tab="setup"]').click();
  showSub(sub);
}

async function openZoneEditor(cameraId) {
  document.querySelector('[data-tab="setup"]').click();
  await loadSetup();
  showSub("zones");
  selectZoneCamera(cameraId);
}

async function usePlannedPoints() {
  const points = inst.plan.items.filter((i) => i.kind === "drone_point").map((i) => ({ name: i.name, map: i.point }));
  try {
    await api("/config/drone", { method: "PUT", body: JSON.stringify({ inspection_points: points }) });
    toast("Inspection locations saved", `${points.length} location(s)`, "info");
    loadInstall();
  } catch (error) {
    toast("Not saved", error.message);
  }
}

async function saveAlerts(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = { ntfy: { enabled: form.ntfy_enabled.checked, server: form.server.value.trim(), attach_snapshot: form.attach_snapshot.checked } };
  if (form.topic.value.trim()) body.ntfy.topic = form.topic.value.trim();
  if (!form.ntfy_enabled.checked && !inst.view.notifications.ntfy.topic_set && !body.ntfy.topic) delete body.ntfy;
  if (!form.mqtt_enabled.disabled) body.mqtt = { enabled: form.mqtt_enabled.checked };
  if (body.ntfy?.attach_snapshot && !inst.view.notifications.ntfy.attach_snapshot && !confirm("Snapshots will be sent to the push service, outside your property. Continue?")) return;
  try {
    await api("/config/notifications", { method: "PUT", body: JSON.stringify(body) });
    toast("Notification settings saved", "", "info");
    form.topic.value = "";
    loadInstall();
  } catch (error) {
    toast("Not saved", error.message);
  }
}

async function sendTestNotification() {
  const button = $("#alerts-test");
  button.disabled = true;
  button.textContent = "Sending…";
  try {
    const result = await api("/installation/test-notification", { method: "POST", body: "{}" });
    const failed = Object.entries(result.outcomes).filter(([, v]) => !String(v).startsWith("sent"));
    toast(failed.length ? "Test notification failed" : "Test notification sent", Object.entries(result.outcomes).map(([k, v]) => `${k}: ${v}`).join(", "), failed.length ? "high" : "info");
  } catch (error) {
    toast("Test failed", error.message);
  }
  await loadInstall();
}

async function completeInstallation() {
  const ack = $("#finish-ack");
  if (ack && !ack.checked) return toast("Please confirm", "Tick the box to acknowledge the open issues.");
  try {
    inst.view = await api("/installation/complete", { method: "POST", body: JSON.stringify({ acknowledge_issues: true }) });
    toast("Installation completed", inst.view.readiness.statement.text, "info");
    renderInstall();
  } catch (error) {
    toast("Not completed", error.message);
  }
}

// ---- planner --------------------------------------------------------------------------------

function mapDims() {
  return [Number(inst.view.site.width_m) || 40, Number(inst.view.site.height_m) || 30];
}

function newId(kind) {
  return `${kind}_${Math.random().toString(36).slice(2, 8)}`;
}

function placeRecommended() {
  const r = inst.view.state.recommendation;
  if (!r) return;
  const [w, h] = mapDims();
  let n = 0;
  const perRow = Math.max(1, Math.min(r.total, 6));
  for (const line of r.lines) {
    for (let i = 0; i < line.count; i++, n++) {
      // Parked in a row along the top edge, ready to be dragged to their mounting points.
      const x = Math.round(((n % perRow) + 0.5) * (w / perRow) * 10) / 10;
      const y = Math.min(h - 1, 1.5 + Math.floor(n / perRow) * Math.max(3, h / 8));
      inst.plan.items.push({ id: newId("camera"), kind: "camera", name: `${line.role.replace(/s$/, "")} ${i + 1}`, point: [x, y],
        heading_deg: 180, fov_deg: { wide: 105, standard: 90, narrow: 60 }[line.lens] || 90, mount: line.mount, power: r.poe_ports ? "poe" : "wifi", status: "proposed" });
    }
  }
  inst.planDirty = true;
  inst.tool = "select";
  toast("Cameras added to the plan", "Drag each one to its mounting point and aim it, then check coverage.", "info");
  renderInstall();
}

function svgCoords(event, svg) {
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const local = point.matrixTransform(svg.getScreenCTM().inverse());
  return [Math.round(local.x * 10) / 10, Math.round(local.y * 10) / 10];
}

function rangeOf(item) {
  if (item.range_m) return item.range_m;
  const fov = Math.max(10, Math.min(item.fov_deg || 90, 170));
  const vertical = 2 * Math.atan(Math.tan((fov * Math.PI) / 360) * (9 / 16));
  return Math.max(3, Math.min(1.7 / (0.1 * 2 * Math.tan(vertical / 2)), 60));
}

function renderPlanSvg() {
  const svg = $("#plan-svg");
  if (!svg) return;
  const [w, h] = mapDims();
  svg.setAttribute("viewBox", `-1 -1 ${w + 2} ${h + 2}`);
  const parts = [`<rect class="plan-bg" x="0" y="0" width="${w}" height="${h}"/>`];
  if (inst.view.site.has_image) parts.push(`<image href="${API}/config/map/image" x="0" y="0" width="${w}" height="${h}" preserveAspectRatio="none" opacity="0.6"/>`);
  const cov = inst.coverage;
  if (cov && inst.showCoverage) {
    cov.grid.forEach((row, r) => row.forEach((value, c) => {
      if (value > 0) parts.push(`<rect class="cov cov-${Math.min(value, 3)}" x="${c * cov.cell_m}" y="${r * cov.cell_m}" width="${cov.cell_m}" height="${cov.cell_m}"/>`);
    }));
  }
  const pts = (list) => list.map((p) => p.join(",")).join(" ");
  // Cameras already configured with a map position: shown for context, edited on Setup → Property map.
  for (const camera of (cov?.cameras || []).filter((c) => c.source === "config")) {
    const [x, y] = camera.point;
    parts.push(`<circle class="pt pt-installed" cx="${x}" cy="${y}" r="0.6"><title>${esc(camera.name)} (installed; edit on Setup → Property map)</title></circle>`);
    parts.push(`<text class="pt-label muted-label" x="${x + 0.9}" y="${y - 0.8}">${esc(camera.name)}</text>`);
  }
  for (const item of inst.plan.items) {
    const selected = inst.selected === item.id ? " selected" : "";
    if (item.polygon) parts.push(`<polygon class="pl pl-${esc(item.kind)}${selected}" data-item="${esc(item.id)}" points="${pts(item.polygon)}"><title>${esc(item.name)}</title></polygon>`);
    else if (item.line) parts.push(`<polyline class="pl pl-${esc(item.kind)}${selected}" data-item="${esc(item.id)}" points="${pts(item.line)}"><title>${esc(item.name)}</title></polyline>`);
  }
  for (const item of inst.plan.items.filter((i) => i.point)) {
    const [x, y] = item.point;
    const selected = inst.selected === item.id ? " selected" : "";
    if (item.kind === "camera") {
      const reach = rangeOf(item);
      const fov = Math.min(item.fov_deg || 90, 359.9);
      const rad = (deg) => ((deg - 90) * Math.PI) / 180;
      const a = rad((item.heading_deg || 0) - fov / 2);
      const b = rad((item.heading_deg || 0) + fov / 2);
      parts.push(`<path class="pl-fov${item.status === "installed" ? " installed" : ""}" d="M${x},${y} L${x + reach * Math.cos(a)},${y + reach * Math.sin(a)} A${reach},${reach} 0 ${fov > 180 ? 1 : 0} 1 ${x + reach * Math.cos(b)},${y + reach * Math.sin(b)} Z"/>`);
      const hx = x + 2.2 * Math.cos(rad(item.heading_deg || 0));
      const hy = y + 2.2 * Math.sin(rad(item.heading_deg || 0));
      if (selected) parts.push(`<line class="aim-line" x1="${x}" y1="${y}" x2="${hx}" y2="${hy}"/><circle class="aim-handle" data-aim="${esc(item.id)}" cx="${hx}" cy="${hy}" r="0.55"><title>Drag to aim</title></circle>`);
    }
    parts.push(`<circle class="pt pt-${esc(item.kind)}${selected}" data-item="${esc(item.id)}" cx="${x}" cy="${y}" r="0.75"><title>${esc(item.name)}</title></circle>`);
    parts.push(`<text class="pt-label" x="${x + 1}" y="${y - 0.9}">${esc(item.name)}</text>`);
  }
  const chosen = inst.plan.items.find((i) => i.id === inst.selected);
  if (chosen && (chosen.polygon || chosen.line)) (chosen.polygon || chosen.line).forEach((p, index) => parts.push(`<circle class="vertex" data-vertex="${index}" cx="${p[0]}" cy="${p[1]}" r="0.45"/>`));
  if (inst.drafting) {
    const list = inst.drafting.points;
    if (list.length) parts.push(`<polyline class="drafting" points="${pts(list)}"/>`);
    list.forEach((p, index) => parts.push(`<circle class="vertex${index === 0 ? " first" : ""}" cx="${p[0]}" cy="${p[1]}" r="0.45"/>`));
  }
  svg.innerHTML = parts.join("");
}

function renderPlanProps() {
  const node = $("#plan-props");
  if (!node) return;
  const item = inst.plan.items.find((i) => i.id === inst.selected);
  if (!item) { node.innerHTML = `<p class="small muted">${inst.plan.items.length} item(s) on the plan.</p>`; return; }
  const sel = (name, options, value) => `<label class="field">${esc(name[1])} <select data-prop="${name[0]}">${options.map(([v, l]) => `<option value="${v}" ${String(value) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>`;
  const num = (key, label, value, extra = "") => `<label class="field">${esc(label)} <input type="number" data-prop="${key}" value="${value ?? ""}" ${extra}></label>`;
  let fields = "";
  if (item.kind === "camera") {
    fields = `<div class="grid2">${num("heading_deg", "Aim (°, 0 = up)", Math.round(item.heading_deg || 0), 'min="0" max="359"')}${num("fov_deg", "Field of view (°)", item.fov_deg || 90, 'min="10" max="360"')}
      ${num("range_m", "Detection range (m)", item.range_m ?? "", `min="1" max="200" placeholder="about ${Math.round(rangeOf({ fov_deg: item.fov_deg }))} (estimate)"`)}
      ${num("power_w", "Max power (W, from datasheet)", item.power_w ?? "", 'min="0.5" max="100" step="0.1"')}</div>
      ${sel(["mount", "Mount"], [["outdoor", "Outdoor"], ["indoor", "Indoor"]], item.mount || "outdoor")}
      ${sel(["power", "Power"], [["poe", "PoE (network cable)"], ["wifi", "Wi-Fi + power adapter"], ["dc", "Wired + power adapter"], ["battery", "Battery"]], item.power || "poe")}
      ${sel(["status", "Status"], [["proposed", "Proposed"], ["installed", "Installed"]], item.status || "proposed")}
      <p class="small muted">Blank range uses an estimate: where a standing person fills about 10% of the picture height. It is not your camera's specification.</p>`;
  } else if (item.kind === "sensor") {
    fields = sel(["sensor", "Sensor"], [["contact", "Door/window contact"], ["motion", "Motion"], ["environmental", "Environmental"]], item.sensor || "contact")
      + sel(["power", "Power"], [["battery", "Battery"], ["wired", "Wired"]], item.power || "battery");
  } else if (item.kind === "network") {
    fields = sel(["equipment", "Equipment"], [["router", "Router"], ["switch", "PoE switch"], ["recorder", "Recorder (NVR)"], ["hub", "Sentinel computer / hub"], ["access_point", "Wi-Fi access point"], ["ups", "UPS"]], item.equipment || "switch")
      + num("poe_ports", "PoE ports", item.poe_ports ?? "", 'min="0" max="96"');
  }
  node.innerHTML = `<div class="card inner"><div class="row-between"><strong>${esc(PLAN_KIND_LABELS[item.kind])}</strong><button type="button" class="danger" id="plan-delete">Delete</button></div>
    <label class="field">Name <input type="text" data-prop="name" maxlength="60" value="${esc(item.name)}"></label>${fields}</div>`;
  node.querySelectorAll("[data-prop]").forEach((input) => input.addEventListener("change", () => {
    const key = input.dataset.prop;
    const numeric = ["heading_deg", "fov_deg", "range_m", "power_w", "poe_ports"].includes(key);
    if (numeric && input.value === "") delete item[key];
    else item[key] = numeric ? Number(input.value) : input.value;
    planChanged();
    renderPlanSvg();
  }));
  $("#plan-delete").addEventListener("click", deleteSelectedItem);
}

function renderPlanCoverage() {
  const node = $("#plan-coverage");
  if (!node) return;
  const cov = inst.coverage;
  if (!cov) { node.innerHTML = '<p class="small muted">Check coverage to see roughly what the cameras on the plan would see.</p>'; return; }
  const est = cov.estimate || {};
  node.innerHTML = `<div class="card inner"><strong>${esc(cov.summary)}</strong>
    <ul class="small">${cov.features.map((f) => `<li>${esc(f.name)}: ${f.covered_by ? (f.covered_by.length ? `seen by ${esc(f.covered_by.join(", "))}` : '<span class="status-bad">not in view</span>') : `about ${f.covered_pct}% in view`}</li>`).join("")}</ul>
    ${cov.privacy.length ? `<p class="small"><strong>Privacy:</strong></p><ul class="small">${cov.privacy.map((p) => `<li>${esc(p.advice)}</li>`).join("")}</ul>` : ""}
    ${est.poe ? `<p class="small"><strong>Equipment:</strong> ${est.poe.ports_needed} PoE port(s)${est.poe.ports_needed ? ` (a ${est.poe.suggested_switch_ports}-port switch leaves a spare), budget ${est.poe.budget_w} W` : ""}; ${est.cable_runs.length ? `about ${est.cable_total_m} m of cable` : "cable length unknown (no switch on the plan)"}; ${est.outlets} power outlet(s).</p>` : ""}
    ${(est.warnings || []).length ? `<ul class="small status-warn">${est.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    <details class="small"><summary>Assumptions</summary><ul>${cov.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></details></div>`;
}

function planChanged() {
  inst.planDirty = true;
  const status = $("#plan-status");
  if (status) status.textContent = "Unsaved changes";
}

function deleteSelectedItem() {
  inst.plan.items = inst.plan.items.filter((i) => i.id !== inst.selected);
  inst.selected = null;
  planChanged();
  renderPlanSvg();
  renderPlanProps();
}

function finishPlanDrafting() {
  const draft = inst.drafting;
  if (!draft) return;
  // A double-click also lands as two clicks: drop corners that repeat the previous one.
  draft.points = draft.points.filter((p, i, all) => i === 0 || Math.hypot(p[0] - all[i - 1][0], p[1] - all[i - 1][1]) > 0.3);
  const minimum = LINE.has(draft.kind) ? 2 : 3;
  if (draft.points.length >= minimum) {
    const item = { id: newId(draft.kind), kind: draft.kind, name: PLAN_KIND_LABELS[draft.kind], [LINE.has(draft.kind) ? "line" : "polygon"]: draft.points };
    inst.plan.items.push(item);
    inst.selected = item.id;
    planChanged();
  }
  inst.drafting = null;
  inst.tool = "select";
  refreshPlanner();
}

function refreshPlanner() {
  document.querySelectorAll("[data-tool]").forEach((b) => b.classList.toggle("active", b.dataset.tool === inst.tool));
  $("#plan-help").innerHTML = planHelp();
  $("#plan-finish")?.addEventListener("click", finishPlanDrafting);
  renderPlanSvg();
  renderPlanProps();
}

async function checkCoverage(save) {
  try {
    if (save) {
      const saved = await api("/installation/plan", { method: "PUT", body: JSON.stringify(inst.plan) });
      inst.plan = { items: saved.items, updated_ms: saved.updated_ms };
      inst.planDirty = false;
      inst.coverage = { ...saved.coverage, estimate: (await api("/installation/plan/coverage", { method: "POST", body: JSON.stringify(inst.plan) })).estimate };
      toast("Plan saved", inst.coverage.summary, "info");
      await loadInstall();
      return;
    }
    inst.coverage = await api("/installation/plan/coverage", { method: "POST", body: JSON.stringify(inst.plan) });
    renderPlanSvg();
    renderPlanCoverage();
  } catch (error) {
    toast(save ? "Plan not saved" : "Coverage not checked", error.message);
  }
}

function bindPlanner() {
  const svg = $("#plan-svg");
  document.querySelectorAll("[data-tool]").forEach((button) => button.addEventListener("click", () => {
    inst.tool = button.dataset.tool;
    inst.drafting = AREA.has(inst.tool) || LINE.has(inst.tool) ? { kind: inst.tool, points: [] } : null;
    refreshPlanner();
  }));
  $("#plan-save").addEventListener("click", () => checkCoverage(true));
  $("#plan-check").addEventListener("click", () => checkCoverage(false));
  $("#plan-show-coverage").addEventListener("change", (e) => { inst.showCoverage = e.target.checked; renderPlanSvg(); });
  $("#plan-finish")?.addEventListener("click", finishPlanDrafting);
  svg.addEventListener("pointerdown", (event) => {
    const [x, y] = svgCoords(event, svg);
    if (inst.drafting) {
      const list = inst.drafting.points;
      if (list.length >= 3 && AREA.has(inst.drafting.kind) && Math.hypot(list[0][0] - x, list[0][1] - y) < 1) return finishPlanDrafting();
      list.push([x, y]);
      return renderPlanSvg();
    }
    if (inst.tool !== "select") {
      const item = { id: newId(inst.tool), kind: inst.tool, name: PLAN_KIND_LABELS[inst.tool], point: [x, y] };
      if (item.kind === "camera") Object.assign(item, { heading_deg: 180, fov_deg: 90, mount: "outdoor", power: "poe", status: "proposed" });
      if (item.kind === "sensor") Object.assign(item, { sensor: "contact", power: "battery" });
      if (item.kind === "network") item.equipment = "switch";
      inst.plan.items.push(item);
      inst.selected = item.id;
      inst.tool = "select";
      planChanged();
      return refreshPlanner();
    }
    const target = event.target;
    if (target.dataset.aim) {
      inst.drag = { kind: "aim", id: target.dataset.aim };
    } else if (target.dataset.vertex !== undefined && inst.selected) {
      inst.drag = { kind: "vertex", id: inst.selected, index: Number(target.dataset.vertex) };
    } else if (target.dataset.item) {
      inst.selected = target.dataset.item;
      inst.drag = { kind: "move", id: target.dataset.item, from: [x, y], moved: false };
      renderPlanProps();
    } else {
      inst.selected = null;
      renderPlanProps();
    }
    renderPlanSvg();
    if (inst.drag) svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", (event) => {
    if (!inst.drag) return;
    const [x, y] = svgCoords(event, svg);
    const item = inst.plan.items.find((i) => i.id === inst.drag.id);
    if (!item) return;
    if (inst.drag.kind === "aim") {
      item.heading_deg = Math.round(((Math.atan2(x - item.point[0], -(y - item.point[1])) * 180) / Math.PI + 360) % 360);
    } else if (inst.drag.kind === "vertex") {
      (item.polygon || item.line)[inst.drag.index] = [x, y];
    } else {
      const [dx, dy] = [x - inst.drag.from[0], y - inst.drag.from[1]];
      if (!dx && !dy) return;
      inst.drag.from = [x, y];
      const shift = (p) => [Math.round((p[0] + dx) * 10) / 10, Math.round((p[1] + dy) * 10) / 10];
      if (item.point) item.point = shift(item.point);
      if (item.polygon) item.polygon = item.polygon.map(shift);
      if (item.line) item.line = item.line.map(shift);
    }
    inst.drag.moved = true;
    planChanged();
    renderPlanSvg();
  });
  const endDrag = () => {
    if (inst.drag?.moved) renderPlanProps();
    inst.drag = null;
  };
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  svg.addEventListener("dblclick", () => inst.drafting && finishPlanDrafting());
  svg.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && inst.drafting) finishPlanDrafting();
    if (event.key === "Escape" && inst.drafting) { inst.drafting = null; inst.tool = "select"; refreshPlanner(); }
    if ((event.key === "Delete" || event.key === "Backspace") && inst.selected && !inst.drafting) deleteSelectedItem();
  });
  if (!inst.coverage && inst.plan.items.length) checkCoverage(false);
  renderPlanSvg();
  renderPlanProps();
  renderPlanCoverage();
}

// Device states change by themselves (cameras connect, drones charge); keep the
// live parts current without disturbing a form the person is filling in.
setInterval(() => {
  if (!document.querySelector("#tab-install.active") || !inst.view) return;
  if (!["hardware", "verify", "health", "finish"].includes(inst.step) || inst.form) return;
  if (document.activeElement && document.activeElement.closest("#install-step") && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  loadInstall();
}, 5000);

async function bootInstall() {
  try {
    const view = await api("/installation");
    installBadge(view);
  } catch {
    /* the Install tab loads on demand */
  }
}

bootInstall();
