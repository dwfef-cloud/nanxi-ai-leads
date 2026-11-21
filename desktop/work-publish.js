/**
 * 作品发布执行器（壳侧）。
 *
 * 边界
 * ----
 * 发布是把本地文件真的传上抖音创作者中心，属于不可撤销的写操作。这个模块只做
 * **用户已经点过启动**的那一篇：文件、标题、描述都由后端任务队列给定，不接受
 * 临时参数；每一步失败都要带回「卡在哪一步 + 页面原话」，绝不返回 ok:true 蒙混。
 *
 * 为什么不猜死选择器
 * ------------------
 * 创作者中心改版频繁（data-e2e 时有时无、描述框是 contenteditable 而不是
 * textarea）。所以每一步都用「多候选 + 页面文本兜底」：找不到就回报实际 DOM
 * 线索（按钮文案、input 数量），供下次校准，而不是一句「元素不存在」。
 *
 * 官方定时（2026-11-18）
 * ----------------
 * cmd.officialScheduleAt 有值（距发布时间 ≥ 2 小时）时优先设置平台的
 * 「定时发布」：开关打开 → 填时间 → 再点发布。控件找不到 / 设置失败时
 * 返回 status:"schedule_failed"，由后端回落「软件等到点提交」，绝不提前发。
 *
 * 视频自定义封面
 * --------
 * cmd.coverLandscapePath / coverPortraitPath 有值时，在「编辑封面」弹窗里
 * 把封面文件塞进 input[type=file]。找不到入口时只记录原因（coverSkipped），
 * 不阻断发布——封面缺失不该让整篇作品发不出去。
 */
"use strict";

const UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload";
const MANAGE_HINT = "/creator-micro/content/manage";

/** 页面现状：是否登录、有没有文件输入框、按钮文案都有哪些。 */
const PROBE_JS = `(() => {
  const text = (document.body && document.body.innerText) || "";
  const inputs = [...document.querySelectorAll('input[type=file]')].map(i => ({
    accept: i.getAttribute('accept') || '', multiple: !!i.multiple,
    className: String(i.className || '').slice(0, 60),
  }));
  const buttons = [...document.querySelectorAll('button')].map(b => (b.innerText || '').trim())
    .filter(Boolean).slice(0, 24);
  return {
    url: location.href,
    loginWall: /扫码登录|登录后|请先登录|密码登录/.test(text.slice(0, 3000)),
    uploadText: /上传视频|上传图文|点击上传|拖拽/.test(text),
    inputs,
    buttons,
    textLen: text.length,
    uploading: /上传中|处理中|上传失败|重新上传/.test(text),
  };
})()`;

/** 填输入框：走原生 setter + input 事件，React 受控组件才认。 */
const FILL_JS = (title, desc) => `(() => {
  const setNative = (el, value) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
      : el.getAttribute('contenteditable') === 'true' ? null : HTMLInputElement.prototype;
    if (proto) {
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) {
        desc.set.call(el, value);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }
    }
    // contenteditable：先聚焦再插入文本，编辑器自己的状态才会更新
    el.focus();
    el.textContent = value;
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
    return true;
  };
  const report = [];
  const titleEl = document.querySelector('input[placeholder*="标题"]')
    || document.querySelector('[data-e2e="title-input"] input')
    || [...document.querySelectorAll('input[type=text]')].find(i => /标题/.test(i.placeholder || ''));
  if (titleEl) report.push(['title', setNative(titleEl, ${JSON.stringify(String(title || ""))})]);
  const descEl = document.querySelector('div[contenteditable="true"]')
    || document.querySelector('[data-e2e="video-desc"]')
    || document.querySelector('textarea[placeholder*="描述"]')
    || document.querySelector('textarea');
  if (descEl) report.push(['desc', setNative(descEl, ${JSON.stringify(String(desc || ""))})]);
  return { filled: report.map(r => r[0]), titleFound: !!titleEl, descFound: !!descEl };
})()`;

