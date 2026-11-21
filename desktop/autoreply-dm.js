"use strict";
/**
 * 自动回复 · 会话内私信发送
 * ------------------------
 * 与 live-dm.js（用户主页 → 私信按钮）的分工：
 *   live-dm      监控任务用，目标是「视频评论区里的某个人」，入口在他的主页；
 *   autoreply-dm 自动回复用，目标是「私信列表里的某个会话」，入口就在会话里。
 *
 * 为什么单独一套校验而不是复用 live-platform.scopeFor：
 *   1) 那套要求 command.videoUrl 是视频/直播间地址（videoIdOf 必须成立），
 *      私信会话没有视频地址，天然过不了；
 *   2) 那套对 send-dm 强制 reviewedActions 逐条白名单（预览阶段就要写死正文），
 *      自动回复的收件人与正文是运行时才出现的，无法预生成。
 * 所以这里用同强度、不同形状的作用域：monitor 作用域 + 动作键 send-dm-chat，
 * 签名字段额外绑定会话标题（commentAuthor）与正文，壳侧仍然逐项验签、限时、一次性消费。
 *
 * 回执判定以「页面上真的出现了我发的那条消息」为准（可见证据），
 * 网络回执只作为辅助；拿不到可见证据就返回 uncertain，绝不报成功、也不重试。
 */
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { DM_HOOK_JS } = require("./live-dm");

// 排查日志：发送链路的每一步都留一行，出问题时不用重新猜选择器。
const LOG_FILE = path.join(__dirname, "_verify", "autoreply.log");
function trace(...parts) {
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, new Date().toISOString().slice(11, 23) + " "
      + parts.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ") + "\n");
  } catch (_) { /* 日志失败不影响发送 */ }
}

const ACTION = "send-dm-chat";
// 必须与后端 live_scope._canonical() 的字段顺序逐字一致，否则 HMAC 对不上：
// 后端对 reply-comment / send-dm / send-dm-chat 是「基础 8 字段 + commentAuthor + recipientId」。
const FIELDS = ["type", "accountId", "leadgenTaskId", "videoUrl", "commentId", "text",
  "scopeId", "deadlineAt", "commentAuthor", "recipientId"];

function readScope(command) {
  const file = String(command.scopeFile || "").trim();
  if (!file) return null;
  try {
    const scope = JSON.parse(fs.readFileSync(file, "utf8"));
    const now = Date.now();
    if (!scope || typeof scope.secret !== "string" || scope.secret.length < 32) return null;
    if (typeof scope.expiresAt !== "number" || !Number.isFinite(scope.expiresAt) || scope.expiresAt * 1000 <= now) return null;
    if (scope.accountId !== command.accountId || scope.leadgenTaskId !== command.leadgenTaskId) return null;
    if (scope.scopeId !== command.scopeId) return null;
    if (scope.mode !== "monitor") return null;
    if (!Number.isInteger(scope.actions && scope.actions[ACTION]) || scope.actions[ACTION] < 1) return null;
    if (!Number.isSafeInteger(command.deadlineAt) || command.deadlineAt <= now
        || command.deadlineAt > now + 110000) return null;
    const signature = crypto.createHmac("sha256", scope.secret)
      .update(JSON.stringify(FIELDS.map((key) => String(command[key] || "")))).digest("hex");
    const supplied = String(command.acceptanceSignature || "");
    if (signature.length !== supplied.length
        || !crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(supplied))) return null;
    return Object.assign({}, scope, { file });
  } catch (_) {
    return null;
  }
}

/** 一次性消费：同一条会话同一正文只允许点一次发送，防重试造成重复消息。 */
function claimAttempt(scope, command) {
  const dir = path.join(path.dirname(scope.file), "attempts");
  fs.mkdirSync(dir, { recursive: true });
  const target = [ACTION, command.commentAuthor || "", crypto.createHash("sha256")
    .update(String(command.text || "")).digest("hex").slice(0, 16)].join("|");
  const key = crypto.createHash("sha256").update(scope.scopeId + ":" + target).digest("hex");
  try {
    const fd = fs.openSync(path.join(dir, key + ".claim"), "wx");
    fs.writeSync(fd, JSON.stringify({ action: ACTION, conversation: command.commentAuthor,
      createdAt: Date.now() }));
    fs.closeSync(fd);
    return true;
  } catch (error) {
    if (error.code === "EEXIST") return false;
    throw error;
  }
}

const HELPERS = `
  const cls = (el) => String((el.className && el.className.baseVal !== undefined
    ? el.className.baseVal : el.className) || "").trim();
  const has = (el, name) => cls(el).split(/\\s+/).includes(name);
  const byClass = (root, name) => [...root.querySelectorAll("*")].filter((e) => has(e, name));
  const visible = (el) => { const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8 && r.top >= 0 && r.bottom <= innerHeight; };
  const dispatch = (el) => { const r = el.getBoundingClientRect();
    for (const type of ["mouseover", "mousedown", "mouseup", "click"])
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window,
        clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 })); };
  const point = (el) => { if (!el || el.disabled || el.getAttribute("aria-disabled") === "true") return null;
    const r = el.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    if (!hit) return null;
    const ok = hit === el || el.contains(hit) || (hit.closest && hit.closest('[class*="publishBtn"]') === el);
    return ok ? { x, y } : null; };
`;

