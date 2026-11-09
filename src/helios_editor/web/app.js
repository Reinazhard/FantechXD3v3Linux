"use strict";

/* UI_TEXT — single source of user-facing copy. Values are the exact approved
   contract; no technical/implementation terminology in the default UI. */
const UI_TEXT = {
  conn: { connected: "Connected", connecting: "Connecting…",
          disconnected: "Not connected", error: "Connection problem",
          hintOffline: "Move your mouse or reconnect the receiver to continue." },
  transport: { demo: "Demo mode — no device", preview: "Preview mode — changes won't be sent",
               dongle: "2.4 GHz wireless", wired: "USB cable", unknown: "Wireless" },
  battery: { ok: (p) => `Battery ${p}%`, na: "Battery unavailable",
             asleep: "Battery — wake your mouse" },
  sync: { idle: "Keeping settings in sync", updated: "Updated from your mouse",
          paused: "Sync paused while you have unsaved changes",
          error: "Can't reach your mouse — retrying" },
  pending: { saved: "All changes saved",
             n: (n) => `${n} pending change${n === 1 ? "" : "s"}`,
             writing: "Applying changes…", success: "Changes saved to your mouse",
             error: "Couldn't save changes" },
  applyError: "Couldn't save settings. Check that your mouse is awake and connected, then try again.",
  reloadConfirm: "Discard your unsaved changes and reload settings from the mouse?",
  resetConfirm: "Restore the factory defaults? Your current settings will be replaced.",
  dpiWarn: "DPI above 3200 hasn't been verified on this mouse. The value will be saved, but we can't confirm the exact hardware behavior.",
  dpiNumericError: "Enter a DPI between 50 and 26000, in steps of 50.",
  profileMeta: "For Helios II XD3v3 mice · settings are stored on the device",
  technicalDetails: "Technical details",
  reservedHint: "Left click — can't be changed",
  modeDesc: { 0: "LP — longer battery life", 1: "HP — fastest response" },
  buttonNames: { 1: "Left click", 2: "Right click", 3: "Scroll wheel",
                 4: "Side button 1", 5: "Side button 2", 6: "DPI button" },
};

/* State ---------------------------------------------------------------- */
let state = null;              // {catalog, profile, dirty, sync} from /api/state
let serverDirty = false;       // MIRROR of server dirty; owned by api()/refreshState
let applied = null;            // local applied-baseline reconstruction (change dots)
let selectedButton = null;
let searchTerm = "";
let pendingFn = null;
let activeStage = 0;
let sliderDraft = null;        // transient DPI value not yet committed
let categoryFilter = null;     // active function-category filter (Phase 3)
let writeStatus = "idle";      // idle|writing|success|error|reconciling (visible result)
let activeTab = "buttons";     // survives renderAll() refreshes

/* Apply lock/freeze scaffolding (lifecycle implemented in Phase 6). */
let mutationsFrozen = false;
let applyInFlight = false;
const inFlightMutations = new Set();

const $ = (id) => document.getElementById(id);
const clone = (o) => JSON.parse(JSON.stringify(o));

/* serverDirty is set ONLY here from a SERVER-PROVIDED boolean dirty value,
   or in refreshState()/watchSync() from a /api/state response. No local
   inference or optimistic assignment is permitted. */
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

/* Race-safe mutation helper: registers its promise SYNCHRONOUSLY before any
   async work; rejects while frozen; removes from tracking in finally. */
function mutate(fn) {
  if (mutationsFrozen) return Promise.resolve(null);
  const p = (async () => fn())();
  inFlightMutations.add(p);
  p.finally(() => inFlightMutations.delete(p));
  return p;
}

function setStatus(msg, kind = "") {
  const el = $("status");
  el.textContent = msg;
  el.className = "status " + kind;
}

function isDirty() { return serverDirty; }

/* Single writer for the dirty/discard indicators. */
function syncDirty() {
  $("dirty").hidden = !isDirty();
  $("discard-btn").hidden = !isDirty();
}

/* Transient-input guard: valid OR invalid uncommitted numeric text counts, but
   invalid text never alters the accepted draft. */
function hasTransientInput() {
  const el = $("dpi-numeric");
  if (sliderDraft !== null) return true;
  if (el && el.value.trim() !== "") {
    const committed = state && state.profile.settings.dpi_stages[activeStage];
    if (Number(el.value) !== committed) return true;
  }
  return false;
}

