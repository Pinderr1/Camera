"use strict";
// Network and camera troubleshooter (Setup → Cameras). Four steps: this
// computer's network, user-approved discovery, diagnosis of one address with
// evidence and fix steps, then login/stream tests and recorder channels.
// Uses $, esc, api, toast (app.js) and setup, openWizard, wizard, renderWizard,
// testResultHtml, loadSetup (setup.js).

const STATUS_LABELS = { confirmed: "Confirmed problem", suspected: "Possible cause", info: "Note", ruled_out: "Ruled out", ok: "OK" };
const KIND_LABELS = { ethernet: "Ethernet", wifi: "Wi-Fi", vpn: "VPN", virtual: "Virtual" };
const CHECK_WORDS = { reply: "replied", timeout: "no answer", open: "open", refused: "refused (device there, port closed)", no_route: "no route",
  host_unreachable: "host unreachable", net_unreachable: "network unreachable", failure: "failed", unavailable: "not available", error: "error" };

const ts = {
  step: "network",
  network: null,
  networkFindings: [],
  brands: [],
  loadingNetwork: false,
  showAllAdapters: false,
  discovery: null,
  discoverOptions: { sadp: true, onvif: true, port_check: false, confirm: false },
  target: { host: "", brand: "", device_mask: "", device_gateway: "", rtsp_port: 554, http_port: 80, discover: true },
  report: null,
  previous: null,
  testedAt: null,
  busy: "",
  login: { host: "", template: "hikvision", channel: 1, port: 554, username: "admin", password: "" },
  streamResult: null,
  channels: null,
};

$("#open-troubleshooter").addEventListener("click", () => openTroubleshooter({}));
$("#ts-close").addEventListener("click", () => $("#troubleshooter").close());
$("#troubleshooter").addEventListener("close", () => { ts.login.password = ""; });
document.querySelectorAll("[data-ts-step]").forEach((button) => button.addEventListener("click", () => tsGo(button.dataset.tsStep)));

function openTroubleshooter({ host, template, port, autorun }) {
  if (host) {
    ts.target.host = host;
    const brand = ts.brands.find((b) => b.id === template) || ts.brands.find((b) => b.template === template);
    ts.target.brand = brand ? brand.id : template && template !== "custom" ? template : "";
    if (port && port !== 554) ts.target.rtsp_port = Number(port);
    ts.report = null;
  }
  if (!$("#troubleshooter").open) $("#troubleshooter").showModal();
  if (!ts.network && !ts.loadingNetwork) loadNetwork(false);
  tsGo(host ? "check" : ts.step);
  if (host && autorun) runDiagnosis();
}

function tsGo(step) {
  collectTs();
  ts.step = step;
  renderTs();
}

function brandTemplate(brandId) {
  return ts.brands.find((b) => b.id === brandId)?.template || "custom";
}

// ---- rendering --------------------------------------------------------------------

function renderTs() {
  const order = ["network", "find", "check", "streams"];
  document.querySelectorAll("#ts-steps li").forEach((li) => {
    li.classList.toggle("current", li.dataset.step === ts.step);
    li.classList.toggle("done", order.indexOf(li.dataset.step) < order.indexOf(ts.step));
  });
  const body = $("#ts-body");
  body.innerHTML = { network: renderNetworkStep, find: renderFindStep, check: renderCheckStep, streams: renderStreamsStep }[ts.step]();
  bindTs();
}

