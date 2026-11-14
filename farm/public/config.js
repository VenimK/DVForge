"use strict";

/* Farm config editor.
 *
 * The form markup is the exact Config panel from web/index.html (queue.py
 * injects it into config.html), and style.css / advanced-keys.js are served
 * straight from web/. This script only adds the browser-side behaviour the
 * local app normally gets from its /api endpoints:
 *
 *   - load an existing RustDesk.json from disk
 *   - edit it with the same fields, toggles, colours and advanced keys
 *   - download a new RustDesk.json to this computer
 *   - preview custom_.txt through the queue's /config/preview endpoint
 *
 * Nothing is written to the queue. Icon/logo/signing files are embedded as
 * *base64 blobs right in the JSON, which is exactly what a remote worker needs.
 */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const state = { config: {} };

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

const THEME_DEFAULTS = {
  theme: "system",
  themeDorO: "default",
  themeColor: "#0071FF",
  themeSurfaceLight: "#EFEFF2",
  themeSurfaceDark: "#18191E",
  themeMeColor: "#21790B",
};

/* File-backed settings that must travel with a farm job. The local app stores
 * files under workspace/; here the browser embeds the bytes in the JSON. */
const ASSETS = [
  { pathKey: "iconFile", blobKey: "iconbase64", type: "icon",
    input: "icon-file", hidden: "icon-file-path", preview: "icon-preview",
    clear: "icon-clear", image: true },
  { pathKey: "logoFile", blobKey: "logobase64", type: "logo",
    input: "logo-file", hidden: "logo-file-path", preview: "logo-preview",
    clear: "logo-clear", image: true },
  { pathKey: "signWinPfx", blobKey: "signWinPfxBase64", type: "winpfx",
    input: "winpfx-file", hidden: "winpfx-file-path", preview: "winpfx-preview",
    clear: "winpfx-clear" },
  { pathKey: "signAndroidKeystore", blobKey: "signAndroidKeystoreBase64", type: "androidks",
    input: "androidks-file", hidden: "androidks-file-path", preview: "androidks-preview",
    clear: "androidks-clear" },
  { pathKey: "signMacP12", blobKey: "signMacP12Base64", type: "macp12",
    input: "macp12-file", hidden: "macp12-file-path", preview: "macp12-preview",
    clear: "macp12-clear" },
  { pathKey: "signMacNotaryKey", blobKey: "signMacNotaryKeyBase64", type: "macnotary",
    input: "macnotary-file", hidden: "macnotary-file-path", preview: "macnotary-preview",
    clear: "macnotary-clear" },
];
const BLOB_KEYS = ASSETS.map(a => a.blobKey);

/* ── form construction ────────────────────────────────── */
function buildToggles() {
  $("#perm-toggles").innerHTML = PERMS.map(([k, l]) =>
    `<label class="tg"><input type="checkbox" data-key="${k}"><span>${l}</span></label>`).join("");
  $("#flag-toggles").innerHTML = FLAGS.map(([k, l]) =>
    `<label class="tg"><input type="checkbox" data-key="${k}"><span>${l}</span></label>`).join("");
}

function androidIdSeg(s, fallback) {
  s = String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  if (!s) return fallback;
  if (/^\d/.test(s)) s = "a" + s;
  return s;
}

function deriveAndroidAppId(appname, compname) {
  const app = androidIdSeg(appname, "client");
  if (app === "rustdesk") return "com.carriez.flutter_hbb";
  const comp = androidIdSeg(compname, "");
  if (!comp || comp === app) return `com.${app}.client`;
  return `com.${comp}.${app}`;
}

function newConfig() {
  return {
    direction: "both",
    installation: "installationY",
    settings: "settingsY",
    passApproveMode: "password-click",
    theme: "system",
    themeDorO: "default",
    themeColor: "#0071FF",
    themeSurfaceLight: "#EFEFF2",
    themeSurfaceDark: "#18191E",
    themeMeColor: "#21790B",
    signWinTimestamp: "http://timestamp.digicert.com",
  };
}

function resetFields() {
  $$("[data-key]").forEach(el => {
    if (el.type === "checkbox") el.checked = false;
    else if (el.tagName === "SELECT") {
      const first = [...el.options].find(o => !o.disabled);
      el.value = first ? first.value : "";
    } else el.value = "";
  });
}

