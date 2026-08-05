"use strict";

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = (p, opts) => fetch(p, opts).then(r => r.json());

const state = {
  targets: [],          // matrix rows
  selected: new Set(),
  config: {},
  running: false,
};

/* ── tabs ─────────────────────────────────────────────── */
$$(".tab").forEach(t => t.addEventListener("click", () => {
  $$(".tab").forEach(x => x.classList.remove("is-active"));
  $$(".panel").forEach(x => x.classList.remove("is-active"));
  t.classList.add("is-active");
  $("#tab-" + t.dataset.tab).classList.add("is-active");
}));

/* ── machine spec ─────────────────────────────────────── */
function renderSpec(h) {
  const ram  = h.ram_gb != null ? `${h.ram_gb}<span class="u"> GB</span>` : "—";
  const disk = h.free_disk_gb != null ? `${h.free_disk_gb}<span class="u"> GB free</span>` : "—";
  const rows = [
    ["host",  h.hostname || "—"],
    ["os",    `<span class="spec-os">${h.os} ${h.os_release || ""}</span>`],
    ["arch",  h.arch],
    ["cpu",   h.cpu],
    ["cores", `${h.cores_logical}`],
    ["ram",   ram],
    ["disk",  disk],
  ];
  if (h.distro) rows.splice(2, 0, ["distro", h.distro]);
  $("#spec").innerHTML = rows.map(([k, v]) =>
    `<div class="spec-row"><span class="spec-k">${k}</span><span class="spec-v">${v}</span></div>`
  ).join("");
}

/* ── prereqs ──────────────────────────────────────────── */
// map of tool id -> full row from /api/toolchains
state.installable = {};
state.localTotal = "";

function renderPrereqs(list) {
  $("#prereqs").innerHTML = list.map(p => {
    const ok = p.present;
    const inst = state.installable[p.id];
    const canInstall = !ok && inst && inst.installable;
    const cls = ok ? "ok" : "no";
    const hint = (!ok && p.hint && !canInstall) ? `<div class="pr-hint">${esc(p.hint)}</div>` : "";

    let right;
    if (canInstall) {
      const sz = inst.id === "vs_buildtools" ? inst.size_disk : inst.size_download;
      const dl = sz ? ` <span class="pr-size">${esc(sz)}</span>` : "";
      right = `<button class="pr-install" data-install="${inst.id}">install${dl}</button>`;
    } else if (ok) {
      const ver = esc(shortVer(p.version)) || "ok";
      // if it came from our local .toolchains folder, show its size + a remove ✕
      const rm = inst && inst.local
        ? ` <span class="pr-local" title="installed locally">${esc(inst.local_size)}<button class="pr-remove" data-remove="${p.id}" title="remove local copy">✕</button></span>`
        : "";
      right = `<span class="pr-ver" title="${esc(p.version || "")}">${ver}</span>${rm}`;
    } else {
      right = `<span class="pr-ver">not found</span>`;
    }

    return `<li class="pr ${ok ? "" : "missing"}">
      <div class="pr-top">
        <span class="pr-dot ${cls}"></span>
        <span class="pr-name">${esc(p.label)}</span>
        ${right}
      </div>${hint}</li>`;
  }).join("");

  const anyInstallable = list.some(p => !p.present && state.installable[p.id]?.installable);
  $("#install-missing").hidden = !anyInstallable;
  // local footprint line
  const foot = $("#local-foot");
  if (state.localTotal && state.localTotal !== "0 B") {
    foot.hidden = false;
    foot.innerHTML = `local toolchains: <strong>${esc(state.localTotal)}</strong> in <code>.toolchains/</code>`;
  } else {
    foot.hidden = true;
  }
}
const shortVer = v => (v || "").replace(/^[^\d]*/, "").split(" ")[0] || v || "";

async function loadToolchains() {
  try {
    const t = await api("/api/toolchains");
    state.installable = {};
    // key by the prereq id each tool satisfies (e.g. vs_buildtools -> "msbuild"),
    // so the sidebar rows can find their installer. Value keeps the real tool id.
    t.tools.forEach(x => state.installable[x.satisfies || x.id] = x);
    state.localTotal = t.local_total || "";
  } catch { state.installable = {}; }
}

/* ── toolchain install ────────────────────────────────── */
const icon = $("#install-console");
function installLine(text) {
  const span = document.createElement("span");
  if (/✓|installed|done\./.test(text)) span.className = "con-ok";
  else if (/✗|failed|error/i.test(text)) span.className = "con-err";
  else if (/^===|↓/.test(text.trim())) span.className = "con-head";
  span.textContent = text + "\n";
  icon.appendChild(span);
  icon.scrollTop = icon.scrollHeight;
}

