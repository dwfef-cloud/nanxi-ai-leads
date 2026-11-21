(function () {
  "use strict";
  const invoke = (channel, payload = {}) => window.ipcRenderer.invoke(channel, payload);
  const node = (tag, text, parent) => {
    const el = document.createElement(tag); el.textContent = text || "";
    if (parent) parent.append(el); return el;
  };
  async function mount() {
    let info;
    try { info = await invoke("get-live-acceptance-info"); } catch (_) { return; }
    if (!info || !info.enabled) return;
    const entry = node("button", "本次真机验收", document.body);
    entry.style.cssText = "position:fixed;right:110px;bottom:16px;z-index:2147482000;padding:8px 14px;background:#17253b;border:1px solid #496494;border-radius:999px;color:#d5e1fc;cursor:pointer";
    entry.id = "live-acceptance-entry";
    entry.onclick = async () => {
      const dialog = node("dialog"); dialog.id = "live-acceptance-dialog";
      dialog.style.cssText = "width:620px;padding:24px;background:#142034;color:#e5edf9;border:1px solid #506687;border-radius:12px;font:14px system-ui";
      node("h3", "本次账号与视频动作验收", dialog);
      node("p", "账号：" + info.nickname + "。每项最多尝试一次；已有赞或收藏直接跳过。此入口用于逐项验收，养号自动编排需另外核对。", dialog);
      node("p", info.videoUrl, dialog);
      const bar = node("div", "", dialog); bar.style.cssText = "display:flex;gap:14px;flex-wrap:wrap";
      const status = node("pre", "", dialog); status.style.cssText = "white-space:pre-wrap;max-height:300px;overflow:auto";
      const buttons = [];
      const refresh = async () => {
        info = await invoke("get-live-acceptance-info");
        status.textContent = (info.results || []).filter(r => !["search-video", "generate-comment"].includes(r.action))
          .map(r => r.action + " · " + r.status + " · " + r.code).join("\n") || "尚无已确认的动作结果。";
      };
      for (const [action, label] of [["copy-video-link", "复制链接并恢复剪贴板"], ["favorite-video", "收藏一次"], ["like-video", "点赞一次"]]) {
        const btn = node("button", label, bar); btn.type = "button"; buttons.push(btn);
        btn.onclick = async () => {
          buttons.forEach(b => { b.disabled = true; }); status.textContent = "正在执行并核对结果…";
          try {
            const result = await invoke("run-live-acceptance-action", {action, accountId: info.accountId,
              leadgenTaskId: info.leadgenTaskId, videoUrl: info.videoUrl});
            await refresh();
            status.textContent = (result.success ? "已确认成功" : "未计入成功") + "：" + (result.code || result.error || result.msg || result.status) + "\n" + status.textContent;
          } catch (_) { status.textContent = "结果未确认，请勿重复执行。"; }
          finally { buttons.forEach(b => { b.disabled = false; }); }
        };
      }
      const close = node("button", "关闭", bar); close.type = "button"; close.onclick = () => { dialog.close(); dialog.remove(); };
      dialog.addEventListener("cancel", () => dialog.remove());
      document.body.append(dialog); dialog.showModal(); await refresh();
    };
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();
