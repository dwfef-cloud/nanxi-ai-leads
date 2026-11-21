/**
 * 消息聚合 / 创作聚合内嵌视图的多账号分区回归（2026-11-17 真机实锤）：
 * ensureView 以前创建 BrowserView 时不带 partition → 所有账号共用默认 session
 * 的登录态，消息聚合切换账号永远显示默认分区里登录的那个账号的私信。
 * 修复后：chat/creator 视图按 viewKey 里的账号 id 走 resolvePartition，
 * 与采集窗口 / 登录视图同一套分区规则。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const main = fs.readFileSync(path.resolve(__dirname, '../main.js'), 'utf8');

function section(start, end) {
  const at = main.indexOf(start);
  assert.ok(at >= 0, `source section not found: ${start}`);
  const until = main.indexOf(end, at + start.length);
  assert.ok(until > at, `source section end not found: ${end}`);
  return main.slice(at, until);
}

function makeFixture(bindings) {
  const created = [];
  class FakeBrowserView {
    constructor(opts) { created.push(opts); this.webContents = { loadURL: async () => {} }; }
  }
  const views = { chat: new Map(), creator: new Map() };
  const context = vm.createContext({
    views,
    BrowserView: FakeBrowserView,
    protectRemoteView() {},
    KIND_URL: { chat: 'https://www.douyin.com/chat?isPopup=1', creator: 'https://creator.douyin.com/creator-micro/home' },
    resolvePartition: id => Object.prototype.hasOwnProperty.call(bindings, id) ? bindings[id] : null,
    viewKeyAccount: null, // 下方注入真实实现
    attachProfileSync() {}, // creator 视图分支调用，桩掉即可
  });
  vm.runInContext(
    section('const AUTO_VIEW_KEY_RE', 'function resolveAutoView(') +
    section('function ensureView(', '/* ---------------------------------------------------- 创作者中心资料采集'),
    context);
  return { views, created, ensureView: (kind, key) => context.ensureView(kind, key) };
}

test('chat 视图按账号绑定使用独立分区', () => {
  const fx = makeFixture({ '1794886618996': 'persist:douyin-1794886618996' });
  const entry = fx.ensureView('chat', 'douyin_1794886618996');
  assert.equal(entry.accountId, '1794886618996');
  assert.equal(fx.created.length, 1);
  assert.equal(fx.created[0].webPreferences.partition, 'persist:douyin-1794886618996');
});

test('绑定到默认分区的账号（存量登录态）不带 partition', () => {
  const fx = makeFixture({ 'legacy-account': null });
  fx.ensureView('chat', 'douyin_legacy-account');
  assert.equal(fx.created.length, 1);
  assert.ok(!('partition' in fx.created[0].webPreferences));
});

test('同一账号复用视图，不重复创建', () => {
  const fx = makeFixture({ 'acc-1': 'persist:douyin-acc-1' });
  const a = fx.ensureView('chat', 'douyin_acc-1');
  const b = fx.ensureView('chat', 'douyin_acc-1');
  assert.equal(a, b);
  assert.equal(fx.created.length, 1);
});

test('不同账号各自建视图、各用各的分区', () => {
  const fx = makeFixture({ 'acc-1': 'persist:douyin-acc-1', 'acc-2': 'persist:douyin-acc-2' });
  fx.ensureView('chat', 'douyin_acc-1');
  fx.ensureView('chat', 'douyin_acc-2');
  const parts = fx.created.map(o => o.webPreferences.partition).sort();
  assert.deepEqual(parts, ['persist:douyin-acc-1', 'persist:douyin-acc-2']);
});

test('creator 视图同样带账号分区', () => {
  const fx = makeFixture({ 'acc-9': 'persist:douyin-acc-9' });
  fx.ensureView('creator', 'douyin_acc-9');
  assert.equal(fx.created[0].webPreferences.partition, 'persist:douyin-acc-9');
});
