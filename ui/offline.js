(function () {
  "use strict";
  const el = id => document.getElementById(id);
  const states = { running: "模拟执行中", stopping: "正在停止", completed: "模拟完成", stopped: "已停止",
    needs_confirmation: "模拟结果不确定，已停止", failed: "模拟失败", interrupted: "上次运行已中断",
    simulated_ok: "模拟成功", simulated_fail: "模拟被拒绝", uncertain: "结果不确定",
    skipped_already_sent: "重复，已拦截", skipped_uncertain_pending_manual: "待确认，已拦截" };
  const reasons = { fixture_confirmed: "已收到匹配的模拟确认", platform_declined_fixture: "模拟平台明确拒绝，本次不计成功",
    message_declined_fixture: "模拟消息被拒绝，保留草稿", comment_not_confirmed: "缺少匹配的评论回执，停止后续动作",
    message_not_confirmed: "没有新的已发送消息确认", favorite_not_confirmed: "收藏状态未确认",
    clipboard_not_confirmed: "复制后的内容核对失败", skipped_already_sent: "该样例已成功，重启后仍不会重复执行",
    skipped_uncertain_pending_manual: "上次结果不确定，本轮不重试",
    offline_driver_error: "离线执行器异常，请检查软件文件与运行环境；没有计入成功" };
  const fields = { simulatedComments: "模拟评论", simulatedReplies: "模拟回复", simulatedMessages: "模拟私信",
    simulatedFavorites: "模拟收藏", simulatedLinksCopied: "模拟复制链接" };
  const terminal = new Set(["completed", "failed", "stopped", "needs_confirmation", "interrupted"]);
  let selected = null, active = null, loading = false, mutating = false;
  function text(tag, value, cls) { const node = document.createElement(tag); node.textContent = value; if (cls) node.className = cls; return node; }
  function error(message) { el("error").hidden = !message; el("error").textContent = message || ""; }
  async function request(path, body) {
    const response = await fetch("/api/offline/runs" + path, body === undefined ? { cache: "no-store" } : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "离线测试请求失败，请检查本地服务");
    return data;
  }
  function render(item) {
    el("counts").replaceChildren();
    Object.entries(fields).forEach(([key, label]) => {
      const box = text("div", "", "metric"); box.append(text("b", item ? item.stats[key] : 0), text("span", label)); el("counts").append(box);
    });
    if (!item) return;
    selected = item.id;
    el("run-title").textContent = "样例 " + item.sample + " · " + new Date(item.startedAt).toLocaleString("zh-CN");
    el("run-status").textContent = states[item.status] || item.status;
    el("run-status").dataset.runId = item.id;
    el("progress").value = item.progress;
    el("summary").textContent = "重复拦截 " + item.already + " · 模拟失败 " + item.failed + " · 待确认 " + item.uncertain +
      " · 跳过 " + item.skipped + "。真实业务统计：评论 0、互动 0、触达 0。";
    el("logs").replaceChildren();
    item.logs.forEach((log, index) => {
      const row = document.createElement("tr"); row.append(text("td", index + 1), text("td", log.label),
        text("td", states[log.status] || log.status, log.status === "simulated_ok" ? "good" : log.status.includes("fail") ? "bad" : "warn"),
        text("td", reasons[log.reason] || log.reason)); el("logs").append(row);
    });
    if (!item.logs.length) { const row = document.createElement("tr"), cell = text("td", item.status === "stopped" ? "已停止，尚未执行动作。" : "正在准备离线样例…", "empty"); cell.colSpan = 4; row.append(cell); el("logs").append(row); }
  }
  function buttons() { el("start").disabled = mutating || !!active; el("stop").disabled = mutating || !active; }
  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      const data = await request("");
      active = data.runs.find(item => !terminal.has(item.status)) || null;
      const chosen = data.runs.find(item => item.id === selected) || active || data.runs[0];
      render(chosen);
      el("history").replaceChildren();
      data.runs.forEach(item => {
        const button = text("button", "", item.id === selected ? "selected" : ""); button.type = "button";
        button.dataset.runId = item.id;
        button.append(text("span", new Date(item.startedAt).toLocaleString("zh-CN") + " · 样例 " + item.sample + " · " +
          ({success:"正常执行", uncertain:"回执缺失", declined:"评论拒绝"}[item.scenario])), text("span", states[item.status] || item.status));
        button.addEventListener("click", () => {
          selected = item.id; el("suite").value = item.suite; el("scenario").value = item.scenario; el("sample").value = item.sample;
          render(item); refresh();
        }); el("history").append(button);
      });
      if (!data.runs.length) el("history").append(text("p", "暂无离线测试记录。"));
      buttons();
    } catch (err) { error("无法读取离线记录：" + err.message); } finally { loading = false; }
  }
  el("start").addEventListener("click", async () => {
    mutating = true; buttons(); error("");
    try { const item = await request("", { suite: el("suite").value, scenario: el("scenario").value, sample: el("sample").value }); selected = item.id; active = item; render(item); }
    catch (err) { error(err.message); } finally { mutating = false; buttons(); await refresh(); }
  });
  el("stop").addEventListener("click", async () => {
    if (!active) return;
    mutating = true; buttons(); error("");
    try { await request("/" + active.id + "/stop", {}); } catch (err) { error(err.message); }
    finally { mutating = false; await refresh(); buttons(); }
  });
  el("refresh").addEventListener("click", () => { error(""); refresh(); });
  render(null); refresh(); setInterval(refresh, 700);
})();
