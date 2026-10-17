"use strict";

// AI Test Lab: pick a recorded clip, press "Analyse and play", and watch what
// Sentinel's real detector, tracker, Guardian, species classifier and rules
// make of it. Frames are analysed on the server (src/sentinel/lab/session.py)
// and played here with the results drawn on top. Labels, where a clip has
// them, are only compared with the output; they never drive it.

const lab = {
  catalog: null, clip: null, runs: [], run: null, frames: [], images: new Map(),
  pos: 0, playing: false, timer: null, poll: null, compare: null, compareFrames: [],
  drawing: false, dragStart: null, dragBox: null, selectedDet: null, corrections: [],
  layers: { zones: true, dets: true, tracks: true, guardian: true, gt: true, errors: true },
};
const LAB_LAYERS = [["zones", "Zones"], ["dets", "Detections"], ["tracks", "Tracks"], ["guardian", "Guardian + species"], ["gt", "Labels (ground truth)"], ["errors", "Mark errors"]];
const LAB_COLORS = { person: "#4c8dff", vehicle: "#b36bff", animal: "#ff9f1a", other: "#9aa3ad" };
const LAB_LEVELS = { observed: "#9aa3ad", approaching: "#f0c43b", elevated: "#ff8a1a", urgent: "#ff3b3b" };
const LAB_ZONE = { yard: "rgba(76,187,122,.16)", entrance: "rgba(76,141,255,.18)", driveway: "rgba(154,163,173,.18)", restricted: "rgba(255,80,80,.18)", perimeter: "rgba(240,162,59,.16)",
  public: "rgba(80,200,220,.12)", general: "rgba(80,200,220,.12)", sidewalk: "rgba(200,200,200,.10)", street: "rgba(200,200,200,.10)", ignore: "rgba(0,0,0,.35)" };
const LAB_ZONE_LINE = { yard: "#4cbb7a", entrance: "#4c8dff", driveway: "#9aa3ad", restricted: "#ff5050", perimeter: "#f0a23b", public: "#50c8dc", general: "#50c8dc", sidewalk: "#c8c8c8", street: "#c8c8c8", ignore: "#666" };
const labTab = document.querySelector('[data-tab="lab"]');
labTab.addEventListener("click", () => { if (!lab.catalog) loadLabCatalog(); });

