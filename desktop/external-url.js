function classifyWindowUrl(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch (_) {
    return "deny";
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return "deny";
  if (url.hostname === "douyin.com" || url.hostname.endsWith(".douyin.com")) {
    return "allow";
  }
  return "external";
}

function blockNonWebNavigation(event, rawUrl) {
  try {
    const protocol = new URL(event.url || rawUrl).protocol;
    if (protocol === "https:" || protocol === "http:") return;
  } catch (_) {
    // Malformed URLs should not reach the operating system either.
  }
  event.preventDefault();
}

/* -------------------------------------------------------------- 协议兜底 */
/**
 * 抖音页面里有 <a href="bytedance://..."> 形式的唤起链接。Chromium 对自定义
 * 协议的 <a> 点击会直接交给操作系统，不触发 will-navigate / will-redirect /
 * setWindowOpenHandler，上面三个钩子都拦不到 —— 这正是 Windows 弹「需要使用
 * 新应用以打开此 bytedance 链接」的来源。
 *
 * 这里在文档级捕获阶段拦掉点击与 window.open，并补一个子框架导航钩子。
 * 只拦字节系唤起协议，不改页面其它行为，也不影响抖音站内 https 跳转。
 */
const BROWSER_PROTOCOL_GUARD = `(() => {
  const blocked = /^(bytedance|snssdk[0-9]*|aweme|douyin|toutiao):/i;
  const hit = (u) => {
    try { return blocked.test(String(u || "")); } catch (_) { return false; }
  };
  document.addEventListener("click", (e) => {
    const t = e.target;
    const a = t && t.closest ? t.closest("a[href]") : null;
    if (a && hit(a.getAttribute("href"))) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
  const open = window.open;
  window.open = function (u) {
    if (hit(u)) return null;
    return open.apply(this, arguments);
  };
})();`;

function installProtocolGuard(webContents) {
  webContents.on("will-frame-navigate", (event) => {
    try {
      const protocol = new URL(event.url).protocol;
      if (protocol === "https:" || protocol === "http:") return;
    } catch (_) {}
    event.preventDefault();
  });
  webContents.on("dom-ready", () => {
    try {
      const pending = webContents.executeJavaScript(BROWSER_PROTOCOL_GUARD);
      if (pending && typeof pending.catch === "function") pending.catch(() => {});
    } catch (_) {}
  });
}

module.exports = {
  classifyWindowUrl,
  blockNonWebNavigation,
  installProtocolGuard,
  BROWSER_PROTOCOL_GUARD,
};