async function installTools(ids) {
  if (!ids.length) return;
  if (ids.includes("vs_buildtools")) {
    if (!confirm("Install Build Tools for Visual Studio (C++)?\n\n" +
      "This is Microsoft's official installer (~4–6 GB, several minutes) and " +
      "will show a UAC prompt. It provides the MSVC linker (link.exe) that Rust " +
      "needs plus MSBuild. Continue?")) return;
  }
  $("#install-log").hidden = false;
  $("#install-log-title").textContent = `installing ${ids.join(", ")}…`;
  icon.textContent = "";
  const r = await api("/api/toolchains/install", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!r.ok) { installLine("!! " + (r.message || r.error || "could not start")); return; }
  $("#install-missing").disabled = true;
  const es = new EventSource("/api/toolchains/stream");
  es.onmessage = ev => { try { installLine(JSON.parse(ev.data).line); } catch {} };
  es.addEventListener("done", async () => {
    es.close();
    $("#install-log-title").textContent = "install finished — re-scanning";
    $("#install-missing").disabled = false;
    await loadToolchains();
    await api("/api/prereqs").then(renderPrereqs);
    await loadMatrix();
    $("#install-log-title").textContent = "done. tools wired into this session.";
  });
  es.onerror = () => {};
}

$("#install-missing").addEventListener("click", () => {
  const ids = Object.values(state.installable)
    .filter(x => x.installable && !x.present && x.id !== "vs_buildtools")
    .map(x => x.id);
  installTools(ids);
});
$("#install-cancel").addEventListener("click", () =>
  api("/api/toolchains/cancel", { method: "POST" }));
document.addEventListener("click", e => {
  const b = e.target.closest("[data-install]");
  if (b) { installTools([b.dataset.install]); return; }
  const rm = e.target.closest("[data-remove]");
  if (rm) removeTool(rm.dataset.remove);
});

