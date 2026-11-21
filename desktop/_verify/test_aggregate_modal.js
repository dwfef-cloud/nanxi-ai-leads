const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const main = fs.readFileSync(path.resolve(__dirname, '../main.js'), 'utf8');
const bridge = fs.readFileSync(path.resolve(__dirname, '../../ui/live-bridge.js'), 'utf8');
function section(start, end) {
  const at = main.indexOf(start);
  assert.ok(at >= 0, start);
  const until = main.indexOf(end, at + start.length);
  assert.ok(until > at, end);
  return main.slice(at, until);
}
function nativeFixture() {
  const attached = new Set(), created = [], views = {chat: new Map(), creator: new Map()};
  const activeKey = {chat: null, creator: null};
  let rect = async () => ({x: 480, y: 135, width: 900, height: 660});
  const win = {isDestroyed: () => false, webContents: {getZoomFactor: () => 1},
    addBrowserView: view => attached.add(view), removeBrowserView: view => attached.delete(view)};
  const context = vm.createContext({win, views, activeKey, autoViews: new Map(),
    applyViewBounds() {}, cssToViewBounds: value => value, hostRect: kind => rect(kind),
    sendStatus() {}, parseArgs: value => JSON.parse(value)[0] || {},
    ensureView(kind, key) {
      if (!views[kind].has(key)) {
        created.push(key);
        views[kind].set(key, {attached: false, visible: false,
          view: {setBounds() {}, webContents: {reload() {}}}});
      }
      return views[kind].get(key);
    }});
  vm.runInContext(section('function viewKeyOf(', 'function hostRect(') +
    section('async function syncBounds()', 'function ensureView(') +
    section('function handleViewChannel(', 'const sourceWindows ='), context);
  return {views, activeKey, attached, created,
    rect: callback => {rect = callback;},
    invoke: (channel, payload = {}) => context.handleViewChannel(channel, JSON.stringify([payload])),
    sync: () => context.syncBounds()};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('keyless modal hide and restore keep the selected native account', async () => {
  const f = nativeFixture();
  f.invoke('open-chat-view', {accountId: 'account-a'});
  await settle();
  const entry = f.views.chat.get('douyin_account-a');
  assert.equal(f.attached.has(entry.view), true);
  f.invoke('set-chat-view-attached', {attached: false});
  assert.equal(f.attached.size, 0);
  await f.sync();
  assert.equal(f.attached.size, 0, 'bounds polling must not cover the modal again');
  assert.equal(f.activeKey.chat, 'douyin_account-a');
  f.invoke('set-chat-view-attached', {attached: true});
  await settle();
  assert.equal(f.attached.has(entry.view), true);
  assert.deepEqual(f.created, ['douyin_account-a']);
});

test('keyless route cleanup destroys the hidden view without creating a default page', async () => {
  const f = nativeFixture();
  f.invoke('open-chat-view', {accountId: 'account-a'});
  await settle();
  f.invoke('set-chat-view-attached', {attached: false});
  f.invoke('destroy-chat-view', {reason: 'message-center-unmounted'});
  await f.sync();
  f.invoke('set-chat-view-attached', {attached: true});
  await settle();
  assert.equal(f.attached.size, 0);
  assert.equal(f.activeKey.chat, null);
  assert.equal(f.views.chat.get('douyin_account-a').visible, false);
  assert.deepEqual(f.created, ['douyin_account-a']);
  assert.equal(f.invoke('reload-chat-view').code, 'CHAT_VIEW_NOT_OPEN');
});

test('in-flight bounds cannot remount a chat page after the modal hides it', async () => {
  const f = nativeFixture();
  f.invoke('open-chat-view', {accountId: 'account-a'});
  await settle();
  let release;
  f.rect(() => new Promise(resolve => {release = resolve;}));
  const pending = f.sync();
  f.invoke('set-chat-view-attached', {attached: false});
  release({x: 480, y: 135, width: 900, height: 660});
  await pending;
  assert.equal(f.attached.size, 0);
  assert.equal(f.views.chat.get('douyin_account-a').attached, false);
});

test('an old account bounds response cannot cover the newly selected account', async () => {
  const f = nativeFixture();
  f.invoke('open-chat-view', {accountId: 'account-a'});
  await settle();
  let release;
  f.rect(() => new Promise(resolve => {release = resolve;}));
  const pending = f.sync();
  f.rect(async () => ({x: 480, y: 135, width: 900, height: 660}));
  f.invoke('open-chat-view', {accountId: 'account-b'});
  await settle();
  release({x: 480, y: 135, width: 900, height: 660});
  await pending;
  assert.equal(f.attached.has(f.views.chat.get('douyin_account-a').view), false);
  assert.equal(f.attached.has(f.views.chat.get('douyin_account-b').view), true);
});

test('explicit account targeting and creator selection remain independent', async () => {
  const f = nativeFixture();
  f.invoke('open-chat-view', {accountId: 'account-a'});
  f.invoke('open-creator-view', {accountId: 'account-b'});
  await settle();
  f.invoke('set-chat-view-attached', {viewKey: 'douyin_missing', attached: false});
  assert.equal(f.views.chat.get('douyin_account-a').attached, true);
  f.invoke('set-creator-view-attached', {attached: false});
  assert.equal(f.views.creator.get('douyin_account-b').attached, false);
  assert.equal(f.views.chat.get('douyin_account-a').attached, true);
});

function bridgeFixture() {
  const calls = [];
  const window = {ipcRenderer: {invoke() {}}, location: {search: ''},
    localStorage: {getItem: () => null}, __DESKTOP_SHELL__: true,
    __desktopShell: {viewInvoke: async (channel, json) => {
      calls.push({channel, payload: JSON.parse(json)[0]});
      return {success: true};
    }}};
  vm.runInNewContext(bridge, {window, URLSearchParams, console: {info() {}, warn() {}},
    fetch: () => assert.fail('native view commands must not use HTTP'), Map, Set});
  return {calls, ipc: window.ipcRenderer};
}

test('shipped bridge supplies missing view keys for modal send and invoke on existing shells', async () => {
  const f = bridgeFixture();
  await f.ipc.invoke('open-chat-view', {accountId: 'account-a'});
  const payload = {attached: false};
  f.ipc.send('set-chat-view-attached', payload);
  await f.ipc.invoke('set-chat-view-attached', {attached: true});
  f.ipc.send('destroy-chat-view', {reason: 'message-center-unmounted'});
  assert.equal(f.calls[1].payload.viewKey, 'douyin_account-a');
  assert.equal(f.calls[2].payload.viewKey, 'douyin_account-a');
  assert.equal(f.calls[3].payload.viewKey, 'douyin_account-a');
  assert.deepEqual(payload, {attached: false}, 'caller state must remain unchanged');
  f.ipc.send('set-chat-view-attached', {attached: true});
  assert.equal(f.calls[4].payload.viewKey, undefined, 'destroyed selection must not be restored');
});

test('bridge tracks account switches and view kinds without overriding explicit targets', async () => {
  const f = bridgeFixture();
  await f.ipc.invoke('open-chat-view', {accountId: 'account-a'});
  await f.ipc.invoke('open-creator-view', {accountId: 'creator'});
  await f.ipc.invoke('open-chat-view', {accountId: 'account-b'});
  f.ipc.send('set-chat-view-attached', {attached: false});
  f.ipc.send('set-creator-view-attached', {attached: false});
  f.ipc.send('set-chat-view-attached', {viewKey: 'douyin_account-a', attached: false});
  f.ipc.send('set-chat-view-attached', {attached: true});
  assert.deepEqual(f.calls.slice(3).map(call => call.payload.viewKey),
    ['douyin_account-b', 'douyin_creator', 'douyin_account-a', 'douyin_account-b']);
});