function fillConfig(cfg) {
  state.config = cfg;
  for (const [k, v] of Object.entries(THEME_DEFAULTS)) {
    if (!cfg[k]) cfg[k] = v;
  }
  if (!String(cfg.androidappid || "").trim()) {
    cfg.androidappid = deriveAndroidAppId(cfg.appname, cfg.compname);
  }
  if (!cfg.installation) cfg.installation = "installationY";
  if (!cfg.settings) cfg.settings = "settingsY";
  if (!cfg.signWinTimestamp) cfg.signWinTimestamp = "http://timestamp.digicert.com";

  resetFields();
  $$("[data-key]").forEach(el => {
    const k = el.dataset.key;
    if (!(k in cfg)) return;
    if (el.type === "checkbox") el.checked = truthy(cfg[k]);
    else el.value = cfg[k] == null ? "" : cfg[k];
  });
  syncAllColorPickers();
  updateThemeLive();
  restoreAssets(cfg);
  renderAdvList();
  refreshPreview();
}

function collectConfig() {
  const cfg = { ...state.config };
  $$("[data-key]").forEach(el => {
    const k = el.dataset.key;
    if (el.type === "checkbox") cfg[k] = el.checked ? "on" : false;
    else cfg[k] = el.value;
  });
  if (!String(cfg.androidappid || "").trim()) {
    cfg.androidappid = deriveAndroidAppId(cfg.appname, cfg.compname);
  }
  return cfg;
}

const truthy = v => v === true || v === "on";

/* ── advanced keys catalog ────────────────────────────── */
const ADV_DEFAULT = "1.5.1";
let advFilter = "all";
let advTarget = "default";

function advPin() {
  const el = $("#version");
  return (el && el.value.trim()) || ADV_DEFAULT;
}

function advLines(which) {
  const el = which === "override" ? $("#override-manual") : $("#default-manual");
  const out = {};
  for (const raw of String(el && el.value || "").split(/\r?\n/)) {
    const line = raw.trim();
    const eq = line.indexOf("=");
    if (eq < 1) continue;
    const k = line.slice(0, eq).trim();
    if (k) out[k] = true;
  }
  return out;
}

function advVerNewer(min) {
  if (!min) return false;
  const pin = advPin();
  if (!/^\d/.test(pin)) return false; // branch builds (master) are newest
  const a = String(min).split(".").map(n => parseInt(n, 10) || 0);
  const b = pin.split(".").map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < 3; i++) {
    if ((a[i] || 0) > (b[i] || 0)) return true;
    if ((a[i] || 0) < (b[i] || 0)) return false;
  }
  return false;
}

function advNote(msg, bad) {
  const el = $("#adv-note");
  if (!el) return;
  el.textContent = msg || "";
  el.style.color = bad ? "var(--bad)" : "";
}

function setAdvTarget(which) {
  advTarget = which === "override" ? "override" : "default";
  const hint = $("#adv-target-hint");
  if (hint) {
    hint.textContent = "Click Default or Override on a row. Last box used: "
      + (advTarget === "override" ? "Override (locked)." : "Default.");
  }
}

function insertAdvKey(item, into) {
  const which = into === "override" ? "override" : "default";
  const ta = which === "override" ? $("#override-manual") : $("#default-manual");
  if (!ta) return;
  setAdvTarget(which);
  if (advVerNewer(item.min)) {
    advNote(item.key + " needs RustDesk " + item.min + ". This build is "
      + advPin() + " — the client will ignore it.", true);
  } else if (item.owned) {
    advNote(item.key + " is already set by " + item.owned
      + " above. Inserting here overwrites that value in "
      + (which === "override" ? "override-settings" : "default-settings") + ".", false);
  } else {
    advNote("");
  }
  const sample = item.sample != null ? item.sample : "";
  const line = item.key + "=" + sample;
  const kept = [];
  let found = false;
  for (const raw of ta.value.split(/\r?\n/)) {
    const t = raw.trim();
    if (!t) continue;
    if (t.slice(0, Math.max(0, t.indexOf("="))).trim() === item.key) {
      kept.push(line);
      found = true;
    } else {
      kept.push(raw.replace(/\s+$/, ""));
    }
  }
  if (!found) kept.push(line);
  ta.value = kept.join("\n") + (kept.length ? "\n" : "");
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  ta.focus();
}