const labApi = (path, method = "GET", body) => api(`/lab${path}`, method === "GET" ? {} : { method, body: JSON.stringify(body ?? {}) });
const labFmtT = (t) => { const s = Math.max(0, t || 0); return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`; };
const labPct = (v) => (v == null ? "-" : `${Math.round(v * 100)}%`);

async function loadLabCatalog() {
  try {
    lab.catalog = await labApi("/catalog");
    lab.runs = await labApi("/runs");
  } catch (error) {
    $("#lab-clips").innerHTML = `<li class="muted">${esc(error.message)}</li>`;
    return;
  }
  $("#lab-profile").innerHTML = lab.catalog.profiles.map((p) =>
    `<option value="${esc(p.id)}" ${p.available ? "" : "disabled"} title="${esc(p.description)}">${esc(p.name)}${p.available ? "" : " (model not installed)"}</option>`).join("");
  $("#lab-drop").innerHTML = `Your own clips: copy video files into <code>${esc(lab.catalog.drop_folder)}</code> and reopen this tab. They stay on this computer.`;
  renderLabClips();
}

function renderLabClips() {
  const split = $("#lab-filter-split").value;
  const subject = $("#lab-filter-subject").value;
  const clips = lab.catalog.clips.filter((c) => (!split || c.split === split) && (!subject || (c.subjects || []).includes(subject)));
  const runsFor = (id) => lab.runs.filter((r) => r.clip.id === id).length;
  $("#lab-clips").innerHTML = clips.map((c) => `
    <li data-clip="${esc(c.id)}" class="${c.available ? "" : "unavailable"} ${lab.clip?.id === c.id ? "selected" : ""}">
      <div class="title">${esc(c.title)}</div>
      <div class="small muted">${esc(c.category.replace(/_/g, " "))} · ${esc(c.light)}${runsFor(c.id) ? ` · ${runsFor(c.id)} run(s)` : ""}</div>
      <span class="pill ${c.split === "test" ? "ok" : ""}">${esc(c.split)}</span><span class="pill">${c.labels === "none" ? "unlabelled" : esc(c.labels) + " labels"}</span>${c.available ? "" : '<span class="pill bad">not downloaded</span>'}
    </li>`).join("") || '<li class="muted small">No clips match.</li>';
}

$("#lab-filter-split").addEventListener("change", renderLabClips);
$("#lab-filter-subject").addEventListener("change", renderLabClips);
$("#lab-clips").addEventListener("click", (event) => {
  const item = event.target.closest("[data-clip]");
  if (item) selectLabClip(item.dataset.clip);
});

function selectLabClip(clipId, runId) {
  lab.clip = lab.catalog.clips.find((c) => c.id === clipId);
  renderLabClips();
  const c = lab.clip;
  $("#lab-clip-title").textContent = c.title;
  $("#lab-clip-meta").innerHTML = `${esc(c.notes)}<br>Licence: ${esc(c.licence)}${c.commercial_training ? " (may be used for training)" : " (evaluation only)"} · split <b>${esc(c.split)}</b> · labels: ${esc(c.labels)}${c.attribution ? `<br>${esc(c.attribution)}` : ""}`;
  $("#lab-run").disabled = !c.available;
  $("#lab-species").checked = (c.subjects || []).includes("animal");
  $("#lab-clock").value = "";
  $("#lab-clock").placeholder = c.light === "night" ? "02:00" : "14:00";
  $("#lab-clock").title = `Empty: ${c.light === "night" ? "02:00 (night clip)" : "14:00"}. Rule hours such as "after hours" are judged on this clock.`;
  loadLabScene(c.id);
  renderLabRuns();
  const runs = lab.runs.filter((r) => r.clip.id === clipId);
  const wanted = runId || runs[0]?.id;
  if (wanted) openLabRun(wanted);
  else { $("#lab-player-card").hidden = true; $("#lab-panels").hidden = true; }
}

function renderLabRuns() {
  const runs = lab.runs.filter((r) => r.clip.id === lab.clip?.id);
  $("#lab-runs-label").hidden = !runs.length;
  $("#lab-runs").innerHTML = runs.map((r) => {
    const profile = lab.catalog.profiles.find((p) => p.id === r.profile);
    return `<button type="button" data-run="${esc(r.id)}" class="${lab.run?.id === r.id ? "active" : ""}">${esc(profile?.name || r.profile)} · ${esc(r.options.fps)} fps · ${esc(r.state)}<br><span class="small">${esc(r.created.replace("T", " "))}${r.corrections ? ` · ${r.corrections} mark(s)` : ""}</span></button>`;
  }).join("");
  const compare = $("#lab-compare");
  const others = runs.filter((r) => r.id !== lab.run?.id && ["done", "cancelled", "interrupted"].includes(r.state));
  compare.innerHTML = '<option value="">-</option>' + others.map((r) => `<option value="${esc(r.id)}" ${lab.compare?.id === r.id ? "selected" : ""}>${esc(lab.catalog.profiles.find((p) => p.id === r.profile)?.name || r.profile)} (${esc(r.created.slice(11, 16))})</option>`).join("");
}

$("#lab-runs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-run]");
  if (button) openLabRun(button.dataset.run);
});

$("#lab-run").addEventListener("click", async () => {
  if (!lab.clip) return;
  $("#lab-run").disabled = true;
  try {
    const run = await labApi("/runs", "POST", {
      clip_id: lab.clip.id, profile: $("#lab-profile").value, fps: Number($("#lab-fps").value), arm_mode: $("#lab-arm").value,
      species: $("#lab-species").checked, max_seconds: Number($("#lab-max").value), start_time: $("#lab-clock").value || null,
    });
    lab.runs.unshift(run);
    await openLabRun(run.id, true);
  } catch (error) {
    toast("Could not start", error.message, "high");
  } finally {
    $("#lab-run").disabled = !lab.clip?.available;
  }
});

async function openLabRun(runId, autoplay = false) {
  labPause();
  clearInterval(lab.poll);
  lab.run = await labApi(`/runs/${runId}`);
  lab.frames = [];
  lab.images.clear();
  lab.pos = 0;
  lab.compare = null;
  lab.compareFrames = [];
  $("#lab-view-b").hidden = true;
  $("#lab-player-card").hidden = false;
  $("#lab-panels").hidden = false;
  renderLabRuns();
  renderLabLayers();
  await fetchLabFrames();
  lab.corrections = await labApi(`/runs/${runId}/corrections`).catch(() => []);
  sizeLabCanvas();
  lab.sized = lab.run.video ? lab.run.id : null;
  renderLabFrame();
  renderLabSide();
  if (["queued", "running"].includes(lab.run.state)) lab.poll = setInterval(pollLabRun, 1000);
  if (autoplay) labPlay();
}

async function fetchLabFrames() {
  const data = await labApi(`/runs/${lab.run.id}/frames?start=${lab.frames.length}`);
  lab.frames.push(...data.frames);
  $("#lab-seek").max = Math.max(0, lab.frames.length - 1);
}

async function pollLabRun() {
  try {
    lab.run = await labApi(`/runs/${lab.run.id}`);
    await fetchLabFrames();
  } catch { return; }
  const p = lab.run.progress || {};
  const done = !["queued", "running"].includes(lab.run.state);
  const bar = $("#lab-progress");
  bar.hidden = done;
  if (!done) {
    const share = p.expected ? Math.min(1, p.frames / p.expected) : 0;
    bar.querySelector("div").style.width = `${share * 100}%`;
    bar.querySelector("span").textContent = lab.run.state === "queued" ? "Waiting for the previous run to finish..." :
      `Analysing with ${lab.run.detector_resolved?.model || lab.run.profile}: ${p.frames} of ~${p.expected} frames (${labFmtT(p.video_s)} of ${labFmtT(p.duration_s)})`;
  }
  if (lab.run.video && lab.sized !== lab.run.id) { sizeLabCanvas(); lab.sized = lab.run.id; }
  if (!lab.playing) renderLabFrame();
  renderLabSide();
  if (done) {
    clearInterval(lab.poll);
    const index = lab.runs.findIndex((r) => r.id === lab.run.id);
    if (index >= 0) lab.runs[index] = lab.run;
    renderLabRuns();
    renderLabClips();
    if (lab.run.state === "failed") toast("Analysis failed", lab.run.error, "high");
  }
}

$("#lab-compare").addEventListener("change", async (event) => {
  const id = event.target.value;
  if (!id) { lab.compare = null; $("#lab-view-b").hidden = true; renderLabFrame(); return; }
  lab.compare = await labApi(`/runs/${id}`);
  lab.compareFrames = (await labApi(`/runs/${id}/frames`)).frames;
  $("#lab-view-b").hidden = false;
  sizeLabCanvas();
  renderLabFrame();
  renderLabSide();
});

// ---- playback ---------------------------------------------------------------

function labPlay() {
  if (!lab.frames.length && !["queued", "running"].includes(lab.run?.state)) return;
  if (lab.pos >= lab.frames.length - 1 && !["queued", "running"].includes(lab.run.state)) lab.pos = 0;
  lab.playing = true;
  $("#lab-play").textContent = "Pause";
  labTick();
}

function labPause() {
  lab.playing = false;
  clearTimeout(lab.timer);
  $("#lab-play").textContent = "Play";
}

function labTick() {
  if (!lab.playing) return;
  const fps = lab.run.options.fps || 5;
  const delay = 1000 / (fps * Number($("#lab-speed").value));
  if (lab.pos < lab.frames.length - 1) {
    lab.pos += 1;
    renderLabFrame();
    lab.timer = setTimeout(labTick, delay);
  } else if (["queued", "running"].includes(lab.run.state)) {
    lab.timer = setTimeout(labTick, 200); // live: wait for the next analysed frame
  } else {
    labPause();
  }
}

function labSeek(n) {
  lab.pos = Math.max(0, Math.min(lab.frames.length - 1, n));
  renderLabFrame();
}

$("#lab-play").addEventListener("click", () => (lab.playing ? labPause() : labPlay()));
$("#lab-back").addEventListener("click", () => { labPause(); labSeek(lab.pos - 1); });
$("#lab-fwd").addEventListener("click", () => { labPause(); labSeek(lab.pos + 1); });
$("#lab-seek").addEventListener("input", (event) => { labPause(); labSeek(Number(event.target.value)); });
$("#lab-next-error").addEventListener("click", () => labJumpError(1));
$("#lab-prev-error").addEventListener("click", () => labJumpError(-1));
document.addEventListener("keydown", (event) => {
  if (!$("#tab-lab").classList.contains("active") || ["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
  if (event.key === " ") { event.preventDefault(); lab.playing ? labPause() : labPlay(); }
  if (event.key === "ArrowRight") { labPause(); labSeek(lab.pos + 1); }
  if (event.key === "ArrowLeft") { labPause(); labSeek(lab.pos - 1); }
});

function labFrameHasError(f) {
  return (f.score && (f.score.fp || f.score.missed)) || ["missed", "false_positive"].includes(f.presence?.verdict);
}

function labJumpError(direction) {
  labPause();
  for (let n = lab.pos + direction; n >= 0 && n < lab.frames.length; n += direction) {
    if (labFrameHasError(lab.frames[n])) return labSeek(n);
  }
  toast(direction > 0 ? "No later errors" : "No earlier errors", lab.clip?.labels === "none" ? "This clip has no labels: mark errors yourself in the frame panel." : "", "info");
}

function renderLabLayers() {
  $("#lab-layers").innerHTML = LAB_LAYERS.map(([key, label]) =>
    `<label><input type="checkbox" data-layer="${key}" ${lab.layers[key] ? "checked" : ""}> ${label}</label>`).join("");
}
$("#lab-layers").addEventListener("change", (event) => {
  lab.layers[event.target.dataset.layer] = event.target.checked;
  renderLabFrame();
});

// ---- drawing ----------------------------------------------------------------

function sizeLabCanvas() {
  const video = lab.run?.video || {};
  const ratio = video.width && video.height ? video.height / video.width : 9 / 16;
  for (const id of ["#lab-canvas-a", "#lab-canvas-b"]) {
    const canvas = $(id);
    canvas.width = 960;
    canvas.height = Math.round(960 * ratio);
  }
}

function labImage(run, n) {
  const key = `${run.id}/${n}`;
  if (!lab.images.has(key)) {
    const image = new Image();
    image.src = `${API}/lab/runs/${encodeURIComponent(run.id)}/frame/${n}.jpg`;
    image.onload = () => { if (lab.frames[lab.pos]?.n === n || lab.compare) renderLabFrame(); };
    lab.images.set(key, image);
    if (lab.images.size > 400) lab.images.delete(lab.images.keys().next().value);
  }
  return lab.images.get(key);
}

function renderLabFrame() {
  const frame = lab.frames[lab.pos];
  $("#lab-seek").value = lab.pos;
  const total = lab.run?.progress?.duration_s;
  $("#lab-time").textContent = frame ? `${labFmtT(frame.t)} / ${labFmtT(total)} · frame ${lab.pos + 1} of ${lab.frames.length}${["queued", "running"].includes(lab.run.state) ? " (analysing)" : ""}` : "Waiting for the first frame...";
  $("#lab-label-a").textContent = labRunName(lab.run);
  if (!frame) return;
  drawLabFrame($("#lab-canvas-a"), lab.run, frame, true);
  for (let k = 1; k <= 3; k++) if (lab.frames[lab.pos + k]) labImage(lab.run, lab.frames[lab.pos + k].n); // preload
  if (lab.compare) {
    const other = labNearest(lab.compareFrames, frame.t);
    $("#lab-label-b").textContent = labRunName(lab.compare);
    if (other) drawLabFrame($("#lab-canvas-b"), lab.compare, other, false);
  }
  renderLabInspector(frame);
  document.querySelectorAll(".lab-event").forEach((node) => node.classList.toggle("current", Number(node.dataset.frame) === frame.n));
}

function labRunName(run) {
  if (!run) return "";
  const profile = lab.catalog.profiles.find((p) => p.id === run.profile);
  return `${profile?.name || run.profile} · ${run.detector_resolved?.model || ""}`;
}

function labNearest(frames, t) {
  let best = null;
  for (const f of frames) if (!best || Math.abs(f.t - t) < Math.abs(best.t - t)) best = f;
  return best;
}

function drawLabFrame(canvas, run, frame, main) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  const image = labImage(run, frame.n);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, W, H);
  if (image.complete && image.naturalWidth) ctx.drawImage(image, 0, 0, W, H);
  const box = (b) => [b[0] * W, b[1] * H, b[2] * W, b[3] * H];
  const tag = (text, x, y, color) => {
    ctx.font = "600 13px system-ui, sans-serif";
    const w = ctx.measureText(text).width + 8;
    const ty = y > 18 ? y - 18 : y + 2;
    x = Math.max(0, Math.min(x, W - w)); // keep labels of boxes at the right edge readable
    ctx.fillStyle = color;
    ctx.fillRect(x, ty, w, 17);
    ctx.fillStyle = "#000";
    ctx.fillText(text, x + 4, ty + 13);
  };
  if (lab.layers.zones) {
    for (const zone of run.zones || []) {
      ctx.beginPath();
      zone.polygon.forEach(([x, y], i) => (i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H)));
      ctx.closePath();
      ctx.fillStyle = LAB_ZONE[zone.type] || "rgba(154,163,173,.14)";
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,.35)";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,.75)";
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillText(zone.name, zone.polygon[0][0] * W + 6, zone.polygon[0][1] * H + 14);
    }
  }
  if (lab.layers.gt) {
    for (const g of frame.gt || []) {
      if (g.ignore) continue;
      const missed = g.missed && lab.layers.errors;
      ctx.setLineDash(missed ? [] : [4, 4]);
      ctx.strokeStyle = missed ? "#ff3b3b" : "rgba(120,255,160,.8)";
      ctx.lineWidth = missed ? 2.5 : 1;
      ctx.strokeRect(...box(g.bbox));
      if (missed) tag("MISSED", g.bbox[0] * W, g.bbox[1] * H, "#ff3b3b");
    }
    ctx.setLineDash([]);
  }
  if (lab.layers.dets) {
    frame.dets.forEach((d, i) => {
      const color = LAB_COLORS[d.cls] || LAB_COLORS.other;
      const fp = d.verdict === "fp" && lab.layers.errors;
      const marked = main && lab.corrections.some((c) => c.frame === frame.n && c.detection === i);
      ctx.setLineDash(d.weak ? [3, 3] : []);
      ctx.strokeStyle = fp ? "#ff3b3b" : color;
      ctx.lineWidth = main && lab.selectedDet === i ? 4 : 2;
      ctx.strokeRect(...box(d.bbox));
      ctx.setLineDash([]);
      if (!lab.layers.tracks) tag(`${fp ? "FALSE " : ""}${d.label} ${Math.round(d.conf * 100)}%${marked ? " ✎" : ""}`, d.bbox[0] * W, d.bbox[1] * H, fp ? "#ff3b3b" : color);
    });
  }
  if (lab.layers.tracks) {
    const dets = frame.dets;
    for (const t of frame.tracks) {
      const color = LAB_COLORS[t.cls] || LAB_COLORS.other;
      const det = dets.find((d) => Math.abs(d.bbox[0] - t.bbox[0]) < 0.02 && Math.abs(d.bbox[1] - t.bbox[1]) < 0.02);
      const fp = det?.verdict === "fp" && lab.layers.errors;
      if (!lab.layers.dets) { ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.strokeRect(...box(t.bbox)); }
      tag(`${fp ? "FALSE " : ""}#${t.id} ${t.label}${det ? ` ${Math.round(det.conf * 100)}%` : ""}`, t.bbox[0] * W, t.bbox[1] * H, fp ? "#ff3b3b" : color);
    }
  }
  if (lab.layers.guardian) {
    for (const g of frame.guardian) {
      const color = LAB_LEVELS[g.level] || LAB_LEVELS.observed;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      g.trail.forEach(([x, y], i) => (i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H)));
      ctx.stroke();
      const [x, y, w, h] = box(g.bbox);
      ctx.lineWidth = 3;
      ctx.strokeRect(x - 3, y - 3, w + 6, h + 6);
      const text = `${g.label} · ${g.level}${g.movement && g.movement !== "unknown" ? ` · ${g.movement}` : ""}`;
      tag(text, x, y + h + 22 < H ? y + h + 22 : y, color);
    }
  }
  if (main && lab.layers.errors && frame.presence) {
    const v = frame.presence.verdict;
    if (v === "missed" || v === "false_positive") {
      ctx.strokeStyle = "#ff3b3b";
      ctx.lineWidth = 6;
      ctx.strokeRect(3, 3, W - 6, H - 6);
      tag(v === "missed" ? "MISSED: labelled subject, no box" : "FALSE POSITIVE: empty scene", 10, 30, "#ff3b3b");
    }
  }
  if (main && lab.dragBox) {
    ctx.strokeStyle = "#fff";
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 2;
    ctx.strokeRect(...box(lab.dragBox));
    ctx.setLineDash([]);
  }
  if (main) {
    for (const c of lab.corrections.filter((c) => c.frame === frame.n && c.kind === "missed" && c.bbox)) {
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.strokeRect(...box(c.bbox));
      tag(`you: missed ${c.label || ""}`, c.bbox[0] * W, c.bbox[1] * H, "#fff");
    }
  }
}