/** 点「发布」并判断结果。 */
const SUBMIT_JS = `(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const label = b => ((b.innerText || b.textContent || '').trim());
  const find = () => [...document.querySelectorAll('button')]
    .find(b => /^发布$|发布作品|立即发布|^定时发布$/.test(label(b)) && !b.disabled);
  let btn = null;
  for (let i = 0; i < 60; i++) { btn = find(); if (btn) break; await sleep(500); }
  if (!btn) {
    const all = [...document.querySelectorAll('button')].map(label).filter(Boolean).slice(0, 20);
    return { clicked: false, reason: '发布按钮不可用', buttons: all };
  }
  btn.click();
  for (let i = 0; i < 40; i++) {
    await sleep(750);
    const text = (document.body && document.body.innerText) || '';
    if (/发布成功|作品已发布|发布完成|定时发布成功|定时发布设置成功/.test(text)
        || location.href.includes('${MANAGE_HINT}')) {
      return { clicked: true, success: true, url: location.href };
    }
    if (/发布失败|审核不通过|内容违规|定时设置失败/.test(text)) {
      const line = (text.match(/.{0,40}(发布失败|审核不通过|内容违规|定时设置失败).{0,60}/) || [''])[0];
      return { clicked: true, success: false, reason: line.trim() };
    }
  }
  return { clicked: true, success: false, reason: '点击发布后未出现成功/失败提示（可能在审核中）',
           url: location.href };
})()`;

/**
 * 设置平台的「定时发布」（2 小时外交给官方定时）。
 * 多候选：优先 data-e2e / switch 控件，其次按文本找开关；时间控件是
 * datetime-local 或 「日期 + 时间」两组输入。任何一步失败都回报具体卡点。
 */
const SCHEDULE_JS = (scheduleMs) => `(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const label = el => ((el.innerText || el.textContent || '')).trim();
  // 1) 找定时发布开关：switch 组件 / 含「定时发布」的可点击行
  const toggles = [...document.querySelectorAll('[class*=switch], [role=switch], .semi-switch')]
    .filter(el => el.offsetParent !== null);
  const rowByText = [...document.querySelectorAll('label, div, span')]
    .filter(el => el.offsetParent !== null && /定时发布/.test(label(el)) && label(el).length < 30);
  let sw = toggles.find(el => {
    const box = el.closest('div');
    return box && /定时发布/.test(label(box).slice(0, 80));
  }) || (rowByText.length ? rowByText[rowByText.length - 1].closest('label') || rowByText[0] : null);
  if (!sw) return { ok: false, reason: '页面上没有「定时发布」开关', buttons:
    [...document.querySelectorAll('button')].map(label).filter(Boolean).slice(0, 16) };
  const cls = String(sw.className || '');
  if (!/checked/.test(cls)) {
    sw.click();
    await sleep(600);
  }
  // 2) 填时间：datetime-local 输入（原生 setter + input 事件，React 受控才认）
  const stamp = ${JSON.stringify(Number(scheduleMs || 0))};
  const d = new Date(stamp);
  const pad = n => String(n).padStart(2, '0');
  const value = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
    + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  const dt = document.querySelector('input[type=datetime-local]');
  if (!dt) return { ok: false, reason: '定时开关已打开但没找到时间输入框' };
  const proto = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  proto.set.call(dt, value);
  dt.dispatchEvent(new Event('input', { bubbles: true }));
  dt.dispatchEvent(new Event('change', { bubbles: true }));
  await sleep(400);
  return { ok: true, value };
})()`;

/**
 * 打开「编辑封面」弹窗，返回弹窗内可用的封面文件输入框数量。
 * （文件的写入走 Node 侧 CDP setFiles，所以这里只负责定位与打开。）
 */
const COVER_OPEN_JS = `(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const label = el => ((el.innerText || el.textContent || '')).trim();
  const entry = [...document.querySelectorAll('button, div, span')]
    .filter(el => el.offsetParent !== null && /^(编辑封面|设置封面|修改封面)$/.test(label(el)))[0];
  if (!entry) return { ok: false, reason: '页面上没有「编辑封面」入口' };
  entry.click();
  for (let i = 0; i < 12; i++) {
    await sleep(500);
    const inputs = [...document.querySelectorAll('input[type=file]')]
      .filter(i => i.offsetParent !== null && (i.getAttribute('accept') || '').includes('image'));
    if (inputs.length) return { ok: true, inputs: inputs.length };
  }
  return { ok: false, reason: '封面弹窗已打开但没找到封面图片输入框' };
})()`;