const OPEN_JS = (title) => `(async () => {
  const TITLE = ${JSON.stringify(title)};
  ${HELPERS}
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // 会话列表是异步渲染的：loadPage 的 waitMs 只保证文档就绪，
  // 列表可能还没出来（真机复现：刚重启壳第一次点开会话就报「找不到」）。
  for (let i = 0; i < 30; i++) {
    if (document.querySelector('[data-e2e="conversation-item"]')) break;
    await sleep(500);
  }
  const find = () => {
    for (const it of document.querySelectorAll('[data-e2e="conversation-item"]')) {
      const t = byClass(it, "conversationConversationItemtitle")[0];
      if (t && (t.innerText || "").trim() === TITLE) return it;
    }
    return null;
  };
  let item = find();
  if (!item) {
    let box = null;
    for (const el of document.querySelectorAll('[class*="conversationConversationList"], [class*="onversation"]')) {
      if (el.scrollHeight > el.clientHeight + 40) { box = el; break; }
    }
    for (let i = 0; i < 10 && !item; i++) { if (box) box.scrollTop += 420; await sleep(500); item = find(); }
  }
  if (!item) {
    const titles = [...document.querySelectorAll('[data-e2e="conversation-item"]')].map((it) => {
      const t = byClass(it, "conversationConversationItemtitle")[0];
      return t ? (t.innerText || "").trim() : "";
    }).filter(Boolean).slice(0, 8);
    return { found: false, reason: "会话列表里没有找到该会话", count: titles.length, titles };
  }
  item.scrollIntoView({ block: "center" });
  await sleep(400);
  dispatch(item);
  await sleep(2600);
  const titles = [...document.querySelectorAll('[class*="RightPanelHeadertitle"]')]
    .map((e) => (e.innerText || "").trim()).filter(Boolean);
  const head = titles[0] || "";
  const opened = head === TITLE || head.indexOf(TITLE) === 0;
  // 群聊一律拒绝：群名带成员数「(305)」，或页面出现群 tab / 群公告卡片
  const isGroup = !!document.querySelector(
      '[class*="componentsRightPanelgroupTabs"], [class*="groupTabPane"], [class*="MessageItemGroupNotice"]')
    || /\\(\\d+\\)\\s*$/.test(head);
  return { found: true, opened, isGroup, headTitle: head };
})()`;

const COMPOSER_JS = `(() => {
  ${HELPERS}
  const editors = [...document.querySelectorAll('.editor-kit-container[contenteditable="true"], [contenteditable="true"][role="textbox"]')]
    .filter(visible);
  if (editors.length !== 1) return { error: "editor_not_unique", count: editors.length };
  const editor = editors[0];
  // 发送键不在编辑器的就近容器里：编辑器自身的 class 就是 messageEditorinputArea，
  // closest('[class*="messageEditor"]') 会命中它自己，往里找永远是空的（真机踩过）。
  // 所以往上退 4 层再找，最后再退回全页找唯一可见的 publishBtn。
  let scope = editor;
  for (let i = 0; i < 4 && scope.parentElement; i++) scope = scope.parentElement;
  let submit = [...scope.querySelectorAll('[class*="publishBtn"], [class*="PublishBtn"]')]
    .filter(visible)[0] || null;
  if (!submit) {
    const all = [...document.querySelectorAll('[class*="publishBtn"], [class*="PublishBtn"]')]
      .filter(visible);
    if (all.length === 1) submit = all[0];
  }
  const draft = (editor.innerText || "").replace(/\\u200b/g, "").trim();
  // 富文本编辑器的 activeElement 常常是内部节点（span/div），不一定是容器本身，
  // 所以「焦点在编辑器里」按包含关系判，而不是严格相等。
  const focused = document.activeElement === editor
    || (document.activeElement && editor.contains(document.activeElement));
  return { draft, editor: point(editor), submit: submit ? point(submit) : null,
    focused: !!focused };
})()`;

const LAST_MINE_JS = `(() => {
  // 极简版：只找气泡正文节点，不依赖任何列表容器（发送后容器会重排）。
  // 页面是「新在上」，第一个有正文的节点就是最新一条。
  const nodes = Array.from(document.querySelectorAll('[class*="MessageItemTextbubbleTextContent"]'));
  if (!nodes.length) return null;
  const node = nodes[0];
  const box = node.closest('[class*="messageMessageBox"]') || node.parentElement;
  return {
    text: (node.innerText || "").replace(/\u200b/g, "").trim(),
    fromMe: !!(box && box.querySelector('[class*="isFromMe"]')),
    boxes: document.querySelectorAll('[class*="messageMessageBox"]').length,
  };
})()`;

