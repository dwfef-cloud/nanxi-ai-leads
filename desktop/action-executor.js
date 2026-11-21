// This module has no Electron/network imports. The only executable adapter is an
// explicitly injected offline fixture; live command dispatch always stays closed.
const {
  videoIdOf, validateComment, classifyCommentReceipts,
  COMMENT_PROBE_JS, COMMENT_SUBMIT_JS, COMMENT_HOOK_JS,
} = require("./write-commands");
const { targetIdOf, replyProbe, dmProbe } = require("./interaction-dom");

const VIDEO_ACTIONS = new Set(["post-comment", "reply-comment", "favorite-video", "copy-video-link"]);
const GATED_ACTIONS = new Set([...VIDEO_ACTIONS, "send-dm", "like-video", "like-comment", "follow-user", "share-profile", "share-comment"]);

function rejectLiveAction(command) {
  if (!GATED_ACTIONS.has(command && command.type)) return null;
  return { ok: false, code: "live_writes_disabled", status: "blocked",
    error: "当前动作缺少有效的账号、目标或正文确认，请从桌面对应任务入口确认后执行" };
}

const PAGE_IDENTITY_JS = `(() => ({
  url: location.href,
  loginWall: !!document.querySelector('#login-pannel,[data-e2e="login-guide"],.login-mask'),
  verification: !!document.querySelector('#captcha_container,[data-e2e="verify-dialog"]')
}))()`;

const FAVORITE_PROBE_JS = `(() => {
  const labels = ['收藏', '取消收藏', '已收藏'];
  const nodes = Array.from(document.querySelectorAll('[aria-label],[title]')).filter(el => {
    const name = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    if (!labels.includes(name)) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.left < 0 || r.top < 0 ||
        r.right > innerWidth || r.bottom > innerHeight) return false;
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return hit === el || (hit && el.contains(hit));
  });
  if (nodes.length !== 1) return { found: false, reason: 'favorite-control-ambiguous-or-missing' };
  const el = nodes[0], r = el.getBoundingClientRect();
  const name = el.getAttribute('aria-label') || el.getAttribute('title') || '';
  const pressed = el.getAttribute('aria-pressed');
  return { found: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
    already: pressed === 'true' || name === '取消收藏' || name === '已收藏' };
})()`;

const EDITOR_TEXT_JS = `(() => {
  const el = document.activeElement;
  if (!el || !el.matches('textarea,input,[contenteditable="true"]')) return null;
  return String(el.value == null ? el.innerText || '' : el.value).trim();
})()`;