function renderAdvList() {
  const host = $("#adv-list");
  if (!host) return;
  const keys = window.ADVANCED_KEYS || [];
  const q = String(($("#adv-search") || {}).value || "").trim().toLowerCase();
  const inDef = advLines("default");
  const inOver = advLines("override");
  const groups = [];
  let last = "";
  let n = 0;
  for (const item of keys) {
    if (advFilter === "useful" && !item.useful) continue;
    if (advFilter === "owned" && !item.owned) continue;
    if (q) {
      const blob = [item.key, item.hint, item.group, item.values, item.owned || ""]
        .join(" ").toLowerCase();
      if (!blob.includes(q)) continue;
    }
    if (item.group !== last) {
      groups.push(`<div class="adv-group">${esc(item.group)}</div>`);
      last = item.group;
    }
    const future = advVerNewer(item.min);
    const placed = inDef[item.key] ? "default" : (inOver[item.key] ? "override" : "");
    const cls = [
      "adv-row",
      item.owned ? "is-owned" : "",
      future ? "is-future" : "",
      placed ? "is-in" : "",
    ].filter(Boolean).join(" ");
    const badges = [];
    if (item.useful) badges.push('<span class="adv-badge useful">suggested</span>');
    if (item.owned) badges.push(`<span class="adv-badge owned">${esc(item.owned)}</span>`);
    if (item.pro) badges.push('<span class="adv-badge pro">Pro</span>');
    if (future) badges.push(`<span class="adv-badge future">${esc(item.min)}+</span>`);
    else if (item.min) badges.push(`<span class="adv-badge">since ${esc(item.min)}</span>`);
    if (placed) badges.push(`<span class="adv-badge placed">in ${placed}</span>`);
    groups.push(
      `<div class="${cls}" data-adv-key="${esc(item.key)}" role="listitem">`
      + `<span class="adv-k">${esc(item.key)}</span>`
      + `<span class="adv-badges">${badges.join("")}</span>`
      + `<span class="adv-meta">${esc(item.hint)}`
      + ` <span class="adv-vals">${esc(item.values)}`
      + (item.def ? ` · default ${esc(item.def)}` : "")
      + `</span></span>`
      + `<span class="adv-insert">`
      + `<button type="button" class="mini" data-adv-into="default">Default</button>`
      + `<button type="button" class="mini" data-adv-into="override">Override</button>`
      + `</span></div>`
    );
    n++;
  }
  host.innerHTML = n
    ? groups.join("")
    : '<div class="adv-empty">No keys match that search.</div>';
}

function initAdvancedKeys() {
  if (!Array.isArray(window.ADVANCED_KEYS)) {
    console.warn("advanced-keys.js did not load");
    return;
  }
  const search = $("#adv-search");
  if (search) search.addEventListener("input", renderAdvList);
  const verSel = $("#version");
  if (verSel) verSel.addEventListener("change", renderAdvList);
  $$("[data-adv-filter]").forEach(btn => {
    btn.addEventListener("click", () => {
      advFilter = btn.dataset.advFilter || "all";
      $$("[data-adv-filter]").forEach(b => b.classList.toggle("is-on", b === btn));
      renderAdvList();
    });
  });
  const list = $("#adv-list");
  if (list) {
    list.addEventListener("click", e => {
      const btn = e.target.closest("[data-adv-into]");
      if (!btn) return;
      const row = btn.closest("[data-adv-key]");
      if (!row) return;
      const item = (window.ADVANCED_KEYS || []).find(k => k.key === row.dataset.advKey);
      if (item) insertAdvKey(item, btn.dataset.advInto);
    });
  }
  ["default-manual", "override-manual"].forEach(id => {
    const el = $("#" + id);
    if (!el) return;
    el.addEventListener("focus", () =>
      setAdvTarget(id === "override-manual" ? "override" : "default"));
  });
  renderAdvList();
}