/** 封面弹窗收尾：点「完成 / 确定 / 保存」。 */
const COVER_CLOSE_JS = `(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const label = b => ((b.innerText || b.textContent || '')).trim();
  const btn = [...document.querySelectorAll('button')]
    .filter(b => b.offsetParent !== null && /^(完成|确定|保存)$/.test(label(b)))[0];
  if (!btn) return { ok: false, reason: '封面弹窗里没有完成按钮' };
  btn.click();
  await sleep(800);
  return { ok: true };
})()`;

/** 通过 CDP 把文件塞进 input[type=file]（Electron 里唯一可靠的方式）。 */
async function setFiles(view, files, selector, index = 0) {
  const debug = view.webContents.debugger;
  const attached = debug.isAttached();
  if (!attached) debug.attach("1.3");
  try {
    const { root } = await debug.sendCommand("DOM.getDocument");
    let nodeIds = [];
    if (selector) {
      ({ nodeIds } = await debug.sendCommand("DOM.querySelectorAll", { nodeId: root.nodeId, selector }));
    } else {
      const found = await debug.sendCommand("DOM.performSearch", { query: 'input[type="file"]', includeUserAgentShadowDOM: true });
      const res = await debug.sendCommand("DOM.getSearchResults", { searchId: found.searchId, fromIndex: 0, toIndex: found.resultCount });
      nodeIds = res.nodeIds || [];
      await debug.sendCommand("DOM.discardSearchResults", { searchId: found.searchId });
    }
    if (!nodeIds.length) throw new Error("页面上没有找到文件输入框");
    const node = nodeIds[Math.min(index, nodeIds.length - 1)];
    await debug.sendCommand("DOM.setFileInputFiles", { nodeId: node, files });
    return nodeIds.length;
  } finally {
    if (!attached) {
      try { debug.detach(); } catch (e) { /* ignore */ }
    }
  }
}

/**
 * 视频自定义封面：尽力设置，失败只记录原因，不阻断发布。
 * @returns {{coverApplied?: boolean, coverSkipped?: string}}
 */
async function applyCover(cmd, io) {
  const landscape = String(cmd.coverLandscapePath || "").trim();
  const portrait = String(cmd.coverPortraitPath || "").trim();
  if (!landscape && !portrait) return {};
  const open = await io.evaluate(COVER_OPEN_JS, 12000).catch((e) => ({ ok: false, reason: String((e && e.message) || e) }));
  if (!open || !open.ok) return { coverSkipped: (open && open.reason) || "封面入口不可用" };
  try {
    if (landscape) await setFiles(io.view, [landscape], 'input[type="file"][accept*="image"]', 0);
    if (portrait) {
      await setFiles(io.view, [portrait], 'input[type="file"][accept*="image"]', 1);
    }
  } catch (e) {
    return { coverSkipped: `封面文件写入失败：${String((e && e.message) || e)}` };
  }
  const close = await io.evaluate(COVER_CLOSE_JS, 8000).catch(() => ({ ok: false }));
  if (!close || !close.ok) return { coverSkipped: "封面已写入但弹窗收尾失败" };
  return { coverApplied: true };
}

/**
 * 官方定时：2 小时外交给平台定时发布；设置失败返回 schedule_failed 让后端回落。
 */
async function applyOfficialSchedule(scheduleAtMs, io) {
  if (!Number(scheduleAtMs) || Number(scheduleAtMs) <= Date.now()) return {};
  const res = await io.evaluate(SCHEDULE_JS(Number(scheduleAtMs)), 15000)
    .catch((e) => ({ ok: false, reason: String((e && e.message) || e) }));
  if (!res || !res.ok) {
    return { ok: false, status: "schedule_failed",
             error: (res && res.reason) || "官方定时设置失败" };
  }
  return { ok: true };
}