function createOfflineActionExecutor(io) {
  if (!io || io.offline !== true) throw new Error("Offline fixture adapter required");
  const attempts = new Map();
  const busyAccounts = new Set();
  const counts = { simulatedComments: 0, simulatedReplies: 0, simulatedMessages: 0,
    simulatedFavorites: 0, simulatedLinksCopied: 0 };
  const finish = (status, code, extra = {}) => ({
    ok: status === "simulated_ok", simulated: true, status, code,
    counts: { ...counts }, ...extra,
  });

  async function execute(command) {
    if (!command || !GATED_ACTIONS.has(command.type)) return finish("skipped", "unsupported_action");
    if (command.type === "like-comment") return finish("skipped", "use_comment_like_fixture");
    if (["follow-user", "share-profile", "share-comment"].includes(command.type)) return finish("skipped", "use_extension_fixture");
    const { type } = command;
    const isMessage = type === "send-dm", isReply = type === "reply-comment";
    const id = isMessage ? targetIdOf(command.recipientId) : videoIdOf(command.videoUrl);
    const commentId = isReply ? targetIdOf(command.commentId) : "";
    const account = typeof command.accountId === "string" ? command.accountId.trim() : "";
    if (!id || !account || io.accountId !== account) return finish("skipped", "invalid_target_or_account");
    if (isReply && !commentId) return finish("skipped", "invalid_reply_target");
    if (type === "post-comment" && !validateComment(command).ok) return finish("skipped", "invalid_comment");
    if ((isReply || isMessage) && (typeof command.text !== "string" ||
        !command.text.trim() || command.text.trim().length > (isMessage ? 500 : 200))) {
      return finish("skipped", "invalid_interaction_text");
    }
    const canonicalUrl = "https://www.douyin.com/" + (isMessage ? "user/" : "video/") + id;
    const key = JSON.stringify([account, type, isReply ? commentId : id]);
    if (attempts.has(key)) return finish("skipped", attempts.get(key) === "simulated_ok"
      ? "already_done" : "uncertain_pending_manual");
    if (busyAccounts.has(account)) return finish("skipped", "account_busy");
    busyAccounts.add(account);
    let submitted = false;
    const active = () => !(io.shouldStop && io.shouldStop()) &&
      (!command.deadlineMs || io.now() < command.deadlineMs);
    const identityMatches = async () => {
      const page = await io.evaluate(PAGE_IDENTITY_JS);
      return !!page && !page.loginWall && !page.verification &&
        (isMessage ? page.url === canonicalUrl : videoIdOf(page.url) === id);
    };
    const click = async (point) => {
      if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) {
        throw new Error("Invalid click coordinates");
      }
      // The adapter owns CSS-to-DIP conversion and trusted Chromium input.
      await io.click(point.x, point.y);
    };
    try {
      if (!active()) return finish("skipped", "stopped_or_deadline");
      await io.navigate(canonicalUrl);
      if (!active()) return finish("skipped", "stopped_or_deadline");
      if (!await identityMatches()) return finish("skipped", "page_identity_or_login");

      if (isMessage) {
        const probe = dmProbe(id), text = command.text.trim();
        const before = await io.evaluate(probe);
        if (!before || !before.editor || !before.submit) return finish("skipped", "dm_conversation_or_controls_missing");
        if (before.draft) return finish("skipped", "existing_draft");
        const previous = new Set(before.messages.map(message => message.id));
        if (!active()) return finish("skipped", "stopped_or_deadline");
        await click(before.editor);
        if (await io.evaluate(EDITOR_TEXT_JS) !== "") return finish("skipped", "existing_or_unfocused_draft");
        await io.insertText(text);
        if (await io.evaluate(EDITOR_TEXT_JS) !== text) return finish("skipped", "input_not_verified");
        const ready = await io.evaluate(probe);
        if (!ready || !ready.submit || ready.draft !== text ||
            !await identityMatches() || !active()) return finish("skipped", "stopped_or_target_changed");
        attempts.set(key, "uncertain");
        submitted = true;
        await click(ready.submit);
        let confirmed = false;
        for (let n = 0; n < 12; n++) {
          if (!await identityMatches()) break;
          const after = await io.evaluate(probe);
          if (!after) break;
          const fresh = after.messages.filter(message => typeof message.id === "string" &&
            message.id.trim() && !previous.has(message.id) && message.recipientId === id &&
            message.direction === "outgoing" && message.text === text);
          if (fresh.some(message => message.status === "sent")) { confirmed = true; break; }
          if (fresh.some(message => message.status === "failed")) {
            attempts.delete(key);
            return finish("simulated_fail", "message_declined_fixture");
          }
          if (!active()) break;
          await io.sleep(250);
        }
        if (!confirmed) return finish("uncertain", "message_not_confirmed");
        counts.simulatedMessages += 1;
      } else if (type === "copy-video-link") {
        if (!active()) return finish("skipped", "stopped_or_deadline");
        attempts.set(key, "uncertain");
        submitted = true;
        await io.writeClipboard(canonicalUrl);
        if (await io.readClipboard() !== canonicalUrl) return finish("uncertain", "clipboard_not_confirmed");
        counts.simulatedLinksCopied += 1;
      } else if (type === "favorite-video") {
        const before = await io.evaluate(FAVORITE_PROBE_JS);
        if (!before || !before.found) return finish("skipped", "favorite_control_missing");
        if (before.already) return finish("skipped", "already_favorited");
        if (!await identityMatches() || !active()) return finish("skipped", "stopped_or_target_changed");
        const ready = await io.evaluate(FAVORITE_PROBE_JS);
        if (!ready || !ready.found || ready.already || !active()) {
          return finish("skipped", "favorite_state_changed_before_click");
        }
        attempts.set(key, "uncertain");
        submitted = true;
        await click(ready);
        let stable = 0;
        for (let n = 0; n < 8; n++) {
          if (!active() || !await identityMatches()) return finish("uncertain", "stopped_or_target_changed_after_click");
          const after = await io.evaluate(FAVORITE_PROBE_JS);
          stable = after && after.found && after.already ? stable + 1 : 0;
          if (stable === 2) break;
          await io.sleep(250);
        }
        if (stable < 2) return finish("uncertain", "favorite_not_confirmed");
        counts.simulatedFavorites += 1;
      } else {
        const text = command.text.trim();
        if (isReply) {
          // Check the general editor before opening reply mode so a user's
          // existing main-comment draft cannot be overwritten by the switch.
          const draft = await io.evaluate(COMMENT_PROBE_JS);
          if (!draft || !draft.found || draft.existingText) return finish("skipped", "existing_or_missing_comment_editor");
          const replyButton = await io.evaluate(replyProbe(commentId));
          if (!replyButton || !active()) return finish("skipped", "reply_target_missing");
          await click(replyButton);
          if (await io.evaluate(replyProbe(commentId, true)) !== true) return finish("skipped", "reply_target_not_verified");
        }
        const editor = await io.evaluate(COMMENT_PROBE_JS);
        if (!editor || !editor.found) return finish("skipped", "comment_editor_missing");
        if (editor.existingText) return finish("skipped", "existing_draft");
        if (!active()) return finish("skipped", "stopped_or_deadline");
        await click(editor);
        if (await io.evaluate(EDITOR_TEXT_JS) !== "") return finish("skipped", "existing_or_unfocused_draft");
        await io.insertText(text);
        if (await io.evaluate(EDITOR_TEXT_JS) !== text) return finish("skipped", "input_not_verified");
        const button = await io.evaluate(COMMENT_SUBMIT_JS);
        if (!button) return finish("skipped", "comment_submit_missing");
        // A failed receipt hook must prevent submitting an unobservable comment.
        if (await io.evaluate(COMMENT_HOOK_JS) !== true) return finish("skipped", "receipt_hook_unavailable");
        const sequence = await io.evaluate("window.__commentReceiptSeq || 0");
        if (!await identityMatches() || !active()) return finish("skipped", "stopped_or_target_changed");
        if (isReply && await io.evaluate(replyProbe(commentId, true)) !== true) return finish("skipped", "reply_target_changed");
        attempts.set(key, "uncertain");
        submitted = true;
        await click(button);
        let status = "uncertain";
        for (let n = 0; n < 12; n++) {
          const receipts = await io.evaluate("(window.__commentReceipts || []).filter(e => e.seq > " + sequence + ")");
          status = classifyCommentReceipts(receipts, { videoId: id, text, commentId });
          if (status !== "uncertain") break;
          if (!active()) break;
          await io.sleep(250);
        }
        if (status === "sent_fail") {
          attempts.delete(key);
          return finish("simulated_fail", "platform_declined_fixture");
        }
        if (status !== "sent_ok") return finish("uncertain", "comment_not_confirmed");
        counts[isReply ? "simulatedReplies" : "simulatedComments"] += 1;
      }
      attempts.set(key, "simulated_ok");
      return finish("simulated_ok", "fixture_confirmed");
    } catch (_) {
      return finish(submitted ? "uncertain" : "skipped", submitted
        ? "exception_after_submit" : "preparation_failed");
    } finally {
      busyAccounts.delete(account);
    }
  }
  return { execute, counts: () => ({ ...counts }) };
}

module.exports = {
  createOfflineActionExecutor, rejectLiveAction, VIDEO_ACTIONS, GATED_ACTIONS,
  PAGE_IDENTITY_JS, FAVORITE_PROBE_JS, EDITOR_TEXT_JS,
};
