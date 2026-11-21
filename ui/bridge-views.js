/**
 * bridge-views.js —— 创作页 / 消息页的真实抖音嵌入
 *
 * 原版行为：这两处是 Electron BrowserView 加载的抖音页面（只是一条抖音链接）。
 * 浏览器环境没有 BrowserView，本文件把它们还原成 iframe 嵌入：
 *   .creator-host → https://creator.douyin.com/creator-micro/home
 *   .chat-host    → https://www.douyin.com/
 *
 * 约束：
 *  - 不改 bundle、不改样式；仅往宿主容器注入 iframe。
 *  - 服务端已给 /ui/ 加 COEP credentialless（创作页自带 COEP，宿主必须匹配）。
 *  - 只做页面展示，不做任何自动化：不代登录、不抓取、不点击。
 *  - 抖音页内登录由用户自己扫码完成，会话 cookie 属于用户浏览器。
 */
(function () {
  "use strict";
  if (window.__BRIDGE_VIEWS__) return;
  /* 桌面壳（Electron）模式：视图由主进程的 BrowserView 原生承载并覆盖宿主区域，
     本文件的 iframe 方案仅在纯浏览器环境生效。 */
  if (window.__DESKTOP_SHELL__) {
    window.__BRIDGE_VIEWS__ = { scan: function () {}, desktop: true };
    return;
  }

  var VIEWS = {
    creator: {
      host: ".creator-host",
      url: "https://creator.douyin.com/creator-micro/home",
      label: "抖音创作者中心",
    },
    message: {
      host: ".chat-host",
      url: "https://www.douyin.com/",
      label: "抖音消息",
    },
  };

  var embedded = new WeakSet(); // 已注入过 iframe 的宿主

  var style = document.createElement("style");
  style.textContent = [
    ".bv-frame{position:absolute;inset:0;width:100%;height:100%;border:0;",
    "background:#0b0e14;display:block}",
    ".bv-frame-bar{position:absolute;left:0;right:0;bottom:0;z-index:30;",
    "display:flex;align-items:center;gap:10px;padding:5px 12px;",
    "background:rgba(9,12,20,.88);border-top:1px solid #222e46;",
    "font:12px/1.6 system-ui,'PingFang SC','Microsoft YaHei',sans-serif;",
    "color:#8b98b3;backdrop-filter:blur(6px)}",
    ".bv-frame-bar a{color:#6f8dff;text-decoration:none}",
    ".bv-frame-bar a:hover{text-decoration:underline}",
    ".bv-frame-bar .bv-tip{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
  ].join("");
  document.documentElement.appendChild(style);

  function build(kind) {
    var cfg = VIEWS[kind];
    var frame = document.createElement("iframe");
    frame.className = "bv-frame";
    frame.setAttribute("referrerpolicy", "no-referrer-when-downgrade");
    frame.setAttribute("allow", "clipboard-read; clipboard-write");
    // 不加 sandbox：抖音页面需要脚本与登录态
    frame.src = cfg.url;

    var bar = document.createElement("div");
    bar.className = "bv-frame-bar";
    var tip = document.createElement("span");
    tip.className = "bv-tip";
    tip.textContent = cfg.label + " · 抖音页面在本窗口内打开，登录请自行扫码，本系统不做任何代操作";
    var open = document.createElement("a");
    open.href = cfg.url;
    open.target = "_blank";
    open.rel = "noopener";
    open.textContent = "新窗口打开 ↗";
    bar.appendChild(tip);
    bar.appendChild(open);

    var wrap = document.createElement("div");
    wrap.className = "bv-embed";
    wrap.style.cssText = "position:absolute;inset:0;overflow:hidden";
    wrap.appendChild(frame);
    wrap.appendChild(bar);
    return wrap;
  }

  function embed(kind, host) {
    if (embedded.has(host)) return;
    // 组件自己的 loading / error 占位不删：iframe 以绝对定位覆盖其上
    var node = build(kind);
    host.appendChild(node);
    embedded.add(host);
  }

  function scan() {
    Object.keys(VIEWS).forEach(function (kind) {
      var host = document.querySelector(VIEWS[kind].host);
      if (!host) return;
      embed(kind, host);
    });
  }

  function init() {
    scan();
    if (typeof MutationObserver !== "undefined") {
      new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
    }
    // 兜底轮询：路由切换后宿主可能重建
    setInterval(scan, 1500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.__BRIDGE_VIEWS__ = { scan: scan };
})();