// ---- inspector, corrections -------------------------------------------------

function renderLabInspector(frame) {
  const marks = lab.corrections.filter((c) => c.frame === frame.n);
  const rows = frame.dets.map((d, i) => {
    const mark = marks.find((c) => c.detection === i);
    const verdict = d.verdict === "fp" ? '<span class="pill bad">no label here</span>' : d.verdict === "tp" ? '<span class="pill ok">matches label</span>' : "";
    return `<div class="lab-det" data-det="${i}">
      <span class="swatch" style="background:${LAB_COLORS[d.cls] || LAB_COLORS.other}"></span>
      <span class="grow">${esc(d.label)} ${Math.round(d.conf * 100)}%${d.weak ? " (weak: tracker only)" : ""} ${verdict}${mark ? ` <span class="pill warn">you: ${esc(mark.kind.replace("_", " "))}${mark.label ? " " + esc(mark.label) : ""}</span>` : ""}</span>
      ${mark ? `<button type="button" data-undo="${esc(mark.id)}">Undo</button>` : `<button type="button" data-mark="correct">Correct</button><button type="button" data-mark="false_positive">Wrong</button><button type="button" data-mark="wrong_label">Wrong label</button>`}
    </div>`;
  }).join("");
  const guardian = frame.guardian.map((g) => `<div class="lab-det"><span class="swatch" style="background:${LAB_LEVELS[g.level] || "#999"}"></span><span class="grow">Guardian ${esc(g.gid)}: ${esc(g.species)}${g.species_confidence != null ? ` (${Math.round(g.species_confidence * 100)}%)` : ""} · ${esc(g.level)} · ${esc(g.movement)}${g.targets?.length ? " toward " + esc(g.targets.join(", ")) : ""}</span></div>`).join("");
  const presence = frame.presence ? `<p class="small">Label for this moment: subject ${frame.presence.expected ? "present" : "absent"} → <b>${esc(frame.presence.verdict.replace("_", " "))}</b></p>` : "";
  const score = frame.score ? `<p class="small">Labelled people: ${frame.score.tp} found, ${frame.score.missed} missed; ${frame.score.fp} box(es) with no labelled person.</p>` : "";
  const missed = marks.filter((c) => c.kind === "missed").map((c) => `<div class="lab-det"><span class="grow">You marked a missed ${esc(c.label || "object")}</span><button type="button" data-undo="${esc(c.id)}">Undo</button></div>`).join("");
  $("#lab-frame").innerHTML = `<div class="small muted">t = ${labFmtT(frame.t)} · detect ${frame.ms?.detect ?? "-"} ms</div>${presence}${score}
    ${rows || '<p class="muted small">No detections in this frame.</p>'}${guardian}${missed}`;
}

