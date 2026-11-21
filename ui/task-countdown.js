/* Show the active wait below the scrollable task log, without adding history rows. */
(function () {
  var active = new Map();
  var rendered = new WeakMap();
  var ticker = null;
  var style = document.createElement("style");
  style.textContent = [
    ".task-countdown-status{display:flex;align-items:center;gap:9px;min-height:42px;min-width:0;padding:7px 12px;border-top:1px solid var(--overlay-05,#263441);background:var(--card-30,#17212b);color:var(--text-secondary,#cbd5e1);box-shadow:0 -5px 14px #0003;box-sizing:border-box}",
    ".zoom-terminal-sidebar>.task-countdown-status{flex:0 0 auto}",
    ".zoom-terminal-sidebar>.sidebar-body{min-height:0}",
    ".task-log-list+.task-countdown-status{flex:0 0 auto;margin-top:0;border:1px solid var(--overlay-05,#263441);border-top:0;border-radius:0 0 6px 6px}",
    ".task-countdown-pulse{width:6px;height:6px;flex:0 0 auto;border-radius:50%;background:var(--text-muted,#64748b);box-shadow:none}",
    ".task-countdown-status[data-active=true] .task-countdown-pulse{background:#48c8f2;box-shadow:0 0 8px #48c8f2aa}",
    ".task-countdown-next{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:11px/1.4 'Segoe UI','Microsoft YaHei',sans-serif}",
    ".task-countdown-seconds{flex:0 0 auto;min-width:3ch;text-align:right;color:var(--text-secondary,#cbd5e1);font:700 13px/1 Consolas,monospace;font-variant-numeric:tabular-nums;white-space:nowrap}",
    ".task-countdown-unit{color:var(--text-secondary,#cbd5e1);font:10px 'Segoe UI','Microsoft YaHei',sans-serif}",
    "@media (max-width:420px){.task-countdown-status{gap:7px;padding-inline:9px}.task-countdown-next{font-size:10px}}",
  ].join("");
  document.head.appendChild(style);

  function describe(action, paused) {
    var next = String(action || "继续执行");
    var current = "等待中";
    if (next === "当前视频停留结束后浏览评论区") {
      current = "正在浏览视频";
      next = "读取评论";
    } else if (next === "视频预览结束后判断是否继续浏览") {
      current = "正在预览视频";
      next = "判断是否继续浏览";
    } else if (next === "继续浏览下一条视频并读取评论") {
      current = "视频间隔等待";
      next = "浏览下一条视频";
    } else if (next === "快速滑过当前视频并继续浏览") {
      current = "短暂停留";
      next = "浏览下一条视频";
    } else if (next === "继续浏览当前视频") {
      current = "正在浏览视频";
      next = "继续当前视频";
    } else if (next === "继续当前浏览流程") {
      next = "继续浏览流程";
    }
    return (paused ? "已暂停" : current) + " · 下一步：" + next;
  }

  function update(event) {
    var detail = event && event.detail;
    if (!detail || !["entity-leadgen-task-event", "automation-data"].includes(detail.channel)) return;
    var envelope = detail.payload || {};
    var payload = detail.channel === "automation-data" ? Object.assign({}, envelope.payload || {}, {
      type: envelope.type, taskId: envelope.taskId,
    }) : envelope;
    if (payload.type !== "countdown" || !payload.taskId) return;
    if (payload.active) {
      var remaining = Math.max(0, Number(payload.remainingSeconds) || 0);
      active.set(String(payload.taskId), Object.assign({}, payload, {
        deadline: payload.paused ? 0 : Date.now() + remaining * 1000,
      }));
      if (!ticker) ticker = window.setInterval(render, 250);
    } else active.delete(String(payload.taskId));
    if (active.size === 0 && ticker) {
      window.clearInterval(ticker);
      ticker = null;
    }
    render();
  }

  function createStatus(container) {
    var status = document.createElement("div");
    status.className = "task-countdown-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    var pulse = document.createElement("span");
    pulse.className = "task-countdown-pulse";
    pulse.setAttribute("aria-hidden", "true");
    var label = document.createElement("span");
    label.className = "task-countdown-next";
    var seconds = document.createElement("strong");
    seconds.className = "task-countdown-seconds";
    var unit = document.createElement("small");
    unit.className = "task-countdown-unit";
    unit.textContent = " 秒";
    status.appendChild(pulse);
    status.appendChild(label);
    status.appendChild(seconds);
    status.appendChild(unit);
    if (container.classList.contains("sidebar-body")) container.parentElement.appendChild(status);
    else container.insertAdjacentElement("afterend", status);
    return { status: status, label: label, seconds: seconds, unit: unit };
  }

  function render() {
    var latest = null;
    active.forEach(function (item) { latest = item; });
    document.querySelectorAll(".task-log-list, .sidebar-body").forEach(function (container) {
      var view = rendered.get(container);
      if (!view || !view.status.isConnected) {
        view = createStatus(container);
        rendered.set(container, view);
      }
      if (!latest) {
        view.status.dataset.active = "false";
        view.label.textContent = "当前无待执行动作";
        view.label.title = "当前无待执行动作";
        view.seconds.textContent = "—";
        view.unit.textContent = "";
        return;
      }
      view.status.dataset.active = "true";
      var remaining = latest.paused ? Number(latest.remainingSeconds) || 0 :
        Math.max(0, Math.ceil((latest.deadline - Date.now()) / 1000));
      var label = describe(latest.action, latest.paused);
      if (view.label.textContent !== label) {
        view.label.textContent = label;
        view.label.title = label;
      }
      var value = String(remaining);
      if (view.seconds.textContent !== value) view.seconds.textContent = value;
      view.unit.textContent = " 秒";
    });
  }

  window.addEventListener("desktop-bridge-event", update);
  new MutationObserver(render).observe(document.documentElement, { childList: true, subtree: true });
})();
