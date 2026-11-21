"use strict";
// 直播间写动作：点赞直播间 / 关注主播 / 参与抽奖 / 关注观众 / 私信观众。
//
// 与 live-platform.js 的区别：那一套只认 www.douyin.com/video/<id> 的视频页，
// 而直播间的目标地址是 live.douyin.com/<roomId>，既没有 videoId 也没有评论 id。
// 所以这里单独一套：作用域仍然从「任务+账号」的 scope 文件读、照样验 HMAC 签名，
// 只是动作对象换成「房间地址 + 昵称」。
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { signatureFields } = require("./leadgen-extensions");

const FIELDS = ["type", "accountId", "leadgenTaskId", "videoUrl", "commentId", "text", "scopeId", "deadlineAt"];
const LIVE_ACTIONS = new Set(["like-live", "follow-live", "lottery-live", "follow-user", "send-dm"]);
// 观众级动作必须带昵称（直播间弹幕没有稳定的用户 id，只能按昵称现场定位）
const VIEWER_ACTIONS = new Set(["follow-user", "send-dm"]);

function liveRoomId(url) {
  try {
    const u = new URL(String(url || ""));
    if (u.protocol !== "https:" || u.hostname !== "live.douyin.com") return "";
    const m = /^\/([0-9]{6,})\/?$/.exec(u.pathname);
    return m ? m[1] : "";
  } catch (_) { return ""; }
}

function isLiveAction(command) {
  return !!command && LIVE_ACTIONS.has(command.type) && !!liveRoomId(command.videoUrl);
}

/** 作用域文件候选：后端在签名命令里给出精确路径；找不到再退回环境变量目录。 */
function scopeCandidates(command) {
  const list = [];
  if (typeof command.scopeFile === "string" && command.scopeFile) list.push(command.scopeFile);
  const dir = process.env.LEADS_MONITOR_SCOPE_DIR;
  if (dir) {
    try {
      for (const name of fs.readdirSync(dir)) {
        if (name.startsWith(String(command.leadgenTaskId || "")) && name.endsWith(".json"))
          list.push(path.join(dir, name));
      }
    } catch (_) { /* 目录不存在就跳过 */ }
  }
  return list;
}

function verifyScope(command) {
  if (!LIVE_ACTIONS.has(command.type)) return null;
  if (!liveRoomId(command.videoUrl)) return null;
  if (VIEWER_ACTIONS.has(command.type) && !String(command.commentAuthor || "").trim()) return null;
  for (const file of scopeCandidates(command)) {
    let scope;
    try { scope = JSON.parse(fs.readFileSync(file, "utf8")); } catch (_) { continue; }
    if (!scope || typeof scope.secret !== "string" || scope.secret.length < 32) continue;
    if (typeof scope.expiresAt !== "number" || !Number.isFinite(scope.expiresAt)) continue;
    if (scope.expiresAt * 1000 <= Date.now()) continue;
    if (scope.accountId !== command.accountId) continue;
    if (scope.leadgenTaskId !== command.leadgenTaskId) continue;
    if (scope.scopeId !== command.scopeId) continue;
    if (scope.mode !== "monitor") continue;      // 直播间动作只由监控任务发起
    const limit = scope.actions && scope.actions[command.type];
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) continue;
    const urls = Array.isArray(scope.videoUrls) ? scope.videoUrls : [];
    if (!urls.includes("*") && !urls.includes(command.videoUrl)) continue;
    if (!Number.isSafeInteger(command.deadlineAt)) continue;
    if (command.deadlineAt <= Date.now() || command.deadlineAt > Date.now() + 115000) continue;
    const fields = signatureFields(command, FIELDS);
    const signature = crypto.createHmac("sha256", scope.secret)
      .update(JSON.stringify(fields.map((k) => String(command[k] || "")))).digest("hex");
    const supplied = String(command.acceptanceSignature || "");
    if (signature.length !== supplied.length) continue;
    if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(supplied))) continue;
    return { scope, file };
  }
  return null;
}

/* --------------------------------------------------------------- DOM 探针 */

