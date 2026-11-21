const assert = require("node:assert/strict");
const test = require("node:test");
const { createOfflineActionExecutor, rejectLiveAction, GATED_ACTIONS,
  PAGE_IDENTITY_JS, FAVORITE_PROBE_JS, EDITOR_TEXT_JS } = require("../action-executor");

const VIDEO = "https://www.douyin.com/video/7400000000000000001";
const VIDEO_ID = "7400000000000000001";

const { fixture } = require("./offline_dom_fixture");

const command = type => ({ type, accountId: "fixture-account", videoUrl: VIDEO,
  text: "这是离线生成的测试评论" });

if (require.main === module) {
test("offline comment executes DOM probes, input, receipt hook and target verification", async () => {
  const fake = fixture();
  const executor = createOfflineActionExecutor(fake.io);
  const result = await executor.execute(command("post-comment"));
  assert.equal(result.status, "simulated_ok");
  assert.equal(fake.posted(), true);
  assert.equal(result.counts.simulatedComments, 1);
  assert.equal((await executor.execute({ ...command("post-comment"), videoUrl: VIDEO + "?from=search" })).code, "already_done");
});

test("existing draft is preserved and never submitted", async () => {
  const fake = fixture({ draft: "用户已有草稿" });
  const result = await createOfflineActionExecutor(fake.io).execute(command("post-comment"));
  assert.equal(result.code, "existing_draft");
  assert.equal(fake.editor.value, "用户已有草稿");
  assert.equal(fake.posted(), false);
  assert.equal(fake.calls.filter(c => c[0] === "insert").length, 0);
});

test("ambiguous or obscured editors cannot receive a comment", async () => {
  for (const options of [{ ambiguous: true }, { obscured: true }, { wrongPage: true }, { loginWall: true }]) {
    const fake = fixture(options);
    assert.equal((await createOfflineActionExecutor(fake.io).execute(command("post-comment"))).status, "skipped");
    assert.equal(fake.posted(), false);
  }
});

test("incorrectly inserted comment is never submitted", async () => {
  const fake = fixture({ badInput: true });
  const result = await createOfflineActionExecutor(fake.io).execute(command("post-comment"));
  assert.equal(result.code, "input_not_verified");
  assert.equal(fake.posted(), false);
});

test("missing confirmation and post-submit exceptions block repeat attempts", async () => {
  for (const options of [{ emptyReceipt: true }, { throwAfterSubmit: true },
    { requestVideo: "other", receiptVideo: "other" }, { receiptVideo: "other" }, { receiptText: "wrong text" },
    { httpStatus: 503 }, { emptyReceipt: true, stopAfterSubmit: true }]) {
    const fake = fixture(options);
    const executor = createOfflineActionExecutor(fake.io);
    assert.equal((await executor.execute(command("post-comment"))).status, "uncertain");
    assert.equal((await executor.execute(command("post-comment"))).code, "uncertain_pending_manual");
    assert.equal(executor.counts().simulatedComments, 0);
  }
});

test("a fixture refusal has no success count", async () => {
  const fake = fixture({ decline: true });
  const executor = createOfflineActionExecutor(fake.io);
  assert.equal((await executor.execute(command("post-comment"))).status, "simulated_fail");
  assert.equal(executor.counts().simulatedComments, 0);
});

test("favorite requires a stable off-to-on state and does not toggle an existing favorite", async () => {
  const fake = fixture();
  const result = await createOfflineActionExecutor(fake.io).execute(command("favorite-video"));
  assert.equal(result.status, "simulated_ok");
  assert.equal(result.counts.simulatedFavorites, 1);
  const existing = fixture({ favorited: true });
  assert.equal((await createOfflineActionExecutor(existing.io).execute(command("favorite-video"))).code, "already_favorited");
  assert.equal(existing.calls.some(c => c[0] === "click"), false);
});

test("favorite with no effect is uncertain and cannot be clicked twice", async () => {
  const fake = fixture({ noFavoriteEffect: true });
  const executor = createOfflineActionExecutor(fake.io);
  assert.equal((await executor.execute(command("favorite-video"))).status, "uncertain");
  assert.equal((await executor.execute(command("favorite-video"))).code, "uncertain_pending_manual");
  assert.equal(fake.calls.filter(c => c[0] === "click").length, 1);
});

test("copy verifies clipboard and only increments copied-link count", async () => {
  const fake = fixture();
  const executor = createOfflineActionExecutor(fake.io);
  const result = await executor.execute(command("copy-video-link"));
  assert.equal(result.status, "simulated_ok");
  assert.deepEqual(result.counts, { simulatedComments: 0, simulatedReplies: 0, simulatedMessages: 0,
    simulatedFavorites: 0, simulatedLinksCopied: 1 });
  assert.deepEqual(fake.calls, [["navigate", VIDEO], ["clipboard", VIDEO]]);
  const bad = fixture({ badClipboard: true });
  assert.equal((await createOfflineActionExecutor(bad.io).execute(command("copy-video-link"))).status, "uncertain");
});

test("wrong account and elapsed deadline have no navigation or submit", async () => {
  for (const overrides of [{ accountId: "another-account" }, { deadlineMs: 500 }]) {
    const fake = fixture();
    assert.equal((await createOfflineActionExecutor(fake.io).execute({ ...command("post-comment"), ...overrides })).status, "skipped");
    assert.deepEqual(fake.calls, []);
  }
});

test("all live actions stay closed regardless of caller-supplied flags", () => {
  for (const type of GATED_ACTIONS) {
    assert.equal(rejectLiveAction({ type, offline: true, allowWrite: true }).code, "live_writes_disabled");
  }
  assert.equal(rejectLiveAction({ type: "read-comments" }), null);
  assert.throws(() => createOfflineActionExecutor({ offline: false }), /Offline fixture/);
});

test("new injected scripts are valid after module evaluation", () => {
  for (const code of [PAGE_IDENTITY_JS, FAVORITE_PROBE_JS, EDITOR_TEXT_JS]) {
    assert.doesNotThrow(() => new Function("return " + code));
  }
});
}