$("#lab-frame").addEventListener("click", async (event) => {
  const frame = lab.frames[lab.pos];
  const undo = event.target.closest("[data-undo]");
  if (undo) {
    await labApi(`/runs/${lab.run.id}/corrections/${undo.dataset.undo}`, "DELETE").catch((e) => toast("Not done", e.message, "high"));
    lab.corrections = lab.corrections.filter((c) => c.id !== undo.dataset.undo);
    return renderLabFrame();
  }
  const mark = event.target.closest("[data-mark]");
  if (!mark) return;
  const det = Number(mark.closest("[data-det]").dataset.det);
  const body = { kind: mark.dataset.mark, frame: frame.n, detection: det, bbox: frame.dets[det].bbox };
  if (body.kind === "wrong_label") {
    const label = prompt("What is it really? (e.g. dog, person, car, bear, deer, nothing)", "");
    if (!label) return;
    if (label.trim().toLowerCase() === "nothing") body.kind = "false_positive";
    else body.label = label.trim().toLowerCase();
  }
  await labCorrect(body);
});

$("#lab-frame").addEventListener("mouseover", (event) => {
  const row = event.target.closest("[data-det]");
  lab.selectedDet = row ? Number(row.dataset.det) : null;
  drawLabFrame($("#lab-canvas-a"), lab.run, lab.frames[lab.pos], true);
});

async function labCorrect(body) {
  try {
    const record = await labApi(`/runs/${lab.run.id}/corrections`, "POST", body);
    lab.corrections.push(record);
    renderLabFrame();
  } catch (error) {
    toast("Not saved", error.message, "high");
  }
}

$("#lab-draw").addEventListener("click", () => {
  labPause();
  lab.drawing = !lab.drawing;
  $("#lab-canvas-a").classList.toggle("drawing", lab.drawing);
  $("#lab-draw").textContent = lab.drawing ? "Drag a box on the picture... (click to cancel)" : "Draw a missed object";
});