async function removeTool(id) {
  const inst = state.installable[id];
  const size = inst?.local_size ? ` (${inst.local_size})` : "";
  if (!confirm(`Remove the locally-installed ${id}${size}? You can reinstall it anytime.`)) return;
  await api("/api/toolchains/remove", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  await loadToolchains();
  await api("/api/prereqs").then(renderPrereqs);
  await loadMatrix();
}

/* ── capability board ─────────────────────────────────── */
function renderBoard() {
  const order = { android: 0, windows: 1, linux: 2, macos: 3 };
  const rows = [...state.targets].sort((a, b) =>
    (order[a.platform] - order[b.platform]) || a.label.localeCompare(b.label));

  $("#board").innerHTML = rows.map((t, i) => {
    let kind = "blocked";
    if (t.buildable && t.ready) kind = "ready";
    else if (t.buildable && !t.ready) kind = "need";

    let reason = "";
    if (kind === "need") reason = `⚠ install first: ${t.missing_tools.join(", ")}`;
    else if (kind === "blocked") reason = t.blocked_reason;
    else if (t.blocked_reason) reason = t.blocked_reason;  // e.g. cross-compile note

    const sel = state.selected.has(t.id) ? "sel" : "";
    return `<article class="cell ${kind} ${sel}" data-id="${t.id}" style="animation-delay:${i * 28}ms">
      <div class="cell-top">
        <span class="cell-plat">${t.platform}</span>
        <span class="led"></span>
      </div>
      <div class="cell-label">${esc(t.label)}</div>
      <div class="cell-arch">${t.arch} · .${t.ext}</div>
      <div class="cell-note">${esc(t.note || "")}</div>
      ${reason ? `<div class="cell-reason">${esc(reason)}</div>` : ""}
      <div class="cell-check"></div>
    </article>`;
  }).join("");

  $$(".cell.ready").forEach(c => c.addEventListener("click", () => toggleSel(c.dataset.id)));
  updateTray();
}

function toggleSel(id) {
  state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
  renderBoard();
}

function updateTray() {
  const n = state.selected.size;
  $("#sel-count").textContent = n;
  $("#btn-build").disabled = n === 0 || state.running;
  $("#btn-plan").disabled  = n === 0;
  const plats = [...new Set([...state.selected].map(id =>
    state.targets.find(t => t.id === id)?.platform))].filter(Boolean);
  $("#sel-hint").textContent = n ? `→ ${plats.join(", ")}` : "";
}

/* ── config form ──────────────────────────────────────── */
const PERMS = [
  ["enableKeyboard", "Keyboard"], ["enableClipboard", "Clipboard"],
  ["enableFileTransfer", "File transfer"], ["enableAudio", "Audio"],
  ["enableTCP", "TCP tunnel"], ["enableRemoteRestart", "Remote restart"],
  ["enableRecording", "Recording"], ["enableBlockingInput", "Block input"],
  ["enableRemoteModi", "Remote config edit"], ["enablePrinter", "Printer"],
  ["enableCamera", "Camera"], ["enableTerminal", "Terminal"],
];
const FLAGS = [
  ["delayFix", "Fix connection delay"], ["hidecm", "Hide connection window"],
  ["xOffline", "Show ‘X’ when offline"], ["removeNewVersionNotif", "Hide update notice"],
  ["removeWallpaper", "Allow wallpaper removal"], ["denyLan", "Disable LAN discovery"],
  ["enableDirectIP", "Direct IP access"], ["autoClose", "Auto-disconnect idle"],
];

function buildToggles() {
  $("#perm-toggles").innerHTML = PERMS.map(([k, l]) =>
    `<label class="tg"><input type="checkbox" data-key="${k}"><span>${l}</span></label>`).join("");
  $("#flag-toggles").innerHTML = FLAGS.map(([k, l]) =>
    `<label class="tg"><input type="checkbox" data-key="${k}"><span>${l}</span></label>`).join("");
}

function fillConfig(cfg) {
  state.config = cfg;
  $$("[data-key]").forEach(el => {
    const k = el.dataset.key;
    if (!(k in cfg)) return;
    if (el.type === "checkbox") el.checked = truthy(cfg[k]);
    else el.value = cfg[k] ?? "";
  });
  syncColorPicker();
  restoreBrandingPreview(cfg);
  refreshPreview();
}

function collectConfig() {
  const cfg = { ...state.config };
  $$("[data-key]").forEach(el => {
    const k = el.dataset.key;
    if (el.type === "checkbox") cfg[k] = el.checked ? "on" : false;
    else cfg[k] = el.value;
  });
  return cfg;
}

const truthy = v => v === true || v === "on";

async function refreshPreview() {
  const cfg = collectConfig();
  const p = await api("/api/preview", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
  try {
    $("#custom-preview").textContent = JSON.stringify(JSON.parse(p.custom_txt), null, 2);
  } catch { $("#custom-preview").textContent = p.custom_txt; }
  const e = p.env;
  $("#env-preview").textContent =
    `server : ${e.CUSTOM_SERVER}\nkey    : ${e.CUSTOM_KEY}\napi    : ${e.CUSTOM_API_SERVER}\n` +
    `app    : ${e.CUSTOM_APPNAME}\ncompany: ${e.CUSTOM_COMPNAME}\n` +
    `slogan : ${e.CUSTOM_SLOGAN || "(default: Powered by " + e.CUSTOM_APPNAME + ")"}\n` +
    `icon   : ${e.CUSTOM_ICON_FILE ? e.CUSTOM_ICON_FILE.split("/").pop() : "(default)"}\n` +
    `logo   : ${e.CUSTOM_LOGO_FILE ? e.CUSTOM_LOGO_FILE.split("/").pop() : "(default)"}\n` +
    `theme  : ${e.CUSTOM_THEME_COLOR || "(default blue)"}\n` +
    `flags  : delayFix=${e.CUSTOM_DELAY_FIX} hidecm=${e.CUSTOM_HIDE_CM} ` +
    `xOffline=${e.CUSTOM_X_OFFLINE}`;
}

$("#btn-save-config").addEventListener("click", async () => {
  const cfg = collectConfig();
  const r = await api("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
  const note = $("#save-note");
  note.textContent = r.ok ? "saved ✓" : ("error: " + (r.error || "?"));
  note.style.color = r.ok ? "" : "var(--bad)";
  state.config = cfg;
  setTimeout(() => (note.textContent = ""), 2500);
});

/* ── branding uploads ─────────────────────────────────── */
async function uploadBranding(file, type) {
  const previewId = type + "-preview";
  const clearId = type + "-clear";
  const pathId = type + "-file-path";
  const reader = new FileReader();
  reader.onload = async () => {
    const b64 = reader.result.split(",")[1];
    const r = await api("/api/upload", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, data: b64, filename: file.name }),
    });
    if (r.ok) {
      const ext = file.name.split(".").pop().toLowerCase();
      const imgUrl = `/api/branding/${type}.${ext}?t=${Date.now()}`;
      $("#" + previewId).innerHTML =
        `<img src="${imgUrl}" alt="${esc(type)}"><span class="up-name">${esc(r.filename)}</span>`;
      $("#" + clearId).hidden = false;
      $("#" + pathId).value = r.path;
      refreshPreview();
    } else {
      $("#" + previewId).innerHTML = `<span class="up-name">error: ${esc(r.error || "?")}</span>`;
    }
  };
  reader.readAsDataURL(file);
}