function bindingValue(b) { return b ? (b.param1 | (b.param2 << 8)) : -1; }

/* Friendly transport label from verified literals. Never render the raw
   state.catalog.transport string. */
function friendlyTransport() {
  const t = state && state.catalog.transport ? state.catalog.transport : "";
  if (t.includes("Simulated")) return UI_TEXT.transport.demo;
  if (t.includes("dry_run")) return UI_TEXT.transport.preview;
  const conn = window.__lastConnection || "";
  if (conn === "2.4 GHz dongle" || conn === "2.4 GHz dongle (4K)") return UI_TEXT.transport.dongle;
  if (conn === "Wired USB-C") return UI_TEXT.transport.wired;
  if (conn === "dry run (no device access)") return UI_TEXT.transport.preview;
  if (conn === "simulated") return UI_TEXT.transport.demo;
  return UI_TEXT.transport.unknown;
}

function isRealHid() {
  const t = state && state.catalog.transport ? state.catalog.transport : "";
  return t.includes("HIDTransport") && !t.includes("Simulated") && !t.includes("dry_run");
}

/* Pending-changes preview: the ONLY authoritative pending source. On failure
   clear stale rows and show UI_TEXT.pending.error; never present stale data. */
async function refreshPending() {
  try {
    const p = await api("/api/preview");
    $("preview-count").textContent = p.count;
    const list = $("preview-list");
    list.innerHTML = "";
    if (!p.count) {
      list.innerHTML = `<div class="preview-empty">No pending changes. Apply would write nothing.</div>`;
    } else {
      for (const c of p.changes) {
        const row = document.createElement("div");
        row.className = "preview-row";
        row.innerHTML = `<span class="pname">${c.label} <span class="dim">${c.address}</span></span>`
          + `<span class="pvals">${c.from} → <b>${c.to}</b></span>`;
        list.appendChild(row);
      }
    }
    return p;
  } catch (e) {
    console.error(e);
    $("preview-count").textContent = "—";
    const list = $("preview-list");
    list.innerHTML = `<div class="preview-empty">${UI_TEXT.pending.error}</div>`;
    return null;
  }
}

/* Legacy alias used by older call sites; kept until Phase 6 rewires them. */
async function refreshPreview() { return refreshPending(); }

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
  return "Custom";
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

/* Navigation: activeTab is the authority; pure render, no API calls. */
function initTabs() {
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
    activeTab = t.dataset.tab;
    renderNavigation();
  });
}

