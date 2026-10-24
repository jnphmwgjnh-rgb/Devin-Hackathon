import * as THREE from "three";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const state = {
  runs: new Map(),
  summary: null,
  filter: "all",
  liveRunId: null,
  modalRunId: null,
  latestCompletedId: null,
};

const $ = (id) => document.getElementById(id);
const COLORS = { accent: "#ff9f43", blue: "#4dabf7", muted: "#5b6878", grid: "#2a3340", text: "#e6edf3", bad: "#f85149" };
const RESIDUAL_COLORS = ["#4dabf7", "#ff9f43", "#3fb950", "#d2a8ff", "#f85149", "#e3b341", "#39c5cf"];

// ---------- formatting ----------
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
const fmtInt = (v) => (v === null || v === undefined ? "–" : Number(v).toLocaleString());
function fmtDuration(s) {
  if (s === null || s === undefined) return "–";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}
const timeNow = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hasMetrics = (r) => r.metrics && r.metrics.pressure_drop_pa != null && r.metrics.max_temperature_k != null;

const PARAM_LABELS = {
  fin_count: ["Fin count", 0, ""],
  fin_height_mm: ["Fin height", 2, "mm"],
  fin_thickness_mm: ["Fin thickness", 2, "mm"],
  base_thickness_mm: ["Base thickness", 2, "mm"],
  inlet_velocity_m_s: ["Inlet velocity", 2, "m/s"],
};
function fmtParam(key, value) {
  const spec = PARAM_LABELS[key];
  if (value && typeof value === "object") {
    const text = Object.entries(value).map(([k, v]) => `${k}: ${typeof v === "number" ? fmt(v, 2) : v}`).join(", ");
    return { label: spec ? spec[0] : key, text: spec?.[2] ? `${text} ${spec[2]}` : text };
  }
  if (!spec) return { label: key, text: typeof value === "number" ? fmt(value, 3) : String(value) };
  return { label: spec[0], text: `${typeof value === "number" ? fmt(value, spec[1]) : value}${spec[2] ? " " + spec[2] : ""}` };
}

// ---------- live connection ----------
let ws;
let reconnectDelay = 1000;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { reconnectDelay = 1000; setConn(true); };
  ws.onclose = () => { setConn(false); setTimeout(connect, reconnectDelay); reconnectDelay = Math.min(reconnectDelay * 2, 15000); };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => handleMessage(JSON.parse(ev.data));
}
function setConn(up) {
  $("conn").className = `conn ${up ? "conn-up" : "conn-down"}`;
  $("conn").querySelector(".conn-text").textContent = up ? "Live" : "Reconnecting…";
}

function handleMessage(msg) {
  if (msg.summary) state.summary = msg.summary;
  switch (msg.type) {
    case "snapshot":
      state.runs = new Map(msg.runs.map((r) => [r.id, r]));
      break;
    case "run_updated":
      upsert(msg.run);
      break;
    case "run_completed":
      upsert(msg.run);
      state.latestCompletedId = msg.run.id;
      onRunCompleted(msg.run, msg.joined_pareto);
      break;
    case "run_failed":
      upsert(msg.run);
      onRunFailed(msg.run);
      break;
  }
  scheduleRender();
  if (state.modalRunId && msg.run && msg.run.id === state.modalRunId) refreshModal(false);
}
function upsert(run) {
  const prev = state.runs.get(run.id);
  state.runs.set(run.id, run);
  if (!prev && run.status === "running") addActivity("▶", `${run.id} started`, run.id);
}

// ---------- notifications ----------
function notifyMode() { return $("notify-mode").value; }

