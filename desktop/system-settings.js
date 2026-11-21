/**
 * 系统设置（壳侧执行层）
 * ======================
 * 后端把用户选择写进 <DATA_DIR>/app_settings.json（app/api/app_settings.py），
 * 这里读同一份文件并用 Electron 真正落地：
 *
 *   - hardwareAcceleration ⚠️ 必须在 app ready 之前调用 disableHardwareAcceleration，
 *     所以由 main.js 在模块加载时调 applyBoot(app)，改完提示「重启生效」。
 *   - uiZoomFactor / allViewsMuted / useSystemProxy  → 运行期生效（按 mtime 轮询）
 *   - backgroundPreviewAutoClose / manualLinkOpenMode → 由 main.js 在使用点读取
 *   - clear-cache / cleanup-idle-accounts            → 通过 /desktop/command 下发
 *     （douyin_agent 的 shell-task 指令，执行完回执给后端，后端等结果）
 *   - read-clipboard-image                           → 同上通道；剪贴板只在壳里，
 *     常见问题页「粘贴截图」需要它（返回 data:image/png;base64,...）
 *
 * 只读消费、失败不抛：任何一项应用失败都不该让主界面起不来。
 */
const fs = require("fs");
const path = require("path");

const DEFAULTS = {
  uiZoomFactor: 100,
  useSystemProxy: false,
  backgroundPreviewAutoClose: true,
  manualLinkOpenMode: "browser",
  hardwareAcceleration: true,
  autoCacheCleanup: true,
  allViewsMuted: false,
};

const SHELL_CACHE_DIRS = [
  "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache",
  "DawnWebGPUCache", "ShaderCache", "GrShaderCache", "VideoDecodeStats",
];

function dataDir() {
  const backend = process.env.BRIDGE_BACKEND_DIR || path.resolve(__dirname, "..", "backend");
  return process.env.DATA_DIR || path.resolve(backend, "..", "data");
}

function settingsPath() {
  return process.env.APP_SETTINGS_PATH || path.join(dataDir(), "app_settings.json");
}

let cached = Object.assign({}, DEFAULTS);
let cachedStamp = "";

function normalize(raw) {
  const out = Object.assign({}, DEFAULTS);
  if (!raw || typeof raw !== "object") return out;
  const num = Number(raw.uiZoomFactor);
  out.uiZoomFactor = Number.isFinite(num) ? Math.min(200, Math.max(50, Math.round(num))) : 100;
  for (const key of ["useSystemProxy", "backgroundPreviewAutoClose", "hardwareAcceleration",
    "autoCacheCleanup", "allViewsMuted"]) {
    if (typeof raw[key] === "boolean") out[key] = raw[key];
    else if (raw[key] === 0 || raw[key] === 1) out[key] = !!raw[key];
  }
  out.manualLinkOpenMode = String(raw.manualLinkOpenMode || "browser").toLowerCase() === "account"
    ? "account" : "browser";
  return out;
}

function readSettings() {
  try {
    const p = settingsPath();
    const stat = fs.statSync(p);
    const stamp = `${stat.mtimeMs}:${stat.size}`;
    if (stamp !== cachedStamp) {
      cached = normalize(JSON.parse(fs.readFileSync(p, "utf8")));
      cachedStamp = stamp;
    }
  } catch (e) {
    /* 文件不存在 / 正在被替换：用上一次的值，不影响界面 */
  }
  return cached;
}

/** 当前设置（同步、带缓存）。main.js 的使用点直接调它。 */
function get() {
  return readSettings();
}

/** 启动期：硬件加速必须在 ready 之前决定，改完需要重启才生效。 */
function applyBoot(app) {
  try {
    const s = readSettings();
    if (s.hardwareAcceleration === false && typeof app.disableHardwareAcceleration === "function") {
      app.disableHardwareAcceleration();
      console.info("[system-settings] 已按设置关闭硬件加速（软件渲染）");
    }
  } catch (e) {
    console.warn("[system-settings] 启动设置应用失败:", (e && e.message) || e);
  }
}

function safeZoom(value) {
  const z = Number(value) / 100;
  return Number.isFinite(z) && z >= 0.5 && z <= 2 ? z : 1;
}

