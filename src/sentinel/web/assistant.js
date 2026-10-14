"use strict";
// Sentinel AI Guide: the conversational front end. Uses $, esc, api, toast and
// state from app.js, and opens existing screens (install.js, setup.js,
// troubleshoot.js, intel.js) instead of duplicating them.
//
// Passwords never go to the assistant endpoints: the sign-in card posts them
// straight to the existing channel search, which keeps them server-side.

const guide = { status: null, conversationId: null, conversations: [], sending: false, navigated: false };
const GUIDE_KEY = "sentinel.guide.conversation";

document.querySelectorAll(".tabs button").forEach((button) => button.addEventListener("click", () => { guide.navigated = true; }));
document.querySelector('[data-tab="guide"]').addEventListener("click", () => loadGuide());
$("#guide-open").addEventListener("click", () => document.querySelector('[data-tab="guide"]').click());

// ---- text rendering (escaped first, then a few safe markdown forms) ----------------------

function guideText(text) {
  const blocks = String(text || "").split(/\n{2,}/);
  return blocks.map((block) => {
    const lines = block.split("\n");
    const inline = (line) => esc(line).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    if (lines.every((line) => /^\s*[-•] /.test(line))) return `<ul>${lines.map((line) => `<li>${inline(line.replace(/^\s*[-•] /, ""))}</li>`).join("")}</ul>`;
    if (lines.every((line) => /^\s*\d+\. /.test(line))) return `<ol>${lines.map((line) => `<li>${inline(line.replace(/^\s*\d+\. /, ""))}</li>`).join("")}</ol>`;
    const [first, ...rest] = lines;
    if (rest.length && rest.every((line) => /^\s*[-•] /.test(line))) {
      return `<p>${inline(first)}</p><ul>${rest.map((line) => `<li>${inline(line.replace(/^\s*[-•] /, ""))}</li>`).join("")}</ul>`;
    }
    return `<p>${lines.map(inline).join("<br>")}</p>`;
  }).join("");
}

// ---- loading ------------------------------------------------------------------------------

async function loadGuide() {
  try {
    guide.status = await api("/assistant");
    guide.conversations = await api("/assistant/conversations");
  } catch (error) {
    $("#guide-mode").textContent = `The guide is unavailable: ${error.message}`;
    return;
  }
  renderGuideStatus();
  let wanted = null;
  try { wanted = localStorage.getItem(GUIDE_KEY); } catch { /* private mode */ }
  const known = guide.conversations.find((c) => c.id === (guide.conversationId || wanted));
  if (known) await openConversation(known.id);
  else renderConversation({ id: null, messages: [] });
  renderConversationList();
}

function renderGuideStatus() {
  const s = guide.status;
  const mode = $("#guide-mode");
  mode.className = `guide-mode ${s.external ? "external" : "local"}`;
  mode.innerHTML = `<strong>${s.mode === "ai" ? `AI assistant (${esc(s.provider)})` : "Built-in guide (offline)"}</strong>
    <details><summary class="small">What is shared</summary><ul class="small">${s.privacy.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
    ${s.reason ? `<p class="small muted">${esc(s.reason)}</p>` : ""}</details>`;
  const form = $("#guide-settings");
  form.provider.value = s.provider || "none";
  form.share_network_details.checked = !!s.share_network_details;
  if (s.model) form.model.value = s.model;
}

function renderConversationList() {
  $("#guide-conversations").innerHTML = guide.conversations.map((c) => `<li class="${c.id === guide.conversationId ? "selected" : ""}">
      <button type="button" class="link" data-conversation="${esc(c.id)}">${esc(c.title || "New conversation")}</button>
      <span class="small muted">${dateTime(c.updated_ms)}</span></li>`).join("") || '<li class="muted small">No conversations yet.</li>';
  document.querySelectorAll("[data-conversation]").forEach((b) => b.addEventListener("click", () => openConversation(b.dataset.conversation)));
}

async function openConversation(id) {
  try {
    const conversation = await api(`/assistant/conversations/${encodeURIComponent(id)}`);
    guide.conversationId = conversation.id;
    try { localStorage.setItem(GUIDE_KEY, conversation.id); } catch { /* private mode */ }
    renderConversation(conversation);
    renderConversationList();
  } catch (error) {
    toast("Could not open the conversation", error.message);
  }
}

// ---- conversation ---------------------------------------------------------------------------