function onRunCompleted(run, joinedPareto) {
  const m = run.metrics || {};
  const body = `ΔP ${fmt(m.pressure_drop_pa)} Pa · Tmax ${fmt(m.max_temperature_k)} K · ${fmtDuration(run.runtime_s)}`;
  addActivity(joinedPareto ? "★" : "✓", `${run.id} finished${joinedPareto ? " — new Pareto design" : ""}`, run.id);
  flashRow(run.id);
  const mode = notifyMode();
  if (mode === "off" || (mode === "pareto" && !joinedPareto)) return;
  const title = joinedPareto ? `★ ${run.id} joined the Pareto front` : `${run.id} finished`;
  showToast({ title, body, runId: run.id, kind: joinedPareto ? "pareto" : "" });
  desktopNotify(title, body, run.id);
}
function onRunFailed(run) {
  addActivity("✕", `${run.id} failed`, run.id);
  if (notifyMode() === "all") showToast({ title: `${run.id} failed`, body: firstLine(run.error) || "Solver error", runId: run.id, kind: "failed" });
}
const firstLine = (s) => (s ? String(s).split("\n").find((l) => l.trim()) || "" : "");

function showToast({ title, body, runId, kind }) {
  const el = document.createElement("div");
  el.className = `toast ${kind || ""}`;
  el.innerHTML = `<div class="toast-title">${esc(title)}</div><div class="toast-body">${esc(body)}</div>
    <div class="toast-actions"><button class="btn btn-small btn-ghost" data-act="dismiss">Dismiss</button>
    <button class="btn btn-small" data-act="view">View details</button></div>`;
  el.querySelector('[data-act="dismiss"]').onclick = () => el.remove();
  el.querySelector('[data-act="view"]').onclick = () => { el.remove(); openModal(runId); };
  $("toasts").prepend(el);
  while ($("toasts").children.length > 4) $("toasts").lastChild.remove();
  setTimeout(() => el.remove(), 12000);
}
function desktopNotify(title, body, runId) {
  if (!("Notification" in window) || Notification.permission !== "granted" || !document.hidden) return;
  const n = new Notification(title, { body, tag: runId });
  n.onclick = () => { window.focus(); openModal(runId); n.close(); };
}
function initNotificationButton() {
  const btn = $("enable-notifications");
  const sync = () => {
    if (!("Notification" in window)) { btn.textContent = "Desktop alerts unsupported"; btn.disabled = true; return; }
    if (Notification.permission === "granted") { btn.textContent = "Desktop alerts on"; btn.disabled = true; }
    else if (Notification.permission === "denied") { btn.textContent = "Desktop alerts blocked"; btn.disabled = true; }
  };
  btn.onclick = () => Notification.requestPermission().then(sync);
  sync();
  const saved = localStorage.getItem("notifyMode");
  if (saved) $("notify-mode").value = saved;
  $("notify-mode").onchange = (e) => localStorage.setItem("notifyMode", e.target.value);
}

// ---------- activity ----------
function addActivity(icon, text, runId) {
  const list = $("activity");
  list.querySelector(".empty")?.remove();
  const li = document.createElement("li");
  if (runId) { li.className = "link"; li.onclick = () => openModal(runId); }
  li.innerHTML = `<span class="time">${timeNow()}</span><span class="ico">${icon}</span><span class="msg">${esc(text)}</span>`;
  list.prepend(li);
  while (list.children.length > 50) list.lastChild.remove();
}

// ---------- rendering ----------
let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; render(); });
}
function render() {
  renderStats();
  renderPareto();
  renderRunning();
  renderTable();
}
const sortedRuns = () => [...state.runs.values()].sort((a, b) => a.index - b.index);

function renderStats() {
  const s = state.summary;
  if (!s) return;
  const done = s.completed + s.failed;
  $("stat-progress").textContent = s.budget ? `${done} / ${s.budget} runs` : `${done} runs`;
  $("stat-progress-bar").style.width = s.budget ? `${Math.min(100, (100 * done) / s.budget)}%` : "0";
  $("stat-running").textContent = s.running;
  $("stat-failed").textContent = s.failed;
  $("stat-pareto").textContent = s.pareto_ids.length;
  const bestDp = s.best.pressure_drop_pa && state.runs.get(s.best.pressure_drop_pa);
  const bestT = s.best.max_temperature_k && state.runs.get(s.best.max_temperature_k);
  $("stat-best-dp").textContent = bestDp ? `${fmt(bestDp.metrics.pressure_drop_pa)} Pa` : "–";
  $("stat-best-dp-id").textContent = bestDp ? bestDp.id : "";
  $("stat-best-t").textContent = bestT ? `${fmt(bestT.metrics.max_temperature_k)} K` : "–";
  $("stat-best-t-id").textContent = bestT ? bestT.id : "";
  $("stat-best-dp-wrap").onclick = bestDp ? () => openModal(bestDp.id) : null;
  $("stat-best-t-wrap").onclick = bestT ? () => openModal(bestT.id) : null;
  const finished = s.budget && done >= s.budget;
  $("stat-eta").textContent = finished ? "Done" : fmtDuration(s.eta_s);
  $("stat-avg").textContent = s.avg_runtime_s ? `avg ${fmtDuration(s.avg_runtime_s)} / run` : "";
}