/** 把设置应用到当前窗口与视图（缩放 / 静音 / 默认会话代理）。 */
async function applyLive(ctx = {}) {
  const s = readSettings();
  const zoom = safeZoom(s.uiZoomFactor);
  const muted = !!s.allViewsMuted;
  const problems = [];

  const each = (list) => {
    let touched = 0;
    for (const item of list || []) {
      try {
        const wc = item && item.webContents ? item.webContents : item;
        if (!wc || wc.isDestroyed()) continue;
        if (Math.abs(wc.getZoomFactor() - zoom) > 1e-6) { wc.setZoomFactor(zoom); touched += 1; }
        if (wc.isAudioMuted() !== muted) { wc.setAudioMuted(muted); touched += 1; }
      } catch (e) { problems.push(String((e && e.message) || e)); }
    }
    return touched;
  };

  let touched = 0;
  try {
    touched += each(typeof ctx.windows === "function" ? ctx.windows() : []);
    touched += each(typeof ctx.views === "function" ? ctx.views() : []);
  } catch (e) { problems.push(String((e && e.message) || e)); }
  if (touched) {
    console.info(`[system-settings] 已应用缩放 ${s.uiZoomFactor}% / 静音=${muted}（${touched} 个页面）`);
  }

  // 默认会话（授权 / AI / 更新请求）。账号分区有自己的 setProxy，不受影响。
  try {
    const { session } = require("electron");
    const mode = s.useSystemProxy ? "system" : "direct";
    if (defaultProxyMode !== mode) {
      await session.defaultSession.setProxy({ mode });
      defaultProxyMode = mode;
      console.info("[system-settings] 默认会话代理模式:", mode);
    }
  } catch (e) { problems.push(String((e && e.message) || e)); }

  return { settings: s, problems };
}

let defaultProxyMode = "";
let watcher = null;

/**
 * 运行期轮询：设置页保存后立即生效（前端提示「立即生效 / 修改立即生效」）。
 * 300ms 内连续保存多次也只应用最后一份。
 */
function startWatcher(ctx = {}) {
  if (watcher) return watcher;
  let applying = false;
  let pending = false;
  const tick = async () => {
    if (applying) { pending = true; return; }
    const before = cachedStamp;
    readSettings();
    if (cachedStamp === before) return;
    applying = true;
    try {
      const result = await applyLive(ctx);
      if (typeof ctx.onApplied === "function") ctx.onApplied(result);
    } finally {
      applying = false;
      if (pending) { pending = false; setImmediate(tick); }
    }
  };
  watcher = setInterval(tick, 1200);
  // 首次进入也应用一遍：上次会话残留的缩放/静音状态需要立刻对齐
  setTimeout(tick, 800);
  return watcher;
}

function stopWatcher() {
  if (watcher) clearInterval(watcher);
  watcher = null;
}

/* ------------------------------------------------------------------ 缓存 */

function dirSize(root) {
  let total = 0;
  const walk = (dir, depth) => {
    if (depth > 6) return;
    let list = [];
    try { list = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return; }
    for (const item of list) {
      const full = path.join(dir, item.name);
      try {
        if (item.isDirectory()) walk(full, depth + 1);
        else if (item.isFile()) total += fs.statSync(full).size;
      } catch (e) { /* 被占用 / 已删除：跳过 */ }
    }
  };
  if (fs.existsSync(root)) walk(root, 0);
  return total;
}

function partitionNames(userData) {
  const root = path.join(userData, "Partitions");
  try {
    return fs.readdirSync(root, { withFileTypes: true })
      .filter((item) => item.isDirectory()).map((item) => item.name);
  } catch (e) {
    return [];
  }
}

/**
 * 清理缓存：先让 Chromium 释放自己的网络缓存（clearCache），再删图形缓存目录。
 * 不碰账号池 / 配置 / 任务 / 历史，也不删分区里的登录存储（Local Storage / Cookies）。
 */
async function clearCaches() {
  const { app, session } = require("electron");
  const userData = app.getPath("userData");
  let cleared = 0;
  let failed = 0;
  let freed = 0;
  const busy = [];

  const targets = SHELL_CACHE_DIRS.map((name) => path.join(userData, name));
  for (const name of SHELL_CACHE_DIRS) freed += dirSize(path.join(userData, name));

  // 1) 网络缓存：default 会话 + 每个账号分区（并行，串行会拖到十几秒）
  const sessions = [];
  try { sessions.push(session.defaultSession); } catch (e) { failed += 1; }
  for (const name of partitionNames(userData)) {
    try { sessions.push(session.fromPartition(`persist:${name}`)); } catch (e) { busy.push(name); }
  }
  const settled = await Promise.allSettled(sessions.map((s) => s.clearCache()));
  settled.forEach((item, index) => {
    if (item.status === "fulfilled") cleared += 1;
    else if (index === 0) failed += 1;
  });

  // 2) 图形 / 代码缓存目录：运行中可能被 Chromium 占用，失败即记为 skipped
  let skipped = 0;
  for (const dir of targets) {
    if (!fs.existsSync(dir)) continue;
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch (e) {
      skipped += 1;
    }
  }
  if (busy.length) skipped += busy.length;

  return { ok: true, cleared, failed, skipped, freed };
}