function labPoint(event) {
  const rect = $("#lab-canvas-a").getBoundingClientRect();
  return [Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)), Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))];
}
$("#lab-canvas-a").addEventListener("mousedown", (event) => { if (lab.drawing) lab.dragStart = labPoint(event); });
$("#lab-canvas-a").addEventListener("mousemove", (event) => {
  if (!lab.drawing || !lab.dragStart) return;
  const [x, y] = labPoint(event);
  const [x0, y0] = lab.dragStart;
  lab.dragBox = [Math.min(x, x0), Math.min(y, y0), Math.abs(x - x0), Math.abs(y - y0)];
  drawLabFrame($("#lab-canvas-a"), lab.run, lab.frames[lab.pos], true);
});
$("#lab-canvas-a").addEventListener("mouseup", async () => {
  if (!lab.drawing || !lab.dragBox) { lab.dragStart = null; return; }
  const bbox = lab.dragBox.map((v) => Math.round(v * 10000) / 10000);
  lab.dragStart = lab.dragBox = null;
  lab.drawing = false;
  $("#lab-canvas-a").classList.remove("drawing");
  $("#lab-draw").textContent = "Draw a missed object";
  if (bbox[2] < 0.005 || bbox[3] < 0.005) return renderLabFrame();
  await labCorrect({ kind: "missed", frame: lab.frames[lab.pos].n, bbox, label: $("#lab-draw-label").value });
});

// ---- events, alerts, summary ------------------------------------------------

function renderLabSide() {
  const run = lab.run;
  const events = (run.events || []).filter((e) => !e.health);
  const alerts = run.alerts || [];
  const eventMark = (e) => lab.corrections.find((c) => c.event_id === e.id);
  $("#lab-events").innerHTML = `
    ${labSceneLine(run)}
    ${alerts.length ? alerts.map((a) => `<div class="lab-alert" data-t="${a.t}"><b>Simulated alert</b> at ${labFmtT(a.t)} (${esc(a.severity)}): ${esc(a.title)}<div class="small muted">${esc(a.body || "")}</div>${labAlertBecause(a, events)}<div class="small muted">Not sent: the lab records what the alert policy would push.</div></div>`).join("") : `<p class="small muted">No alert would have been pushed (arm mode ${esc(run.options.arm_mode)}). Ordinary activity is logged as info, never pushed.</p>`}
    ${events.length ? events.map((e) => {
      const mark = eventMark(e);
      return `<div class="lab-event" data-frame="${e.frame}">
        <div><span class="pill ${e.severity === "high" ? "bad" : e.severity === "medium" ? "warn" : ""}">${esc(e.severity)}</span> ${labFmtT(e.t)} · ${esc(e.summary)}</div>
        <div class="meta">${esc(e.type)}${e.rule ? " · rule " + esc(e.rule) : ""}${e.guardian ? " · Guardian " + esc(e.level || "") : ""}${e.confidence != null ? ` · confidence ${Math.round(e.confidence * 100)}%` : ""}${e.reasons?.length ? " · " + esc(e.reasons.slice(0, 4).join(", ")) : ""}</div>
        ${labWhy(e)}
        <div class="button-row tight">${mark ? `<span class="pill warn">you: ${esc(mark.kind.replace("event_", "").replace("_", " "))}</span> <button type="button" data-undo-event="${esc(mark.id)}">Undo</button>` : `<button type="button" data-event-mark="event_correct" data-event="${esc(e.id)}">Right</button><button type="button" data-event-mark="event_false_alarm" data-event="${esc(e.id)}">False alarm</button>`}</div>
      </div>`;
    }).join("") : '<p class="small muted">No events yet.</p>'}`;
  const s = run.summary || {};
  const tiles = [];
  if (s.boxes) tiles.push(["People found", labPct(s.boxes.recall), `${s.boxes.tp} of ${s.boxes.tp + s.boxes.missed} labelled boxes`], ["Box precision", labPct(s.boxes.precision), `${s.boxes.fp} box(es) without a label`]);
  if (s.presence) tiles.push(["Subject found", labPct(s.presence.recall), `${s.presence.found} of ${s.presence.present_frames} frames`], ["False-positive frames", String(s.presence.false_positive_frames ?? "-"), `of ${s.presence.absent_frames} empty frames`]);
  tiles.push(["Tracks", String(s.tracks ?? 0), `Guardian: ${s.guardian_tracks ?? 0}`], ["Events", String(Object.values(s.events || {}).reduce((a, b) => a + b, 0)), `${s.alerts ?? 0} simulated alert(s)`], ["Detect p50", s.detect_ms?.p50 != null ? `${s.detect_ms.p50} ms` : "-", `p95 ${s.detect_ms?.p95 ?? "-"} ms`]);
  const duration = run.progress?.duration_s || 1;
  const markers = (s.errors || []).slice(0, 600).map((n) => { const f = lab.frames[n]; return f ? `<i style="left:${(f.t / duration) * 100}%;background:#ff3b3b"></i>` : ""; }).join("")
    + events.map((e) => `<i style="left:${(e.t / duration) * 100}%;background:#4c8dff"></i>`).join("");
  const basis = s.boxes ? `Measured against ${esc(s.boxes.labels)} on this clip only (${esc(run.clip.licence)}).` : s.presence ? `Measured against ${esc(s.presence.labels)} on this clip only.` : "This clip has no labels, so no accuracy is measured. Mark errors yourself and they become labels for training.";
  const compare = lab.compare?.summary ? labCompareTable(s, lab.compare.summary) : "";
  $("#lab-summary").innerHTML = `<div class="lab-metrics">${tiles.map(([k, v, sub]) => `<div><div class="small muted">${k}</div><div class="lab-metric">${v}</div><div class="small muted">${sub}</div></div>`).join("")}</div>
    <div class="small muted">Errors (red) and events (blue) along the clip:</div><div class="lab-timeline" id="lab-timeline">${markers}</div>
    <p class="small muted">${basis}</p>
    ${s.species?.length ? `<p class="small">Species decisions: ${esc(s.species.join("; "))}</p>` : ""}
    <p class="small">Detected: ${Object.entries(s.detections || {}).map(([k, v]) => `${esc(k)} ${v}`).join(", ") || "nothing"}. Model ${esc(run.detector_resolved?.model || run.profile)} ${esc((run.detector_resolved?.input_size || []).join("x"))}${run.detector_resolved?.precision ? " " + esc(run.detector_resolved.precision) : ""}. Every analysed frame went through the detector (no motion gate).</p>
    ${compare}`;
}

function labCompareTable(a, b) {
  const row = (label, f) => `<tr><td>${label}</td><td>${f(a)}</td><td>${f(b)}</td></tr>`;
  return `<h4>Comparison</h4><table class="table"><tr><th></th><th>${esc(labRunName(lab.run))}</th><th>${esc(labRunName(lab.compare))}</th></tr>
    ${a.boxes || b.boxes ? row("People found", (s) => (s.boxes ? `${labPct(s.boxes.recall)} (${s.boxes.tp})` : "-")) + row("Boxes without a label", (s) => s.boxes?.fp ?? "-") : ""}
    ${a.presence || b.presence ? row("Subject found", (s) => (s.presence ? `${labPct(s.presence.recall)} (${s.presence.found})` : "-")) + row("False-positive frames", (s) => s.presence?.false_positive_frames ?? "-") : ""}
    ${row("Tracks", (s) => s.tracks ?? "-")}${row("Events", (s) => Object.values(s.events || {}).reduce((x, y) => x + y, 0))}${row("Simulated alerts", (s) => s.alerts ?? 0)}
    ${row("Detect p50 ms", (s) => s.detect_ms?.p50 ?? "-")}</table>`;
}

