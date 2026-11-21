/**
 * live-bridge.js —— M0 真桥接（前端侧）
 *
 * 契约：原版前端只认 window.ipcRenderer.invoke(channel, ...args) 的返回值，
 * 且期望「通道原本的形状」（数组 / {items,total} / {success,...}），不认包装。
 * 因此本文件负责把后端统一协议 {success, data, error} 拆包后再交给页面。
 *
 * 设计约束：
 *  - 不改 bundle、不改样式、不改 demo-bridge.js；本文件在其之后加载并覆盖 invoke。
 *  - ?demo=1 时完全不接管，回到纯演示数据，便于对照与回退。
 *  - 桥接失败绝不回退到假数据（避免把虚构数据当真实数据展示），
 *    而是返回安全空态并告警。
 *  - 业务层「功能未开通」由后端以 data.success=false 表达，
 *    外层 success 仍为 true，因此不会触发回退、也不会绕过禁用。
 */
(function () {
  var demo = window.ipcRenderer;
  if (!demo || !demo.invoke) return;

  var params = new URLSearchParams(window.location.search);
  var demoMode = params.get("demo") === "1" || window.localStorage.getItem("bridgeMode") === "demo";
  if (demoMode) {
    console.info("[live-bridge] ?demo=1 —— 保持演示数据桥，不接管");
    return;
  }

  var demoInvoke = demo.invoke.bind(demo);
  var state = { mode: "live", lastError: null, failures: 0, calls: 0 };

  /* 桌面壳（Electron）模式：窗口类通道由原生 BrowserView 承载，不走 HTTP。
     预加载脚本已注入 window.__DESKTOP_SHELL__ / window.__desktopShell。 */
  var DESKTOP = window.__DESKTOP_SHELL__ && window.__desktopShell;
  var NATIVE_VIEW_RE = /^(open|reload|destroy)-(creator|chat)-view$|^set-(creator|chat)-view-attached$|^set-views-visible$|^init-automation-view$|^update-automation-bounds$|^set-account-detail-mode$|^open-url$|^lead-comment-interact$|^start-comment-like-real$|^open-profile-qr-directory$/;

  // MessageCenter sends temporary hide/restore and unmount without a viewKey.
  // Carry the selected account across those calls, including on older shells.
  var selectedNativeViews = { creator: null, chat: null };
  function nativeViewArgs(channel, args) {
    var action = /^(open|reload|destroy)-(creator|chat)-view$/.exec(channel);
    var attach = /^set-(creator|chat)-view-attached$/.exec(channel);
    if (!action && !attach) return args;
    var kind = action ? action[2] : attach[1];
    var first = args[0];
    var payload = first && typeof first === "object" ? first :
      typeof first === "string" ? { accountId: first } : {};
    var key = payload.viewKey || (payload.accountId ? "douyin_" + payload.accountId : null) ||
      selectedNativeViews[kind];
    if (!key) return args;
    if (action && action[1] === "open") selectedNativeViews[kind] = key;
    if (action && action[1] === "destroy" && selectedNativeViews[kind] === key)
      selectedNativeViews[kind] = null;
    return [Object.assign({}, payload, { viewKey: key })].concat(args.slice(1));
  }

  /* 浏览器环境没有 Electron BrowserView：窗口通道成功后主动派发 ready，
     否则组件会永远停在「正在打开 …」的 loading 状态。
     viewKey 与组件内部一致：douyin_${accountId}。 */
  var VIEW_EVENTS = {
    "open-creator-view": "creator-view-status",
    "reload-creator-view": "creator-view-status",
    "open-chat-view": "chat-view-status",
    "reload-chat-view": "chat-view-status",
  };

  /* 网络/协议故障时的安全空态：形状尽量中性，不伪造业务数据 */
  function safeFallback(channel, message) {
    console.warn("[live-bridge] 通道不可用，返回安全空态:", channel, message);
    state.lastError = message;
    state.failures += 1;
    return { success: false, msg: "数据服务暂不可用：" + message };
  }

  function leadgenPreflightText(report) {
    var lines = ["评论获客启动前检查"];
    (report && report.summary || []).forEach(function (item) {
      lines.push("• " + String(item));
    });
    if (report && report.warnings && report.warnings.length) {
      lines.push("", "需要留意：");
      report.warnings.forEach(function (item) { lines.push("• " + String(item)); });
    }
    if (report && report.errors && report.errors.length) {
      lines.push("", "无法启动：");
      report.errors.forEach(function (item) { lines.push("• " + String(item)); });
    }
    return lines.join("\n");
  }

  async function invoke(channel) {
    var args = Array.prototype.slice.call(arguments, 1);
    state.calls += 1;
    if (DESKTOP && channel === 'start-task' && args[0] && args[0].taskMode !== 'nurture') {
      if (!DESKTOP.leadgenReal) return {success:false,error:'桌面仍在运行旧版，请从托盘正常退出后重新启动，以加载真实评论、回复和私信执行器'};
      if (!(DESKTOP.leadgenPlanVersion >= 4)) return {success:false,error:'当前客户端尚未加载新版任务配置执行器，请正常退出桌面端后重新打开；本次未执行'};
      {
        var report = await invoke('validate-leadgen-config', args[0]);
        if (window.dispatchEvent && window.CustomEvent)
          window.dispatchEvent(new window.CustomEvent('leadgen-config-report', {detail:report}));
        if (!report || report.success !== true) {
          var validationErrors = report && report.errors || [report && report.msg || '配置检查失败，请重试'];
          if (typeof window.alert === 'function') window.alert(leadgenPreflightText(report));
          return {success:false, error:validationErrors.join('；')};
        }
        if (typeof window.confirm === 'function' && !window.confirm(leadgenPreflightText(report)))
          return {success:false, error:'用户取消启动'};
      }
      try { return await DESKTOP.viewInvoke('start-leadgen-real', JSON.stringify(args)); }
      catch (err) { return safeFallback(channel, '真机任务启动失败：' + String(err.message || err)); }
    }
    if (DESKTOP && NATIVE_VIEW_RE.test(channel)) {
      try {
        var native = await DESKTOP.viewInvoke(channel, JSON.stringify(nativeViewArgs(channel, args)));
        return native && typeof native === "object" ? native : { success: true };
      } catch (err) {
        return safeFallback(channel, "桌面壳视图通道失败：" + String((err && err.message) || err));
      }
    }
    try {
      var res = await fetch("/ipc/" + encodeURIComponent(channel), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ args: args }),
      });
      if (!res.ok) return safeFallback(channel, "HTTP " + res.status);
      var packet = await res.json();
      if (!packet || packet.success !== true) {
        return safeFallback(channel, (packet && packet.error) || "后端返回失败");
      }
      var data = packet.data;
      var eventName = VIEW_EVENTS[channel];
      if (eventName && data && data.success !== false) {
        var p = (args[0] && typeof args[0] === "object") ? args[0] : {};
        var viewKey = p.viewKey || (p.accountId ? "douyin_" + p.accountId : "");
        if (viewKey) {
          window.setTimeout(function () {
            emit(eventName, {
              viewKey: viewKey, status: "ready", mode: "offline-panel",
            });
          }, 0);
        }
      }
      return data;
    } catch (err) {
      return safeFallback(channel, String((err && err.message) || err));
    }
  }

  /* 有些页面（如账号池登录弹窗）用 send 而非 invoke 发送窗口类指令：
     send 是单向通道，原版由主进程 ipcMain.on 接收。桌面壳模式下同样要交给原生，
     否则登录窗口永远等不到 init-automation-view（表现：一直转圈「正在拉取环境」）。 */
  function send(channel) {
    var args = Array.prototype.slice.call(arguments, 1);
    state.calls += 1;
    if (DESKTOP && NATIVE_VIEW_RE.test(channel)) {
      try {
        var pending = DESKTOP.viewInvoke(channel, JSON.stringify(nativeViewArgs(channel, args)));
        if (pending && typeof pending.catch === "function") pending.catch(function () {});
      } catch (e) { /* 单向通道：失败不阻断页面 */ }
      return;
    }
    /* 其余通道必须真发到后端。
       历史 P0：这里只转发给 demo 假桥，导致「开始养号 / 启动任务」这类走 send 的
       写操作在界面变「运行中」，后端却零记录（假运行中）。
       修复：send 与 invoke 同路径（POST /ipc/{channel}），单向语义只体现在
       不把结果回传给调用方，失败仅告警。 */
    try {
      var pending = invoke.apply(null, [channel].concat(args));
      if (pending && typeof pending.then === "function") {
        pending.then(function (result) {
          if (channel !== "start-task" || (result && result.success !== false)) return;
          var task = args[0] && typeof args[0] === "object" ? args[0] : {};
          emit("task-start-rejected", {
            taskId: task.taskId || task.id,
            taskMode: task.taskMode,
            reason: result.error || result.msg || "养号启动失败",
          });
        }).catch(function (err) {
          if (channel !== "start-task") return;
          var task = args[0] && typeof args[0] === "object" ? args[0] : {};
          emit("task-start-rejected", {
            taskId: task.taskId || task.id,
            taskMode: task.taskMode,
            reason: String((err && err.message) || err || "养号启动失败"),
          });
        });
      }
    } catch (e) { /* 单向通道：失败不阻断页面 */ }
  }

  /* 事件通道：保留 Electron 形状，用同源轻量轮询承载推送（当前后端无事件源，骨架先就位） */
  var listeners = new Map();
  var pollTimer = null;

  function emit(channel, payload) {
    if (window.dispatchEvent && window.CustomEvent) {
      window.dispatchEvent(new window.CustomEvent("desktop-bridge-event", {
        detail: { channel: channel, payload: payload },
      }));
    }
    var bucket = listeners.get(channel);
    if (!bucket) return;
    bucket.forEach(function (cb) {
      try { cb(null, payload); } catch (e) { console.warn("[live-bridge] listener error", channel, e); }
    });
  }

  function on(channel, cb) {
    if (typeof cb !== "function") return function () {};
    if (!listeners.has(channel)) listeners.set(channel, new Set());
    listeners.get(channel).add(cb);
    if (!pollTimer) {
      pollTimer = window.setInterval(async function () {
        try {
          var res = await fetch("/ipc/events", { method: "GET" });
          if (!res.ok) return;
          var body = await res.json();
          (body.events || []).forEach(function (ev) { emit(ev.channel, ev.payload); });
        } catch (e) { /* 静默：事件通道不阻断页面 */ }
      }, 1000);
    }
    return function () {
      var bucket = listeners.get(channel);
      if (bucket) bucket.delete(cb);
      if (bucket && bucket.size === 0) listeners.delete(channel);
      if (listeners.size === 0 && pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    };
  }

  window.ipcRenderer = Object.assign({}, demo, { invoke: invoke, send: send, on: on, once: on });

  /* 桌面壳：主进程推送的视图状态并入本桥的事件流（驱动「加载中→ready」） */
  if (DESKTOP && typeof DESKTOP.onStatus === "function") {
    DESKTOP.onStatus(function (ev) {
      if (ev && ev.channel) emit(ev.channel, ev.payload);
    });
  }

  window.__LIVE_BRIDGE__ = Object.assign(state, {
    channelCountUrl: "/ipc/health",
    useDemo: function () {
      window.localStorage.setItem("bridgeMode", "demo");
      window.location.reload();
    },
    useLive: function () {
      window.localStorage.removeItem("bridgeMode");
      window.location.reload();
    },
    emit: emit,
  });
  console.info("[live-bridge] 真桥接已启用 → POST /ipc/{channel}（同源）");
})();
