const vm = require("node:vm");
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            const VIDEO = "https://www.douyin.com/video/7400000000000000001";

// Execute the shipped page scripts against a small DOM fixture. No Electron
// process, system clipboard, platform URL, or logged-in account is accessed.
function fixture(options = {}) {
  const calls = [];
  let clock = 1000;
  let clipboard = "previous clipboard";
  let stopped = false;
  let posted = false;
  let replyTarget = "";
  const mode = options.mode || "comment";
  const recipientId = options.recipientId || "recipient_1";
  const commentId = options.commentId || "comment_1";
  const messages = [];
  const element = (attrs, box, text = "") => ({
    value: "", innerText: text, disabled: false,
    getAttribute(name) { return attrs[name] == null ? null : attrs[name]; },
    getBoundingClientRect() { return { ...box, right: box.left + box.width, bottom: box.top + box.height }; },
    contains(other) { return this === other; },
    matches() { return this === editor; },
  });
  const editor = element({ placeholder: "留下你的评论" }, { left: 100, top: 100, width: 400, height: 40 });
  editor.value = options.draft || "";
  const button = element({}, { left: 520, top: 100, width: 60, height: 40 }, "发送");
  const replyButton = element({}, { left: 100, top: 200, width: 60, height: 40 }, "回复");
  const row = {
    getAttribute(name) { return name === "data-comment-id" ? commentId : replyTarget; },
    querySelectorAll() { return [replyButton]; },
  };
  const panel = {
    getAttribute() { return options.wrongRecipient ? "another-recipient" : recipientId; },
    querySelectorAll(selector) {
      if (selector.startsWith("textarea")) return options.ambiguous ? [editor, editor] : [editor];
      if (selector.startsWith("button")) return options.ambiguousSubmit ? [button, button] : [button];
      if (selector.startsWith("[data-message-id]")) return messages;
      return [];
    },
  };
  const addMessage = (id, status, recipient, text) => messages.push(element({
    "data-message-id": id, "data-send-status": status, "data-recipient-id": recipient,
    "data-direction": options.messageDirection || "outgoing",
  }, {}, text));
  if (options.previousMessage) addMessage("previous-message", "sent", recipientId, options.previousMessage);
  if (options.disabledSubmit) button.disabled = true;
  const attrs = { "aria-label": options.favorited ? "取消收藏" : "收藏" };
  const favorite = element(attrs, { left: 800, top: 300, width: 50, height: 50 });
  const document = {
    activeElement: null,
    querySelector() { return options.loginWall ? {} : null; },
    querySelectorAll(selector) {
      if (selector.startsWith("textarea")) return options.ambiguous ? [editor, editor] : [editor];
      if (selector.startsWith("button")) return [button];
      if (selector.startsWith("[aria-label]")) return [favorite];
      if (selector.startsWith("[data-comment-id]")) return options.ambiguousReply ? [row, row] : [row];
      if (selector.startsWith("[data-conversation-id]")) return options.ambiguousConversation ? [panel, panel] : [panel];
      return [];
    },
    elementFromPoint(x, y) {
      if (options.obscured) return null;
      return [editor, button, favorite, replyButton].find(el => {
        const r = el.getBoundingClientRect();
        return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
      }) || null;
    },
  };
  const location = { href: VIDEO };
  const currentId = () => new URL(location.href).pathname.split("/")[2];
  const window = { fetch: async () => ({
    status: options.httpStatus || 200,
    clone: () => ({ text: async () => options.emptyReceipt ? "{}" : JSON.stringify({
      status_code: options.decline ? 1001 : 0,
      comment: { cid: "fixture-comment", aweme_id: options.receiptVideo || currentId(),
        text: options.receiptText || editor.value,
        reply_id: mode === "reply" ? options.receiptComment || replyTarget : "" },
    }) }),
  }) };
  function XHR() {}
  XHR.prototype.open = function() {};
  XHR.prototype.send = function() {};
  const context = vm.createContext({ document, location, window,
    innerWidth: 1000, innerHeight: 800, XMLHttpRequest: XHR, URL, URLSearchParams });
  const io = {
    offline: true, accountId: "fixture-account", now: () => clock,
    shouldStop: () => stopped,
    sleep: async ms => { clock += ms; },
    navigate: async url => { calls.push(["navigate", url]); location.href = options.wrongPage
      ? "https://www.douyin.com/video/7400000000000000002" : url; },
    evaluate: async code => vm.runInContext(code, context),
    click: async (x, y) => {
      calls.push(["click", x, y]);
      const hit = document.elementFromPoint(x, y);
      if (hit === editor) document.activeElement = editor;
      if (hit === favorite && !options.noFavoriteEffect) attrs["aria-label"] = "取消收藏";
      if (hit === replyButton && !options.noReplyEffect) replyTarget = options.replyTargetMismatch ? "wrong-comment" : commentId;
      if (hit === button) {
        posted = true;
        if (options.throwAfterSubmit) throw new Error("fixture connection lost");
        if (options.stopAfterSubmit) stopped = true;
        if (mode === "dm") {
          if (options.changeConversationAfterSubmit) options.wrongRecipient = true;
          if (!options.emptyReceipt) addMessage(options.previousMessage ? "previous-message" : "fixture-message-" + messages.length,
            options.messageStatus || (options.decline ? "failed" : "sent"),
            options.receiptRecipient || recipientId, options.receiptText || editor.value);
          return;
        }
        await window.fetch("https://www.douyin.com/aweme/v1/web/comment/publish/", {
          body: new URLSearchParams({ aweme_id: options.requestVideo || currentId(), text: editor.value,
            reply_id: mode === "reply" ? options.requestComment || replyTarget : "" }),
        });
      }
    },
    insertText: async text => {
      calls.push(["insert", text]); editor.value = options.badInput ? "broken input" : text;
      if (options.changeReplyBeforeSubmit) replyTarget = "wrong-comment";
      if (options.changeRecipientBeforeSubmit) options.wrongRecipient = true;
      if (options.stopAfterInput) stopped = true;
    },
    writeClipboard: async text => { calls.push(["clipboard", text]); clipboard = text; },
    readClipboard: async () => options.badClipboard ? "different content" : clipboard,
  };
  return { io, calls, editor, posted: () => posted, window };
}

module.exports = { fixture };