function renderConversation(conversation) {
  const list = $("#guide-messages");
  if (!conversation.messages.length) {
    const starters = (guide.status?.starters || []).map((s) => `<button type="button" class="chip" data-say="${esc(s)}">${esc(s)}</button>`).join("");
    list.innerHTML = `<div class="guide-welcome"><h2>${esc(guide.status?.welcome || "Welcome to Sentinel.")}</h2>
      <p class="muted">Tell me what you'd like to do, in your own words, or pick one:</p><div class="chips-row">${starters}</div></div>`;
  } else {
    list.innerHTML = conversation.messages.map(messageHtml).join("");
  }
  bindMessages(list);
  list.scrollTop = list.scrollHeight;
}

function appendMessages(messages) {
  const list = $("#guide-messages");
  if (list.querySelector(".guide-welcome")) list.innerHTML = "";
  list.insertAdjacentHTML("beforeend", messages.filter((m) => m.role !== "user" || !list.querySelector(`[data-message="${m.id}"]`)).map(messageHtml).join(""));
  bindMessages(list);
  list.scrollTop = list.scrollHeight;
}

function messageHtml(message) {
  if (message.role === "note") return `<div class="guide-note small" data-message="${esc(message.id)}">${guideText(message.text)}${actionsHtml(message.actions)}</div>`;
  const source = message.role === "assistant" ? `<span class="guide-source">${message.source === "ai" ? "AI" : "Sentinel guide"}</span>` : "";
  return `<div class="guide-msg ${esc(message.role)}" data-message="${esc(message.id)}">${source}<div class="guide-bubble">${guideText(message.text)}</div>
    ${(message.cards || []).map(cardHtml).join("")}${actionsHtml(message.actions)}</div>`;
}

function actionsHtml(actions) {
  if (!actions || !actions.length) return "";
  return `<div class="guide-actions">${actions.map((a) => `<button type="button" class="link-button" data-open='${esc(JSON.stringify(a))}'>${esc(a.label)} →</button>`).join("")}</div>`;
}