// 直播间「本场点赞」计数：主播信息栏里形如 "7102本场点赞"（可能带万/亿缩写）。
// 同时找一下有没有独立的点赞按钮：不同版本 PC 直播页布局不一样，
// 有的把点赞做成图标按钮，有的只能点播放器。
const LIKE_COUNT_JS = `(() => {
  const parse = (raw) => {
    if (!raw) return null;
    const n = Number(String(raw).replace(/[,，]/g, ""));
    if (!Number.isFinite(n)) return null;
    return n;
  };
  const text = (document.body && document.body.innerText) || "";
  const m = /([0-9][0-9,\\.]*\\s*[万亿]?)\\s*本场点赞/.exec(text);
  const player = document.querySelector('[data-e2e="living-container"], .living-container, video');
  let point = null;
  if (player) {
    const r = player.getBoundingClientRect();
    // 点播放器中部偏左：避开右侧礼物栏、底部操作条和聊天输入框
    point = { x: Math.round(r.left + r.width * 0.42), y: Math.round(r.top + r.height * 0.45) };
  }
  // 独立点赞按钮：优先 aria-label / data-e2e，其次 class 里带 like 的可见元素
  let btn = null;
  const cands = Array.from(document.querySelectorAll('[aria-label*="点赞"],[data-e2e*="like"],[class*="like" i]'));
  for (let i = cands.length - 1; i >= 0; i--) {
    const el = cands[i];
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 20 || r.width > 140) continue;
    if (r.left < 0 || r.top < 0 || r.bottom > innerHeight) continue;
    btn = { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
      label: (el.getAttribute("aria-label") || el.getAttribute("data-e2e") || "").slice(0, 20) };
    break;
  }
  let count = parse(m && m[1]);
  let unit = 1;
  if (m && /万/.test(m[1])) unit = 10000;
  if (m && /亿/.test(m[1])) unit = 100000000;
  if (count != null) count = count * unit;
  return { count, point, button: btn,
    loggedIn: !/需先登录|请先登录|登录后参与/.test(text) };
})()`;

// 悬停后右侧工具栏才会出现。点赞是一枚没有文字/aria 的心形图标，
// 只能按「右侧、下半区、可点、带 svg」筛出一小撮候选，逐个点一次看计数是否变化。
const TOOLBAR_JS = `(() => {
  const out = [];
  document.querySelectorAll("div,button,span").forEach((el) => {
    if (out.length > 8) return;
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.width > 72 || r.height < 24 || r.height > 72) return;
    if (r.left < innerWidth * 0.62 || r.top < innerHeight * 0.35) return;
    if (getComputedStyle(el).cursor !== "pointer") return;
    if (!el.querySelector("svg")) return;
    out.push({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
      cls: String(el.className || "").slice(0, 26),
      label: (el.getAttribute("aria-label") || "").slice(0, 12) });
  });
  return { candidates: out };
})()`;

const FOLLOW_JS = `(() => {
  const el = document.querySelector('[data-e2e="live-followbutton"]');
  if (!el) return { found: false };
  const r = el.getBoundingClientRect();
  if (r.width < 8 || r.height < 8) return { found: false };
  const text = (el.innerText || "").replace(/\\s+/g, " ").trim();
  return { found: true, text, already: /已关注|互相关注/.test(text),
    x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
})()`;

// 抽奖/福袋入口：PC 端没有固定 data-e2e，按文案 + 图片地址兜底
const LOTTERY_JS = `(() => {
  const ok = /福袋|抽奖|免费抽|幸运袋/.test;
  const nodes = [];
  document.querySelectorAll("div,span,button,img,[role='button']").forEach((el) => {
    const text = (el.innerText || "").replace(/\\s+/g, " ").trim();
    const alt = el.getAttribute("alt") || "";
    const src = el.getAttribute("src") || "";
    if (!(ok(text) || ok(alt) || /lucky|lottery|fudai/i.test(src))) return;
    const r = el.getBoundingClientRect();
    if (r.width < 16 || r.height < 16 || r.width > 260) return;
    nodes.push({ text: text.slice(0, 20), alt: alt.slice(0, 16), w: Math.round(r.width),
      x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) });
  });
  if (!nodes.length) return { found: false };
  const join = nodes.find((n) => /参与|立即|去抽|领取|免费/.test(n.text)) || nodes[0];
  return { found: true, node: join, count: nodes.length };
})()`;