/* ── custom_.txt preview ──────────────────────────────── */
let previewSeq = 0;
let previewTimer = null;

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 250);
}

async function refreshPreview() {
  const seq = ++previewSeq;
  const cfg = collectConfig();
  // Keep the request small: config_gen only needs the path keys, never the
  // multi-megabyte base64 asset blobs. The blobs stay in the form/state and
  // are still written to the downloaded JSON.
  const previewCfg = { ...cfg };
  BLOB_KEYS.forEach(k => { delete previewCfg[k]; });
  let p;
  try {
    const r = await fetch("/config/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(previewCfg),
    });
    p = await r.json().catch(() => ({}));
    if (!r.ok || p.error) throw new Error(p.error || ("HTTP " + r.status));
  } catch (e) {
    if (seq !== previewSeq) return;
    $("#custom-preview").textContent =
      "(preview unavailable on this queue)\n\n" + (e.message || String(e));
    $("#env-preview").textContent = "(preview unavailable)";
    return;
  }
  if (seq !== previewSeq) return;
  try {
    $("#custom-preview").textContent = JSON.stringify(JSON.parse(p.custom_txt), null, 2);
  } catch { $("#custom-preview").textContent = p.custom_txt; }
  const e = p.env;
  $("#env-preview").textContent =
    `server : ${e.CUSTOM_SERVER}\nkey    : ${e.CUSTOM_KEY}\napi    : ${e.CUSTOM_API_SERVER}\n` +
    `app    : ${e.CUSTOM_APPNAME}\ncompany: ${e.CUSTOM_COMPNAME}\n` +
    `android: ${e.CUSTOM_ANDROID_APP_ID || "(stock com.carriez.flutter_hbb)"}\n` +
    `slogan : ${e.CUSTOM_SLOGAN || "(default: Powered by " + e.CUSTOM_APPNAME + ")"}\n` +
    `icon   : ${e.CUSTOM_ICON_FILE ? e.CUSTOM_ICON_FILE.split("/").pop() : "(default)"}\n` +
    `logo   : ${e.CUSTOM_LOGO_FILE ? e.CUSTOM_LOGO_FILE.split("/").pop() : "(default)"}\n` +
    `theme  : ${e.CUSTOM_THEME_MODE || "system"} (${e.CUSTOM_THEME_DORO || "default"})\n` +
    `accent : ${e.CUSTOM_THEME_COLOR || "(stock blue)"}\n` +
    `light  : ${e.CUSTOM_THEME_SURFACE_LIGHT || "(stock)"}\n` +
    `dark   : ${e.CUSTOM_THEME_SURFACE_DARK || "(stock)"}\n` +
    `id/me  : ${e.CUSTOM_THEME_ME_COLOR || "(stock green)"}\n` +
    `flags  : delayFix=${e.CUSTOM_DELAY_FIX} hidecm=${e.CUSTOM_HIDE_CM} ` +
    `xOffline=${e.CUSTOM_X_OFFLINE}\n` +
    `dir    : ${cfg.direction || "both"}\n` +
    `lock   : install=${cfg.installation === "installationN" ? "disabled" : "allowed"} ` +
    `settings=${cfg.settings === "settingsN" ? "disabled" : "allowed"}\n` +
    `sign   : win=${cfg.signWinPfx ? "Authenticode" : "unsigned"} ` +
    `android=${cfg.signAndroidKeystore ? "keystore" : "debug"} ` +
    `mac=${/developer id/i.test(cfg.signMacIdentity || "") ? "Developer ID" : (cfg.signMacP12 || cfg.signMacIdentity ? "self-signed" : "ad-hoc")}`;
}

/* ── asset uploads (browser side) ─────────────────────── */
function readDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("could not read file"));
    reader.readAsDataURL(file);
  });
}

function mimeForImage(path, b64) {
  const ext = String(path || "").split(".").pop().toLowerCase();
  if (ext === "svg") return "image/svg+xml";
  if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
  if (ext === "ico") return "image/x-icon";
  if (ext === "webp") return "image/webp";
  if (ext === "gif") return "image/gif";
  if (b64.startsWith("PHN2Zy") || b64.startsWith("PHN2Z")) return "image/svg+xml";
  if (b64.startsWith("/9j/")) return "image/jpeg";
  if (b64.startsWith("R0lGOD")) return "image/gif";
  if (b64.startsWith("UklGR")) return "image/webp";
  return "image/png";
}

