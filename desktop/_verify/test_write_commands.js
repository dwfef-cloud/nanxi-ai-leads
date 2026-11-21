const assert = require("node:assert/strict");
const test = require("node:test");
const writes = require("../write-commands");
const vm = require("node:vm");

test("large platform responses retain confirmation without exposing user profiles", async () => {
  const payload = {status_code: 0, comment: {cid: "c1", aweme_id: "7400000000000000001",
    reply_id: "0", text: "正文一致", user: {nickname: "private", profile: "x".repeat(8000)}}};
  const context = {window: {}, location: {href: "https://www.douyin.com"}, URL, URLSearchParams,
    XMLHttpRequest: function () {}};
  context.window.fetch = async () => ({status: 200, clone: () => ({text: async () => JSON.stringify(payload)})});
  context.XMLHttpRequest.prototype.open = () => {};
  context.XMLHttpRequest.prototype.send = () => {};
  vm.runInNewContext(writes.COMMENT_HOOK_JS, context);
  await context.window.fetch("/aweme/v1/web/comment/publish", {body: "aweme_id=7400000000000000001&reply_id=0"});
  await new Promise(resolve => setImmediate(resolve));
  const receipts = context.window.__commentReceipts;
  assert.equal(writes.classifyCommentReceipts(receipts, {videoId: "7400000000000000001", commentId: "", text: "正文一致"}), "sent_ok");
  assert.equal(JSON.parse(receipts[0].body).comment.user, undefined);
  assert.ok(receipts[0].body.length < 500);
});

test("comment input requires a video URL and bounded text", () => {
  assert.equal(writes.validateComment({videoUrl: "https://evil.example/video/7400000000000000001", text: "valid comment"}).ok, false);
  assert.equal(writes.validateComment({videoUrl: "https://www.douyin.com/video/7400000000000000001", text: "ok"}).ok, false);
  assert.deepEqual(writes.validateComment({videoUrl: "https://www.douyin.com/video/7400000000000000001", text: "valid comment"}),
    {ok: true, videoId: "7400000000000000001", text: "valid comment"});
});

test("injected scripts remain valid after template evaluation", () => {
  for (const code of [writes.COMMENT_PROBE_JS, writes.COMMENT_SUBMIT_JS, writes.COMMENT_HOOK_JS]) {
    assert.doesNotThrow(() => new Function(`return ${code}`));
  }
});

test("comment receipts require platform code and comment id", () => {
  const receipt = (status, body) => [{status, body: JSON.stringify(body)}];
  assert.equal(writes.classifyCommentReceipts(receipt(200, {status_code: 0, comment: {cid: "c1"}})), "sent_ok");
  assert.equal(writes.classifyCommentReceipts(receipt(200, {status_code: 0})), "uncertain");
  assert.equal(writes.classifyCommentReceipts(receipt(503, {status_code: 0, comment: {cid: "c1"}})), "uncertain");
  assert.equal(writes.classifyCommentReceipts(receipt(403, {status_code: 1001})), "sent_fail");
  assert.equal(writes.classifyCommentReceipts([{status: 200, body: "null"}]), "uncertain");
  assert.equal(writes.classifyCommentReceipts([]), "uncertain");
});

test("like receipts require both HTTP and platform confirmation", () => {
  const receipt = (status, body) => [{status, body: JSON.stringify(body)}];
  assert.equal(writes.classifyDiggReceipts(receipt(200, {status_code: 0})).ok, true);
  assert.equal(writes.classifyDiggReceipts(receipt(503, {status_code: 0})).ok, false);
  assert.equal(writes.classifyDiggReceipts(receipt(200, {status_code: 1001})).failed, true);
  assert.equal(writes.classifyDiggReceipts([{status: 200, body: "not json"}]).ok, false);
});