$("#lab-events").addEventListener("click", async (event) => {
  const mark = event.target.closest("[data-event-mark]");
  const undo = event.target.closest("[data-undo-event]");
  if (undo) {
    await labApi(`/runs/${lab.run.id}/corrections/${undo.dataset.undoEvent}`, "DELETE").catch(() => {});
    lab.corrections = lab.corrections.filter((c) => c.id !== undo.dataset.undoEvent);
    return renderLabSide();
  }
  if (mark) {
    const item = mark.closest("[data-frame]");
    await labCorrect({ kind: mark.dataset.eventMark, event_id: mark.dataset.event, frame: Number(item.dataset.frame) });
    return renderLabSide();
  }
  const item = event.target.closest("[data-frame]");
  if (item) { labPause(); labSeek(Number(item.dataset.frame)); return; }
  const alert = event.target.closest("[data-t]");
  if (alert) { labPause(); const f = labNearest(lab.frames, Number(alert.dataset.t)); if (f) labSeek(f.n); }
});

$("#lab-summary").addEventListener("click", (event) => {
  const line = event.target.closest("#lab-timeline");
  if (!line || !lab.frames.length) return;
  const rect = line.getBoundingClientRect();
  const t = ((event.clientX - rect.left) / rect.width) * (lab.run.progress?.duration_s || 0);
  labPause();
  const f = labNearest(lab.frames, t);
  if (f) labSeek(f.n);
});

// ---- explanations -----------------------------------------------------------

const labZoneTypeName = (type) => lab.catalog?.scenes?.zone_types.find((z) => z.id === type)?.name || type || "";

function labWhy(e) {
  const x = e.explain;
  if (!x) return "";
  if (e.type === "activity") return `<div class="lab-why">First sighting in ${esc(x.zone)} (${esc(labZoneTypeName(x.zone_type))}${x.ordinary_area ? ", ordinary activity" : ""}): logged as info, never pushed.</div>`;
  const parts = [];
  if (x.rule) parts.push(`<b>Rule</b> "${esc(x.rule)}": ${esc(x.rule_what)}${x.rule_when ? ` (${esc(x.rule_when)})` : ""}.`);
  else if (x.basis) parts.push(esc(x.basis));
  if (x.zone) parts.push(`<b>Zone</b> ${esc(x.zone)}${x.zone_type ? ` (${esc(labZoneTypeName(x.zone_type))}${x.ordinary_area ? ", ordinary activity" : ""})` : ""}.`);
  const facts = [];
  if (x.duration_s) facts.push(`in the zone for ${x.duration_s} s${x.threshold_s != null ? ` (rule: ${x.threshold_s} s)` : ""}`);
  if (x.tracked_s != null) facts.push(`tracked for ${x.tracked_s} s`);
  if (x.confidence != null) facts.push(`detection confidence ${Math.round(x.confidence * 100)}%`);
  if (x.observations != null) facts.push(`${x.observations} detections of this subject`);
  if (facts.length) parts.push(`<b>Observed</b> ${esc(facts.join(", "))}.`);
  if (x.alert_reason) parts.push(`<b>${x.alert === "alerted" ? "Alert" : "No alert"}</b> ${esc(x.alert_reason)}`);
  return `<div class="lab-why">${parts.join("<br>")}</div>`;
}

function labAlertBecause(alert, events) {
  const causes = events.filter((e) => e.incident && e.incident === alert.incident && e.explain?.rule && Math.abs(e.t - alert.t) <= 0.5);
  if (!causes.length) return "";
  return `<div class="lab-why"><b>Because</b> ${causes.map((e) => `rule "${esc(e.explain.rule)}" matched in ${esc(e.explain.zone)}${e.explain.duration_s ? ` after ${e.explain.duration_s} s in the zone` : ""} (confidence ${Math.round((e.explain.confidence || 0) * 100)}%)`).join("; ")}.</div>`;
}

function labSceneLine(run) {
  const scene = run.scene;
  if (!scene && !run.options?.start_time) return ""; // a run from before scenes existed
  const preset = scene && lab.catalog?.scenes?.presets.find((p) => p.id === scene.preset);
  const where = scene ? `${esc(preset?.name || scene.preset)} scene${scene.source === "saved" ? " (zones saved for this video)" : " (default zones)"}` : "benchmark zones and rules (config/bench/site.json)";
  return `<p class="small muted">This run: ${where}, ${(run.zones || []).length} zone(s), clock ${esc(run.options?.start_time || "-")}, arm mode ${esc(run.options?.arm_mode)}.</p>`;
}

// ---- scene editor: zones drawn on the footage and the preset's rules, per video --

lab.scene = null;
lab.draft = null;
lab.zoneSel = null;
lab.zoneDrag = null;
lab.zoneAdding = false;
lab.still = null;

async function loadLabScene(clipId) {
  $("#lab-scene-row").hidden = true;
  $("#lab-scene-card").hidden = true;
  if (!lab.catalog?.scenes) return;
  try {
    lab.scene = await labApi(`/clips/${encodeURIComponent(clipId)}/scene`);
  } catch { lab.scene = null; return; }
  if (lab.clip?.id !== clipId) return;
  $("#lab-preset").innerHTML = lab.catalog.scenes.presets.map((p) => `<option value="${esc(p.id)}" title="${esc(p.description)}">${esc(p.name)}</option>`).join("");
  $("#lab-zone-add-type").innerHTML = lab.catalog.scenes.zone_types.map((z) => `<option value="${esc(z.id)}">${esc(z.name)}</option>`).join("");
  renderLabSceneRow();
  $("#lab-scene-row").hidden = false;
}

function renderLabSceneRow() {
  const scene = lab.scene;
  $("#lab-preset").value = scene.preset;
  const source = { saved: "zones saved for this video", preset: "default zones for this kind of footage", bench: "this benchmark clip uses its own zones and rules until you save zones for it" }[scene.source] || "";
  $("#lab-scene-source").textContent = `${scene.zones.length} zone(s): ${scene.zones.map((z) => z.name).join(", ")} · ${source}`;
}

function labPresetDraft(presetId) {
  const preset = lab.catalog.scenes.presets.find((p) => p.id === presetId);
  return { preset: presetId, zones: preset.zones.map((z) => ({ ...z, polygon: z.polygon.map((pt) => [...pt]) })),
           rules: preset.rules.map((r) => ({ ...r, enabled: r.enabled ?? true })) };
}