function setJsonStatus(text, kind) {
  const el = $("#json-status");
  if (!el) return;
  el.textContent = text;
  el.className = "json-note" + (kind ? " is-" + kind : "");
}

function showAsset(spec) {
  const el = $("#" + spec.preview);
  const clear = $("#" + spec.clear);
  const cfg = state.config;
  const blob = cfg[spec.blobKey];
  const path = cfg[spec.pathKey];
  if (!el) return;
  if (spec.image && typeof blob === "string" && blob.length > 20) {
    el.innerHTML = `<img src="data:${mimeForImage(path, blob)};base64,${blob}" `
      + `alt="${esc(spec.type)}"><span class="up-name">`
      + esc(String(path || "embedded image").split("/").pop()) + `</span>`;
  } else if (typeof blob === "string" && blob.length > 20) {
    el.innerHTML = `<span class="up-name">`
      + esc(String(path || "embedded file").split("/").pop()) + ` (embedded)</span>`;
  } else if (path) {
    el.innerHTML = `<span class="up-name">${esc(String(path).split("/").pop())}</span>`;
  } else {
    el.innerHTML = "";
  }
  if (clear) clear.hidden = !path && !(typeof blob === "string" && blob.length > 20);
}

function restoreAssets(cfg) {
  ASSETS.forEach(spec => {
    const hidden = $("#" + spec.hidden);
    if (hidden) hidden.value = cfg[spec.pathKey] || "";
    showAsset(spec);
  });
}

async function uploadAsset(file, spec) {
  const dataUrl = await readDataURL(file);
  const b64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
  state.config[spec.blobKey] = b64;
  state.config[spec.pathKey] = file.name;
  const hidden = $("#" + spec.hidden);
  if (hidden) hidden.value = file.name;
  showAsset(spec);
  schedulePreview();
}

function clearAsset(spec) {
  delete state.config[spec.blobKey];
  state.config[spec.pathKey] = "";
  const hidden = $("#" + spec.hidden);
  if (hidden) hidden.value = "";
  const input = $("#" + spec.input);
  if (input) input.value = "";
  showAsset(spec);
  schedulePreview();
}

function initAssets() {
  ASSETS.forEach(spec => {
    const input = $("#" + spec.input);
    const clear = $("#" + spec.clear);
    if (input) input.addEventListener("change", async e => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      try {
        await uploadAsset(file, spec);
      } catch (err) {
        setJsonStatus("could not read " + file.name + ": " + (err.message || err), "bad");
      }
    });
    if (clear) clear.addEventListener("click", () => clearAsset(spec));
  });

  // The local app can create self-signed certificates with desktop tools.
  // A browser cannot, so say what to do instead of showing a dead button.
  const winNote = $("#winpfx-selfsigned-note");
  if (winNote) winNote.textContent =
    "Optional. Upload an existing .pfx/.p12; it is embedded in this JSON so the "
    + "worker can sign the Windows build. Create one locally in DVForge if needed.";
  const ksNote = $("#androidks-create-note");
  if (ksNote) ksNote.textContent =
    "Optional. Upload an existing .jks/.keystore; it is embedded in this JSON so "
    + "the worker can sign the APK. Create one locally in DVForge if needed.";
  const macNote = $("#macp12-selfsigned-note");
  if (macNote) macNote.textContent =
    "Optional. Upload an existing .p12; it is embedded in this JSON so the Mac "
    + "worker can sign. A Developer ID identity must already exist on that Mac.";
}

/* ── colour pickers + live theme preview ──────────────── */
function hslToHex(h, s, l) {
  s /= 100; l /= 100;
  const k = n => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = n => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  const toHex = x => Math.round(255 * x).toString(16).padStart(2, "0");
  return `#${toHex(f(0))}${toHex(f(8))}${toHex(f(4))}`.toUpperCase();
}

