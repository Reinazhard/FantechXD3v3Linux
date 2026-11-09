"use strict";

/* State ---------------------------------------------------------------- */
let state = null;              // {catalog, profile} from /api/state
let serverDirty = false;       // authoritative unsaved-state from the server
let applied = null;            // local snapshot for per-button change dots
let selectedButton = null;
let searchTerm = "";
let pendingFn = null;          // function chosen before picking a variant

const $ = (id) => document.getElementById(id);
const clone = (o) => JSON.parse(JSON.stringify(o));

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j.detail || j.error || ("HTTP " + r.status));
  if (typeof j.dirty === "boolean") serverDirty = j.dirty;
  return j;
}

function setStatus(msg, kind = "") {
  const el = $("status");
  el.textContent = msg;
  el.className = "status " + kind;
}

function isDirty() {
  return serverDirty;
}
function syncDirty() {
  $("dirty").hidden = !isDirty();
}

function bindingValue(b) { return b ? (b.param1 | (b.param2 << 8)) : -1; }

function bindingLabel(buttonId) {
  const b = state.profile.bindings[String(buttonId)];
  if (!b) return "—";
  const v = bindingValue(b);
  for (const kf of state.catalog.keyFunctions) {
    if (kf.type === b.type && kf.param === v && kf.pull === 0) return kf.label;
  }
  for (const kf of state.catalog.keyFunctions) {
    for (const s of kf.subs) {
      if (s.type === b.type && s.value === v) return kf.label + " → " + s.label;
    }
  }
  return `0x${b.type.toString(16)} / 0x${v.toString(16)}`;
}

function changedButtons() {
  if (!applied) return new Set();
  const out = new Set();
  for (const h of state.catalog.hotspots) {
    const a = applied.bindings[String(h.id)], b = state.profile.bindings[String(h.id)];
    if (!a || !b || a.type !== b.type || a.param1 !== b.param1 || a.param2 !== b.param2) out.add(h.id);
  }
  return out;
}

/* Tabs ----------------------------------------------------------------- */
function initTabs() {
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
    document.querySelectorAll(".tab-panel").forEach(p =>
      p.classList.toggle("active", p.id === "panel-" + t.dataset.tab));
  });
}

/* Mouse diagram -------------------------------------------------------- */
function renderHotspots() {
  const wrap = $("hotspots");
  const changed = changedButtons();
  wrap.innerHTML = "";
  for (const h of state.catalog.hotspots) {
    const d = document.createElement("div");
    d.className = "hotspot"
      + (selectedButton === h.id ? " selected" : "")
      + (h.id === 1 ? " reserved" : "")
      + (changed.has(h.id) ? " changed" : "");
    d.style.left = h.x + "px";
    d.style.top = h.y + "px";
    d.textContent = h.id;
    d.title = `Button ${h.id}: ${h.label} — ${bindingLabel(h.id)}`;
    d.onclick = () => selectButton(h.id);
    wrap.appendChild(d);
  }
}

function renderBindingsList() {
  const changed = changedButtons();
  const list = $("bindings-list");
  list.innerHTML = "";
  for (const h of state.catalog.hotspots) {
    const row = document.createElement("div");
    row.className = "binding-row"
      + (selectedButton === h.id ? " selected" : "")
      + (changed.has(h.id) ? " changed" : "");
    row.innerHTML = `<span class="num">${h.id}</span><span class="blabel">${h.label}</span>`
      + `<span class="bval">${bindingLabel(h.id)}</span>`;
    row.onclick = () => { selectButton(h.id); switchTab("buttons"); };
    list.appendChild(row);
  }
  $("binding-count").textContent = `${state.catalog.hotspots.length} buttons`;
}

function switchTab(name) {
  const t = document.querySelector(`.tab[data-tab="${name}"]`);
  if (t) t.click();
}

/* Button editor -------------------------------------------------------- */
function selectButton(id) {
  selectedButton = id;
  pendingFn = null;
  searchTerm = "";
  const h = state.catalog.hotspots.find(x => x.id === id);
  $("selection").textContent = `Button ${id} · ${h.label}`;
  $("selection").classList.add("active");
  renderHotspots();
  renderBindingsList();
  renderEditor();
}

function renderEditor() {
  if (selectedButton === null) {
    $("editor-empty").hidden = false;
    $("editor").hidden = true;
    return;
  }
  $("editor-empty").hidden = true;
  $("editor").hidden = false;
  const h = state.catalog.hotspots.find(x => x.id === selectedButton);
  $("editor-title").textContent = `Button ${h.id} — ${h.label}`;
  $("editor-current").textContent = bindingLabel(h.id);
  $("fn-search").value = searchTerm;
  renderFnList();
  renderSubs();
}

