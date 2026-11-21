// 直播间写动作的作用域校验：必须只认「监控任务签出来的、限时限次的」命令。
const test = require("node:test");
const assert = require("node:assert");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const { verifyScope, liveRoomId, isLiveAction } = require("../live-room");
const { signatureFields } = require("../leadgen-extensions");

const FIELDS = ["type", "accountId", "leadgenTaskId", "videoUrl", "commentId", "text", "scopeId", "deadlineAt"];

function makeScope(dir, overrides = {}) {
  const scope = {
    mode: "monitor",
    scopeId: "abc123",
    secret: "s".repeat(64),
    accountId: "acc-1",
    leadgenTaskId: "task-1",
    nickname: "AI学长小马",
    expiresAt: Math.floor(Date.now() / 1000) + 3600,
    actions: { "like-live": 5, "follow-live": 1, "lottery-live": 1, "follow-user": 1, "send-dm": 1 },
    videoUrls: ["*"],
    ...overrides,
  };
  const file = path.join(dir, `task-1-${Math.random().toString(36).slice(2, 8)}.json`);
  fs.writeFileSync(file, JSON.stringify(scope));
  return { scope, file };
}

function sign(scope, cmd) {
  const fields = signatureFields(cmd, FIELDS);
  return crypto.createHmac("sha256", scope.secret)
    .update(JSON.stringify(fields.map((k) => String(cmd[k] || "")))).digest("hex");
}

function command(scope, file, overrides = {}) {
  const cmd = {
    type: "like-live",
    accountId: scope.accountId,
    leadgenTaskId: scope.leadgenTaskId,
    videoUrl: "https://live.douyin.com/584947519700",
    commentId: "",
    text: "5",
    scopeId: scope.scopeId,
    deadlineAt: Date.now() + 60000,
    scopeFile: file,
    ...overrides,
  };
  cmd.acceptanceSignature = sign(scope, cmd);
  return cmd;
}

test("直播房间号只认 live.douyin.com/<数字>", () => {
  assert.equal(liveRoomId("https://live.douyin.com/584947519700"), "584947519700");
  assert.equal(liveRoomId("https://live.douyin.com/584947519700/"), "584947519700");
  assert.equal(liveRoomId("https://www.douyin.com/video/123456"), "");
  assert.equal(liveRoomId("http://live.douyin.com/123456"), "");
  assert.equal(isLiveAction({ type: "like-live", videoUrl: "https://live.douyin.com/123456" }), true);
  assert.equal(isLiveAction({ type: "like-video", videoUrl: "https://live.douyin.com/123456" }), false);
});

test("签名与作用域都对得上才放行", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "liveroom-"));
  const { scope, file } = makeScope(dir);
  const ok = verifyScope(command(scope, file));
  assert.ok(ok, "合法命令应通过");
  assert.equal(ok.file, file);

  const tampered = command(scope, file);
  tampered.text = "8";                                 // 改次数但不重新签名
  assert.equal(verifyScope(tampered), null);

  const other = command(scope, file, { accountId: "acc-2" });
  other.acceptanceSignature = sign(scope, other);
  assert.equal(verifyScope(other), null, "换账号必须拒绝");
});

test("过期、未开启的动作、非监控模式一律拒绝", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "liveroom-"));

  const expired = makeScope(dir, { expiresAt: Math.floor(Date.now() / 1000) - 10 });
  assert.equal(verifyScope(command(expired.scope, expired.file)), null);

  const disabled = makeScope(dir, { actions: { "follow-live": 1 } });
  assert.equal(verifyScope(command(disabled.scope, disabled.file)), null);

  const preview = makeScope(dir, { mode: "preview" });
  assert.equal(verifyScope(command(preview.scope, preview.file)), null);
});

test("礼物/点赞次数上限与视频列表限制", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "liveroom-"));

  const wildcard = makeScope(dir);
  assert.ok(verifyScope(command(wildcard.scope, wildcard.file)));

  const pinned = makeScope(dir, { videoUrls: ["https://live.douyin.com/1"] });
  assert.equal(verifyScope(command(pinned.scope, pinned.file)), null, "不在名单里的房间不能操作");

  const tooMany = makeScope(dir, { actions: { "like-live": 101 } });
  assert.equal(verifyScope(command(tooMany.scope, tooMany.file, { text: "5" })), null);
});

test("观众级动作必须带昵称", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "liveroom-"));
  const { scope, file } = makeScope(dir);

  const dm = command(scope, file, { type: "send-dm", text: "您好" });
  assert.equal(verifyScope(dm), null, "没有昵称就定位不到人，必须拒绝");

  const withNick = command(scope, file, { type: "send-dm", text: "您好", commentAuthor: "小王" });
  assert.ok(verifyScope(withNick));
});