function cardHtml(card) {
  if (card.type === "choices") {
    return `<div class="chips-row">${card.options.map((o) => `<button type="button" class="chip" data-say="${esc(o)}">${esc(o)}</button>`).join("")}</div>`;
  }
  if (card.type === "proposal") {
    const labels = { pending: "Waiting for your confirmation", applied: "Done", failed: "Not applied", cancelled: "Cancelled", expired: "Expired" };
    return `<div class="guide-card proposal status-${esc(card.status)}" data-proposal="${esc(card.action_id)}">
      <div class="row-between"><strong>${esc(card.title)}</strong><span class="small">${esc(labels[card.status] || card.status)}</span></div>
      ${card.summary ? `<p class="small">${esc(card.summary)}</p>` : ""}
      ${card.effects?.length ? `<ul class="small">${card.effects.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
      ${card.result ? `<p class="small"><em>${esc(card.result)}</em></p>` : ""}
      ${card.status === "pending" ? `<div class="button-row tight"><button type="button" class="primary" data-decide="confirm">Confirm</button><button type="button" data-decide="cancel">Cancel</button></div>` : ""}</div>`;
  }
  if (card.type === "preview") {
    return `<div class="guide-previews">${card.cameras.map((c) => `<figure><img alt="${esc(c.name)} preview" loading="lazy" src="${API}/cameras/${encodeURIComponent(c.id)}/snapshot.jpg?t=${Date.now()}"
      onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'no-frame small muted',textContent:'No picture yet'}))"><figcaption class="small">${esc(c.name)}</figcaption></figure>`).join("")}</div>`;
  }
  if (card.type === "devices_found") {
    return `<div class="guide-card"><ul class="list compact">${card.devices.map((d) => `<li class="row-between"><span>${esc([d.brand, d.model].filter(Boolean).join(" ") || "Device")} · ${esc(d.host)}
      <span class="small muted">${d.on_local_network ? "on this network" : "on a different network"}</span></span>
      <button type="button" data-say="${esc(`Check ${d.host}`)}">Check</button></li>`).join("")}</ul></div>`;
  }
  if (card.type === "recorder_login") {
    return `<form class="guide-card recorder-login" data-host="${esc(card.host)}" data-template="${esc(card.template)}" autocomplete="off">
      <strong>Sign in to ${esc(card.host)}</strong>
      <p class="small muted">Your password goes straight to Sentinel's credential store and the channel search. It is never sent to the assistant or an AI service.</p>
      <div class="grid2"><label class="field">Username <input type="text" name="username" value="admin" maxlength="64" autocomplete="off"></label>
        <label class="field">Password <input type="password" name="password" maxlength="128" autocomplete="new-password" required></label>
        <label class="field">RTSP port <input type="number" name="port" value="${esc(card.port || 554)}" min="1" max="65535"></label>
        <label class="field">Channels to search <input type="number" name="max_channels" value="16" min="1" max="64"></label></div>
      <button type="submit" class="primary">Sign in and find cameras</button><div class="channel-results"></div></form>`;
  }
  return "";
}

function bindMessages(root) {
  root.querySelectorAll("[data-say]").forEach((b) => b.onclick = () => sendGuide(b.dataset.say));
  root.querySelectorAll("[data-open]").forEach((b) => b.onclick = () => openTarget(JSON.parse(b.dataset.open)));
  root.querySelectorAll("[data-proposal] [data-decide]").forEach((b) => b.onclick = () => decide(b.closest("[data-proposal]").dataset.proposal, b.dataset.decide === "confirm", b));
  root.querySelectorAll("form.recorder-login").forEach((form) => form.onsubmit = (event) => recorderSignIn(event, form));
}

async function ensureConversation() {
  if (guide.conversationId) return guide.conversationId;
  const created = await api("/assistant/conversations", { method: "POST", body: "{}" });
  guide.conversationId = created.id;
  try { localStorage.setItem(GUIDE_KEY, created.id); } catch { /* private mode */ }
  return created.id;
}

async function sendGuide(text) {
  text = String(text || "").trim();
  if (!text || guide.sending) return;
  guide.sending = true;
  const input = $("#guide-input");
  input.value = "";
  $("#guide-send").disabled = true;
  const list = $("#guide-messages");
  if (list.querySelector(".guide-welcome")) list.innerHTML = "";
  list.insertAdjacentHTML("beforeend", `<div class="guide-msg user pending-local"><div class="guide-bubble">${guideText(text)}</div></div><div class="guide-typing small muted">Sentinel is checking…</div>`);
  list.scrollTop = list.scrollHeight;
  try {
    const id = await ensureConversation();
    const result = await api(`/assistant/conversations/${encodeURIComponent(id)}/messages`, { method: "POST", body: JSON.stringify({ text }) });
    list.querySelectorAll(".pending-local, .guide-typing").forEach((n) => n.remove());
    appendMessages(result.messages);
    guide.conversations = await api("/assistant/conversations");
    renderConversationList();
  } catch (error) {
    list.querySelectorAll(".guide-typing").forEach((n) => n.remove());
    list.insertAdjacentHTML("beforeend", `<div class="guide-note small status-bad">Not sent: ${esc(error.message)}</div>`);
  } finally {
    guide.sending = false;
    $("#guide-send").disabled = false;
    input.focus();
  }
}

async function decide(actionId, confirmIt, button) {
  button.closest(".button-row").querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    await api(`/assistant/conversations/${encodeURIComponent(guide.conversationId)}/actions/${encodeURIComponent(actionId)}`,
      { method: "POST", body: JSON.stringify({ confirm: confirmIt }) });
    await openConversation(guide.conversationId); // re-render with the backend's result
    if (typeof refreshAfterConfigChange === "function") refreshAfterConfigChange();
  } catch (error) {
    toast("Not applied", error.message);
    button.closest(".button-row").querySelectorAll("button").forEach((b) => { b.disabled = false; });
  }
}

// ---- secure sign-in and channel search (existing onboarding API) ------------------------------

async function recorderSignIn(event, form) {
  event.preventDefault();
  const results = form.querySelector(".channel-results");
  const source = { type: "rtsp", host: form.dataset.host, port: Number(form.port.value) || 554, template: form.dataset.template || "custom",
    username: form.username.value.trim(), password: form.password.value, channel: 1, prefer_substream: true };
  form.password.value = ""; // the password lives only in this one request
  results.innerHTML = '<p class="small muted">Searching channels…</p>';
  let scan;
  try {
    scan = await api("/onboarding/channels", { method: "POST", body: JSON.stringify({ source, max_channels: Number(form.max_channels.value) || 16 }) });
  } catch (error) {
    results.innerHTML = `<p class="small status-bad">${esc(error.message)}</p>`;
    return;
  }
  const found = scan.channels.filter((c) => c.draft_id);
  results.innerHTML = found.length
    ? `<p class="small">${found.length} camera channel(s) answered. Choose the ones to add:</p>
      <ul class="list compact">${found.map((c) => `<li><label><input type="checkbox" data-pick="${esc(c.draft_id)}" checked> Channel ${c.channel}</label>
        <input type="text" data-name="${esc(c.draft_id)}" value="Camera ${c.channel}" maxlength="60" aria-label="Name for channel ${c.channel}">
        <span class="small muted">${esc(c.main?.codec || "")}</span></li>`).join("")}</ul>
      <button type="button" class="primary" data-add-channels>Add selected cameras</button>`
    : `<p class="small status-bad">${esc(scan.message || "No channel answered.")}</p>`;
  const add = results.querySelector("[data-add-channels]");
  if (add) add.onclick = () => addGuideChannels(results, add);
  await guideEvent("channels_scanned");
}

async function addGuideChannels(results, button) {
  button.disabled = true;
  const picks = [...results.querySelectorAll("[data-pick]:checked")];
  let added = 0;
  for (const pick of picks) {
    const name = results.querySelector(`[data-name="${CSS.escape(pick.dataset.pick)}"]`).value.trim() || "Camera";
    try {
      await api("/config/cameras", { method: "POST", body: JSON.stringify({ name, fps: 5, enabled: true, draft_id: pick.dataset.pick }) });
      added += 1;
    } catch (error) {
      toast(`Could not add ${name}`, error.message);
    }
  }
  if (added) await guideEvent("cameras_added");
}

async function guideEvent(event) {
  try {
    const id = await ensureConversation();
    const result = await api(`/assistant/conversations/${encodeURIComponent(id)}/events`, { method: "POST", body: JSON.stringify({ event }) });
    appendMessages(result.messages);
  } catch {
    /* the next message will pick up the state from Sentinel anyway */
  }
}

// ---- opening existing screens -------------------------------------------------------------------

async function openTarget(action) {
  const [base, detail] = action.target.split(":");
  const tab = (name) => document.querySelector(`[data-tab="${name}"]`).click();
  if (["live", "map", "timeline", "insights", "drone", "system", "settings"].includes(base)) {
    tab(base);
    if (base === "timeline" && action.incident_id && typeof openIncident === "function") openIncident(action.incident_id);
  } else if (base === "report") {
    window.open(`${API}/installation/report.html`, "_blank", "noopener");
  } else if (base === "install") {
    tab("install");
    await loadInstall(true);
    if (detail) gotoStep(detail);
  } else if (base === "setup") {
    tab("setup");
    showSub(detail || "cameras");
  } else if (base === "zones") {
    openZoneEditor(detail);
  } else if (base === "troubleshooter") {
    openTroubleshooter({ host: action.host, template: action.brand, autorun: !!action.host });
  } else if (base === "camera_wizard") {
    openWizard(null);
  }
}

// ---- composer, conversations, settings ---------------------------------------------------------

$("#guide-form").addEventListener("submit", (event) => { event.preventDefault(); sendGuide($("#guide-input").value); });
$("#guide-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendGuide(event.target.value); }
});
$("#guide-new").addEventListener("click", () => {
  guide.conversationId = null;
  try { localStorage.removeItem(GUIDE_KEY); } catch { /* private mode */ }
  renderConversation({ id: null, messages: [] });
  renderConversationList();
});
$("#guide-delete").addEventListener("click", async () => {
  if (!guide.conversationId) return;
  if (!confirm("Delete this conversation? It is removed from this hub permanently.")) return;
  try {
    await api(`/assistant/conversations/${encodeURIComponent(guide.conversationId)}`, { method: "DELETE" });
    guide.conversationId = null;
    try { localStorage.removeItem(GUIDE_KEY); } catch { /* private mode */ }
    toast("Conversation deleted", "", "info");
    loadGuide();
  } catch (error) {
    toast("Not deleted", error.message);
  }
});
$("#guide-settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = { provider: form.provider.value, share_network_details: form.share_network_details.checked };
  if (form.model.value.trim()) body.model = form.model.value.trim();
  if (form.api_key.value.trim()) body.api_key = form.api_key.value.trim();
  if (body.provider !== "none" && !confirm("Messages and summaries of Sentinel's status will be sent to the AI provider to answer you. Video, passwords and the property location are never sent. Continue?")) return;
  try {
    guide.status = await api("/assistant/settings", { method: "PUT", body: JSON.stringify(body) });
    form.api_key.value = "";
    renderGuideStatus();
    toast("Assistant settings saved", guide.status.mode === "ai" ? "AI answers are on." : guide.status.reason, "info");
  } catch (error) {
    toast("Not saved", error.message);
  }
});

// Open on the guide for a first run (no cameras yet) or an installation in progress, unless the person
// already chose a tab. A site that already has cameras and never used the journey keeps its Live view.
(async () => {
  try {
    const view = await api("/installation");
    const firstRun = !view.devices.some((device) => device.category === "camera");
    const inProgress = !!view.state.path && !view.state.completed_ms;
    if ((firstRun || inProgress) && !view.state.completed_ms && !guide.navigated && document.querySelector("#tab-live.active")) {
      document.querySelector('[data-tab="guide"]').click();
    }
  } catch {
    /* sign-in or loading problems are shown elsewhere */
  }
})();
