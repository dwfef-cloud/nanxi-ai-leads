(function () {
  "use strict";
  if (window.__OFFLINE_ENTRY__) return;
  function close() { const panel = document.getElementById("offline-panel"); if (panel) panel.remove(); }
  async function open() {
    if (window.__desktopShell && window.__desktopShell.viewInvoke) {
      const result = await window.__desktopShell.viewInvoke("open-offline-test", "[]").catch(() => null);
      if (result && result.success && result.mode === "offline") {
        if (new URLSearchParams(location.search).get("pane") === "offline") {
          const url = new URL(location.href); url.searchParams.delete("pane"); history.replaceState({}, "", url.pathname + url.search + url.hash);
        }
        return;
      }
    }
    if (document.getElementById("offline-panel")) return;
    const panel = document.createElement("div"); panel.id = "offline-panel";
    panel.style.cssText = "position:fixed;inset:0;z-index:2147483100;background:#0b101b;display:flex;flex-direction:column";
    const bar = document.createElement("div"); bar.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:10px 20px;background:#121b2b;color:#dce5f6;border-bottom:1px solid #26334c;font:14px system-ui";
    const label = document.createElement("span"); label.textContent = "离线测试 · 真实操作关闭";
    const exit = document.createElement("button"); exit.textContent = "关闭面板";
    exit.style.cssText = "padding:7px 14px;border:1px solid #394965;border-radius:8px;background:#1a263c;color:#e7eefb;cursor:pointer";
    exit.addEventListener("click", close); bar.append(label, exit);
    const frame = document.createElement("iframe"); frame.src = "/ui/offline.html"; frame.title = "离线测试";
    frame.style.cssText = "width:100%;flex:1;border:0"; panel.append(bar, frame); document.body.append(panel);
    if (new URLSearchParams(location.search).get("pane") === "offline") { const url = new URL(location.href); url.searchParams.delete("pane"); history.replaceState({}, "", url.pathname + url.search + url.hash); }
  }
  function mount() {
    const button = document.createElement("button"); button.id = "offline-test-entry"; button.type = "button"; button.textContent = "离线测试";
    button.style.cssText = "position:fixed;left:145px;bottom:16px;z-index:2147482000;padding:8px 14px;background:#17253b;border:1px solid #496494;border-radius:999px;color:#d5e1fc;font:12.5px system-ui;cursor:pointer";
    button.addEventListener("click", open); document.body.append(button);
    if (new URLSearchParams(location.search).get("pane") === "offline") open();
  }
  window.__OFFLINE_ENTRY__ = { open, close };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();