function findingHtml(f) {
  const host = f.data && f.data.host;
  return `<article class="finding s-${esc(f.status)}" data-code="${esc(f.code)}">
    <div><span class="tag">${esc(STATUS_LABELS[f.status] || f.status)}</span><strong>${esc(f.title)}</strong></div>
    <p class="small">${esc(f.explanation)}</p>
    ${f.evidence.length ? `<details ${["confirmed", "suspected", "ruled_out"].includes(f.status) ? "open" : ""}><summary>Evidence</summary><ul>${f.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>` : ""}
    ${f.steps.length ? `<div class="small"><strong>What to do</strong><ol>${f.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></div>` : ""}
    ${host ? `<div class="button-row tight"><button type="button" class="primary" data-check-host="${esc(host)}">Check ${esc(host)}</button></div>` : ""}
  </article>`;
}

function renderNetworkStep() {
  if (!ts.network) return `<p>Reading this computer's network settings… The first check can take up to 15 seconds.</p>`;
  const adapters = ts.network.adapters.filter((a) => ts.showAllAdapters || (a.up && a.kind !== "virtual"));
  return `<p class="small muted">What Sentinel sees on this computer. Nothing is changed.</p>
    <div class="scroll"><table class="table" id="ts-adapters"><tr><th>Adapter</th><th>Type</th><th>Status</th><th>Address</th><th>Router</th><th>Address from</th><th>Profile</th></tr>
    ${adapters.map((a) => `<tr><td>${esc(a.name)}</td><td>${esc(KIND_LABELS[a.kind] || a.kind)}</td><td>${a.up ? `<span class="pill ok">connected</span>` : `<span class="pill">disconnected</span>`}</td>
      <td>${esc(a.ipv4.join(", ") || "–")}</td><td>${esc(a.gateway || "–")}</td><td>${a.dhcp === true ? "router (DHCP)" : a.dhcp === false ? "fixed" : "–"}</td><td>${esc(a.profile || "–")}</td></tr>`).join("")
      || `<tr><td colspan="7" class="muted">No connected adapters.</td></tr>`}</table></div>
    <label class="small"><input type="checkbox" id="ts-all-adapters" ${ts.showAllAdapters ? "checked" : ""}> Show disconnected and virtual adapters</label>
    <div id="ts-network-findings">${ts.networkFindings.map(findingHtml).join("")}</div>
    <div class="button-row"><button type="button" id="ts-refresh" ${ts.loadingNetwork ? "disabled" : ""}>${ts.loadingNetwork ? "Checking…" : "Check again"}</button>
      <button type="button" class="primary" data-ts-next="find">Next: find devices</button></div>`;
}

function renderFindStep() {
  const o = ts.discoverOptions;
  const d = ts.discovery;
  const option = (key, label, help) => `<label class="field checkbox"><input type="checkbox" data-discover="${key}" ${o[key] ? "checked" : ""}> <span>${esc(label)}<br><span class="small muted">${esc(help)}</span></span></label>`;
  return `<p class="small muted">Searches stay on this computer's own network. Nothing is searched until you ask.</p>
    ${option("sadp", "Hikvision devices (SADP)", "Also finds Hikvision recorders whose address belongs to a different network.")}
    ${option("onvif", "ONVIF cameras and recorders", "Most brands. Only answers from devices with a working address on this network.")}
    ${option("port_check", "Check common camera ports on every address of this network", "Up to 254 addresses, ports 80, 554, 8000, 37777 and 9000. Takes about 10 seconds.")}
    <label class="field checkbox"><input type="checkbox" data-discover="confirm" ${o.confirm ? "checked" : ""}> <strong>I own or manage this network and want Sentinel to search it.</strong></label>
    <div class="button-row"><button type="button" class="primary" id="ts-search" ${o.confirm && !ts.busy ? "" : "disabled"}>${ts.busy === "search" ? "Searching…" : "Search"}</button>
      <button type="button" data-ts-next="check">Enter an address instead</button></div>
    ${d ? `<div id="ts-devices">${d.devices.length ? `<div class="scroll"><table class="table"><tr><th>Address</th><th>Device</th><th>Found by</th><th>Network</th><th></th></tr>${d.devices.map((dev) => `<tr>
        <td>${esc(dev.host)}</td><td>${esc([dev.brand ? ts.brands.find((b) => b.id === dev.brand)?.label || dev.brand : "", dev.model, dev.name].filter(Boolean).join(" · ") || "unknown")}${dev.activated === false ? ` <span class="pill bad">not activated</span>` : ""}</td>
        <td>${esc((dev.found_by || [dev.method]).map((m) => ({ sadp: "SADP", onvif: "ONVIF", ports: "open ports " + (dev.ports || []).join("/") })[m] || m).join(", "))}</td>
        <td>${dev.on_local_network ? `<span class="pill ok">on your network</span>` : `<span class="pill bad">different network${dev.network ? " " + esc(dev.network) : ""}</span>`}</td>
        <td><div class="button-row tight"><button type="button" data-check-host="${esc(dev.host)}" data-brand="${esc(dev.brand || "")}" data-mask="${esc(dev.mask || "")}" data-gateway="${esc(dev.gateway || "")}">Check</button>
          ${dev.on_local_network ? `<button type="button" data-add-host="${esc(dev.host)}" data-brand="${esc(dev.brand || "")}" data-channels="${esc(dev.channels || "")}">Add camera</button>` : ""}</div></td></tr>`).join("")}</table></div>`
        : `<p>No device answered.</p>`}${(d.notes || []).map(findingHtml).join("")}</div>` : ""}`;
}

function renderCheckStep() {
  const t = ts.target;
  // The brand list arrives with the network overview; until then keep a preselected brand selectable.
  const brands = ts.brands.length || !t.brand ? ts.brands : [{ id: t.brand, label: t.brand }];
  const brandOptions = [`<option value="">Not sure</option>`, ...brands.filter((b) => b.id !== "other").map((b) => `<option value="${esc(b.id)}" ${b.id === t.brand ? "selected" : ""}>${esc(b.label)}</option>`)].join("");
  return `<div class="ts-form">
      <label class="field">Recorder or camera IP address <input type="text" name="ts_host" value="${esc(t.host)}" placeholder="e.g. 192.168.1.84" autocomplete="off"></label>
      <label class="field">Brand <select name="ts_brand">${brandOptions}</select></label></div>
    <details ${t.device_mask || t.device_gateway ? "open" : ""}><summary class="small">What the recorder's own screen shows (optional, helps the diagnosis)</summary>
      <div class="ts-form"><label class="field">Subnet mask <input type="text" name="ts_mask" value="${esc(t.device_mask)}" placeholder="255.255.255.0"></label>
      <label class="field">Gateway <input type="text" name="ts_gateway" value="${esc(t.device_gateway)}" placeholder="192.168.1.1"></label></div></details>
    <details><summary class="small">More options</summary>
      <div class="ts-form"><label class="field">RTSP port <input type="number" name="ts_rtsp_port" value="${esc(t.rtsp_port)}" min="1" max="65535"></label>
      <label class="field">Web port <input type="number" name="ts_http_port" value="${esc(t.http_port)}" min="1" max="65535"></label></div>
      <label class="field checkbox"><input type="checkbox" name="ts_discover" ${t.discover ? "checked" : ""}> Also look for the device with discovery on this network (SADP and ONVIF)</label></details>
    <div class="button-row"><button type="button" class="primary" id="ts-diagnose" ${ts.busy ? "disabled" : ""}>${ts.busy === "diagnose" ? "Checking…" : ts.report ? "Retest" : "Diagnose"}</button></div>
    ${ts.report ? reportHtml(ts.report) : ""}`;
}

function reportHtml(r) {
  const changed = ts.previous && ts.previous.host === r.host && ts.previous.reachable !== r.reachable
    ? `<div class="small"><strong>${r.reachable ? "Fixed since the last test: the device now answers." : "Changed since the last test: the device no longer answers."}</strong></div>` : "";
  return `<div class="banner ${r.reachable ? "good" : "bad"}" id="ts-summary"><strong>${esc(r.summary)}</strong>
      <div class="small muted">Tested ${esc(r.host)} at ${esc(ts.testedAt)}${r.route ? ` · leaves through ${esc(r.route.adapter)}${r.route.via ? " via router " + esc(r.route.via) : " directly"}` : ""}</div>${changed}</div>
    <div id="ts-findings">${r.findings.map(findingHtml).join("")}</div>
    <details><summary class="small">All checks</summary><table class="table">${r.checks.map((c) => `<tr><td>${esc(c.label)}</td><td>${esc(CHECK_WORDS[c.result] || c.result)}</td></tr>`).join("")}</table></details>
    ${r.reachable ? `<div class="button-row"><button type="button" class="primary" data-ts-next="streams">Next: test login and streams</button></div>` : ""}`;
}

function renderStreamsStep() {
  const l = ts.login;
  const templates = Object.entries(setup.config?.catalog?.templates || {}).filter(([key]) => key !== "custom");
  return `<p class="small muted">Checks the login and each stream. The password goes to this Sentinel computer only, and is stored encrypted if you save the camera.</p>
    <div class="ts-form">
      <label class="field">IP address <input type="text" name="ts_l_host" value="${esc(l.host)}" autocomplete="off"></label>
      <label class="field">RTSP port <input type="number" name="ts_l_port" value="${esc(l.port)}" min="1" max="65535"></label>
      <label class="field">Brand (stream paths) <select name="ts_l_template">${templates.map(([key, label]) => `<option value="${esc(key)}" ${key === l.template ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>
      <label class="field">Channel <input type="number" name="ts_l_channel" value="${esc(l.channel)}" min="1" max="64"></label>
      <label class="field">Username <input type="text" name="ts_l_username" value="${esc(l.username)}" autocomplete="off"></label>
      <label class="field">Password <input type="password" name="ts_l_password" autocomplete="new-password"></label></div>
    <div class="button-row"><button type="button" class="primary" id="ts-test-streams" ${ts.busy ? "disabled" : ""}>${ts.busy === "streams" ? "Testing… (up to 20 s)" : "Test login and streams"}</button>
      <button type="button" id="ts-find-channels" ${ts.busy ? "disabled" : ""}>${ts.busy === "channels" ? "Searching channels…" : "Find recorder channels"}</button></div>
    <p class="small muted">Wrong passwords are tried once, never repeated: recorders lock the account after several failures.</p>
    ${ts.streamResult ? `<div id="ts-stream-result">${testResultHtml(ts.streamResult)}
      ${ts.streamResult.ok ? `<div class="button-row"><input type="text" id="ts-camera-name" placeholder="Camera name" maxlength="60" value="Channel ${esc(l.channel)}"><button type="button" class="primary" id="ts-save-camera">Save camera</button></div>` : ""}</div>` : ""}
    ${ts.channels ? channelsHtml(ts.channels) : ""}`;
}

function channelsHtml(result) {
  const found = result.channels.filter((c) => c.draft_id);
  const cell = (s) => (s ? `${esc(s.kind === "ok" ? "ok" : s.kind)}${s.codec ? " · " + esc(s.codec) : ""}` : "–");
  return `<div id="ts-channels"><p><strong>${esc(result.message)}</strong></p>
    ${result.channels.length ? `<div class="scroll"><table class="table"><tr><th></th><th>Channel</th><th>Main stream</th><th>Sub-stream</th><th>Name</th></tr>${result.channels.map((c) => `<tr>
      <td>${c.draft_id ? `<input type="checkbox" data-channel-pick="${c.channel}" checked aria-label="Add channel ${c.channel}">` : ""}</td><td>${c.channel}</td><td>${cell(c.main)}</td><td>${cell(c.sub)}</td>
      <td>${c.draft_id ? `<input type="text" data-channel-name="${c.channel}" value="Camera ${c.channel}" maxlength="60">` : ""}</td></tr>`).join("")}</table></div>` : ""}
    ${found.length ? `<p class="small muted">A recorder may answer for a channel with no camera connected; check each camera's picture after adding.</p>
      <div class="button-row"><button type="button" class="primary" id="ts-add-channels">Add selected cameras</button></div>` : ""}</div>`;
}

// ---- actions ------------------------------------------------------------------------

function collectTs() {
  const value = (name) => document.querySelector(`#ts-body [name="${name}"]`);
  if (value("ts_host")) {
    Object.assign(ts.target, {
      host: value("ts_host").value.trim(), brand: value("ts_brand").value, device_mask: value("ts_mask").value.trim(),
      device_gateway: value("ts_gateway").value.trim(), rtsp_port: Number(value("ts_rtsp_port").value) || 554,
      http_port: Number(value("ts_http_port").value) || 80, discover: value("ts_discover").checked,
    });
  }
  if (value("ts_l_host")) {
    Object.assign(ts.login, {
      host: value("ts_l_host").value.trim(), port: Number(value("ts_l_port").value) || 554, template: value("ts_l_template").value,
      channel: Number(value("ts_l_channel").value) || 1, username: value("ts_l_username").value.trim(), password: value("ts_l_password").value,
    });
  }
}

function bindTs() {
  const on = (selector, handler) => { const node = $(selector); if (node) node.addEventListener("click", handler); };
  const password = document.querySelector('#ts-body [name="ts_l_password"]');
  if (password) password.value = ts.login.password; // a property, never written into the page markup
  on("#ts-refresh", () => loadNetwork(true));
  on("#ts-search", runDiscovery);
  on("#ts-diagnose", runDiagnosis);
  on("#ts-test-streams", runStreamTest);
  on("#ts-find-channels", runChannelScan);
  on("#ts-save-camera", saveTestedCamera);
  on("#ts-add-channels", addChannels);
  const all = $("#ts-all-adapters");
  if (all) all.addEventListener("change", () => { ts.showAllAdapters = all.checked; renderTs(); });
  document.querySelectorAll("[data-ts-next]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.tsNext === "streams") prepareLogin();
    tsGo(b.dataset.tsNext);
  }));
  document.querySelectorAll("[data-discover]").forEach((input) => input.addEventListener("change", () => {
    ts.discoverOptions[input.dataset.discover] = input.checked;
    const search = $("#ts-search");
    if (search) search.disabled = !ts.discoverOptions.confirm || Boolean(ts.busy);
  }));
  document.querySelectorAll("[data-check-host]").forEach((b) => b.addEventListener("click", () => {
    collectTs();
    ts.target.host = b.dataset.checkHost;
    if (b.dataset.brand !== undefined) Object.assign(ts.target, { brand: b.dataset.brand, device_mask: b.dataset.mask || "", device_gateway: b.dataset.gateway || "" });
    ts.step = "check";
    renderTs();
    runDiagnosis();
  }));
  document.querySelectorAll("[data-add-host]").forEach((b) => b.addEventListener("click", () => addFromDiscovery(b.dataset.addHost, b.dataset.brand, Number(b.dataset.channels) || 0)));
}

async function loadNetwork(refresh) {
  ts.loadingNetwork = true;
  if (ts.step === "network") renderTs();
  try {
    const result = await api(`/onboarding/network${refresh ? "?refresh=true" : ""}`);
    ts.network = result.network;
    ts.networkFindings = result.findings;
    ts.brands = result.brands;
  } catch (error) {
    ts.network = { adapters: [] };
    ts.networkFindings = [{ code: "error", status: "info", title: "Could not read the network settings", explanation: error.message, evidence: [], steps: [], data: {} }];
  }
  ts.loadingNetwork = false;
  if ($("#troubleshooter").open) {
    collectTs(); // keep anything typed while the overview loaded
    renderTs();
  }
}

async function busy(kind, work) {
  collectTs();
  ts.busy = kind;
  renderTs();
  try {
    await work();
  } catch (error) {
    toast("Troubleshooter", error.message);
  } finally {
    ts.busy = "";
    if ($("#troubleshooter").open) renderTs();
  }
}

function runDiscovery() {
  return busy("search", async () => {
    ts.discovery = await api("/onboarding/network/discover", { method: "POST", body: JSON.stringify(ts.discoverOptions) });
  });
}

function runDiagnosis() {
  return busy("diagnose", async () => {
    if (!ts.target.host) throw new Error("Enter the recorder's IP address first.");
    const t = ts.target;
    const body = { host: t.host, brand: t.brand || null, device_mask: t.device_mask || null, device_gateway: t.device_gateway || null,
      rtsp_port: t.rtsp_port, http_port: t.http_port, discover: t.discover };
    const report = await api("/onboarding/network/diagnose", { method: "POST", body: JSON.stringify(body) });
    ts.previous = ts.report;
    ts.report = report;
    ts.testedAt = new Date().toLocaleTimeString();
    const identified = report.identity?.brand;
    if (identified && !ts.target.brand) ts.target.brand = identified;
  });
}

function prepareLogin() {
  const t = ts.target;
  ts.login.host = ts.report?.host || t.host;
  ts.login.port = t.rtsp_port || 554;
  ts.login.template = brandTemplate(t.brand || ts.report?.identity?.brand) === "custom" ? ts.login.template : brandTemplate(t.brand || ts.report?.identity?.brand);
}

function loginSource() {
  const l = ts.login;
  if (!l.host) throw new Error("Enter the IP address.");
  const source = { type: "rtsp", host: l.host, port: l.port, template: l.template, channel: l.channel, username: l.username, prefer_substream: true, transport: "tcp", hw_decode: "auto" };
  if (l.password) source.password = l.password;
  return source;
}

function runStreamTest() {
  return busy("streams", async () => {
    ts.streamResult = null;
    ts.streamResult = await api("/onboarding/test", { method: "POST", body: JSON.stringify({ source: loginSource() }) });
  });
}

function runChannelScan() {
  return busy("channels", async () => {
    ts.channels = null;
    ts.channels = await api("/onboarding/channels", { method: "POST", body: JSON.stringify({ source: loginSource(), max_channels: 16 }) });
  });
}

async function saveTestedCamera() {
  const name = $("#ts-camera-name").value.trim() || `Channel ${ts.login.channel}`;
  try {
    await api("/config/cameras", { method: "POST", body: JSON.stringify({ name, fps: 5, enabled: true, draft_id: ts.streamResult.draft_id }) });
    toast("Camera added", `${name}. Draw its zones next.`, "medium");
    await loadSetup();
  } catch (error) {
    toast("Not saved", error.message);
  }
}

async function addChannels() {
  const picks = [...document.querySelectorAll("[data-channel-pick]:checked")].map((box) => Number(box.dataset.channelPick));
  if (!picks.length) return toast("Nothing selected", "Tick the channels to add.");
  const added = [];
  for (const channel of picks) {
    const entry = ts.channels.channels.find((c) => c.channel === channel);
    const name = document.querySelector(`[data-channel-name="${channel}"]`).value.trim() || `Camera ${channel}`;
    try {
      await api("/config/cameras", { method: "POST", body: JSON.stringify({ name, fps: 5, enabled: true, draft_id: entry.draft_id }) });
      added.push(name);
      entry.draft_id = null; // a draft is used once
    } catch (error) {
      toast(`Could not add ${name}`, error.message);
    }
  }
  if (added.length) toast(`${added.length} camera(s) added`, added.join(", "), "medium");
  await loadSetup();
  renderTs();
}

function addFromDiscovery(host, brand, channels) {
  $("#troubleshooter").close();
  openWizard(null);
  wizard.kind = channels > 1 || brand === "hikvision" ? "dvr" : "rtsp";
  Object.assign(wizard.values, { host, template: brandTemplate(brand) === "custom" ? wizard.values.template : brandTemplate(brand), port: 554 });
  wizard.step = 2;
  renderWizard();
}