let paretoInit = false;
function renderPareto() {
  const runs = sortedRuns();
  const completed = runs.filter((r) => r.status === "completed" && hasMetrics(r));
  const front = completed.filter((r) => r.on_pareto).sort((a, b) => a.metrics.pressure_drop_pa - b.metrics.pressure_drop_pa);
  const dominated = completed.filter((r) => !r.on_pareto);
  const running = runs.filter((r) => r.status === "running" && hasMetrics(r));
  const latest = state.latestCompletedId && state.runs.get(state.latestCompletedId);

  const hover = "<b>%{customdata[0]}</b><br>ΔP %{x:.1f} Pa<br>Tmax %{y:.1f} K<extra></extra>";
  const pts = (list) => ({
    x: list.map((r) => r.metrics.pressure_drop_pa),
    y: list.map((r) => r.metrics.max_temperature_k),
    customdata: list.map((r) => [r.id]),
  });
  const traces = [
    { ...pts(dominated), name: "Dominated", mode: "markers", type: "scatter", hovertemplate: hover,
      marker: { size: 9, color: "rgba(139,152,168,.55)", line: { width: 1, color: "#8b98a8" } } },
    { ...pts(front), name: "Pareto front", mode: "lines+markers", type: "scatter", hovertemplate: hover,
      line: { shape: "hv", color: COLORS.accent, width: 2, dash: "dot" },
      marker: { size: 12, color: COLORS.accent, symbol: "star", line: { width: 1, color: "#fff" } } },
    { ...pts(running), name: "Running (live estimate)", mode: "markers", type: "scatter",
      hovertemplate: "<b>%{customdata[0]}</b> (running)<br>ΔP %{x:.1f} Pa<br>Tmax %{y:.1f} K<extra></extra>",
      marker: { size: 11, color: "rgba(0,0,0,0)", line: { width: 2, color: COLORS.blue } } },
  ];
  if (latest && hasMetrics(latest)) {
    traces.push({ ...pts([latest]), name: "Latest result", mode: "markers", type: "scatter", hoverinfo: "skip",
      marker: { size: 22, color: "rgba(0,0,0,0)", line: { width: 2, color: "#ffffff" } } });
  }
  const layout = {
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: COLORS.text, size: 12 },
    margin: { l: 60, r: 20, t: 10, b: 50 },
    xaxis: { title: "Pressure drop ΔP [Pa]", gridcolor: COLORS.grid, zerolinecolor: COLORS.grid },
    yaxis: { title: "Max chip temperature [K]", gridcolor: COLORS.grid, zerolinecolor: COLORS.grid },
    legend: { orientation: "h", y: 1.06, x: 0 },
    hovermode: "closest", uirevision: "pareto",
    annotations: completed.length ? [] : [{ text: "Waiting for the first finished run…", showarrow: false, font: { color: COLORS.muted, size: 14 }, xref: "paper", yref: "paper", x: 0.5, y: 0.5 }],
  };
  Plotly.react("pareto-plot", traces, layout, { displaylogo: false, responsive: true });
  if (!paretoInit) {
    paretoInit = true;
    $("pareto-plot").on("plotly_click", (ev) => {
      const id = ev.points?.[0]?.customdata?.[0];
      if (id) openModal(id);
    });
  }
}