// 用户卡片：同时含「粉丝」与按钮的最近容器
const CARD_JS = `(() => {
  let card = null;
  document.querySelectorAll("div").forEach((el) => {
    if (card) return;
    const t = el.innerText || "";
    if (!/粉丝/.test(t) || t.length > 320) return;
    const btns = el.querySelectorAll("button,[role='button']");
    if (btns.length >= 1 && btns.length <= 10) card = el;
  });
  if (!card) return { found: false };
  const hit = (el) => {
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width), h: Math.round(r.height) };
  };
  const out = { found: true, follow: null, dm: null };
  card.querySelectorAll("button,[role='button'],div,span").forEach((el) => {
    const t = (el.innerText || "").replace(/\\s+/g, "").trim();
    if (!t || t.length > 6) return;
    const box = hit(el);
    if (box.w < 24 || box.h < 14) return;
    if (!out.dm && (t === "私信" || t === "发私信")) out.dm = box;
    if (!out.follow && (t === "关注" || t === "+关注")) out.follow = { ...box, already: false };
    if (!out.follow && (t === "已关注" || t === "互相关注")) out.follow = { ...box, already: true };
  });
  return out;
})()`;

function nickRectJs(nickname) {
  return `(() => {
  const want = ${JSON.stringify(String(nickname || ""))};
  const itemsBox = document.querySelector('[class*="webcast-chatroom___items"]');
  const list = document.querySelector('[class*="webcast-chatroom___list"]');
  const host = itemsBox || list;
  let rows = [];
  if (host) { let n = host; for (let i = 0; i < 6 && n; i++) { if (n.children.length > 1) { rows = Array.from(n.children); break; } n = n.firstElementChild; } }
  for (let i = rows.length - 1; i >= 0; i--) {
    const el = rows[i];
    const text = (el.innerText || "").replace(/\\s+/g, " ").trim();
    if (!text.startsWith(want)) continue;
    for (const cand of el.querySelectorAll("span,div,a")) {
      const t = (cand.innerText || "").replace(/\\s+/g, " ").trim();
      if (!t || t.length > want.length + 4) continue;
      if (getComputedStyle(cand).cursor !== "pointer") continue;
      const r = cand.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) return null;
      return { x: Math.round(r.left + Math.min(r.width / 2, 24)), y: Math.round(r.top + r.height / 2) };
    }
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left + 30), y: Math.round(r.top + r.height / 2) };
  }
  return null;
})()`;
}

const DM_BOX_JS = `(() => {
  const el = document.querySelector('textarea,[contenteditable="true"],[class*="chat-input"] [contenteditable]');
  if (!el) return { found: false };
  const r = el.getBoundingClientRect();
  if (r.width < 40 || r.height < 12) return { found: false };
  return { found: true, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
})()`;

const DM_SEND_JS = `(() => {
  const nodes = Array.from(document.querySelectorAll("button,[role='button'],div,span"));
  for (const el of nodes) {
    const t = (el.innerText || "").replace(/\\s+/g, "").trim();
    if (t !== "发送" && t !== "发送私信") continue;
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 14) continue;
    return { found: true, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  }
  return { found: false };
})()`;

/* --------------------------------------------------------------- 执行 */

function skipped(code, extra) {
  return Object.assign({ ok: false, status: "skipped", code }, extra || {});
}

async function openCard(command, io) {
  const box = await io.evaluate(nickRectJs(command.commentAuthor));
  if (!box) return skipped("viewer_row_not_found");
  await io.click(box.x, box.y);
  await io.sleep(1800);
  const card = await io.evaluate(CARD_JS);
  if (!card || !card.found) return skipped("viewer_card_not_found");
  return { ok: true, card };
}

