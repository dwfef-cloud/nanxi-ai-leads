/**
 * history-entry.js —— 执行历史入口
 *
 * 为什么是注入而不是改 bundle
 * ---------------------------
 * /ui 是编译产物（左侧导航写死在 bundle 的数组里），改它既脆弱又会让后续升级
 * 无法对齐。这里沿用 bridge-views.js 的做法：只往宿主页面追加一个入口按钮，
 * 点开在浮层里加载同源的 /history 页面（数据走 /ipc/get-execution-history）。
 *
 * 界面零改动承诺仍然成立：不覆盖样式、不改任何已有节点，只新增两个元素
 * （入口按钮 + 关闭时才创建的浮层）。
 * 桌面壳与浏览器环境都可用：同源 iframe，不需要额外权限。
 */
(function () {
  "use strict";
  if (window.__EXEC_HISTORY_ENTRY__) return;
  var params = new URLSearchParams(window.location.search);
  if (params.get("demo") === "1") return; // 演示模式不挂真数据入口

  var BTN_ID = "exec-history-entry";
  var OVERLAY_ID = "exec-history-overlay";

  var style = document.createElement("style");
  style.textContent = [
    "#" + BTN_ID + "{position:fixed;left:16px;bottom:16px;z-index:2147482000;",
    "display:flex;align-items:center;gap:7px;padding:8px 14px;border-radius:999px;",
    "background:rgba(18,23,34,.94);border:1px solid #2b3a5c;color:#c9d5ea;",
    "font:12.5px/1 system-ui,'PingFang SC','Microsoft YaHei',sans-serif;",
    "cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.45);backdrop-filter:blur(8px);",
    "transition:border-color .15s ease,color .15s ease}",
    "#" + BTN_ID + ":hover{border-color:#4560a0;color:#fff}",
    "#" + BTN_ID + " .eh-dot{width:6px;height:6px;border-radius:50%;background:#6f8dff;",
    "box-shadow:0 0 7px rgba(111,141,255,.9)}",
    "#" + OVERLAY_ID + "{position:fixed;inset:0;z-index:2147483000;background:rgba(6,9,14,.78);",
    "display:flex;flex-direction:column;backdrop-filter:blur(3px)}",
    "#" + OVERLAY_ID + " .eh-bar{display:flex;align-items:center;gap:12px;",
    "padding:9px 16px;background:#0d1219;border-bottom:1px solid #22304a;",
    "font:12.5px/1.4 system-ui,'PingFang SC','Microsoft YaHei',sans-serif;color:#8b98b3}",
    "#" + OVERLAY_ID + " .eh-bar b{color:#e6ecf7;font-weight:600}",
    "#" + OVERLAY_ID + " .eh-bar .eh-grow{flex:1}",
    "#" + OVERLAY_ID + " .eh-close{padding:5px 12px;border-radius:8px;border:1px solid #2b3a5c;",
    "background:#151c29;color:#c9d5ea;cursor:pointer;font-size:12px}",
    "#" + OVERLAY_ID + " .eh-close:hover{border-color:#4560a0;color:#fff}",
    "#" + OVERLAY_ID + " iframe{flex:1;width:100%;border:0;background:#0b0e14}",
  ].join("");
  document.documentElement.appendChild(style);

  function open() {
    if (document.getElementById(OVERLAY_ID)) return;
    var overlay = document.createElement("div");
    overlay.id = OVERLAY_ID;
    var bar = document.createElement("div");
    bar.className = "eh-bar";
    var title = document.createElement("span");
    title.innerHTML = "<b>执行历史</b> · 到点到底跑没跑，这里看";
    var grow = document.createElement("span");
    grow.className = "eh-grow";
    var close = document.createElement("button");
    close.className = "eh-close";
    close.textContent = "关闭 ✕";
    close.addEventListener("click", shutdown);
    bar.appendChild(title);
    bar.appendChild(grow);
    bar.appendChild(close);

    var frame = document.createElement("iframe");
    frame.src = "/history";
    frame.setAttribute("title", "执行历史");

    overlay.appendChild(bar);
    overlay.appendChild(frame);
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey);
    // 用完即清：?pane=history 只负责「这一次打开」，留着会让刷新时又弹一次。
    try {
      var u = new URL(window.location.href);
      if (u.searchParams.get("pane")) {
        u.searchParams.delete("pane");
        window.history.replaceState({}, "", u.pathname + u.search + u.hash);
      }
    } catch (e) { /* 老浏览器忽略 */ }
  }

  function onKey(e) {
    if (e.key === "Escape") shutdown();
  }

  function shutdown() {
    var overlay = document.getElementById(OVERLAY_ID);
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    document.removeEventListener("keydown", onKey);
  }

  function mount() {
    if (document.getElementById(BTN_ID)) return;
    var btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.type = "button";
    btn.title = "查看每一次执行的触发方式、结果与每轮统计";
    btn.innerHTML = '<span class="eh-dot"></span><span>执行历史</span>';
    btn.addEventListener("click", open);
    document.body.appendChild(btn);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
  window.__EXEC_HISTORY_ENTRY__ = { open: open, close: shutdown };

  // 托盘菜单「执行历史」直接带 ?pane=history 打开，不必让用户再点一次浮层按钮。
  if (params.get("pane") === "history") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", open);
    } else {
      setTimeout(open, 300); // 让宿主首屏先渲染完，避免浮层被后续挂载覆盖
    }
  }
})();
