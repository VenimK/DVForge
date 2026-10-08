"use strict";
(() => {
  const footer = document.createElement("footer");
  footer.className = "wizard-footer";
  footer.setAttribute("aria-label", "Wizard navigation");
  const groups = { environment: ["prepare-environment", "environment-next"], config: ["btn-save-config", "config-next"], targets: ["btn-plan", "btn-build"], review: ["review-back", "review-start"], console: ["btn-open-folder", "btn-cancel"] };
  for (const [step, ids] of Object.entries(groups)) {
    const group = document.createElement("div");
    group.dataset.footerStep = step;
    if (["config", "targets", "console"].includes(step)) {
      const back = document.createElement("button");
      back.className = "btn ghost";
      back.textContent = "Back";
      back.addEventListener("click", () => showStep({config: "environment", targets: "config", console: "review"}[step]));
      group.append(back);
    }
    ids.forEach(id => { const button = document.getElementById(id); if (button) group.append(button); });
    footer.append(group);
  }
  document.body.append(footer);
  const sync = () => {
    const active = document.querySelector(".panel.is-active")?.id.replace("tab-", "");
    footer.querySelectorAll("[data-footer-step]").forEach(g => { g.hidden = g.dataset.footerStep !== active; });
  };
  new MutationObserver(sync).observe(document.querySelector("main"), {subtree: true, attributes: true, attributeFilter: ["class"]});
  new ResizeObserver(() => document.documentElement.style.setProperty("--wizard-footer-height", `${footer.getBoundingClientRect().height}px`)).observe(footer);
  sync();

  const log = document.getElementById("console"), show = document.getElementById("show-log"), filter = document.getElementById("log-filter");
  let entries = [], errors = 0, warnings = 0, started = 0, stepStarted = 0, ended = 0, fraction = null, completed = 0, total = 0, timer, frame;
  const duration = s => { const n = Math.max(0, Math.floor(s)); return `${String(Math.floor(n / 3600)).padStart(2,"0")}:${String(Math.floor(n / 60) % 60).padStart(2,"0")}:${String(n % 60).padStart(2,"0")}`; };
  const matches = e => filter.value === "all" || e.level === filter.value || (filter.value === "diagnostics" && e.level !== "normal");
  function render() {
    frame = null;
    if (!show.checked) return;
    const fragment = document.createDocumentFragment();
    entries.filter(matches).forEach(e => { const span = document.createElement("span"); span.className = e.style; span.textContent = e.line + "\n"; fragment.append(span); });
    log.replaceChildren(fragment);
    log.scrollTop = log.scrollHeight;
  }
  function schedule() { if (show.checked && !frame) frame = requestAnimationFrame(render); }
  function update() {
    const now = ended || Date.now(), elapsed = started ? (now - started) / 1000 : 0, stepElapsed = stepStarted ? (now - stepStarted) / 1000 : 0;
    document.getElementById("step-elapsed").textContent = "Elapsed " + duration(stepElapsed);
    document.getElementById("session-elapsed").textContent = "Elapsed " + duration(elapsed);
    const step = document.getElementById("step-progress");
    if (fraction === null) step.removeAttribute("value"); else step.value = fraction * 100;
    document.getElementById("step-percent").textContent = fraction === null ? "—" : Math.floor(fraction * 100) + "%";
    document.getElementById("step-eta").textContent = "ETA " + (!ended && fraction > 0 && stepElapsed >= 2 ? "~" + duration(stepElapsed * (1 - fraction) / fraction) : "—");
    const percent = total ? Math.floor(completed / total * 100) : 0;
    document.getElementById("session-progress").value = percent;
    document.getElementById("session-percent").textContent = percent + "%";
    document.getElementById("session-phases").textContent = total ? `${completed}/${total} phases` : "";
    document.getElementById("session-eta").textContent = "ETA " + (completed && total && !ended && elapsed >= 2 ? "~" + duration(elapsed / completed * (total - completed)) : completed === total && total ? duration(0) : "—");
  }
  show.addEventListener("change", () => { document.getElementById("log-view").hidden = !show.checked; schedule(); });
  filter.addEventListener("change", schedule);
  window.buildView = {
    append(line) {
      const command = line.startsWith("$ ");
      const level = !command && /(^\s*!!|\bFAILED\b|\berror(?:\[[^\]]+\])?:|\bexception:|Traceback \()/i.test(line) ? "error" : !command && /(^\s*!(?![!])|\bwarning:|\bWARNING\b)/i.test(line) ? "warning" : "normal";
      const style = level === "error" ? "con-err" : level === "warning" ? "con-warn" : command ? "con-cmd" : /^\s*===/.test(line) ? "con-head" : /✓|artifact:|DONE/.test(line) ? "con-ok" : "";
      entries.push({line, level, style}); errors += level === "error" ? 1 : 0; warnings += level === "warning" ? 1 : 0;
      document.getElementById("log-counts").textContent = `${errors} errors · ${warnings} warnings`;
      schedule();
    },
    consume(line) {
      const phases = line.match(/^Progress: (\d+)\/(\d+) phases$/);
      if (phases) { completed = Number(phases[1]); total = Number(phases[2]); update(); return true; }
      if (/^\s*===|^\$ /.test(line)) { stepStarted = Date.now(); fraction = null; }
      const count = line.match(/\[(?:[^\]]*?\s)?(\d+)\/(\d+)(?:[^\]]*)\]/) || line.match(/\b(\d+)\/(\d+):/);
      const percent = line.match(/(?:Receiving objects|Resolving deltas|Downloading|download).*?\b(\d{1,3})%/i);
      if (!line.startsWith("$ ") && count && Number(count[2]) > 0 && Number(count[1]) <= Number(count[2])) fraction = Number(count[1]) / Number(count[2]);
      else if (!line.startsWith("$ ") && percent && Number(percent[1]) <= 100) fraction = Number(percent[1]) / 100;
      update(); return false;
    },
    text(level) { return entries.filter(e => !level || e.level === level).map(e => e.line + "\n").join(""); },
    reset() { clearInterval(timer); entries = []; errors = warnings = completed = total = 0; started = stepStarted = ended = 0; fraction = null; log.replaceChildren(); document.getElementById("log-counts").textContent = "0 errors · 0 warnings"; update(); },
    start() { started = stepStarted = Date.now(); ended = 0; clearInterval(timer); timer = setInterval(update, 500); update(); },
    stop() { ended = Date.now(); clearInterval(timer); update(); },
  };
})();