function renderNavigation() {
  document.querySelectorAll(".tab").forEach(t => {
    const on = t.dataset.tab === activeTab;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".tab-panel").forEach(p =>
    p.classList.toggle("active", p.id === "panel-" + activeTab));
}

function switchTab(name) {
  activeTab = name;
  renderNavigation();
}

/* Device identity: ONE authoritative renderer (friendly labels only). */
function renderDeviceIdentity() {
  const label = friendlyTransport();
  $("backend").textContent = label;
  $("device-label").textContent = label;
  $("pill-conn").textContent = label;
  $("link-dot").classList.toggle("sim", !isRealHid());
}

/* Connection + battery: ONE authoritative renderer for /api/status data.
   It NEVER writes #status-note (static) and NEVER fabricates telemetry. */
function renderConnection(st) {
  if (!st) return;
  window.__lastConnection = st.connection || "";
  let connKey;
  if (st.online) connKey = "connected";
  else if (st.connection === "simulated") connKey = "disconnected";
  else connKey = "disconnected";
  $("conn-state").setAttribute("data-state", connKey);
  $("conn-text").textContent = UI_TEXT.conn[connKey];
  const batt = $("pill-batt");
  if (st.battery_percent !== null && st.battery_percent !== undefined) {
    batt.textContent = UI_TEXT.battery.ok(st.battery_percent);
    batt.title = st.battery_mv != null ? `${st.battery_mv} mV` : "";
    batt.className = "pill " + (st.battery_percent <= 20 ? "err" : st.battery_percent <= 50 ? "warn" : "good");
  } else {
    batt.textContent = st.online ? UI_TEXT.battery.na : UI_TEXT.battery.asleep;
    batt.className = "pill warn";
  }
  $("offline-hint").hidden = !!st.online;
  if (!st.online) $("offline-hint").textContent = UI_TEXT.conn.hintOffline;
  $("pill-conn").title = "";               // no VID/PID/hidraw/note in tooltip
  console.log("status note:", st.note);    // diagnostic only, never in DOM
}

async function refreshConnection() {
  try {
    const st = await api("/api/status");
    renderConnection(st);
    renderDeviceIdentity();
  } catch (e) {
    console.error(e);
    $("conn-state").setAttribute("data-state", "error");
    $("conn-text").textContent = UI_TEXT.conn.error;
  }
}

/* Mouse diagram -------------------------------------------------------- */
function renderHotspots() {
  const wrap = $("hotspots");
  const changed = changedButtons();
  wrap.innerHTML = "";
  for (const h of state.catalog.hotspots) {
    const d = document.createElement("div");
    const reserved = h.id === 1 || h.fixed === true;
    d.className = "hotspot"
      + (selectedButton === h.id ? " selected" : "")
      + (reserved ? " reserved" : "")
      + (changed.has(h.id) ? " changed" : "");
    d.style.left = h.x + "px";
    d.style.top = h.y + "px";
    d.textContent = h.id;
    const name = UI_TEXT.buttonNames[h.id] || h.label;
    d.title = reserved
      ? UI_TEXT.reservedHint
      : `Button ${h.id}: ${name} — ${bindingLabel(h.id)}`;
    if (reserved) {
      d.tabIndex = -1;
      d.setAttribute("aria-disabled", "true");
    } else {
      d.tabIndex = 0;
      d.setAttribute("role", "button");
      d.setAttribute("aria-label", name);
      d.onclick = () => selectButton(h.id);
      d.onkeydown = (ev) => {
        if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
          ev.preventDefault(); selectButton(h.id);
        }
      };
    }
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
    row.innerHTML = `<span class="num">${h.id}</span><span class="blabel">${UI_TEXT.buttonNames[h.id] || h.label}</span>`
      + `<span class="bval">${bindingLabel(h.id)}</span>`;
    row.onclick = () => { selectButton(h.id); switchTab("buttons"); };
    list.appendChild(row);
  }
  $("binding-count").textContent = `${state.catalog.hotspots.length} buttons`;
}

/* Button editor -------------------------------------------------------- */
function selectButton(id) {
  selectedButton = id;
  pendingFn = null;
  searchTerm = "";
  categoryFilter = null;
  const h = state.catalog.hotspots.find(x => x.id === id);
  $("selection").textContent = `Button ${id} · ${UI_TEXT.buttonNames[id] || h.label}`;
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
  $("editor-title").textContent = `Button ${h.id} — ${UI_TEXT.buttonNames[h.id] || h.label}`;
  $("editor-current").textContent = bindingLabel(h.id);
  $("fn-search").value = searchTerm;
  renderCategories();
  renderFnList();
  renderSubs();
}

/* Function categories derived from KEY_FUNCTIONS.type; only populated ones
   are shown. Values mirror catalog KC_* constants (catalog.py:38-48). */
function categoryOf(type) {
  switch (type) {
    case 0x00: return "Disabled";
    case 0x01: return "Mouse functions";
    case 0x02: case 0x0A: case 0x08: return "Special functions";
    case 0x03: case 0x0B: return "System functions";
    case 0x05: return "Multimedia";
    case 0x04: case 0x06: case 0x07: return "Keyboard functions";
    default: return "Other";
  }
}

function renderCategories() {
  const wrap = $("button-categories");
  if (!wrap) return;
  wrap.innerHTML = "";
  const present = new Set(state.catalog.keyFunctions.map(k => categoryOf(k.type)));
  for (const cat of ["Mouse functions", "Special functions", "System functions",
                     "Multimedia", "Keyboard functions", "Disabled", "Other"]) {
    if (!present.has(cat)) continue;
    const b = document.createElement("button");
    b.className = "seg" + (categoryFilter === cat ? " active" : "");
    b.type = "button";
    b.textContent = cat;
    b.setAttribute("aria-pressed", categoryFilter === cat ? "true" : "false");
    b.onclick = () => {
      categoryFilter = (categoryFilter === cat) ? null : cat;
      renderCategories();
      renderFnList();
    };
    wrap.appendChild(b);
  }
}