function renderFnList() {
  const cur = state.profile.bindings[String(selectedButton)];
  const list = $("fn-list");
  list.innerHTML = "";
  const q = searchTerm.toLowerCase();
  const items = state.catalog.keyFunctions.filter(k => k.label.toLowerCase().includes(q));
  if (!items.length) { list.innerHTML = `<div class="dim" style="padding:8px">No match.</div>`; return; }
  for (const kf of items) {
    const active = cur && cur.type === kf.type && (kf.pull === 1 || bindingValue(cur) === kf.param);
    const b = document.createElement("button");
    b.className = "fn-item" + (active ? " active" : "");
    b.innerHTML = `<span class="fn-type">0x${kf.type.toString(16)}</span>${kf.label}`;
    b.onclick = () => chooseFunction(kf);
    list.appendChild(b);
  }
}

function chooseFunction(kf) {
  if (kf.pull === 1 && kf.subs.length) {
    pendingFn = kf;
    renderFnList();
    renderSubs();
    return;
  }
  assign(kf.type, kf.param);
}

function renderSubs() {
  const wrap = $("sub-wrap"), list = $("sub-list");
  if (!pendingFn || !pendingFn.subs.length) { wrap.hidden = true; list.innerHTML = ""; return; }
  wrap.hidden = false;
  const cur = state.profile.bindings[String(selectedButton)];
  list.innerHTML = "";
  for (const s of pendingFn.subs) {
    const active = cur && cur.type === pendingFn.type && bindingValue(cur) === s.value;
    const b = document.createElement("button");
    b.className = "chip" + (active ? " active" : "");
    b.textContent = s.label;
    b.onclick = () => assign(pendingFn.type, s.value);
    list.appendChild(b);
  }
}

async function assign(type, value) {
  try {
    const j = await api("/api/binding", { buttonId: selectedButton, type, value });
    state.profile = j.profile;
    pendingFn = null;
    syncDirty();
    renderAll();
    setStatus(`Button ${selectedButton} → ${bindingLabel(selectedButton)}`, "ok");
  } catch (e) { setStatus(String(e), "err"); }
}

/* Sensor controls ------------------------------------------------------ */
function renderSensor() {
  const s = state.profile.settings;
  // segmented polling rate
  const rateSeg = $("rate-seg");
  rateSeg.innerHTML = "";
  for (const r of state.catalog.reportRates) {
    const b = document.createElement("button");
    b.className = "seg" + (s.report_rate === r.value ? " active" : "");
    b.textContent = r.label;
    b.onclick = () => patchSettings({ report_rate: r.value }, "rate-seg", () =>
      [...rateSeg.children].forEach(x => x.classList.toggle("active", x === b)));
    rateSeg.appendChild(b);
  }
  // segmented LOD
  const lodSeg = $("lod-seg");
  lodSeg.innerHTML = "";
  for (const l of state.catalog.lod) {
    const b = document.createElement("button");
    b.className = "seg" + (s.lod_mm === l.value ? " active" : "");
    b.textContent = l.label;
    b.onclick = () => patchSettings({ lod_mm: l.value }, "lod-seg", () =>
      [...lodSeg.children].forEach(x => x.classList.toggle("active", x === b)));
    lodSeg.appendChild(b);
  }
  // debounce slider
  $("debounce").value = s.debounce_ms;
  $("debounce-val").textContent = s.debounce_ms + " ms";
  $("debounce-warn").hidden = s.debounce_ms >= 4;
  // DPI stages
  renderDpiStages();
  // toggles
  renderToggles();
}

let activeStage = 0;
let sliderDraft = null;   // value while dragging, not yet saved to a stage

const rgbHex = (c) => "#" + c.map(v => (v & 255).toString(16).padStart(2, "0")).join("");