async function send(command, io) {
  const skipped = (code, extra) => Object.assign({ ok: false, status: "skipped", code }, extra || {});
  const title = String(command.commentAuthor || "").trim();
  const text = typeof command.text === "string" ? command.text : "";
  if (command.type !== ACTION) return skipped("unexpected_action");
  if (!title || !text || text !== text.trim() || text.length < 2 || text.length > 500) {
    return skipped("invalid_conversation_or_text");
  }
  const scope = readScope(command);
  if (!scope) {
    trace("scope_failed", command.commentAuthor, command.scopeId, command.type);
    return skipped("scope_not_verified");
  }
  trace("start", title, "len", text.length);
  if (io.stopped()) return skipped("stopped");
  let submitted = false;
  try {
    const open = await io.evaluate(OPEN_JS(title), 45000);
    trace("open_result", JSON.stringify(open));
    if (!open || !open.found) return skipped("conversation_not_found", open ? { titles: open.titles } : {});
    if (open.isGroup) return skipped("group_chat_skipped", { headTitle: open.headTitle });
    trace("opened", JSON.stringify(open));
    if (!open.opened) return skipped("conversation_not_opened", { headTitle: open.headTitle });
    let box = await io.evaluate(COMPOSER_JS, 20000);
    if (!box || box.error) return skipped("editor_not_unique", { count: box && box.count });
    if (!box.editor || !box.submit) return skipped("composer_not_usable");
    // 用户自己打了字就不动：覆盖草稿等于替用户说话
    if (box.draft) return skipped("existing_draft_preserved");
    await io.evaluate(DM_HOOK_JS, 8000).catch(() => null);
    if (io.stopped()) return skipped("stopped");
    if (io.stopped()) return skipped("stopped");
    trace("composer", JSON.stringify(box));
    await io.click(box.editor.x, box.editor.y);
    await io.sleep(250);
    await io.type(text);
    await io.sleep(350);
    box = await io.evaluate(COMPOSER_JS, 20000);
    if (!box || box.error || box.draft !== text || !box.focused || !box.submit) {
      return skipped("draft_not_confirmed", { draft: box && box.draft });
    }
    if (io.stopped()) return skipped("stopped");
    trace("draft_ok", "click submit");
    if (!claimAttempt(scope, command)) return skipped("attempt_consumed");
    const seq = Number(await io.evaluate("window.__leadDmSeq || 0", 8000).catch(() => 0)) || 0;
    submitted = true;
    await io.click(box.submit.x, box.submit.y);
    // 两个证据任一成立才算发出：
    //   ① 网络回执：200 + status_code 0 + server_message_id 是 6 位以上数字 + 正文一致；
    //   ② 页面可见：会话里确实多了一条「我发的、正文一致」的消息。
    // 只认页面可见会漏判（列表虚拟化 / 刚发完还没重排，真机踩过：消息已经发出去了却报未确认）。
    const receiptsJs = "(window.__leadDmReceipts||[]).filter(function(e){return e.seq > " + seq + "})";
    // 正文比较一律去掉所有空白：富文本编辑器会把一段话拆成多个段落，
    // innerText 里带换行/全角空格，直接 === 比会永远不相等（真机踩过：
    // 消息明明已经在会话里，却报「未确认」）。
    const norm = (value) => String(value == null ? "" : value).replace(/\s+/g, "");
    for (let i = 0; i < 24; i++) {
      await io.sleep(500);
      if (io.stopped()) break;
      const receipts = await io.evaluate(receiptsJs, 12000).catch(() => []);
      const hit = (Array.isArray(receipts) ? receipts : []).find((e) => e
        && norm(e.text) === norm(text) && e.http === 200 && e.code === 0
        && /^[0-9]{6,}$/.test(String(e.messageId || "")));
      const last = await io.evaluate(LAST_MINE_JS, 12000).catch((e) => {
        trace("eval_err", String((e && e.message) || e).slice(0, 200));
        return null;
      });
      if (i % 3 === 0) trace("poll", i, JSON.stringify(last), "receipts", (receipts || []).length);
      const visible = !!(last && last.fromMe && norm(last.text) === norm(text));
      if (hit || visible) {
        return { ok: true, status: "sent_ok",
          code: hit ? "dm_receipt_verified" : "message_visible_in_conversation",
          title, length: text.length };
      }
    }
    const lastSeen = await io.evaluate(LAST_MINE_JS, 12000).catch(() => null);
    trace("uncertain", JSON.stringify(lastSeen));
    return { ok: false, status: "uncertain", code: "message_not_visible_no_retry", title,
      seen: lastSeen ? { fromMe: !!lastSeen.fromMe, text: String(lastSeen.text || "").slice(0, 40) } : null };
  } catch (error) {
    return {
      ok: false,
      status: submitted ? "uncertain" : "skipped",
      code: submitted ? "send_outcome_unknown" : "conversation_page_not_verified",
      error: String((error && error.message) || error),
    };
  }
}

module.exports = { send, readScope, claimAttempt, ACTION, FIELDS, OPEN_JS, COMPOSER_JS, LAST_MINE_JS };