function hexToHue(hexVal) {
  hexVal = (hexVal || "").replace("#", "");
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

function mixHex(a, b, t) {
  const p = h => {
    h = h.replace("#", "");
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  };
  const [r1, g1, b1] = p(a), [r2, g2, b2] = p(b);
  const c = (x, y) => Math.round(x + (y - x) * t).toString(16).padStart(2, "0");
  return `#${c(r1, r2)}${c(g1, g2)}${c(b1, b2)}`.toUpperCase();
}

function colorPickerParts(row) {
  return {
    native: $("input[type=color]", row),
    hue: $("input[type=range]", row),
    hex: $("input[data-key]", row),
    swatch: $(".color-swatch", row),
    fallback: row.dataset.fallback || "#0071FF",
  };
}

function applyPickerVisual(row, hexVal) {
  const p = colorPickerParts(row);
  const v = hexVal || p.fallback;
  if (p.native) p.native.value = v;
  if (p.hue) p.hue.value = hexToHue(v);
  if (p.swatch) p.swatch.style.background = v;
}

function syncAllColorPickers() {
  $$(".color-picker[data-color]").forEach(row => {
    const p = colorPickerParts(row);
    applyPickerVisual(row, (p.hex && p.hex.value) || p.fallback);
  });
}

function bindColorPickers() {
  $$(".color-picker[data-color]").forEach(row => {
    const p = colorPickerParts(row);
    if (p.native) p.native.addEventListener("input", () => {
      const v = p.native.value.toUpperCase();
      if (p.hex) p.hex.value = v;
      applyPickerVisual(row, v);
      updateThemeLive();
      schedulePreview();
    });
    if (p.hue) p.hue.addEventListener("input", () => {
      const v = hslToHex(+p.hue.value, 80, 50);
      if (p.hex) p.hex.value = v;
      applyPickerVisual(row, v);
      updateThemeLive();
      schedulePreview();
    });
    if (p.hex) p.hex.addEventListener("input", () => {
      const v = p.hex.value.trim();
      if (/^#[0-9A-Fa-f]{6}$/.test(v)) {
        applyPickerVisual(row, v.toUpperCase());
        updateThemeLive();
        schedulePreview();
      }
    });
  });
}

function themeHex(key) {
  const el = $(`[data-key="${key}"]`);
  const v = (el && el.value || "").trim();
  return /^#[0-9A-Fa-f]{6}$/.test(v) ? v.toUpperCase() : (THEME_DEFAULTS[key] || "#0071FF");
}

function hexRgb(h) {
  h = (h || "").replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function luma(h) {
  const [r, g, b] = hexRgb(h);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
}

function tooClose(a, b) {
  const [r1, g1, b1] = hexRgb(a), [r2, g2, b2] = hexRgb(b);
  const dist = Math.hypot(r1 - r2, g1 - g2, b1 - b2);
  return dist < 55 || Math.abs(luma(a) - luma(b)) < 0.14;
}

function contrastOn(fg, bg) {
  if (!tooClose(fg, bg)) return fg;
  return mixHex(fg, luma(bg) > 0.5 ? "#000000" : "#FFFFFF", 0.38);
}

function updateThemeLive() {
  const win = $("#theme-live");
  if (!win) return;
  const mode = win.dataset.mode || "dark";
  const accent = themeHex("themeColor");
  const light = themeHex("themeSurfaceLight");
  const dark = themeHex("themeSurfaceDark");
  const me = themeHex("themeMeColor");
  const surface = mode === "light" ? light : dark;
  const title = mode === "light" ? "#FFFFFF" : "#18191E";
  win.style.setProperty("--tp-bg", surface);
  win.style.setProperty("--tp-card", surface);
  win.style.setProperty("--tp-accent", accent);
  win.style.setProperty("--tp-btn", contrastOn(accent, surface));
  win.style.setProperty("--tp-me", me);
  win.style.setProperty("--tp-title", title);
  const app = $("[data-key=appname]");
  if ($("#tp-app")) $("#tp-app").textContent = (app && app.value) || "App";
  const lightBtn = $("#tp-light"), darkBtn = $("#tp-dark");
  if (lightBtn) lightBtn.classList.toggle("is-on", mode === "light");
  if (darkBtn) darkBtn.classList.toggle("is-on", mode === "dark");
}

function bindThemeLive() {
  const setMode = mode => {
    const win = $("#theme-live");
    if (!win) return;
    win.dataset.mode = mode;
    updateThemeLive();
  };
  const lightBtn = $("#tp-light"), darkBtn = $("#tp-dark");
  if (lightBtn) lightBtn.addEventListener("click", () => setMode("light"));
  if (darkBtn) darkBtn.addEventListener("click", () => setMode("dark"));
  const themeSel = $("[data-key=theme]");
  if (themeSel) themeSel.addEventListener("change", () => {
    if (themeSel.value === "light" || themeSel.value === "dark") setMode(themeSel.value);
    schedulePreview();
  });
  const doro = $("[data-key=themeDorO]");
  if (doro) doro.addEventListener("change", schedulePreview);
  const app = $("[data-key=appname]");
  if (app) app.addEventListener("input", updateThemeLive);
}

/* ── load / save JSON ─────────────────────────────────── */
function safeFilename(cfg) {
  const raw = String(cfg.exename || cfg.appname || "RustDesk").trim();
  const name = raw.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-._]+|[-._]+$/g, "");
  return (name || "RustDesk") + ".json";
}

