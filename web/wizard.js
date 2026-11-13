"use strict";

async function loadEnvironment() {
  const result = await api("/api/environment");
  state.environment = result;
  // The full toolchain list is /api/prereqs (machine-wide). This endpoint is
  // only the wizard gate for the default/primary target.
  $("#environment-next").disabled = !result.ok;
  const labels = (result.targets || [])
    .map(id => (state.targets || []).find(t => t.id === id)?.label || id);
  if (result.ok) {
    $("#environment-status").textContent = labels.length
      ? `Ready for ${labels.join(", ")}.`
      : "Environment ready.";
  } else {
    $("#environment-status").textContent =
      (result.problems || [result.error]).filter(Boolean).join("; ");
  }

  // Compact left-rail headline: the wizard gate, not a second full list.
  const summary = $("#toolchain-summary");
  if (summary) {
    const missing = (result.requirements || []).filter(r => !r.present).length;
    summary.title = labels.length
      ? `Default target: ${labels.join(", ")}`
      : "Default build target";
    summary.innerHTML = result.ok
      ? `<span class="sum-ok">default ready</span>`
      : `<span class="sum-bad">${missing} missing</span>`;
  }
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
    $("#install-log-title").textContent = "preparing environment…";
    $("#install-console").textContent = "";
    const stream = new EventSource("/api/toolchains/stream");
    stream.onmessage = event => {
      try {
        installLine(JSON.parse(event.data).line);
      } catch {}
    };
    stream.addEventListener("done", async () => {
      stream.close();
      button.disabled = false;
      const status = await api("/api/toolchains/status");
      await loadToolchains();
      await api("/api/prereqs?refresh=1").then(renderPrereqs);
      await loadEnvironment();
      await loadMatrix();
      $("#install-log-title").textContent = "done.";
      if (status.result?.errors?.length) $("#environment-status").textContent = status.result.errors.map(e => e.join(": ")).join("; ");
    });
  } catch (error) {
    button.disabled = false;
    $("#environment-status").textContent = error.message;
  }
});
