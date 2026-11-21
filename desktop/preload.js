/**
 * desktop-shell/preload.js
 *
 * 向页面世界暴露最小桌面能力：
 *  - __DESKTOP_SHELL: true  → live-bridge 据此把「窗口类通道」交给原生 BrowserView
 *  - __desktopShell.viewInvoke(channel, argsJson) → 主进程处理视图通道
 *  - __desktopShell.onStatus(cb) → 主进程推送视图状态（creator-view-status 等）
 *
 * 不暴露 Node 能力、不暴露远程模块；数据通道仍走页面自己的 fetch（同源 /ipc）。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("__DESKTOP_SHELL__", true);
contextBridge.exposeInMainWorld("__desktopShell", {
  version: "1.0.0",
  commentLikeReal: true,
  leadgenReal: true,
  leadgenPlanVersion: 4,
  viewInvoke: (channel, argsJson) =>
    ipcRenderer.invoke("desktop:view-invoke", channel, argsJson),
  onStatus: (cb) => {
    if (typeof cb !== "function") return () => {};
    const handler = (_event, payload) => {
      try { cb(payload); } catch (e) { /* 页面回调异常不进主进程 */ }
    };
    ipcRenderer.on("view-status", handler);
    return () => ipcRenderer.removeListener("view-status", handler);
  },
});