/**
 * 清理闲置账号数据：删除超过 N 天没动过的 Session 分区。
 * 只删「分区目录整目录 mtime 超过 N 天」的——正在用的账号随时在写 cookie/缓存，
 * mtime 必然是新的，所以不需要额外的白名单也删不到在用账号。
 */
function cleanupIdlePartitions(days = 14) {
  const { app } = require("electron");
  const userData = app.getPath("userData");
  const root = path.join(userData, "Partitions");
  const cutoff = Date.now() - Math.max(1, Number(days) || 14) * 86400e3;
  let scanned = 0;
  let deleted = 0;
  const kept = [];
  let list;
  try { list = fs.readdirSync(root, { withFileTypes: true }); } catch (e) { list = []; }
  for (const item of list) {
    if (!item.isDirectory()) continue;
    scanned += 1;
    const dir = path.join(root, item.name);
    let newest = 0;
    try {
      const stack = [dir];
      while (stack.length) {
        const cur = stack.pop();
        const stat = fs.statSync(cur);
        newest = Math.max(newest, stat.mtimeMs);
        let kids = [];
        try { kids = fs.readdirSync(cur, { withFileTypes: true }); } catch (e) { kids = []; }
        for (const kid of kids) {
          const full = path.join(cur, kid.name);
          if (kid.isDirectory()) stack.push(full);
          else {
            try { newest = Math.max(newest, fs.statSync(full).mtimeMs); } catch (e) { /* ignore */ }
          }
        }
      }
    } catch (e) { newest = Date.now(); }  // 读不了就当在用，绝不误删
    if (newest >= cutoff) { kept.push(item.name); continue; }
    try {
      fs.rmSync(dir, { recursive: true, force: true });
      deleted += 1;
    } catch (e) { kept.push(item.name); }
  }
  return { ok: true, partitions: { scanned, deleted }, storePrune: { changed: false }, kept: kept.length };
}

/** 读剪贴板里的截图，返回 PNG data URL（常见问题页「粘贴截图」用）。 */
function readClipboardImage() {
  const { clipboard } = require("electron");
  const image = clipboard.readImage();
  if (!image || image.isEmpty()) return { ok: false, error: "剪贴板里没有图片" };
  const png = image.toPNG();
  if (!png || !png.length) return { ok: false, error: "剪贴板图片读取失败" };
  if (png.length > 5 * 1024 * 1024) return { ok: false, error: "剪贴板图片超过 5MB，请压缩后重试" };
  return { ok: true, base64: `data:image/png;base64,${png.toString("base64")}`, bytes: png.length };
}

/** 壳侧任务入口（douyin_agent 收到 type=shell-task 时调用）。 */
async function handleShellCommand(cmd) {
  try {
    if (cmd.action === "clear-cache") return await clearCaches();
    if (cmd.action === "cleanup-idle-accounts") return cleanupIdlePartitions(cmd.days);
    if (cmd.action === "read-clipboard-image") return readClipboardImage();
    // 账号池 CK / 作品发布素材：登录态与文件系统都只在壳里，后端只能下发任务
    const cookie = require("./cookie-session");
    if (cmd.action === "apply-cookies") {
      return await cookie.applyCookies(cmd.accountId, cmd.cookies);
    }
    if (cmd.action === "read-cookies") return await cookie.readCookies(cmd.accountId);
    if (cmd.action === "pick-cookie-files") return await cookie.pickCookieFiles();
    if (cmd.action === "copy-text") return cookie.copyText(cmd.text);
    if (cmd.action === "pick-work-publish-media") return await cookie.pickPublishMedia(cmd.mediaType || cmd.type);
    if (cmd.action === "pick-work-publish-cover") return await cookie.pickPublishCover();
    if (cmd.action === "reveal-path") return cookie.revealPaths(cmd.paths || cmd.path);
    return { ok: false, error: `未知的壳侧设置任务：${cmd.action}` };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

module.exports = {
  DEFAULTS, get, readSettings, applyBoot, applyLive, startWatcher, stopWatcher,
  clearCaches, cleanupIdlePartitions, readClipboardImage, handleShellCommand, settingsPath,
};