function renderFnList() {
  const cur = state.profile.bindings[String(selectedButton)];
  const list = $("fn-list");
  list.innerHTML = "";
  const q = searchTerm.toLowerCase();
  const items = state.catalog.keyFunctions.filter(k =>
    k.label.toLowerCase().includes(q) &&
    (categoryFilter === null || categoryOf(k.type) === categoryFilter));
  if (!items.length) { list.innerHTML = `<div class="dim" style="padding:8px">No match.</div>`; return; }
  for (const kf of items) {
    const active = cur && cur.type === kf.type && (kf.pull === 1 || bindingValue(cur) === kf.param);
    const b = document.createElement("button");
    b.className = "fn-item" + (active ? " active" : "");
    b.setAttribute("aria-pressed", active ? "true" : "false");
    b.textContent = kf.label;
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
  const acc = $("advanced-accordion");
  if (!pendingFn || !pendingFn.subs.length) {
    if (acc) acc.hidden = true;
    list.innerHTML = "";
    return;
  }
  if (acc) { acc.hidden = false; acc.open = true; }
  const cur = state.profile.bindings[String(selectedButton)];
  list.innerHTML = "";
  for (const s of pendingFn.subs) {
    const active = cur && cur.type === pendingFn.type && bindingValue(cur) === s.value;
    const b = document.createElement("button");
    b.className = "chip" + (active ? " active" : "");
    b.setAttribute("aria-pressed", active ? "true" : "false");
    b.textContent = s.label;
    b.onclick = () => assign(pendingFn.type, s.value);
    list.appendChild(b);
  }
}

async function assign(type, value) {
  return mutate(async () => {
    try {
      const j = await api("/api/binding", { buttonId: selectedButton, type, value });
      state.profile = j.profile;
      pendingFn = null;
      syncDirty();
      renderAll();
      refreshPending();
      setStatus(`Button ${selectedButton} → ${bindingLabel(selectedButton)}`, "ok");
    } catch (e) { console.error(e); }
  });
}

/* Sensor controls ------------------------------------------------------ */
function renderSensor() {
  const s = state.profile.settings;
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
  const modeSeg = $("mode-seg");
  modeSeg.innerHTML = "";
  for (const m of (state.catalog.sensorModes || [{value:0,label:"LP"},{value:1,label:"HP"}])) {
    const b = document.createElement("button");
    b.className = "seg" + (s.sensor_mode === m.value ? " active" : "");
    b.textContent = m.label;
    b.onclick = () => patchSettings({ sensor_mode: m.value }, "mode-seg", () =>
      [...modeSeg.children].forEach(x => x.classList.toggle("active", x === b)));
    modeSeg.appendChild(b);
  }
  $("mode-desc").textContent = UI_TEXT.modeDesc[s.sensor_mode] || "";
  $("debounce").value = s.debounce_ms;
  $("debounce-val").textContent = s.debounce_ms + " ms";
  $("debounce-warn").hidden = s.debounce_ms >= 4;
  renderDpiStages();
  renderToggles();
}

const rgbHex = (c) => "#" + c.map(v => (v & 255).toString(16).padStart(2, "0")).join("");

/* Strict DPI validation against live catalog constraints. NO rounding, NO
   clamping. Invalid input never mutates the accepted draft. */
function validateDpi(raw) {
  const d = state.catalog.dpi;
  const text = String(raw).trim();
  if (text === "") return { ok: false };
  const v = Number(text);
  if (!Number.isFinite(v) || Number.isNaN(v) || !Number.isInteger(v)) return { ok: false };
  if (v < d.min || v > d.max || (v - d.min) % d.step !== 0) return { ok: false };
  return { ok: true, value: v };
}

let dpiCommitInFlight = false;

/* ONE commit path, guarded against duplicate POSTs and the mutation freeze.
   Re-validates immediately before the write. */
function commitDpi() {
  if (dpiCommitInFlight || mutationsFrozen) return Promise.resolve(null);
  if (sliderDraft === null) return Promise.resolve(null);
  const check = validateDpi(sliderDraft);
  if (!check.ok) return Promise.resolve(null);
  const dpi = check.value;
  dpiCommitInFlight = true;
  return mutate(async () => {
    try {
      const j = await api("/api/dpi", { stage: activeStage, dpi });
      state.profile = j.profile;
      sliderDraft = null;
      syncDirty();
      renderDpiStages();
      refreshPending();
      setStatus(`Stage ${activeStage + 1} → ${dpi} DPI (Apply to write)`, "ok");
    } catch (e) { console.error(e); }
    finally { dpiCommitInFlight = false; }
  });
}

function renderDpiStages() {
  const s = state.profile.settings;
  const d = state.catalog.dpi;
  if (activeStage >= s.dpi_stages.length) activeStage = 0;
  const tabs = $("stage-tabs");
  tabs.innerHTML = "";
  s.dpi_stages.forEach((v, i) => {
    const b = document.createElement("button");
    b.className = "stage-tab" + (i === activeStage ? " active" : "");
    b.setAttribute("aria-pressed", i === activeStage ? "true" : "false");
    const isActive = i === s.active_dpi_index;
    b.innerHTML = `<span class="swatch" style="background:${rgbHex(s.dpi_colors[i])}"></span>`
      + `S${i + 1} · ${v}` + (isActive ? ` <span class="dim">● on</span>` : "");
    b.title = "Click to make this the active DPI stage (staged; Apply to write)";
    b.onclick = async () => {
      activeStage = i; sliderDraft = null;
      const num0 = $("dpi-numeric"); if (num0) num0.value = "";
      return mutate(async () => {
        try {
          const j = await api("/api/dpi-active", { stage: i });
          state.profile = j.profile; syncDirty(); refreshPending();
          setStatus(`Active DPI stage → S${i + 1} (${v} DPI) · Apply to write`, "ok");
        } catch (e) { console.error(e); }
        renderDpiStages();
      });
    };
    tabs.appendChild(b);
  });
  const sl = $("dpi-slider");
  sl.min = d.min; sl.max = d.max; sl.step = d.step;
  const shown = sliderDraft !== null ? sliderDraft : s.dpi_stages[activeStage];
  sl.value = shown;
  $("dpi-value").textContent = shown;
  $("dpi-saved").hidden = sliderDraft === null;
  const num = $("dpi-numeric");
  if (num) { num.min = d.min; num.max = d.max; num.step = d.step; }
  // PRESERVE in-progress raw numeric value across pure renders; sync to the
  // accepted value only when there is no transient numeric draft.
  if (num && sliderDraft === null) num.value = s.dpi_stages[activeStage];
  const warn = $("dpi-warn");
  if (shown > d.confirmedMax) {
    warn.hidden = false;
    warn.textContent = UI_TEXT.dpiWarn;
  } else { warn.hidden = true; }
  const pal = $("color-palette");
  pal.innerHTML = "";
  const cur = rgbHex(s.dpi_colors[activeStage]);
  d.palette.forEach(hex => {
    const b = document.createElement("button");
    b.className = "swatch-btn" + (hex.toLowerCase() === cur.toLowerCase() ? " active" : "");
    b.style.background = hex; b.title = hex;
    b.setAttribute("aria-pressed", hex.toLowerCase() === cur.toLowerCase() ? "true" : "false");
    b.onclick = () => mutate(async () => {
      const rgb = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
      try {
        const j = await api("/api/dpi-color", { stage: activeStage, rgb });
        state.profile = j.profile; syncDirty(); renderDpiStages();
        refreshPending();
        setStatus(`Stage ${activeStage + 1} LED → ${hex}`, "ok");
      } catch (e) { console.error(e); }
    });
    pal.appendChild(b);
  });
}

function initDpiControls() {
  const sl = $("dpi-slider");
  const num = $("dpi-numeric");
  const err = $("dpi-numeric-error");
  sl.oninput = () => {
    sliderDraft = Number(sl.value);
    $("dpi-value").textContent = sliderDraft;
    $("dpi-saved").hidden = false;
    if (num) num.value = sliderDraft;
    if (err) err.hidden = true;
  };
  const onNumeric = () => {
    const check = validateDpi(num.value);
    if (!check.ok) {
      // Invalid: RETAIN raw text; never mutate sliderDraft/#dpi-value/slider/
      // state.profile; do NOT POST.
      err.hidden = false;
      err.textContent = UI_TEXT.dpiNumericError;
      return;
    }
    err.hidden = true;
    sliderDraft = check.value;
    $("dpi-value").textContent = check.value;
    sl.value = check.value;               // slider + numeric + accepted value synced
    $("dpi-saved").hidden = false;
    // Enter + blur can both fire change; do not POST again when the value
    // already equals the committed stage (no duplicate /api/dpi request).
    const committed = state.profile.settings.dpi_stages[activeStage];
    if (check.value !== committed) commitDpi();
    else { sliderDraft = null; $("dpi-saved").hidden = true; }
  };
  if (num) {
    num.onchange = onNumeric;             // Enter / blur fire change once
    num.onkeydown = (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        // Commit directly; do NOT rely on blur/change (programmatic .value
        // assignments do not fire change). onNumeric is guarded so Enter +
        // a later change event cannot produce two POSTs.
        onNumeric();
      }
    };
  }
  sl.onchange = commitDpi;
  $("dpi-save").onclick = () => { if (num) onNumeric(); else commitDpi(); };
}