function clearBranding(type) {
  const previewId = type + "-preview";
  const clearId = type + "-clear";
  const pathId = type + "-file-path";
  $("#" + previewId).innerHTML = "";
  $("#" + clearId).hidden = true;
  $("#" + pathId).value = "";
  $("#" + type + "-file").value = "";
  refreshPreview();
}

$("#icon-file").addEventListener("change", e => {
  if (e.target.files[0]) uploadBranding(e.target.files[0], "icon");
});
$("#logo-file").addEventListener("change", e => {
  if (e.target.files[0]) uploadBranding(e.target.files[0], "logo");
});
$("#icon-clear").addEventListener("click", () => clearBranding("icon"));
$("#logo-clear").addEventListener("click", () => clearBranding("logo"));

function restoreBrandingPreview(cfg) {
  for (const type of ["icon", "logo"]) {
    const path = cfg[type + "File"];
    const el = $("#" + type + "-preview");
    const clear = $("#" + type + "-clear");
    if (path) {
      const ext = path.split(".").pop().toLowerCase();
      const imgUrl = `/api/branding/${type}.${ext}?t=${Date.now()}`;
      el.innerHTML = `<img src="${imgUrl}" alt="${type}"><span class="up-name">${esc(path.split("/").pop())}</span>`;
      clear.hidden = false;
    } else {
      el.innerHTML = "";
      clear.hidden = true;
    }
  }
}

document.addEventListener("input", e => {
  if (e.target.closest("#tab-config") && e.target.dataset.key) refreshPreview();
});

/* ── config presence banner ───────────────────────────── */
// When there's no real RustDesk.json, tell the user exactly where to put it
// instead of only failing with a console traceback on the server side.
function renderConfigBanner(st) {
  const el = $("#config-banner");
  if (!el || !st || st.source === "config") {
    if (el) el.hidden = true;
    return;
  }
  const isMissing = st.source === "missing";
  el.hidden = false;
  el.className = "config-banner " + (isMissing ? "is-error" : "is-warn");
  const where = esc(st.expected || st.path || "configs/RustDesk.json");
  const lead = isMissing
    ? "No RustDesk config found — the build has nothing to customize."
    : "Using the bundled example config (RustDesk.example.json).";
  el.innerHTML =
    `<div class="cb-title">${isMissing ? "⚠ Config missing" : "ℹ Using example config"}</div>` +
    `<div class="cb-body">${esc(lead)} To build with your own server, key and ` +
    `password, put your config file here:</div>` +
    `<code class="cb-path">${where}</code>` +
    `<div class="cb-body">Generate one at ` +
    `<a href="https://rdgen.crayoneater.org/" target="_blank" rel="noopener">rdgen.crayoneater.org</a> ` +
    `(download as <strong>RustDesk.json</strong>), or fill in the ` +
    `<a href="#" id="cb-goto-config">Config tab</a> and click Save.</div>`;
  const goto = $("#cb-goto-config");
  if (goto) goto.addEventListener("click", ev => {
    ev.preventDefault();
    $$(".tab").forEach(x => x.classList.remove("is-active"));
    $$(".panel").forEach(x => x.classList.remove("is-active"));
    $('.tab[data-tab="config"]').classList.add("is-active");
    $("#tab-config").classList.add("is-active");
  });
}

/* ── build console ────────────────────────────────────── */
const con = $("#console");
function conLine(text) {
  const span = document.createElement("span");
  let cls = "";
  if (text.startsWith("$ ")) cls = "con-cmd";
  else if (text.startsWith("=== ") || text.startsWith("\n=== ")) cls = "con-head";
  else if (/✓|artifact:|DONE/.test(text)) cls = "con-ok";
  else if (/^\s*!!|FAILED|error/i.test(text)) cls = "con-err";
  else if (/^\s*!|WARNING|warn/i.test(text)) cls = "con-warn";
  if (cls) span.className = cls;
  span.textContent = text + "\n";
  con.appendChild(span);
  con.scrollTop = con.scrollHeight;
}