function renderDpiStages() {
  const s = state.profile.settings;
  const d = state.catalog.dpi;
  if (activeStage >= s.dpi_stages.length) activeStage = 0;
  // stage tabs, each showing its unique LED color
  const tabs = $("stage-tabs");
  tabs.innerHTML = "";
  s.dpi_stages.forEach((v, i) => {
    const b = document.createElement("button");
    b.className = "stage-tab" + (i === activeStage ? " active" : "");
    b.innerHTML = `<span class="swatch" style="background:${rgbHex(s.dpi_colors[i])}"></span>`
      + `S${i + 1} · ${v}`;
    b.onclick = () => { activeStage = i; sliderDraft = null; renderDpiStages(); };
    tabs.appendChild(b);
  });
  // slider
  const sl = $("dpi-slider");
  sl.min = d.min; sl.max = d.max; sl.step = d.step;
  const shown = sliderDraft !== null ? sliderDraft : s.dpi_stages[activeStage];
  sl.value = shown;
  $("dpi-value").textContent = shown;
  $("dpi-saved").hidden = sliderDraft === null;
  const warn = $("dpi-warn");
  if (shown > d.confirmedMax) {
    warn.hidden = false;
    warn.textContent = `Above ${d.confirmedMax} DPI the sensor-register encoding is not fully `
      + `capture-confirmed; the vendor advertises up to ${d.max}. The value is stored in the `
      + `profile but Apply uses the only observed extended record (3f 3f 55).`;
  } else { warn.hidden = true; }
  // palette (14 vendor swatches)
  const pal = $("color-palette");
  pal.innerHTML = "";
  const cur = rgbHex(s.dpi_colors[activeStage]);
  d.palette.forEach(hex => {
    const b = document.createElement("button");
    b.className = "swatch-btn" + (hex.toLowerCase() === cur.toLowerCase() ? " active" : "");
    b.style.background = hex; b.title = hex;
    b.onclick = async () => {
      const rgb = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
      try {
        const j = await api("/api/dpi-color", { stage: activeStage, rgb });
        state.profile = j.profile; syncDirty(); renderDpiStages();
        setStatus(`Stage ${activeStage + 1} LED → ${hex}`, "ok");
      } catch (e) { setStatus(String(e), "err"); }
    };
    pal.appendChild(b);
  });
}

function initDpiControls() {
  const sl = $("dpi-slider");
  // Live drag: update the readout only; do NOT spam the device.
  sl.oninput = () => {
    sliderDraft = Number(sl.value);
    $("dpi-value").textContent = sliderDraft;
    $("dpi-saved").hidden = false;
    const w = $("dpi-warn"), d = state.catalog.dpi;
    if (sliderDraft > d.confirmedMax) {
      w.hidden = false;
      w.textContent = `Above ${d.confirmedMax} DPI the register encoding is not fully capture-confirmed `
        + `(vendor max ${d.max}); Apply will use the observed extended record.`;
    } else { w.hidden = true; }
  };
  // Save to the active stage on release / explicit button.
  const commit = async () => {
    if (sliderDraft === null) return;
    const dpi = Math.round(sliderDraft / state.catalog.dpi.step) * state.catalog.dpi.step;
    try {
      const j = await api("/api/dpi", { stage: activeStage, dpi });
      state.profile = j.profile; sliderDraft = null; syncDirty(); renderDpiStages();
      setStatus(`Stage ${activeStage + 1} → ${dpi} DPI (Apply to write)`, "ok");
    } catch (e) { setStatus(String(e), "err"); }
  };
  sl.onchange = commit;
  $("dpi-save").onclick = commit;
}

function renderToggles() {
  const s = state.profile.settings;
  const defs = [
    ["motion_sync", "Motion sync", "Align sensor frames to USB polls"],
    ["angle_snapping", "Angle snapping", "Straighten near-linear movement"],
    ["ripple_control", "Ripple control", "Smooth small jitter"],
  ];
  const wrap = $("toggles");
  wrap.innerHTML = "";
  for (const [key, label, desc] of defs) {
    const row = document.createElement("div");
    row.className = "toggle-row";
    const left = document.createElement("div");
    left.innerHTML = `<div>${label}</div><div class="desc">${desc}</div>`;
    const sw = document.createElement("label");
    sw.className = "switch";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!s[key];
    cb.onchange = () => patchSettings({ [key]: cb.checked }, "toggles");
    const track = document.createElement("span"); track.className = "track";
    sw.append(cb, track);
    row.append(left, sw);
    wrap.appendChild(row);
  }
}

/* Patch a single settings key WITHOUT rebuilding the whole DOM ----------- */
async function patchSettings(patch, kind, uiUpdate) {
  Object.assign(state.profile.settings, patch);
  if (uiUpdate) uiUpdate();
  if (kind === "debounce") {
    $("debounce-val").textContent = state.profile.settings.debounce_ms + " ms";
    $("debounce-warn").hidden = state.profile.settings.debounce_ms >= 4;
  }
  syncDirty();
  try {
    const j = await api("/api/settings", patch);
    state.profile = j.profile;
    syncDirty();
  } catch (e) { setStatus(String(e), "err"); }
}

/* Profile tab ---------------------------------------------------------- */
function renderProfileTab() {
  $("profile-name").value = state.profile.name;
}

/* Render everything (only on structural changes) ------------------------ */
function renderAll() {
  $("backend").textContent = state.catalog.transport;
  const real = !state.catalog.transport.startsWith("Simulated");
  const dry = state.catalog.transport.includes("dry_run");
  $("device-label").textContent = real
    ? (dry ? "Helios II · dry run (no writes)" : "Helios II · connected")
    : "simulated device";
  $("link-dot").classList.toggle("sim", !real || dry);
  renderHotspots();
  renderBindingsList();
  renderEditor();
  renderSensor();
  renderProfileTab();
  syncDirty();
  renderStatus();
}