/* Compact 14-color popover; /api/dpi-color mapping unchanged. */
function initColorPopover() {
  const toggle = $("color-picker-toggle"), pop = $("color-popover");
  if (!toggle || !pop) return;
  const close = () => { pop.hidden = true; toggle.setAttribute("aria-expanded", "false"); };
  toggle.onclick = () => {
    const open = pop.hidden;
    pop.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  };
  document.addEventListener("click", (ev) => {
    if (!pop.hidden && !pop.contains(ev.target) && ev.target !== toggle) close();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !pop.hidden) { close(); toggle.focus(); }
  });
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

async function patchSettings(patch, kind, uiUpdate) {
  return mutate(async () => {
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
      refreshPending();
    } catch (e) { console.error(e); }
  });
}

/* Profile tab ---------------------------------------------------------- */
function renderProfileTab() {
  const el = $("profile-name");
  if (el) el.value = state.profile.name;
}

/* Static status-note set ONCE at init: the profileMeta literal claims on-device
   storage, false for simulated/dry-run, so gate on a real HID transport. */
function initStatusNote() {
  const el = $("status-note");
  if (!el) return;
  if (isRealHid()) el.textContent = UI_TEXT.profileMeta;
  else { el.textContent = ""; el.hidden = true; }
}

/* Render everything (structural changes only). NO /api/* calls here:
   refreshConnection()/refreshPending() are explicit, called by init/watchSync.
   Preserves activeTab and sliderDraft; syncDirty() is the single dirty writer. */