let timerHandle = null, startedAt = 0;
function setStatus(s) {
  const el = $("#build-status");
  el.textContent = s;
  el.className = "build-status " + (
    s === "running" ? "running" : s === "done" ? "done" : s === "failed" ? "failed" : "");
}
function startTimer() {
  startedAt = Date.now();
  timerHandle = setInterval(() => {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    $("#build-timer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }, 500);
}
function stopTimer() { clearInterval(timerHandle); }

function openStream() {
  const es = new EventSource("/api/build/stream");
  es.onmessage = ev => {
    try { conLine(JSON.parse(ev.data).line); } catch {}
  };
  es.addEventListener("done", () => { es.close(); onBuildEnd(); });
  es.onerror = () => { /* keep-alive gaps are normal; browser retries */ };
}

async function onBuildEnd() {
  stopTimer();
  state.running = false;
  $("#btn-cancel").disabled = true;
  const st = await api("/api/build/status");
  const r = st.result || {};
  setStatus(r.ok ? "done" : (r.cancelled ? "idle" : "failed"));
  renderArtifacts(r.artifacts || []);
  // Remember where the output landed so "Open folder" goes straight there.
  // An artifact may be the packed .exe (open its parent) or the Release
  // folder itself (open it directly).
  const arts = r.artifacts || [];
  if (arts.length) {
    let p = arts[0];
    if (/\.(exe|msi|apk|zip|dmg|deb|rpm|AppImage|flatpak)$/i.test(p)) {
      const sep = p.lastIndexOf("\\") >= 0 ? "\\" : "/";
      p = p.slice(0, p.lastIndexOf(sep));
    }
    state.lastOutputDir = p;
  }
  updateTray();
}

function renderArtifacts(list) {
  $("#artifacts").innerHTML = list.map(a =>
    `<div class="artifact">${esc(a)}</div>`).join("");
}

/* ── copy log / copy errors / open folder ─────────────── */
// Robust clipboard copy: the async Clipboard API needs a secure context, and
// http://127.0.0.1 is treated as secure by most browsers — but fall back to a
// hidden textarea + execCommand so it also works if that ever fails.
async function copyText(text, btn) {
  const done = ok => {
    if (!btn) return;
    const old = btn.textContent;
    btn.textContent = ok ? "Copied ✓" : "Copy failed";
    setTimeout(() => { btn.textContent = old; }, 1500);
  };
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return done(true);
    }
    throw new Error("no async clipboard");
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return done(ok);
    } catch {
      return done(false);
    }
  }
}

// The DOM is the log. Pull the full text, or just the error lines (the same
// lines conLine() marked .con-err, so "Copy errors" matches what's shown red).
function fullLogText() {
  return Array.from(con.childNodes)
    .map(n => n.textContent).join("").replace(/\n+$/, "\n");
}
function errorLogText() {
  const errs = Array.from(con.querySelectorAll("span.con-err"))
    .map(n => n.textContent.replace(/\n$/, ""));
  return errs.length ? errs.join("\n") + "\n" : "";
}

$("#btn-copy-log").addEventListener("click", e => {
  const t = fullLogText().trim();
  if (!t) { return copyText("", e.currentTarget); }
  copyText(t, e.currentTarget);
});
$("#btn-copy-errors").addEventListener("click", e => {
  const t = errorLogText();
  if (!t) {
    const btn = e.currentTarget, old = btn.textContent;
    btn.textContent = "No errors ✓";
    setTimeout(() => { btn.textContent = old; }, 1500);
    return;
  }
  copyText(t, e.currentTarget);
});
$("#btn-open-folder").addEventListener("click", async e => {
  const btn = e.currentTarget, old = btn.textContent;
  // Prefer the exact output dir the last build reported, if we have it;
  // otherwise the server opens the newest output/ folder.
  const opts = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (state.lastOutputDir) opts.body = JSON.stringify({ path: state.lastOutputDir });
  else opts.body = JSON.stringify({});
  const r = await api("/api/open-folder", opts);
  if (r && r.error) {
    btn.textContent = "Can't open";
    setTimeout(() => { btn.textContent = old; }, 1500);
  }
});

$("#btn-build").addEventListener("click", () => startBuild(false));
$("#btn-plan").addEventListener("click", () => startBuild(true));
$("#btn-cancel").addEventListener("click", () =>
  api("/api/build/cancel", { method: "POST" }));