async function followViewer(command, io, active) {
  const opened = await openCard(command, io);
  if (!opened.ok) return opened;
  const { card } = opened;
  if (!card.follow) return skipped("follow_control_not_verified");
  if (card.follow.already) return skipped("already_followed");
  if (!active()) return skipped("scope_expired_or_stopped");
  await io.click(card.follow.x, card.follow.y);
  await io.sleep(1600);
  const after = await io.evaluate(CARD_JS);
  if (after && after.follow && after.follow.already)
    return { ok: true, status: "sent_ok", code: "follow_state_verified", user: command.commentAuthor };
  return { ok: false, status: "uncertain", code: "follow_state_not_confirmed" };
}

async function dmViewer(command, io, active) {
  const opened = await openCard(command, io);
  if (!opened.ok) return opened;
  const { card } = opened;
  if (!card.dm) return skipped("dm_entry_not_verified");
  if (!active()) return skipped("scope_expired_or_stopped");
  await io.click(card.dm.x, card.dm.y);
  await io.sleep(2200);
  const box = await io.evaluate(DM_BOX_JS);
  if (!box || !box.found) return skipped("dm_editor_not_found");
  await io.click(box.x, box.y);
  await io.sleep(400);
  await io.type(command.text);
  await io.sleep(500);
  const send = await io.evaluate(DM_SEND_JS);
  if (!send || !send.found) return skipped("dm_send_control_not_found");
  await io.click(send.x, send.y);
  await io.sleep(2000);
  const echo = await io.evaluate(`(() => {
    const t = (document.body && document.body.innerText) || "";
    return { sent: t.includes(${JSON.stringify(String(command.text || "").slice(0, 24))}) };
  })()`);
  if (echo && echo.sent)
    return { ok: true, status: "sent_ok", code: "dm_visible_in_conversation", user: command.commentAuthor };
  return { ok: false, status: "uncertain", code: "dm_receipt_missing_no_retry" };
}

async function likeLive(command, io, active, scope) {
  const cap = Math.max(1, Math.min(30, parseInt(String(command.text || ""), 10) || 5,
    Number(scope.actions["like-live"]) || 1));
  const before = await io.evaluate(LIKE_COUNT_JS);
  if (!before) return skipped("page_not_ready");
  if (!before.loggedIn) return skipped("login_required");
  if (!before.point && !before.button) return skipped("like_control_not_found");

  // 抖音 PC 直播页的点赞入口在不同版本里位置不同：可能是独立图标按钮，
  // 也可能只能点播放器（有的版本要双击）。逐个试，只有「本场点赞」真的涨了才算成功。
  const readCount = async () => {
    const now = await io.evaluate(LIKE_COUNT_JS);
    return now && now.count != null ? now.count : null;
  };
  const base = before.count;
  let clicks = 0;

  const burst = async (label, action) => {
    for (let i = 0; i < cap; i++) {
      if (!active()) return false;
      await action();
      clicks += 1;
      await io.sleep(150 + Math.floor(Math.random() * 160));
      if (i === 0) {
        await io.sleep(1100);
        const now = await readCount();
        if (base != null && now != null && now > base) return label;
        if (base == null) return label;          // 没有计数可看，按「已尝试」处理
      }
    }
    const now = await readCount();
    return base != null && now != null && now > base ? label : "";
  };

  let via = "";
  if (before.button) via = await burst("button", () => io.click(before.button.x, before.button.y));

  if (!via && before.point && io.hover) {
    // 先悬停到播放器：右侧工具栏（含点赞心形）要 hover 才出现
    await io.hover(before.point.x, before.point.y);
    await io.sleep(800);
    const hovered = await io.evaluate(LIKE_COUNT_JS);
    if (hovered && hovered.button)
      via = await burst("hover-button", () => io.click(hovered.button.x, hovered.button.y));
    if (!via) {
      const bar = await io.evaluate(TOOLBAR_JS);
      for (const cand of ((bar && bar.candidates) || []).slice(0, 4)) {
        if (!active()) break;
        await io.click(cand.x, cand.y);
        clicks += 1;
        await io.sleep(1200);
        const now = await readCount();
        if (base != null && now != null && now > base) { via = "toolbar:" + cand.cls; break; }
        // 点开的是面板而不是点赞：Esc 关掉，继续试下一个
        if (io.key) { await io.key("Escape"); await io.sleep(500); }
      }
    }
  }
  if (!via && before.point) {
    via = await burst("player-click", () => io.click(before.point.x, before.point.y));
  }
  if (!via && before.point && io.dblclick) {
    via = await burst("player-dblclick", () => io.dblclick(before.point.x, before.point.y));
  }

  const after = await readCount();
  let gained = base != null && after != null ? after - base : null;
  if (!gained && base != null) {
    // 平台的本场点赞计数有延迟，多等几秒再复核一次
    await io.sleep(4500);
    const late = await readCount();
    gained = late != null ? late - base : null;
  }
  if (via && (gained == null || gained > 0))
    return { ok: true, status: "sent_ok",
      code: gained ? "like_count_increased" : "like_clicked_no_counter", times: gained || clicks,
      clicks, via };
  // 该房间不显示本场点赞（主播自己的房间常见），或平台合并计数：
  // 点击确实发出去了但状态没法复核，按「不确定」上报，不重试。
  return { ok: false, status: "uncertain",
    code: base == null ? "like_count_not_displayed" : "like_count_not_confirmed",
    clicks, via: via || "none" };
}

