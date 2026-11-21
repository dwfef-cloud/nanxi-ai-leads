import {x as reactive, ck as useStore} from "./index-1pPBwPwk.js";
import {projectLiveCards} from "./LiveDashboardModel.js";

export const liveDashboard = reactive({items: [], focus: null, frames: {}, error: "", revision: 0, dismissed: new Set()});
let installed = false, inFlight = null;
const phases = new Map(), logKeys = new Set();

export async function refreshLiveDashboard() {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const result = await window.ipcRenderer.invoke("get-live-dashboard-state");
      if (!result?.success || !Array.isArray(result.items)) throw Error(result?.error || "无法核对实况任务状态");
      liveDashboard.items = result.items.map(item => {
        const phase = phases.get(item.runId);
        return !item.isSessionFinished && phase ? {...item, phase} : item;
      });
      liveDashboard.error = "";
      if (window.__desktopShell?.viewInvoke) {
        const frames = await window.__desktopShell.viewInvoke("get-live-dashboard-frames", "[]");
        liveDashboard.frames = frames?.frames || {};
      }
      const store = useStore();
      store.liveDashboardRunningCount = liveDashboard.items.filter(item => !item.isSessionFinished).length;
      // Replay only the displayed run; never append a previous run to a new one.
      for (const card of projectLiveCards(liveDashboard.items)) {
        if (!card.logs?.length) continue;
        for (const line of card.logs) {
          const key = `${card.runId}:${line.ts}:${line.message}`;
          if (logKeys.has(key)) continue;
          logKeys.add(key);
          store.addAccountLog(card.accountId, line.message, {level: line.level, ts: line.ts});
        }
      }
      liveDashboard.revision++;
    } catch (error) {
      // Keep the last state during failures; an error is not an empty task list.
      liveDashboard.error = error.message || "实况状态连接中断";
    } finally { inFlight = null; }
  })();
  return inFlight;
}

export async function focusLiveTask(payload = {}) {
  if (!payload.accountId) return;
  liveDashboard.focus = {...payload, requestedAt: Date.now()};
  await refreshLiveDashboard();
  useStore().activeKey = "dashboard";
  window.dispatchEvent(new CustomEvent("live-dashboard-focus", {detail: liveDashboard.focus}));
}

export function dismissLiveTask(card) { liveDashboard.dismissed.add(card.runId); }

export async function stopLiveTask(card) {
  const result = await window.ipcRenderer.invoke("stop-live-dashboard-task", {
    taskKind: card.dashboardKind, taskId: card.taskId, runId: card.runId});
  if (!result?.success) throw Error(result?.error || "停止请求未确认");
  // An acknowledged request does not mean the execution thread has stopped.
  phases.set(card.runId, "已请求停止，正在等待执行器确认");
  await refreshLiveDashboard();
  return result;
}

export function installLiveDashboard() {
  if (installed || !window.__LIVE_BRIDGE__ || !window.ipcRenderer?.on) return;
  installed = true;
  window.ipcRenderer.on("live-dashboard-focus", (_event, payload) => { void focusLiveTask(payload); });
  window.ipcRenderer.on("live-dashboard-phase", (_event, payload) => {
    if (!payload?.runId || !payload.phase) return;
    phases.set(payload.runId, payload.phase);
    const item = liveDashboard.items.find(item => item.runId === payload.runId);
    if (item && !item.isSessionFinished) item.phase = payload.phase;
  });
  window.ipcRenderer.on("comment-like-task-event", (_event, payload) => {
    if (payload?.mode === "live") void refreshLiveDashboard();
  });
  void refreshLiveDashboard();
  setInterval(() => { void refreshLiveDashboard(); }, 1000);
}

setTimeout(installLiveDashboard, 0);