async function startBuild(dry) {
  if (state.selected.size === 0) return;
  // jump to console tab
  $$(".tab").forEach(x => x.classList.remove("is-active"));
  $$(".panel").forEach(x => x.classList.remove("is-active"));
  $('.tab[data-tab="console"]').classList.add("is-active");
  $("#tab-console").classList.add("is-active");

  con.innerHTML = "";
  renderArtifacts([]);
  const payload = {
    version: $("#version").value.trim() || "latest",
    targets: [...state.selected],
    dry_run: dry || $("#dry-run").checked,
  };

  // preflight (non-dry): warn about missing tools but let the user proceed
  if (!payload.dry_run) {
    const pf = await api("/api/build/preflight", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!pf.ok) {
      conLine("! preflight found missing toolchains:");
      pf.problems.forEach(p => conLine("    - " + p));
      conLine("  (fix these in the Toolchain panel, or use Dry run to preview)\n");
    }
  }

  const r = await api("/api/build/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) { conLine("!! " + (r.message || r.error || "could not start")); return; }

  state.running = true;
  $("#btn-cancel").disabled = payload.dry_run;
  $("#btn-build").disabled = true;
  setStatus("running");
  startTimer();
  openStream();
}

/* ── util ─────────────────────────────────────────────── */
function esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ── boot ─────────────────────────────────────────────── */
async function loadMatrix() {
  const m = await api("/api/matrix");
  renderSpec(m.host);
  state.targets = m.targets;
  renderBoard();
  const ready = m.targets.filter(t => t.ready).length;
  const buildable = m.targets.filter(t => t.buildable).length;
  $("#matrix-lede").textContent =
    `This ${m.host.os} machine can build ${buildable} target${buildable === 1 ? "" : "s"}; ` +
    `${ready} ${ready === 1 ? "is" : "are"} ready to go right now.`;
}

async function boot() {
  buildToggles();
  await loadToolchains();
  await Promise.all([
    loadMatrix(),
    api("/api/prereqs").then(renderPrereqs),
    api("/api/config").then(fillConfig),
    api("/api/config/status").then(renderConfigBanner),
  ]);
}
$("#recheck").addEventListener("click", async () => {
  $("#prereqs").innerHTML = '<li class="muted">re-scanning…</li>';
  await loadToolchains();
  await api("/api/prereqs").then(renderPrereqs);
  await loadMatrix();
});

boot();

/* ── theme color picker ───────────────────────────────── */
const cp = $("#themeColorPicker");
const hue = $("#themeColorHue");
const hex = $("#themeColorHex");
const swatch = $("#themeColorSwatch");

function hslToHex(h, s, l) {
  s /= 100; l /= 100;
  const k = n => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = n => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  const toHex = x => Math.round(255 * x).toString(16).padStart(2, "0");
  return `#${toHex(f(0))}${toHex(f(8))}${toHex(f(4))}`.toUpperCase();
}

function hexToHue(hexVal) {
  hexVal = hexVal.replace("#", "");
  if (hexVal.length !== 6) return 212;
  const r = parseInt(hexVal.slice(0, 2), 16) / 255;
  const g = parseInt(hexVal.slice(2, 4), 16) / 255;
  const b = parseInt(hexVal.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  if (max === min) return 0;
  let h;
  if (max === r) h = ((g - b) / (max - min) + (g < b ? 6 : 0)) * 60;
  else if (max === g) h = ((b - r) / (max - min) + 2) * 60;
  else h = ((r - g) / (max - min) + 4) * 60;
  return Math.round(h);
}

function syncColorPicker() {
  const v = hex.value || "#0071FF";
  cp.value = v;
  hue.value = hexToHue(v);
  swatch.style.background = v;
}

cp.addEventListener("input", () => {
  hex.value = cp.value.toUpperCase();
  hue.value = hexToHue(cp.value);
  swatch.style.background = cp.value;
  refreshPreview();
});

hue.addEventListener("input", () => {
  const v = hslToHex(+hue.value, 80, 50);
  hex.value = v;
  cp.value = v;
  swatch.style.background = v;
  refreshPreview();
});

hex.addEventListener("input", () => {
  const v = hex.value.trim();
  if (/^#[0-9A-Fa-f]{6}$/.test(v)) {
    cp.value = v;
    hue.value = hexToHue(v);
    swatch.style.background = v;
    refreshPreview();
  }
});