async function followLive(command, io, active) {
  const before = await io.evaluate(FOLLOW_JS);
  if (!before || !before.found) return skipped("follow_control_not_verified");
  if (before.already) return skipped("already_followed");
  if (!active()) return skipped("scope_expired_or_stopped");
  await io.click(before.x, before.y);
  await io.sleep(1800);
  const after = await io.evaluate(FOLLOW_JS);
  if (after && after.found && after.already)
    return { ok: true, status: "sent_ok", code: "follow_state_verified" };
  return { ok: false, status: "uncertain", code: "follow_state_not_confirmed" };
}

async function lotteryLive(command, io, active) {
  const before = await io.evaluate(LOTTERY_JS);
  if (!before || !before.found) return skipped("no_lottery_running");
  if (!active()) return skipped("scope_expired_or_stopped");
  await io.click(before.node.x, before.node.y);
  await io.sleep(1600);
  const panel = await io.evaluate(LOTTERY_JS);
  if (panel && panel.found && active()) {
    await io.click(panel.node.x, panel.node.y);
    await io.sleep(1600);
  }
  const after = await io.evaluate(`(() => {
    const t = (document.body && document.body.innerText) || "";
    return { joined: /已参与|参与成功|抽奖中|等待开奖|恭喜/.test(t) };
  })()`);
  if (after && after.joined)
    return { ok: true, status: "sent_ok", code: "lottery_joined" };
  return { ok: false, status: "uncertain", code: "lottery_state_not_confirmed" };
}

async function execute(command, io) {
  const verified = verifyScope(command);
  if (!verified) return { ok: false, status: "blocked", code: "live_writes_disabled" };
  const { scope } = verified;
  const active = () => {
    if (io.stopped && io.stopped()) return false;
    return !!verifyScope(command);
  };
  try {
    if (!active()) return skipped("scope_expired_or_stopped");
    if (command.type === "like-live") return await likeLive(command, io, active, scope);
    if (command.type === "follow-live") return await followLive(command, io, active);
    if (command.type === "lottery-live") return await lotteryLive(command, io, active);
    if (command.type === "follow-user") return await followViewer(command, io, active);
    if (command.type === "send-dm") return await dmViewer(command, io, active);
    return skipped("unsupported_action");
  } catch (err) {
    return { ok: false, status: "uncertain", code: "live_action_exception",
      error: String((err && err.message) || err).slice(0, 120) };
  }
}

module.exports = { execute, verifyScope, isLiveAction, liveRoomId, FIELDS, LIVE_ACTIONS };