function renderRunning() {
  const running = sortedRuns().filter((r) => r.status === "running");
  const list = $("running-list");
  if (!running.length) {
    list.innerHTML = `<p class="empty">No runs in progress.</p>`;
  } else {
    if (!running.find((r) => r.id === state.liveRunId)) state.liveRunId = running[0].id;
    list.innerHTML = running.map((r) => {
      const pct = r.end_iteration ? Math.min(100, (100 * (r.iteration || 0)) / r.end_iteration) : 0;
      return `<div class="running-item ${r.id === state.liveRunId ? "selected" : ""}" data-id="${esc(r.id)}">
        <span class="pulse"></span><span class="rid">${esc(r.id)}</span>
        <span class="iter">iter ${fmtInt(r.iteration)} / ${fmtInt(r.end_iteration)} · ${fmtDuration(r.runtime_s)}</span>
        <div class="progress"><div class="progress-bar" style="width:${pct}%"></div></div>
        <div class="live-metrics">ΔP ${fmt(r.metrics?.pressure_drop_pa)} Pa · Tmax ${fmt(r.metrics?.max_temperature_k)} K (converging)</div>
      </div>`;
    }).join("");
    list.querySelectorAll(".running-item").forEach((el) => {
      el.onclick = () => { state.liveRunId = el.dataset.id; renderRunning(); pollLiveResiduals(); };
      el.ondblclick = () => openModal(el.dataset.id);
    });
  }
  if (!running.length && state.liveRunId) { state.liveRunId = null; }
}

function renderTable() {
  const filter = state.filter;
  const rows = sortedRuns().reverse().filter((r) =>
    filter === "all" ? true : filter === "pareto" ? r.on_pareto : r.status === filter);
  const body = $("runs-body");
  if (!rows.length) { body.innerHTML = `<tr><td colspan="9" class="empty">No runs match this filter.</td></tr>`; return; }
  body.innerHTML = rows.map((r) => {
    const m = r.metrics || {}, p = r.params || {};
    return `<tr data-id="${esc(r.id)}">
      <td>${r.on_pareto ? '<span class="star">★</span> ' : ""}${esc(r.id)}</td>
      <td>${statusBadge(r)}</td>
      <td class="num">${fmt(m.pressure_drop_pa)}</td>
      <td class="num">${fmt(m.max_temperature_k)}</td>
      <td class="num">${fmt(m.mean_temperature_k)}</td>
      <td class="num">${p.fin_count ?? "–"}</td>
      <td class="num">${fmt(p.fin_height_mm, 1)}</td>
      <td class="num">${fmt(p.inlet_velocity_m_s, 2)}</td>
      <td class="num">${fmtDuration(r.runtime_s)}</td>
    </tr>`;
  }).join("");
  body.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => openModal(tr.dataset.id)));
}
function statusBadge(r) {
  return `<span class="status status-${esc(r.status)}">${esc(r.status)}</span>`;
}
function flashRow(id) {
  requestAnimationFrame(() => {
    const tr = document.querySelector(`#runs-body tr[data-id="${CSS.escape(id)}"]`);
    if (tr) { tr.classList.remove("flash"); void tr.offsetWidth; tr.classList.add("flash"); }
  });
}
function initFilters() {
  $("filters").querySelectorAll(".chip").forEach((chip) => {
    chip.onclick = () => {
      state.filter = chip.dataset.filter;
      $("filters").querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c === chip));
      renderTable();
    };
  });
}