function openLabSceneEditor(draft) {
  lab.draft = draft || { preset: lab.scene.preset, zones: lab.scene.zones.map((z) => ({ ...z, polygon: z.polygon.map((pt) => [...pt]) })),
                         rules: lab.scene.rule_list.map((r) => ({ ...r })) };
  lab.zoneSel = null;
  $("#lab-scene-card").hidden = false;
  const image = new Image();
  image.onload = () => {
    const canvas = $("#lab-zone-canvas");
    canvas.width = 960;
    canvas.height = Math.round(960 * (image.naturalHeight / image.naturalWidth || 9 / 16));
    drawLabZones();
  };
  image.onerror = () => toast("No picture", "The clip could not be decoded for drawing.", "high");
  image.src = `${API}/lab/clips/${encodeURIComponent(lab.clip.id)}/still.jpg?t=1`;
  lab.still = image;
  renderLabZoneList();
  renderLabRuleList();
  $("#lab-scene-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

$("#lab-scene-edit").addEventListener("click", () => openLabSceneEditor());
$("#lab-scene-close").addEventListener("click", () => { $("#lab-scene-card").hidden = true; lab.draft = null; });
$("#lab-preset").addEventListener("change", (event) => {
  const id = event.target.value;
  const preset = lab.catalog.scenes.presets.find((p) => p.id === id);
  if (!confirm(`Use the ${preset.name} scene? Its default zones replace the current ones (nothing is saved until you press Save).\n\n${preset.description}`)) {
    event.target.value = lab.draft?.preset || lab.scene.preset;
    return;
  }
  openLabSceneEditor(labPresetDraft(id));
});

function drawLabZones() {
  const canvas = $("#lab-zone-canvas");
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, W, H);
  if (lab.still?.complete && lab.still.naturalWidth) ctx.drawImage(lab.still, 0, 0, W, H);
  if (!lab.draft) return;
  lab.draft.zones.forEach((zone, i) => {
    const selected = i === lab.zoneSel;
    ctx.beginPath();
    zone.polygon.forEach(([x, y], k) => (k ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H)));
    ctx.closePath();
    ctx.fillStyle = LAB_ZONE[zone.type] || "rgba(154,163,173,.14)";
    ctx.fill();
    ctx.strokeStyle = LAB_ZONE_LINE[zone.type] || "#fff";
    ctx.lineWidth = selected ? 3 : 1.5;
    ctx.setLineDash(selected ? [] : [6, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
    const [lx, ly] = zone.polygon.reduce(([ax, ay], [x, y]) => [Math.min(ax, x), Math.min(ay, y)], [1, 1]);
    ctx.font = "600 13px system-ui, sans-serif";
    const text = `${zone.name} · ${labZoneTypeName(zone.type).split(" (")[0]}`;
    const w = ctx.measureText(text).width + 8;
    ctx.fillStyle = "rgba(0,0,0,.6)";
    ctx.fillRect(lx * W + 2, ly * H + 2, w, 18);
    ctx.fillStyle = "#fff";
    ctx.fillText(text, lx * W + 6, ly * H + 15);
    if (selected) {
      for (const [x, y] of zone.polygon) {
        ctx.fillStyle = "#fff";
        ctx.fillRect(x * W - 6, y * H - 6, 12, 12);
        ctx.strokeStyle = "#000";
        ctx.lineWidth = 1;
        ctx.strokeRect(x * W - 6, y * H - 6, 12, 12);
      }
    }
  });
  const drag = lab.zoneDrag;
  if (drag?.mode === "new" && drag.box) {
    ctx.strokeStyle = "#fff";
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 2;
    ctx.strokeRect(drag.box[0] * W, drag.box[1] * H, drag.box[2] * W, drag.box[3] * H);
    ctx.setLineDash([]);
  }
}

function labZonePoint(event) {
  const rect = $("#lab-zone-canvas").getBoundingClientRect();
  return [Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)), Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))];
}