function renderAll() {
  renderDeviceIdentity();
  renderNavigation();
  renderHotspots();
  renderBindingsList();
  renderEditor();
  renderSensor();
  renderProfileTab();
  syncDirty();
}

/* Reload the authoritative state. applied is reconstructed from the returned
   profile ONLY when dirty === false; when dirty === true retain the baseline. */
async function refreshState() {
  const fresh = await api("/api/state");
  state = fresh;
  serverDirty = fresh.dirty;
  if (fresh.dirty === false) applied = clone(fresh.profile);
  activeStage = fresh.profile.settings.active_dpi_index;
  sliderDraft = null;
  renderAll();
  return fresh;
}

/* Actions -------------------------------------------------------------- */
/* Full frozen-lifecycle apply() is implemented in Phase 6. Phase 2 keeps the
   current behavior but removes raw exception text / record-ack claims. */
async function apply() {
  if (applyInFlight) return;
  applyInFlight = true;
  mutationsFrozen = true;
  writeStatus = "writing";
  $("apply-btn").disabled = true;
  setStatus(UI_TEXT.pending.writing, "");
  let writeSucceeded = false, reconcileOk = false;
  try {
    try {
      await api("/api/apply", {});
      writeSucceeded = true;
    } catch (e) {
      console.error(e);
    }
    if (writeSucceeded) {
      try {
        await refreshState();
        reconcileOk = (await refreshPending()) !== null;
      } catch (e) { console.error(e); }
    }
    if (writeSucceeded && !reconcileOk) {
      writeStatus = "reconciling";
      setStatus(UI_TEXT.pending.error, "err");
    } else if (!writeSucceeded) {
      writeStatus = "error";
      setStatus(UI_TEXT.applyError, "err");
    } else {
      writeStatus = "success";
      setStatus(isRealHid() ? UI_TEXT.pending.success : UI_TEXT.pending.saved, "ok");
      setTimeout(() => { if (writeStatus === "success") { writeStatus = "idle"; } }, 3000);
    }
  } finally {
    applyInFlight = false;
    mutationsFrozen = false;
    $("apply-btn").disabled = false;
    syncDirty();
  }
}