async function renderStatus() {
  try {
    const st = await api("/api/status");
    const conn = $("pill-conn");
    conn.textContent = st.connection || "unknown";
    conn.className = "pill " + (st.online ? "good" : "warn");
    conn.title = st.hidraw ? `VID ${st.vid?.toString(16)} PID ${st.pid?.toString(16)} · ${st.hidraw}` : "";
    const batt = $("pill-batt");
    if (st.battery_percent !== null && st.battery_percent !== undefined) {
      batt.textContent = `🔋 ${st.battery_percent}% · ${st.battery_mv} mV`;
      batt.className = "pill " + (st.battery_percent <= 20 ? "err" : st.battery_percent <= 50 ? "warn" : "good");
    } else {
      batt.textContent = st.online ? "battery n/a" : "battery · mouse asleep?";
      batt.className = "pill warn";
    }
    const note = st.note || "";
    conn.title = (conn.title ? conn.title + " · " : "") + note;
    batt.title = note;
    // Dedicated status line for the connectivity caveat
    let line = document.getElementById("status-note");
    if (!line) {
      line = document.createElement("div");
      line.id = "status-note"; line.className = "status-note";
      document.querySelector("#panel-profiles .about").before(line);
    }
    line.textContent = note;
  } catch (e) { /* status is informational */ }
}

async function refreshState() {
  state = await api("/api/state");
  serverDirty = state.dirty;
  applied = clone(state.profile);
  activeStage = state.profile.settings.active_dpi_index;
  sliderDraft = null;
  renderAll();
  setStatus("Ready.", "");
}

/* Actions -------------------------------------------------------------- */
async function apply() {
  $("apply-btn").disabled = true;
  setStatus("Applying to device…", "");
  try {
    await api("/api/apply", {});
    applied = clone(state.profile);
    syncDirty();
    const real = !state.catalog.transport.startsWith("Simulated");
    setStatus(real ? "Applied · write acknowledged by device" : "Applied (simulated)", "ok");
  } catch (e) {
    setStatus("Apply failed: " + String(e), "err");
    syncDirty();  // keep unsaved-changes badge visible on failure
  } finally {
    $("apply-btn").disabled = false;
  }
}

function initActions() {
  $("apply-btn").onclick = apply;
  $("load-btn").onclick = async () => {
    try { const j = await api("/api/load", {}); state.profile = j.profile; applied = clone(j.profile);
      renderAll(); setStatus("Reloaded from device.", "ok"); }
    catch (e) { setStatus(String(e), "err"); }
  };
  $("validate-btn").onclick = async () => {
    const j = await api("/api/validate", {});
    setStatus(j.errors.length ? j.errors.join("; ") : "Profile valid.", j.errors.length ? "err" : "ok");
  };
  $("reset-btn").onclick = async () => {
    const j = await api("/api/reset", {}); state.profile = j.profile;
    renderAll(); setStatus("Restored defaults (not yet applied).", "ok");
  };
  $("export-btn").onclick = async () => {
    try {
      const j = await api("/api/export");   // GET route
      const text = JSON.stringify(j, null, 2);
      const pre = $("export-out");
      pre.hidden = false; pre.textContent = text;
      const blob = new Blob([text], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = "helios-profile.json";
      a.click(); URL.revokeObjectURL(a.href);
      setStatus(`Exported ${state.profile.name}.json`, "ok");
    } catch (e) { setStatus(String(e), "err"); }
  };
  $("import-btn").onclick = () => $("import-file").click();
  $("import-file").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try { const j = await api("/api/import", JSON.parse(await f.text()));
      state.profile = j.profile; renderAll(); setStatus("Imported (not yet applied).", "ok"); }
    catch (err) { setStatus(String(err), "err"); }
  };
  $("profile-name").onchange = async (e) => {
    const copy = clone(state.profile); copy.name = e.target.value;
    try { const j = await api("/api/import", copy); state.profile = j.profile; syncDirty(); }
    catch (err) { setStatus(String(err), "err"); }
  };
  // live controls (no re-render)
  $("fn-search").oninput = (e) => { searchTerm = e.target.value; renderFnList(); };
  $("debounce").oninput = (e) => patchSettings({ debounce_ms: Number(e.target.value) }, "debounce");
  initTabs();
  initDpiControls();
  // poll live device status every 15 s
  setInterval(renderStatus, 15000);
}

async function init() {
  initActions();
  await refreshState();
}

init().catch(e => setStatus("Init failed: " + String(e), "err"));