function labInPolygon([x, y], polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i], [xj, yj] = polygon[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

const labIsRect = (p) => p.length === 4 && p[0][1] === p[1][1] && p[1][0] === p[2][0] && p[2][1] === p[3][1] && p[3][0] === p[0][0];
const labRect = (x0, y0, x1, y1) => [[Math.min(x0, x1), Math.min(y0, y1)], [Math.max(x0, x1), Math.min(y0, y1)], [Math.max(x0, x1), Math.max(y0, y1)], [Math.min(x0, x1), Math.max(y0, y1)]];
const labRound = (v) => Math.round(v * 10000) / 10000;

$("#lab-zone-add").addEventListener("click", () => {
  lab.zoneAdding = !lab.zoneAdding;
  $("#lab-zone-canvas").classList.toggle("drawing", lab.zoneAdding);
  $("#lab-zone-add").textContent = lab.zoneAdding ? "Drag on the picture... (click to cancel)" : "Draw a zone";
});

$("#lab-zone-canvas").addEventListener("pointerdown", (event) => {
  if (!lab.draft) return;
  const point = labZonePoint(event);
  const canvas = $("#lab-zone-canvas");
  canvas.setPointerCapture(event.pointerId);
  if (lab.zoneAdding) { lab.zoneDrag = { mode: "new", start: point }; return; }
  const rect = canvas.getBoundingClientRect();
  const near = 10 / rect.width;
  const selected = lab.draft.zones[lab.zoneSel];
  const vertex = selected ? selected.polygon.findIndex(([x, y]) => Math.abs(x - point[0]) <= near && Math.abs(y - point[1]) <= near * (rect.width / rect.height)) : -1;
  if (vertex >= 0) {
    lab.zoneDrag = { mode: "vertex", index: vertex, rect: labIsRect(selected.polygon) };
    return;
  }
  for (let i = lab.draft.zones.length - 1; i >= 0; i--) {
    if (labInPolygon(point, lab.draft.zones[i].polygon)) {
      lab.zoneSel = i;
      lab.zoneDrag = { mode: "move", last: point };
      renderLabZoneList();
      return drawLabZones();
    }
  }
  lab.zoneSel = null;
  renderLabZoneList();
  drawLabZones();
});

$("#lab-zone-canvas").addEventListener("pointermove", (event) => {
  const drag = lab.zoneDrag;
  if (!drag) return;
  const [x, y] = labZonePoint(event);
  const zone = lab.draft.zones[lab.zoneSel];
  if (drag.mode === "new") {
    const [x0, y0] = drag.start;
    drag.box = [Math.min(x, x0), Math.min(y, y0), Math.abs(x - x0), Math.abs(y - y0)];
  } else if (drag.mode === "vertex" && zone) {
    if (drag.rect) { // a rectangle stays a rectangle: the opposite corner is the anchor
      const [ox, oy] = zone.polygon[(drag.index + 2) % 4];
      zone.polygon = labRect(ox, oy, x, y).map((pt) => pt.map(labRound));
      drag.index = zone.polygon.findIndex(([px, py]) => px === labRound(x) && py === labRound(y));
      if (drag.index < 0) drag.index = 0;
    } else {
      zone.polygon[drag.index] = [labRound(x), labRound(y)];
    }
  } else if (drag.mode === "move" && zone) {
    let dx = x - drag.last[0], dy = y - drag.last[1];
    const xs = zone.polygon.map((p) => p[0]), ys = zone.polygon.map((p) => p[1]);
    dx = Math.max(-Math.min(...xs), Math.min(1 - Math.max(...xs), dx));
    dy = Math.max(-Math.min(...ys), Math.min(1 - Math.max(...ys), dy));
    zone.polygon = zone.polygon.map(([px, py]) => [labRound(px + dx), labRound(py + dy)]);
    drag.last = [x, y];
  }
  drawLabZones();
});

$("#lab-zone-canvas").addEventListener("pointerup", () => {
  const drag = lab.zoneDrag;
  lab.zoneDrag = null;
  if (drag?.mode === "new") {
    lab.zoneAdding = false;
    $("#lab-zone-canvas").classList.remove("drawing");
    $("#lab-zone-add").textContent = "Draw a zone";
    const box = drag.box;
    if (box && box[2] > 0.02 && box[3] > 0.02) {
      const type = $("#lab-zone-add-type").value;
      const count = lab.draft.zones.filter((z) => z.type === type).length + 1;
      lab.draft.zones.push({ name: `${labZoneTypeName(type).split(" (")[0]} ${count}`, type, polygon: labRect(box[0], box[1], box[0] + box[2], box[1] + box[3]).map((pt) => pt.map(labRound)) });
      lab.zoneSel = lab.draft.zones.length - 1;
    }
  }
  renderLabZoneList();
  drawLabZones();
});

function renderLabZoneList() {
  if (!lab.draft) return;
  const types = lab.catalog.scenes.zone_types;
  $("#lab-zone-list").innerHTML = lab.draft.zones.map((zone, i) => `
    <div class="lab-zone-item ${i === lab.zoneSel ? "selected" : ""}" data-zone="${i}">
      <span class="swatch" style="background:${LAB_ZONE_LINE[zone.type] || "#999"}"></span>
      <input type="text" data-field="name" value="${esc(zone.name)}" maxlength="40" aria-label="Zone name">
      <select data-field="type" aria-label="Zone type">${types.map((t) => `<option value="${esc(t.id)}" ${t.id === zone.type ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select>
      <button type="button" data-remove="${i}">Delete</button>
    </div>`).join("") || '<p class="muted small">No zones yet: draw one.</p>';
}

$("#lab-zone-list").addEventListener("input", (event) => {
  const item = event.target.closest("[data-zone]");
  if (!item) return;
  const zone = lab.draft.zones[Number(item.dataset.zone)];
  zone[event.target.dataset.field] = event.target.value;
  if (event.target.dataset.field === "type") renderLabZoneList();
  drawLabZones();
});
$("#lab-zone-list").addEventListener("click", (event) => {
  const remove = event.target.closest("[data-remove]");
  if (remove) {
    lab.draft.zones.splice(Number(remove.dataset.remove), 1);
    lab.zoneSel = null;
  } else {
    const item = event.target.closest("[data-zone]");
    if (!item) return;
    lab.zoneSel = Number(item.dataset.zone);
  }
  renderLabZoneList();
  drawLabZones();
});

function renderLabRuleList() {
  const preset = lab.catalog.scenes.presets.find((p) => p.id === lab.draft.preset);
  $("#lab-preset-name").textContent = preset?.name || lab.draft.preset;
  const types = (list) => (list || []).map((t) => labZoneTypeName(t).split(" (")[0].toLowerCase()).join(" / ");
  $("#lab-rule-list").innerHTML = lab.draft.rules.map((rule, i) => `
    <div class="lab-rule-item" data-rule="${i}">
      <label class="check"><input type="checkbox" data-field="enabled" ${rule.enabled !== false ? "checked" : ""}> <b>${esc(rule.name)}</b></label>
      <select data-field="severity" aria-label="Severity">${["info", "low", "medium", "high"].map((sv) => `<option ${sv === rule.severity ? "selected" : ""}>${sv}</option>`).join("")}</select>
      ${rule.type === "dwell" ? `<label class="small">after <input type="number" min="5" max="3600" step="5" data-field="min_dwell_s" value="${esc(rule.min_dwell_s)}"> s</label>` : ""}
      ${rule.active_hours ? `<label class="small">from <input type="time" data-field="start" value="${esc(rule.active_hours.start)}"></label><label class="small">to <input type="time" data-field="end" value="${esc(rule.active_hours.end)}"></label>` : ""}
      <span class="grow small muted">${esc(rule.type.replace("_", " "))} in ${esc(types(rule.zone_types)) || "any zone"} areas</span>
    </div>`).join("");
}

$("#lab-rule-list").addEventListener("input", (event) => {
  const item = event.target.closest("[data-rule]");
  if (!item) return;
  const rule = lab.draft.rules[Number(item.dataset.rule)];
  const field = event.target.dataset.field;
  if (field === "enabled") rule.enabled = event.target.checked;
  else if (field === "severity") rule.severity = event.target.value;
  else if (field === "min_dwell_s") rule.min_dwell_s = Number(event.target.value);
  else if (field === "start" || field === "end") rule.active_hours = { ...rule.active_hours, [field]: event.target.value };
});

$("#lab-scene-save").addEventListener("click", async () => {
  const rules = {};
  for (const rule of lab.draft.rules) {
    rules[rule.id] = { enabled: rule.enabled !== false, severity: rule.severity };
    if (rule.type === "dwell") rules[rule.id].min_dwell_s = rule.min_dwell_s;
    if (rule.active_hours) rules[rule.id].active_hours = rule.active_hours;
  }
  try {
    lab.scene = await labApi(`/clips/${encodeURIComponent(lab.clip.id)}/scene`, "PUT",
      { preset: lab.draft.preset, zones: lab.draft.zones.map(({ name, type, polygon }) => ({ name, type, polygon })), rules });
    renderLabSceneRow();
    openLabSceneEditor();
    toast("Saved for this video", "Press Analyse and play to test the zones. Live cameras are unchanged.", "info");
  } catch (error) {
    toast("Not saved", error.message, "high");
  }
});

$("#lab-scene-reset").addEventListener("click", async () => {
  if (!confirm("Remove the zones saved for this video and go back to the default scene?")) return;
  try {
    lab.scene = await labApi(`/clips/${encodeURIComponent(lab.clip.id)}/scene`, "DELETE");
    renderLabSceneRow();
    openLabSceneEditor();
  } catch (error) {
    toast("Not done", error.message, "high");
  }
});