function downloadedFilename() {
  const input = $("#json-filename");
  let name = (input && input.value || "").trim();
  if (!name) name = safeFilename(collectConfig());
  name = name.replace(/[\\/:*?"<>|]+/g, "-");
  if (!/\.json$/i.test(name)) name += ".json";
  return name;
}

function saveJson() {
  const cfg = collectConfig();
  state.config = cfg;
  const name = downloadedFilename();
  const text = JSON.stringify(cfg, null, 2) + "\n";
  const blob = new Blob([text], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1500);

  const input = $("#json-filename");
  if (input) input.value = name;
  setJsonStatus("saved " + name + " ✓", "ok");
  const note = $("#save-note");
  if (note) {
    note.textContent = "downloaded " + name + " ✓";
    note.style.color = "";
    setTimeout(() => { note.textContent = ""; }, 3000);
  }
}

async function loadJsonFile(file) {
  let cfg;
  try {
    cfg = JSON.parse(await file.text());
  } catch (err) {
    setJsonStatus("that file is not valid JSON: " + (err.message || err), "bad");
    return;
  }
  if (!cfg || typeof cfg !== "object" || Array.isArray(cfg)) {
    setJsonStatus("that file must contain a JSON object (a RustDesk config)", "bad");
    return;
  }
  fillConfig(cfg);
  const input = $("#json-filename");
  if (input) input.value = file.name || "RustDesk.json";
  setJsonStatus("loaded " + file.name + " — edit, then save a new JSON.", "ok");
}

function startNew() {
  fillConfig(newConfig());
  const input = $("#json-filename");
  if (input) input.value = "RustDesk.json";
  setJsonStatus("New config — fill in the form, then save.", "");
}

function initJsonControls() {
  const load = $("#json-load");
  if (load) load.addEventListener("change", e => {
    const file = e.target.files && e.target.files[0];
    if (file) loadJsonFile(file);
    e.target.value = ""; // allow loading the same file again
  });
  const save = $("#json-save");
  if (save) save.addEventListener("click", saveJson);
  const formSave = $("#btn-save-config");
  if (formSave) formSave.addEventListener("click", saveJson);
  const fresh = $("#json-new");
  if (fresh) fresh.addEventListener("click", startNew);
}

/* ── input wiring / boot ──────────────────────────────── */
function initInputs() {
  const handler = e => {
    if (!e.target.dataset || !e.target.dataset.key) return;
    if (e.target.id === "default-manual" || e.target.id === "override-manual") renderAdvList();
    schedulePreview();
  };
  document.addEventListener("input", handler);
  document.addEventListener("change", handler);
}

function boot() {
  buildToggles();
  initAdvancedKeys();
  initAssets();
  initJsonControls();
  initInputs();
  bindColorPickers();
  bindThemeLive();
  const saveLabel = $("#btn-save-config");
  if (saveLabel) saveLabel.textContent = "Save JSON";
  startNew();
}

boot();
