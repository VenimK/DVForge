"use strict";

async function loadEnvironment() {
  const result = await api("/api/environment");
  state.environment = result;
  const rows = result.requirements || [];
  $("#environment-requirements").innerHTML = rows.map(row =>
    `<li class="pr" data-environment-tool="${esc(row.id)}"><div class="pr-top"><span class="pr-dot ${row.present ? "ok" : "no"}"></span>` +
    `<span class="pr-name">${esc(row.label)}</span><span class="pr-ver">${esc(row.status)}</span></div></li>`).join("");
  $("#environment-next").disabled = !result.ok;
  $("#environment-status").textContent = result.ok ? "Environment ready" : (result.problems || [result.error]).filter(Boolean).join("; ");
}

$("#environment-next").addEventListener("click", () => showStep("config"));
$("#config-next").addEventListener("click", async () => {
  const config = collectConfig();
  const errors = [];
  if (!(config.appname || "").trim()) errors.push("Application name required");
  if (config.androidappid && !/^[a-zA-Z][\w]*(\.[a-zA-Z][\w]*)+$/.test(config.androidappid)) errors.push("Invalid Android application ID");
  if (errors.length) {
    $("#config-validation").textContent = errors.join("; ");
    return;
  }
  try {
    const result = await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    if (!result.ok) throw new Error(result.error || "Configuration rejected");
    state.config = config;
    $("#config-validation").textContent = "Configuration saved";
    showStep("targets");
  } catch (error) { $("#config-validation").textContent = error.message; }
});

async function reviewBuild() {
  showStep("review");
  $("#review-start").disabled = true;
  $("#review-summary").textContent = "Checking selected targets...";
  const result = await api("/api/build/preflight", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version: $("#version").value, targets: [...state.selected] }) });
  if (result.error) throw new Error(result.error);
  const targets = state.targets.filter(t => state.selected.has(t.id));
  const host = result.host || state.host;
  $("#review-summary").innerHTML = `<dl><dt>Host</dt><dd>${esc(host.os)} ${esc(host.arch)}</dd>` +
    `<dt>Version</dt><dd>${esc($("#version").value)}</dd><dt>Configuration</dt><dd>${esc(state.config.appname || "RustDesk")}</dd></dl>` +
    `<ul>${targets.map(t => `<li>${esc(t.label)} (${esc(t.route || "local")})</li>`).join("")}</ul>` +
    `<h2>Output</h2><ul>${[...new Set(result.outputs || [])].map(p => `<li><code>${esc(p)}</code></li>`).join("")}</ul>` +
    `<ul>${(result.problems || []).map(p => `<li>${esc(p)}</li>`).join("")}</ul>`;
  $("#review-start").disabled = !targets.length || (!result.ok && !$("#auto-install").checked && !$("#dry-run").checked);
}
$("#review-back").addEventListener("click", () => showStep("targets"));
$("#review-start").addEventListener("click", () => startBuild(false).catch(reportStartError));

$("#prepare-environment").addEventListener("click", async () => {
  const button = $("#prepare-environment");
  if (!$("#auto-install").checked) {
    await loadEnvironment();
    return;
  }
  button.disabled = true;
  $("#environment-status").textContent = "Preparing environment...";
  try {
    const result = await api("/api/environment/install", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ targets: state.environment.targets }) });
    if (!result.ok) throw new Error(result.error || result.message || "Environment preparation failed");
    $("#install-log").hidden = false;
    const stream = new EventSource("/api/toolchains/stream");
    stream.onmessage = event => {
      try {
        const line = JSON.parse(event.data).line;
        installLine(line);
        const status = line.match(/^Environment: ([a-z_]+): (installed|installing|failed)/);
        if (status) {
          const row = $(`[data-environment-tool="${status[1]}"] .pr-ver`);
          if (row) row.textContent = status[2];
        }
      } catch {}
    };
    stream.addEventListener("done", async () => {
      stream.close();
      button.disabled = false;
      const status = await api("/api/toolchains/status");
      await loadEnvironment();
      await loadMatrix();
      if (status.result?.errors?.length) $("#environment-status").textContent = status.result.errors.map(e => e.join(": ")).join("; ");
    });
  } catch (error) {
    button.disabled = false;
    $("#environment-status").textContent = error.message;
  }
});
