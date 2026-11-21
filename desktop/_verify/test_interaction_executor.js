const assert = require("node:assert/strict");
const test = require("node:test");
const { createOfflineActionExecutor, rejectLiveAction } = require("../action-executor");
const { replyProbe, dmProbe } = require("../interaction-dom");
const { fixture } = require("./offline_dom_fixture");
const VIDEO = "https://www.douyin.com/video/7400000000000000001";
const reply = { type: "reply-comment", accountId: "fixture-account", videoUrl: VIDEO,
  commentId: "comment_1", text: "谢谢分享" };
const dm = { type: "send-dm", accountId: "fixture-account", recipientId: "recipient_1", text: "您好，想进一步了解" };

test("reply selects the requested comment and confirms request, response and text", async () => {
  const fake = fixture({ mode: "reply" });
  const executor = createOfflineActionExecutor(fake.io);
  const result = await executor.execute(reply);
  assert.equal(result.status, "simulated_ok");
  assert.equal(result.counts.simulatedReplies, 1);
  assert.equal(result.counts.simulatedComments, 0);
  assert.equal(fake.window.__commentReceipts[0].targetCommentId, "comment_1");
  assert.equal((await executor.execute({ ...reply, text: "不同文案仍不可重复" })).code, "already_done");
});

test("reply preserves drafts and rejects missing, ambiguous or changed targets", async () => {
  for (const options of [{ draft: "原有评论草稿" }, { ambiguousReply: true }, { noReplyEffect: true },
    { replyTargetMismatch: true }, { changeReplyBeforeSubmit: true }, { stopAfterInput: true }]) {
    const fake = fixture({ mode: "reply", ...options });
    const result = await createOfflineActionExecutor(fake.io).execute(reply);
    assert.equal(result.status, "skipped", JSON.stringify(options));
    assert.equal(fake.posted(), false);
    if (options.draft) assert.equal(fake.editor.value, options.draft);
  }
});

test("a reply receipt for another parent, video or text cannot count or retry", async () => {
  for (const options of [{ requestComment: "other" }, { receiptComment: "other" },
    { requestVideo: "other" }, { receiptVideo: "other" }, { receiptText: "different" },
    { emptyReceipt: true }, { throwAfterSubmit: true }, { httpStatus: 503 }]) {
    const fake = fixture({ mode: "reply", ...options });
    const executor = createOfflineActionExecutor(fake.io);
    assert.equal((await executor.execute(reply)).status, "uncertain", JSON.stringify(options));
    assert.equal(executor.counts().simulatedReplies, 0);
    assert.equal((await executor.execute(reply)).code, "uncertain_pending_manual");
  }
});

test("DM confirms a new sent message for the selected recipient and keeps separate counts", async () => {
  const fake = fixture({ mode: "dm" });
  const executor = createOfflineActionExecutor(fake.io);
  const result = await executor.execute(dm);
  assert.equal(result.status, "simulated_ok");
  assert.equal(result.counts.simulatedMessages, 1);
  assert.equal(result.counts.simulatedComments, 0);
  assert.equal(fake.calls[0][1], "https://www.douyin.com/user/recipient_1");
  assert.equal((await executor.execute({ ...dm, text: "重复私信" })).code, "already_done");
});

test("DM rejects wrong or ambiguous recipients, draft overwrite and invalid controls", async () => {
  for (const options of [{ wrongRecipient: true }, { ambiguousConversation: true },
    { draft: "原有私信草稿" }, { ambiguous: true }, { ambiguousSubmit: true }, { disabledSubmit: true },
    { obscured: true }, { badInput: true }, { changeRecipientBeforeSubmit: true },
    { loginWall: true }, { stopAfterInput: true }]) {
    const fake = fixture({ mode: "dm", ...options });
    assert.equal((await createOfflineActionExecutor(fake.io).execute(dm)).status, "skipped", JSON.stringify(options));
    assert.equal(fake.posted(), false);
    if (options.draft) assert.equal(fake.editor.value, options.draft);
  }
});

test("DM pending bubbles, old messages and mismatched recipients are not confirmation", async () => {
  for (const options of [{ emptyReceipt: true }, { messageStatus: "pending" },
    { receiptRecipient: "another-recipient" }, { receiptText: "different" },
    { previousMessage: dm.text }, { messageDirection: "incoming" }, { throwAfterSubmit: true }, { changeConversationAfterSubmit: true },
    { emptyReceipt: true, stopAfterSubmit: true }]) {
    const fake = fixture({ mode: "dm", ...options });
    const executor = createOfflineActionExecutor(fake.io);
    assert.equal((await executor.execute(dm)).status, "uncertain", JSON.stringify(options));
    assert.equal((await executor.execute(dm)).code, "uncertain_pending_manual");
    assert.equal(executor.counts().simulatedMessages, 0);
  }
});

test("explicit DM rejection preserves the draft until it is cleared before retry", async () => {
  const fake = fixture({ mode: "dm", decline: true });
  const executor = createOfflineActionExecutor(fake.io);
  assert.equal((await executor.execute(dm)).status, "simulated_fail");
  assert.equal((await executor.execute(dm)).code, "existing_draft");
  assert.equal(fake.editor.value, dm.text);
  fake.editor.value = "";
  assert.equal((await executor.execute(dm)).status, "simulated_fail");
  assert.equal(executor.counts().simulatedMessages, 0);
});

test("invalid interaction input never navigates and both live commands remain gated", async () => {
  for (const request of [{ ...reply, commentId: "" }, { ...reply, videoUrl: "https://evil.example/" },
    { ...reply, text: "x".repeat(201) }, { ...dm, recipientId: "../another" },
    { ...dm, text: "" }, { ...dm, text: "x".repeat(501) }, { ...dm, accountId: "another" }]) {
    const fake = fixture();
    assert.equal((await createOfflineActionExecutor(fake.io).execute(request)).status, "skipped");
    assert.deepEqual(fake.calls, []);
    assert.equal(rejectLiveAction({ ...request, offline: true, allowWrite: true }).code, "live_writes_disabled");
  }
});

test("interaction scripts remain valid after interpolation of untrusted IDs", () => {
  for (const id of ["comment_1", "x'); throw new Error('bad') //", 'x"\\\n']) {
    for (const source of [replyProbe(id), replyProbe(id, true), dmProbe(id)]) {
      assert.doesNotThrow(() => new Function("return " + source));
    }
  }
});
