/* 诊断常驻：把 renderer 的完整报错堆栈经 console 转到壳日志（agent.log）。
   主窗 console 已由 main.js 转发，页面级未捕获错误在这里补齐 stack。 */
window.addEventListener("error", function (e) {
  console.error("[err-probe] stack=", (e.error && e.error.stack) || e.message,
    "| file=", e.filename, ":", e.lineno, ":", e.colno);
});
window.addEventListener("unhandledrejection", function (e) {
  var r = e.reason;
  console.error("[rej-probe] ", r && (r.stack || r.message) || String(r));
});