function initActions() {
  $("apply-btn").onclick = apply;
  $("discard-btn").onclick = () => mutate(async () => {
    try {
      const j = await api("/api/discard", {});
      state.profile = j.profile; applied = clone(j.profile);
      renderAll(); setStatus("Draft discarded.", "ok");
    } catch (e) { console.error(e); }
  });
  $("load-btn").onclick = () => mutate(async () => {
    if (isDirty() && !window.confirm(UI_TEXT.reloadConfirm)) {
      setStatus("Reload skipped (unsaved draft kept).", "");
      return;
    }
    try {
      const j = await api("/api/load", {});
      state.profile = j.profile; applied = clone(j.profile);
      renderAll(); setStatus("Reloaded from device.", "ok");
    } catch (e) { console.error(e); }
  });
  const vb = $("validate-btn");
  if (vb) vb.onclick = async () => {
    try {
      const j = await api("/api/validate", {});
      console.error(j.errors.length ? j.errors : "profile valid");
    } catch (e) { console.error(e); }
  };
  $("reset-btn").onclick = () => mutate(async () => {
    try {
      const j = await api("/api/reset", {}); state.profile = j.profile;
      renderAll(); setStatus("Restored defaults (not yet applied).", "ok");
    } catch (e) { console.error(e); }
  });
  $("export-btn").onclick = async () => {
    try {
      const j = await api("/api/export");
      const text = JSON.stringify(j, null, 2);
      const pre = $("export-out");
      pre.hidden = false; pre.textContent = text;
      const blob = new Blob([text], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = "helios-profile.json";
      a.click(); URL.revokeObjectURL(a.href);
      console.log("export ok");
    } catch (e) { console.error(e); }
  };
  $("import-btn").onclick = () => $("import-file").click();
  $("import-file").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try {
      const j = await api("/api/import", JSON.parse(await f.text()));
      state.profile = j.profile; renderAll(); refreshPending();
      console.log("import ok");
    } catch (err) { console.error(err); }
  };
  const pn = $("profile-name");
  if (pn) pn.onchange = () => mutate(async () => {
    const copy = clone(state.profile); copy.name = pn.value;
    try { const j = await api("/api/import", copy); state.profile = j.profile; syncDirty(); }
    catch (err) { console.error(err); }
  });
  $("fn-search").oninput = (e) => { searchTerm = e.target.value; renderFnList(); };
  $("debounce").oninput = (e) => patchSettings({ debounce_ms: Number(e.target.value) }, "debounce");
  initTabs();
  initDpiControls();
  initColorPopover();

  /* beforeunload only while there are unsaved/transient changes. */
  const beforeUnload = (ev) => {
    if (isDirty() || hasTransientInput()) { ev.preventDefault(); ev.returnValue = ""; }
  };
  const updateBeforeUnload = () => {
    window.removeEventListener("beforeunload", beforeUnload);
    if (isDirty() || hasTransientInput()) window.addEventListener("beforeunload", beforeUnload);
  };
  window.__updateBeforeUnload = updateBeforeUnload;
  setInterval(updateBeforeUnload, 500);

  /* Background sync heartbeat: GET only; preserves draft + applied. */
  const syncNote = $("sync-note");
  let lastGen = -1;
  const watchSync = async () => {
    try {
      const st = await api("/api/sync-status");
      syncNote.textContent = st.message ? UI_TEXT.sync.idle : UI_TEXT.sync.error;
      if (st.generation !== lastGen) {
        lastGen = st.generation;
        const fresh = await api("/api/state");
        state = fresh; serverDirty = fresh.dirty;
        if (writeStatus === "reconciling") { writeStatus = "idle"; }
        renderAll();
        refreshPending();
      }
    } catch (e) {
      console.error(e);
      syncNote.textContent = UI_TEXT.sync.error;
    }
  };
  setInterval(watchSync, 1500);
  setTimeout(watchSync, 1200);
  setInterval(refreshConnection, 15000);
}

async function init() {
  initActions();
  await refreshState();
  initStatusNote();
  await refreshConnection();
  await refreshPending();
  setStatus("Ready.", "");
}

init().catch(e => { console.error(e); setStatus(UI_TEXT.conn.error, "err"); });