/**
 * 执行一次发布。
 * @param {{files:string[], title:string, description:string, mediaType:string}} cmd
 * @param {{evaluate:Function, loadPage:Function, sleep:Function, view:any, stopped:Function}} io
 */
async function submit(cmd, io) {
  const files = (cmd.files || cmd.mediaPaths || []).filter(Boolean);
  if (!files.length) return { ok: false, status: "failed", error: "没有素材文件" };
  if (io.stopped && io.stopped()) return { ok: false, status: "stopped", error: "已停止" };

  await io.loadPage(UPLOAD_URL, 6000);
  let probe = await io.evaluate(PROBE_JS, 10000).catch(() => null);
  if (!probe) return { ok: false, status: "uncertain", error: "创作者中心页面读取超时（可能被风控或网络异常）" };
  if (probe.loginWall) {
    return { ok: false, status: "failed", code: "login_required",
             error: "该账号未登录创作者中心，请先在账号池完成登录后再发布" };
  }
  // 上传页会在 SPA 里异步挂载文件框，最多等 20s
  for (let i = 0; i < 20 && !(probe.inputs || []).length; i++) {
    await io.sleep(1000);
    probe = await io.evaluate(PROBE_JS, 8000).catch(() => probe);
  }
  if (!(probe.inputs || []).length) {
    return { ok: false, status: "uncertain",
             error: `上传页没有文件输入框（按钮：${(probe.buttons || []).slice(0, 6).join("/") || "无"}）` };
  }

  try {
    await setFiles(io.view, files);
  } catch (e) {
    return { ok: false, status: "failed", error: `素材写入失败：${String((e && e.message) || e)}` };
  }

  // 等上传/转码完成：成功标志是描述框出现且「上传中」消失
  let ready = null;
  for (let i = 0; i < 240; i++) {
    if (io.stopped && io.stopped()) return { ok: false, status: "stopped", error: "已停止" };
    await io.sleep(2500);
    ready = await io.evaluate(PROBE_JS, 8000).catch(() => null);
    if (!ready) continue;
    if (/上传失败/.test((ready.buttons || []).join(" "))) {
      return { ok: false, status: "failed", error: "平台提示上传失败（可能是格式/时长不满足要求）" };
    }
    if (!ready.uploading && ready.textLen > 200) break;
  }

  const filled = await io.evaluate(FILL_JS(cmd.title, cmd.description), 15000)
    .catch(e => ({ filled: [], error: String((e && e.message) || e) }));
  if (!filled || !filled.descFound) {
    return { ok: false, status: "uncertain", code: "desc_not_found",
             error: "上传疑似完成，但页面上找不到作品描述框，未提交" };
  }

  // 封面与官方定时都在「点发布」之前做；两者失败的处理策略不同：
  // 封面缺了照发（coverSkipped 记原因），定时没设置成必须回报（回落后端）。
  const coverResult = await applyCover(cmd, io);
  const scheduleResult = await applyOfficialSchedule(cmd.officialScheduleAt, io);
  if (!scheduleResult.ok) return scheduleResult;

  const result = await io.evaluate(SUBMIT_JS, 60000).catch(e => ({ clicked: false, reason: String((e && e.message) || e) }));
  if (result && result.success) {
    return { ok: true, status: "published", url: result.url || "",
             filled: filled.filled, ...coverResult };
  }
  // 点了发布但没拿到成功提示：可能真的发出去了（审核中），标 uncertain 而不是失败，
  // 避免用户重试造成重复发布；页面上的失败文案才判 failed。
  return {
    ok: false,
    status: result && result.clicked ? "uncertain" : "failed",
    error: (result && (result.reason || (result.buttons || []).join("/"))) || "发布未完成",
    filled: filled.filled,
    ...coverResult,
  };
}

module.exports = { submit, PROBE_JS, FILL_JS, SUBMIT_JS, SCHEDULE_JS, COVER_OPEN_JS, COVER_CLOSE_JS };