// ---------- residual plots ----------
function residualTraces(res) {
  if (!res || !res.iterations?.length) return [];
  return Object.entries(res.fields).map(([name, vals], i) => ({
    x: res.iterations, y: vals, name, type: "scatter", mode: "lines",
    line: { width: 1.5, color: RESIDUAL_COLORS[i % RESIDUAL_COLORS.length] },
  }));
}
function residualLayout(emptyText) {
  return {
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: COLORS.text, size: 11 }, margin: { l: 50, r: 10, t: 6, b: 34 },
    xaxis: { title: "Iteration", gridcolor: COLORS.grid, zerolinecolor: COLORS.grid },
    yaxis: { type: "log", gridcolor: COLORS.grid, exponentformat: "e", zerolinecolor: COLORS.grid },
    legend: { orientation: "h", y: 1.15, x: 0, font: { size: 10 } }, uirevision: "res",
    annotations: emptyText ? [{ text: emptyText, showarrow: false, font: { color: COLORS.muted }, xref: "paper", yref: "paper", x: 0.5, y: 0.5 }] : [],
  };
}
async function fetchRun(id) {
  const r = await fetch(`/api/runs/${encodeURIComponent(id)}`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
let livePollBusy = false;
async function pollLiveResiduals() {
  if (livePollBusy) return;
  const id = state.liveRunId;
  $("live-run-label").textContent = id || "";
  if (!id) { Plotly.react("live-residuals", [], residualLayout("No run in progress"), { displayModeBar: false, responsive: true }); return; }
  livePollBusy = true;
  try {
    const run = await fetchRun(id);
    if (state.liveRunId === id) Plotly.react("live-residuals", residualTraces(run.residuals), residualLayout(run.residuals?.iterations?.length ? "" : "Waiting for solver output…"), { displayModeBar: false, responsive: true });
  } catch (_) { /* transient */ } finally { livePollBusy = false; }
}

// ---------- modal ----------
let viewer = null;
async function openModal(id) {
  state.modalRunId = id;
  $("modal").classList.remove("hidden");
  document.body.style.overflow = "hidden";
  await refreshModal(true);
}
function closeModal() {
  state.modalRunId = null;
  $("modal").classList.add("hidden");
  document.body.style.overflow = "";
  disposeViewer();
}
function stepModal(delta) {
  const runs = sortedRuns();
  const i = runs.findIndex((r) => r.id === state.modalRunId);
  const next = runs[i + delta];
  if (next) { disposeViewer(); openModal(next.id); }
}

let modalRefreshBusy = false;
async function refreshModal(loadGeometry) {
  const id = state.modalRunId;
  if (!id || (modalRefreshBusy && !loadGeometry)) return;
  modalRefreshBusy = true;
  let run;
  try { run = await fetchRun(id); } catch (_) { run = state.runs.get(id); } finally { modalRefreshBusy = false; }
  if (!run || state.modalRunId !== id) return;

  $("modal-title").textContent = run.id;
  const badges = [statusBadge(run)];
  if (run.on_pareto) badges.push('<span class="status status-pareto">★ Pareto optimal</span>');
  if (run.converged === true) badges.push('<span class="status status-completed">Converged</span>');
  if (run.converged === false) badges.push('<span class="status status-warn">Not converged</span>');
  $("modal-badges").innerHTML = badges.join("");

  const err = $("modal-error");
  err.classList.toggle("hidden", !run.error);
  err.textContent = run.error || "";

  const m = run.metrics || {};
  const live = run.status === "running" ? '<div class="sub">live, still converging</div>' : "";
  $("modal-metrics").innerHTML = [
    ["Pressure drop", fmt(m.pressure_drop_pa), "Pa", ""],
    ["Max temperature", fmt(m.max_temperature_k), "K", m.max_temperature_k != null ? `${fmt(m.max_temperature_k - 273.15)} °C` : ""],
    ["Mean temperature", fmt(m.mean_temperature_k), "K", m.mean_temperature_k != null ? `${fmt(m.mean_temperature_k - 273.15)} °C` : ""],
  ].map(([l, v, u, sub]) => `<div class="metric"><div class="label">${l}</div><div class="value">${v} <span class="unit">${u}</span></div>${sub ? `<div class="sub">${sub}</div>` : live}</div>`).join("");

  $("modal-compare").innerHTML = compareText(run);

  const params = Object.entries(run.params || {});
  $("modal-params").innerHTML = params.length
    ? params.map(([k, v]) => { const f = fmtParam(k, v); return `<tr><td>${esc(f.label)}</td><td>${esc(f.text)}</td></tr>`; }).join("")
    : `<tr><td colspan="2" class="empty">No params.json found</td></tr>`;

  $("modal-solver").innerHTML = [
    ["Solver", run.solver || "–"],
    ["Mesh cells", fmtInt(run.mesh_cells)],
    ["Iterations", `${fmtInt(run.iteration)} / ${fmtInt(run.end_iteration)}`],
    ["Runtime", fmtDuration(run.runtime_s)],
    ["Started", run.started_at ? new Date(run.started_at).toLocaleString() : "–"],
    ["Finished", run.finished_at ? new Date(run.finished_at).toLocaleString() : "–"],
  ].map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("");

  const det = run.details || {};
  if (det.source === "stage3-reduced-order-model") {
    Plotly.react("modal-residual-plot", [],
      residualLayout("Reduced-order model — no solver residuals"),
      { displayModeBar: false, responsive: true });
    const fnum = (v) => v == null ? "–" : Number(v).toFixed(1);
    $("modal-compare").innerHTML =
      `<div class="rom-details">` +
      `<div class="metric"><div class="label">GPU max (hottest)</div><div class="value">${fnum(det.gpu_max_c)} °C <span class="sub">${esc(det.gpu_hottest || "")}</span></div></div>` +
      `<div class="metric"><div class="label">HBM max (hottest)</div><div class="value">${fnum(det.hbm_max_c)} °C <span class="sub">${esc(det.hbm_hottest || "")}</span></div></div>` +
      `<div class="metric"><div class="label">Coolant rise</div><div class="value">${fnum(det.coolant_rise_k)} K</div></div>` +
      `<div class="metric"><div class="label">Feasible</div><div class="value">${det.feasible === false ? "No" : det.feasible === true ? "Yes" : "–"}</div></div>` +
      (det.role ? `<div class="metric"><div class="label">Role</div><div class="value">${esc(det.role.replaceAll("_", " "))}</div></div>` : "") +
      `</div>` +
      ((det.violations || []).length ? `<div class="campaign-error">${det.violations.map((v) => esc(`${v.constraint || ""}:${v.component || ""} ${v.value}>${v.limit}`)).join("; ")}</div>` : "") +
      `<div class="rom-downloads">` +
      (run.has_geometry ? `<a class="cad-download" href="/api/runs/${encodeURIComponent(run.id)}/geometry" download="${esc(run.id)}.stl">Download CAD (.stl)</a>` : "") +
      ((det.files || []).includes("design.step") ? `<a class="cad-download" href="/api/runs/${encodeURIComponent(run.id)}/files/design.step" download="${esc(run.id)}.step">Download CAD (.step)</a>` : "") +
      `</div>` +
      `<div class="rom-files">` +
      (det.files || []).map((n) => `<a href="/api/runs/${encodeURIComponent(run.id)}/files/${n}">${esc(n)}</a>`).join(" ") +
      `</div>`;
  } else {
    Plotly.react("modal-residual-plot", residualTraces(run.residuals),
      residualLayout(run.residuals?.iterations?.length ? "" : "No residual data"), { displayModeBar: false, responsive: true });
  }

  if (loadGeometry) loadViewer(run);
}

function compareText(run) {
  if (!hasMetrics(run) || run.status !== "completed") return "";
  const others = [...state.runs.values()].filter((r) => r.id !== run.id && r.status === "completed" && hasMetrics(r));
  if (!others.length) return "First completed design so far.";
  const dp = run.metrics.pressure_drop_pa, t = run.metrics.max_temperature_k;
  const dominators = others.filter((o) => o.metrics.pressure_drop_pa <= dp && o.metrics.max_temperature_k <= t &&
    (o.metrics.pressure_drop_pa < dp || o.metrics.max_temperature_k < t));
  const rankDp = 1 + others.filter((o) => o.metrics.pressure_drop_pa < dp).length;
  const rankT = 1 + others.filter((o) => o.metrics.max_temperature_k < t).length;
  const n = others.length + 1;
  let txt = `Ranks <strong>#${rankDp}</strong> of ${n} on pressure drop and <strong>#${rankT}</strong> of ${n} on max temperature. `;
  txt += dominators.length
    ? `Dominated by <strong>${dominators.length}</strong> design${dominators.length > 1 ? "s" : ""} (e.g. ${dominators.slice(0, 3).map((d) => esc(d.id)).join(", ")}).`
    : "No other design beats it on both metrics.";
  return txt;
}

// ---------- 3D viewer ----------
function disposeViewer() {
  if (!viewer) return;
  cancelAnimationFrame(viewer.raf);
  viewer.resizeObserver.disconnect();
  viewer.controls.dispose();
  viewer.scene.traverse((o) => { o.geometry?.dispose(); o.material?.dispose?.(); });
  viewer.renderer.dispose();
  viewer.renderer.domElement.remove();
  viewer = null;
}
async function loadViewer(run) {
  disposeViewer();
  const el = $("viewer");
  el.innerHTML = "";
  if (!run.has_geometry) { el.textContent = "No CAD geometry for this run"; return; }
  el.textContent = "Loading geometry…";
  let buf;
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(run.id)}/geometry`);
    if (!res.ok) throw new Error();
    buf = await res.arrayBuffer();
  } catch (_) { el.textContent = "Could not load geometry"; return; }
  if (state.modalRunId !== run.id) return;
  el.textContent = "";

  const geometry = new STLLoader().parse(buf);
  geometry.rotateX(-Math.PI / 2);
  geometry.computeBoundingBox();
  const box = geometry.boundingBox;
  const center = box.getCenter(new THREE.Vector3());
  geometry.translate(-center.x, -box.min.y, -center.z);
  geometry.computeVertexNormals();
  const size = box.getSize(new THREE.Vector3()).length();

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, el.clientWidth / el.clientHeight, size / 100, size * 20);
  camera.position.set(size * 0.8, size * 0.6, size * 0.9);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(el.clientWidth, el.clientHeight);
  el.appendChild(renderer.domElement);

  scene.add(new THREE.HemisphereLight(0xdde8ff, 0x202530, 1.1));
  const key = new THREE.DirectionalLight(0xffffff, 1.6); key.position.set(1, 2, 1.5); scene.add(key);
  const rim = new THREE.DirectionalLight(0x88aaff, 0.6); rim.position.set(-1.5, 1, -1); scene.add(rim);

  const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color: 0xb8c4d0, metalness: 0.55, roughness: 0.35 }));
  scene.add(mesh);
  const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 30), new THREE.LineBasicMaterial({ color: 0x2b3a4a, transparent: true, opacity: 0.6 }));
  scene.add(edges);
  const grid = new THREE.GridHelper(size * 1.5, 20, 0x3a4656, 0x222b36);
  scene.add(grid);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, (box.max.y - box.min.y) * 0.25, 0);
  controls.enableDamping = true;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 1.2;
  renderer.domElement.addEventListener("pointerdown", () => (controls.autoRotate = false), { once: true });

  const resizeObserver = new ResizeObserver(() => {
    if (!el.clientWidth) return;
    camera.aspect = el.clientWidth / el.clientHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(el.clientWidth, el.clientHeight);
  });
  resizeObserver.observe(el);

  viewer = { scene, renderer, controls, resizeObserver, raf: 0 };
  const tick = () => { controls.update(); renderer.render(scene, camera); viewer.raf = requestAnimationFrame(tick); };
  tick();
}

function initModal() {
  $("modal").querySelectorAll("[data-close]").forEach((el) => (el.onclick = closeModal));
  $("modal-prev").onclick = () => stepModal(-1);
  $("modal-next").onclick = () => stepModal(1);
  document.addEventListener("keydown", (e) => {
    if (!state.modalRunId) return;
    if (e.key === "Escape") closeModal();
    if (e.key === "ArrowLeft") stepModal(-1);
    if (e.key === "ArrowRight") stepModal(1);
  });
}

// ---------- boot ----------
async function boot() {
  initNotificationButton();
  initFilters();
  initModal();
  try {
    const res = await fetch("/api/runs");
    if (res.ok) handleMessage({ type: "snapshot", ...(await res.json()) });
  } catch (_) { /* WS snapshot will fill in */ }
  render();
  connect();
  pollLiveResiduals();
  setInterval(pollLiveResiduals, 3000);
  setInterval(() => { if (state.modalRunId && state.runs.get(state.modalRunId)?.status === "running") refreshModal(false); }, 3000);
}
boot();

// ---- stage-3 modelling campaign ------------------------------------------
const campaign = { data: null, timer: null };
const STEP_LABELS = [
  ["interpreting", "Interpreting requirements with Devin"],
  ["generating", "Generating geometry"],
  ["evaluating", "Simulating candidates"],
  ["selecting", "Selecting designs"],
  ["exporting", "Exporting CAD"],
];

async function pollCampaign() {
  try {
    const r = await fetch("/api/modelling/status");
    campaign.data = await r.json();
  } catch (e) { campaign.data = null; }
  renderCampaign();
  const s = campaign.data;
  if (s && (s.status === "running" || s.status === "queued")) {
    if (!campaign.timer) campaign.timer = setInterval(pollCampaign, 1000);
  } else if (campaign.timer) {
    clearInterval(campaign.timer); campaign.timer = null;
  }
}

function renderCampaign() {
  const bar = $("campaign-bar"), ov = $("campaign-overlay");
  const s = campaign.data;
  const hasRuns = [...state.runs.values()].some((r) => r.status === "completed");
  if (!s || s.status === "idle") {
    bar.classList.add("hidden"); ov.classList.add("hidden"); return;
  }
  bar.classList.remove("hidden");
  $("campaign-mode").textContent = s.mode === "demo" ? s.demo_note : "Live computation";
  $("campaign-stage").textContent = s.stage ? `${s.stage} — ${s.message || ""}` : "";
  $("campaign-fill").style.width = `${Math.round((s.progress || 0) * 100)}%`;
  const it = s.interpretation || {};
  let devin = "";
  if (it.provider_label) {
    devin = `Requirements interpreted by ${it.provider_label}`;
    if (it.session_url) devin = `<a href="${it.session_url}" target="_blank" rel="noreferrer">Requirements interpreted by Devin</a>`;
    if (it.cached) devin += " (cached)";
    if (s.mode === "demo") devin += " · replayed";
  }
  $("campaign-devin").innerHTML = devin;
  $("campaign-label").textContent = s.label || "";
  $("campaign-error").textContent = s.error || "";
  bar.classList.toggle("failed", s.status === "failed");
  bar.classList.toggle("done", s.status === "completed");

  // overlay only while active and no completed runs yet
  const active = s.status === "running" || s.status === "queued";
  if (active && !hasRuns) {
    ov.classList.remove("hidden");
    const steps = $("campaign-steps");
    if (!steps.children.length) {
      for (const [, label] of STEP_LABELS) {
        const li = document.createElement("li");
        li.textContent = label; steps.appendChild(li);
      }
    }
    const cur = STEP_LABELS.findIndex(([k]) => k === s.stage);
    [...steps.children].forEach((li, i) =>
      li.classList.toggle("current", i === cur));
  } else {
    ov.classList.add("hidden");
  }
  // temperature label
  const tl = s.temperature_label;
  if (tl) {
    { const w = document.querySelector("#stat-best-t-wrap .stat-label");
      if (w) w.textContent = tl; }
    const th = $("th-tmax"); if (th) th.textContent = tl;
  }
}

// wrap the existing pareto render to add the selected-designs trace + label
const _origRenderPareto = renderPareto;
renderPareto = function () {
  _origRenderPareto();
  const sel = [...state.runs.values()].filter((r) => r.details && r.details.role);
  if (!sel.length) return;
  const tl = campaign.data && campaign.data.temperature_label;
  Plotly.addTraces("pareto-plot", {
    type: "scatter", mode: "markers+text", name: "selected designs",
    x: sel.map((r) => r.metrics.pressure_drop_pa),
    y: sel.map((r) => r.metrics.max_temperature_k),
    text: sel.map((r) => r.details.role.replaceAll("_", " ")),
    textposition: "top center",
    customdata: sel.map((r) => r.id),
    marker: { symbol: "diamond", size: 13, color: "#c0392b",
              line: { color: "#222", width: 1 } },
  });
  if (tl) Plotly.relayout("pareto-plot", { "yaxis.title": tl });
};
pollCampaign();
