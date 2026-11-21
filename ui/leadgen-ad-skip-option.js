/* Persistent leadgen preference for skipping sponsored feed videos. */
(function () {
  var key = "leadgen_skip_ad_videos";
  var sectionClass = "leadgen-ad-skip-option";

  function enabled() {
    try { return window.localStorage.getItem(key) !== "false"; }
    catch (_) { return true; }
  }

  function mount() {
    var tip = document.querySelector(".frequency-risk-tip");
    if (!tip || tip.parentElement.querySelector("." + sectionClass)) return;
    var row = document.createElement("label");
    row.className = sectionClass;
    row.style.cssText = "display:flex;align-items:flex-start;gap:8px;margin:8px 0 0;padding:9px 10px;border:1px solid var(--overlay-08);border-radius:6px;background:var(--surface-35);color:var(--text-secondary);font-size:12px;line-height:1.5;cursor:pointer";
    var checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = enabled();
    checkbox.style.cssText = "margin:3px 0 0;flex:0 0 auto;accent-color:var(--primary-color,#3b82f6)";
    checkbox.addEventListener("change", function () {
      try { window.localStorage.setItem(key, checkbox.checked ? "true" : "false"); }
      catch (_) {}
    });
    var copy = document.createElement("span");
    copy.innerHTML = "<strong>广告视频直接划过</strong><br><span style=\"color:var(--text-muted)\">开启后，广告不计入浏览目标，不执行停留、评论读取或视频间隔；下一条普通视频仍按设置执行。</span>";
    row.append(checkbox, copy);
    tip.after(row);
  }

  function bindStartPayload() {
    var bridge = window.ipcRenderer;
    if (!bridge || typeof bridge.invoke !== "function" || bridge.invoke.__adSkipBound) return;
    var invoke = bridge.invoke;
    function wrapped(channel) {
      var args = Array.prototype.slice.call(arguments, 1);
      if (channel === "start-task" && args[0] && typeof args[0] === "object" && args[0].taskMode !== "nurture") {
        args[0] = Object.assign({}, args[0], { skipAdVideos: enabled() });
      }
      return invoke.apply(this, [channel].concat(args));
    }
    wrapped.__adSkipBound = true;
    bridge.invoke = wrapped;
  }

  function refresh() {
    var intervalLabel = document.querySelector(".frequency-control-copy label");
    if (intervalLabel && intervalLabel.textContent.trim() === "互动间隔 (秒)")
      intervalLabel.textContent = "视频间隔 (秒)";
    mount();
    bindStartPayload();
  }

  document.addEventListener("DOMContentLoaded", refresh);
  new MutationObserver(refresh).observe(document.documentElement, { childList: true, subtree: true });
  refresh();
})();
