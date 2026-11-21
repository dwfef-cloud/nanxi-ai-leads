// Stop delivery is independent of the serial command queue.
function createCommandControl({ check, interrupt, intervalMs = 500 }) {
  let stopped = false;
  let checking = false;
  let closed = false;
  let reason = "manual_stop";
  let rejectStop;
  const stopPromise = new Promise((_, reject) => { rejectStop = reject; });
  stopPromise.catch(() => {});
  const error = () => Object.assign(new Error(reason === "timeout" ? "浏览指令已超时停止" : "已手动停止"),
    { code: "COMMAND_STOPPED", stopped: true });
  const poll = async () => {
    if (checking || stopped || closed) return;
    checking = true;
    try {
      const status = await check();
      if (!closed && status?.cancelled) {
        stopped = true;
        reason = status.reason || reason;
        try { await interrupt(); } finally { rejectStop(error()); }
      }
    } catch (_) { /* A transport failure is not permission to stop or resume. */ }
    finally { checking = false; }
  };
  const timer = setInterval(poll, intervalMs);
  return {
    get stopped() { return stopped; },
    check() { if (stopped) throw error(); },
    wait(pending) { return Promise.race([pending, stopPromise]); },
    close() { closed = true; clearInterval(timer); },
  };
}

module.exports = { createCommandControl };
