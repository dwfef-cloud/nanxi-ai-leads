(function () {
  "use strict";
  async function call(channel, data = {}) {
    return window.ipcRenderer.invoke(channel, data);
  }
  function element(tag, text, parent) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (parent) parent.append(node);
    return node;
  }
  async function open() {
    if (document.getElementById("task-model-dialog")) return;
    const dialog = element("dialog"); dialog.id = "task-model-dialog";
    dialog.style.cssText = "width:500px;padding:24px;background:#142034;color:#e5edf9;border:1px solid #506687;border-radius:12px;font:14px system-ui";
    element("h3", "任务模型设置", dialog);
    element("p", "评论生成与人设试跑共用此配置。密钥不会回显；留空保留原密钥。", dialog);
    const form = element("form", "", dialog);
    const inputs = {};
    for (const [name, label, type] of [["base_url", "模型服务地址", "url"], ["model", "模型名称", "text"], ["api_key", "API 密钥", "password"]]) {
      const wrapper = element("label", label, form);
      wrapper.style.cssText = "display:block;margin:14px 0";
      const input = element("input", "", wrapper); input.name = name; input.type = type;
      input.autocomplete = "off"; input.style.cssText = "box-sizing:border-box;display:block;width:100%;margin-top:6px;padding:10px;background:#0c1423;border:1px solid #506687;border-radius:6px;color:#e5edf9";
      if (name !== "api_key") input.required = true;
      inputs[name] = input;
    }
    inputs.base_url.placeholder = "https://你的模型服务地址/v1";
    const status = element("p", "正在读取任务实际使用的配置…", form);
    status.setAttribute("role", "status"); status.style.whiteSpace = "pre-wrap";
    const bar = element("div", "", form); bar.style.cssText = "display:flex;gap:12px;margin-top:18px";
    const save = element("button", "保存配置", bar); save.type = "submit";
    const test = element("button", "测试评论生成（不发送）", bar); test.type = "button";
    const close = element("button", "关闭", bar); close.type = "button";
    close.onclick = () => { inputs.api_key.value = ""; dialog.close(); dialog.remove(); };
    dialog.addEventListener("cancel", () => { inputs.api_key.value = ""; dialog.remove(); });
    document.body.append(dialog); dialog.showModal();
    try {
      const cfg = await call("get-task-model-settings");
      inputs.base_url.value = cfg.base_url || ""; inputs.model.value = cfg.model || "";
      inputs.api_key.placeholder = cfg.hasKey ? "已保存密钥，留空保留" : "尚未配置密钥";
      status.textContent = cfg.base_url ? "已读取任务当前配置。" : "当前未设置服务地址，旧配置会使用阿里云默认地址。请填写与你的密钥对应的地址。";
    } catch (_) { status.textContent = "配置读取失败，请稍后重新打开。"; }
    form.onsubmit = async event => {
      event.preventDefault(); save.disabled = test.disabled = true;
      try {
        const result = await call("save-task-model-settings", Object.fromEntries(Object.entries(inputs).map(([k, v]) => [k, v.value])));
        inputs.api_key.value = "";
        status.textContent = result.success ? "已保存到任务使用的配置。请测试评论生成。" : result.error || result.msg || "保存失败";
      } catch (_) { status.textContent = "保存失败，请检查本地服务。"; }
      finally { save.disabled = test.disabled = false; }
    };
    test.onclick = async () => {
      save.disabled = test.disabled = true; status.textContent = "正在使用已保存配置生成评论，不会发送到平台…";
      try {
        const result = await call("test-task-model");
        status.textContent = result.success ? "模型生成通过（未发送）：\n" + result.text : result.error || result.msg || "生成失败";
      } catch (_) { status.textContent = "测试请求失败，请检查本地服务。"; }
      finally { save.disabled = test.disabled = false; }
    };
  }
  function mount() {
    const button = element("button", "模型设置", document.body);
    button.id = "task-model-settings-entry";
    button.style.cssText = "position:fixed;right:18px;bottom:16px;z-index:2147482000;padding:8px 14px;background:#17253b;border:1px solid #496494;border-radius:999px;color:#d5e1fc;font:12.5px system-ui;cursor:pointer";
    button.onclick = open;
    const syncVisibility = () => {
      button.style.display = document.querySelector(".sidebar-body") ? "none" : "";
    };
    new MutationObserver(syncVisibility).observe(document.body, { childList: true, subtree: true });
    syncVisibility();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();
