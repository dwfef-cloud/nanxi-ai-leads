/**
 * 南溪 AI 获客 — Electron 主进程
 *
 * 功能：
 *  1. 主窗口加载前端 SPA（file://）
 *  2. 内置浏览器标签页（BrowserView）用于登录抖音
 *  3. CDP 客户端控制浏览器页面
 *  4. 自动回复执行器（Mock / CDP 真实实现）
 *  5. 系统托盘 + 应用菜单
 *  6. 与后端 FastAPI (http://127.0.0.1:8000) 通信
 */

const {
  app, BrowserWindow, BrowserView, Tray, Menu, ipcMain,
  session, nativeImage, shell, Notification,
} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { CDPClient } = require('./src/cdp-client.cjs');
const { createExecutor, MockExecutor } = require('./src/executor.cjs');
const { buildMenu } = require('./src/menu.cjs');

// ─────────── 配置 ───────────
const FRONTEND_DIR = process.env.NANXI_FRONTEND_DIR || 'D:\\27、workbuddy获客系统\\frontend';
const FRONTEND_HTML = path.join(FRONTEND_DIR, 'index.html');
const BACKEND_API = process.env.NANXI_BACKEND_API || 'http://127.0.0.1:8000/api';
const ICON_PATH = path.join(__dirname, 'assets', 'icon.png');

const WINDOW_WIDTH = 1280;
const WINDOW_HEIGHT = 800;
const MIN_WIDTH = 1024;
const MIN_HEIGHT = 680;

// 重定向 Chromium 用户数据目录到 D 盘（避免 C 盘空间不足）
const USER_DATA_DIR = process.env.NANXI_USER_DATA_DIR || 'D:\\.nanxi-desktop-data';
if (!fs.existsSync(USER_DATA_DIR)) fs.mkdirSync(USER_DATA_DIR, { recursive: true });
app.setPath('userData', USER_DATA_DIR);

// ─────────── 全局状态 ───────────
let mainWindow = null;
let browserView = null;
let tray = null;
let cdpClient = null;
let executor = null;
let executorType = 'mock'; // 'mock' | 'cdp'
let browserVisible = true;
let browserRatio = 0.42; // 浏览器面板占宽度比例

// ─────────── 工具函数 ───────────
function loadIcon() {
  try {
    if (fs.existsSync(ICON_PATH)) {
      return nativeImage.createFromPath(ICON_PATH);
    }
  } catch (_) { /* ignore */ }
  // fallback: 1x1 透明像素
  return nativeImage.createEmpty();
}

function getFrontendUrl() {
  // 打包后从 resources/frontend 加载，开发时从原目录加载
  const packagedPath = path.join(process.resourcesPath || '', 'frontend', 'index.html');
  if (app.isPackaged && fs.existsSync(packagedPath)) {
    return `file://${packagedPath.replace(/\\/g, '/')}`;
  }
  if (fs.existsSync(FRONTEND_HTML)) {
    return `file://${FRONTEND_HTML.replace(/\\/g, '/')}`;
  }
  // 兜底：加载后端地址
  return 'http://127.0.0.1:8000';
}

// ─────────── 浏览器面板 ───────────
function resizeBrowserView() {
  if (!mainWindow || !browserView) return;
  if (!browserVisible) {
    browserView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
    return;
  }
  const [width, height] = mainWindow.getContentSize();
  const browserWidth = Math.round(width * browserRatio);
  const x = width - browserWidth;
  browserView.setBounds({ x, y: 0, width: browserWidth, height });
}

function toggleBrowser() {
  browserVisible = !browserVisible;
  if (browserVisible) {
    mainWindow.setBrowserView(browserView);
  } else {
    mainWindow.removeBrowserView(browserView);
  }
  resizeBrowserView();
  return browserVisible;
}

function adjustBrowserRatio(delta) {
  browserRatio = Math.max(0.25, Math.min(0.7, browserRatio + delta * 0.05));
  resizeBrowserView();
  return browserRatio;
}

function createBrowserView() {
  browserView = new BrowserView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      partition: 'persist:nanxi-platforms',
      webSecurity: true,
    },
  });

  // User-Agent：模拟正常 Chrome，避免抖音检测
  const ua = browserView.webContents.getUserAgent().replace(/Electron\/[\d.]+ /, '');
  browserView.webContents.setUserAgent(ua);

  // 新窗口请求：在外部浏览器打开
  browserView.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // 导航拦截：记录日志
  browserView.webContents.on('did-navigate', (_e, url) => {
    console.log('[BrowserView] navigated:', url);
  });

  browserView.webContents.loadURL('https://www.douyin.com');
  mainWindow.setBrowserView(browserView);
  resizeBrowserView();

  // 初始化 CDP 客户端
  cdpClient = new CDPClient(browserView.webContents);
  cdpClient.on('attached', () => console.log('[CDP] attached'));
  cdpClient.on('detached', () => console.log('[CDP] detached'));
}

// ─────────── 托盘 ───────────
function createTray() {
  const icon = loadIcon();
  tray = new Tray(icon);
  tray.setToolTip('南溪 AI 获客');

  const contextMenu = Menu.buildFromTemplate([
    { label: '显示主窗口', click: () => { mainWindow?.show(); mainWindow?.focus(); } },
    { label: '隐藏主窗口', click: () => mainWindow?.hide() },
    { type: 'separator' },
    { label: '重新加载前端', click: () => mainWindow?.webContents.reload() },
    { label: '打开后端', click: () => shell.openExternal('http://127.0.0.1:8000') },
    { type: 'separator' },
    { label: '退出', click: () => { app.isQuitting = true; app.quit(); } },
  ]);

  tray.setContextMenu(contextMenu);
  tray.on('click', () => {
    if (mainWindow?.isVisible()) mainWindow.hide();
    else { mainWindow?.show(); mainWindow?.focus(); }
  });
}

// ─────────── 菜单 ───────────
function setupMenu() {
  const actions = {
    reloadBrowser: () => browserView?.webContents.reload(),
    openFrontendDir: () => shell.openPath(FRONTEND_DIR),
    toggleBrowser: () => toggleBrowser(),
    resizeBrowser: (dir) => adjustBrowserRatio(dir === 1 ? 1 : -1),
    cdpAttach: async () => { try { await cdpClient?.attach(); notify('CDP 已附加'); } catch (e) { notify('CDP 附加失败: ' + e.message); } },
    cdpDetach: () => { cdpClient?.detach(); notify('CDP 已分离'); },
    openBrowserDevTools: () => browserView?.webContents.openDevTools({ mode: 'detach' }),
    showExecutorStatus: () => {
      const stats = executor?.getStats?.() || { ready: executor?.ready, type: executorType };
      notify(`执行器状态: ${JSON.stringify(stats)}`);
    },
    toggleExecutor: () => {
      executorType = executorType === 'mock' ? 'cdp' : 'mock';
      initExecutor();
      notify(`已切换到 ${executorType === 'mock' ? 'Mock' : 'CDP'} 执行器`);
    },
  };

  const template = buildMenu(mainWindow, actions);
  // macOS 需要第一个菜单为 app 名
  if (process.platform === 'darwin') {
    template.unshift({ label: app.name, submenu: [{ role: 'about' }, { type: 'separator' }, { role: 'quit' }] });
  }
  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

function notify(title, body) {
  try {
    new Notification({ title, body: body || '', icon: loadIcon() }).show();
  } catch (_) { /* ignore */ }
}

// ─────────── 执行器 ───────────
function initExecutor() {
  try {
    executor = createExecutor(executorType, { cdpClient });
    executor.init?.().catch(e => console.error('[Executor] init error:', e));
    console.log(`[Executor] initialized: ${executorType}`);
  } catch (e) {
    console.error('[Executor] init failed:', e);
    executor = new MockExecutor();
    executor.init().catch(() => {});
  }
}

// ─────────── 后端通信 ───────────
async function postToBackend(endpoint, data) {
  try {
    const res = await fetch(`${BACKEND_API}${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    return await res.json();
  } catch (e) {
    console.error('[Backend] POST error:', endpoint, e.message);
    return { error: e.message };
  }
}

// ─────────── IPC 处理器 ───────────
function setupIpc() {
  // 浏览器控制
  ipcMain.handle('browser:open', (_e, url) => {
    browserView?.webContents.loadURL(url || 'https://www.douyin.com');
    return true;
  });
  ipcMain.handle('browser:back', () => browserView?.webContents.goBack());
  ipcMain.handle('browser:forward', () => browserView?.webContents.goForward());
  ipcMain.handle('browser:reload', () => browserView?.webContents.reload());
  ipcMain.handle('browser:url', () => browserView?.webContents.getURL() || '');
  ipcMain.handle('browser:title', () => browserView?.webContents.getTitle() || '');
  ipcMain.handle('browser:toggle', () => toggleBrowser());
  ipcMain.handle('browser:resize', (_e, dir) => adjustBrowserRatio(dir));
  ipcMain.handle('browser:visible', () => browserVisible);

  // CDP 控制
  ipcMain.handle('cdp:attach', async () => { try { await cdpClient.attach(); return { ok: true }; } catch (e) { return { ok: false, error: e.message }; } });
  ipcMain.handle('cdp:detach', () => { cdpClient?.detach(); return { ok: true }; });
  ipcMain.handle('cdp:navigate', async (_e, url) => { try { await cdpClient.navigate(url); return { ok: true }; } catch (e) { return { ok: false, error: e.message }; } });
  ipcMain.handle('cdp:evaluate', async (_e, expression) => { try { return { ok: true, result: await cdpClient.evaluate(expression) }; } catch (e) { return { ok: false, error: e.message }; } });
  ipcMain.handle('cdp:click', async (_e, selector) => { try { await cdpClient.click(selector); return { ok: true }; } catch (e) { return { ok: false, error: e.message }; } });
  ipcMain.handle('cdp:type', async (_e, { selector, text }) => { try { await cdpClient.type(selector, text); return { ok: true }; } catch (e) { return { ok: false, error: e.message }; } });
  ipcMain.handle('cdp:scrapeComments', async (_e, opts) => { try { return { ok: true, comments: await cdpClient.scrapeComments(opts) }; } catch (e) { return { ok: false, error: e.message }; } });
  ipcMain.handle('cdp:scroll', async () => { try { await cdpClient.scrollToBottom(); return { ok: true }; } catch (e) { return { ok: false, error: e.message }; } });
  ipcMain.handle('cdp:status', () => ({ attached: cdpClient?.isAttached || false, url: browserView?.webContents.getURL() || '' }));

  // 执行器
  ipcMain.handle('executor:sendComment', async (_e, { videoId, text }) => {
    try { return await executor.sendComment(videoId, text); } catch (e) { return { error: e.message }; }
  });
  ipcMain.handle('executor:sendDm', async (_e, { userId, text }) => {
    try { return await executor.sendDm(userId, text); } catch (e) { return { error: e.message }; }
  });
  ipcMain.handle('executor:getNotifications', async () => {
    try { return await executor.getNotifications(); } catch (e) { return { error: e.message }; }
  });
  ipcMain.handle('executor:getCommentList', async (_e, videoId) => {
    try { return await executor.getCommentList(videoId); } catch (e) { return { error: e.message }; }
  });
  ipcMain.handle('executor:replyComment', async (_e, { commentId, text }) => {
    try { return await executor.replyComment(commentId, text); } catch (e) { return { error: e.message }; }
  });
  ipcMain.handle('executor:status', () => ({
    type: executorType,
    ready: executor?.ready || false,
    stats: executor?.getStats?.() || null,
  }));
  ipcMain.handle('executor:switch', (_e, type) => {
    if (['mock', 'cdp'].includes(type)) {
      executorType = type;
      initExecutor();
      return { ok: true, type };
    }
    return { ok: false, error: 'unknown executor type' };
  });

  // 采集数据上报后端
  ipcMain.handle('collector:submitComments', async (_e, { videoId, comments }) => {
    return postToBackend('/comments/collect', { videoId, comments, source: 'desktop-cdp' });
  });

  // 应用信息
  ipcMain.handle('app:info', () => ({
    version: app.getVersion(),
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    platform: process.platform,
    frontendUrl: getFrontendUrl(),
    backendApi: BACKEND_API,
    packaged: app.isPackaged,
  }));

  ipcMain.handle('app:openExternal', (_e, url) => shell.openExternal(url));
  ipcMain.handle('app:minimize', () => mainWindow?.minimize());
  ipcMain.handle('app:maximize', () => { if (mainWindow?.isMaximized()) mainWindow.unmaximize(); else mainWindow?.maximize(); });
  ipcMain.handle('app:close', () => mainWindow?.close());
}

// ─────────── 主窗口 ───────────
function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    title: '南溪 AI 获客 · 桌面工作区',
    backgroundColor: '#f4f7f7',
    icon: loadIcon(),
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // 加载前端
  const frontendUrl = getFrontendUrl();
  console.log('[Main] loading frontend:', frontendUrl);
  mainWindow.loadURL(frontendUrl);

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    createBrowserView();
  });

  mainWindow.on('resize', resizeBrowserView);
  mainWindow.webContents.on('did-finish-load', resizeBrowserView);

  // 关闭到托盘而非退出
  mainWindow.on('close', (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
      notify('南溪 AI 获客', '应用已最小化到托盘');
    }
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

// ─────────── 应用生命周期 ───────────
app.whenReady().then(() => {
  console.log('=== 南溪 AI 获客 桌面端启动 ===');
  console.log('Frontend:', FRONTEND_HTML, fs.existsSync(FRONTEND_HTML) ? '(exists)' : '(NOT FOUND)');
  console.log('Backend API:', BACKEND_API);

  // 安全：禁用媒体请求（避免权限弹窗）
  session.defaultSession.setDisplayMediaRequestHandler((_req, callback) => callback({ video: undefined }));

  // CSP：允许 file:// 加载和后端 API
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    if (details.url.startsWith('file://')) {
      callback({
        responseHeaders: {
          ...details.responseHeaders,
          'Content-Security-Policy': [
            "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; " +
            "connect-src 'self' http://127.0.0.1:8000 http://localhost:8000 ws:; " +
            "img-src 'self' data: blob: http: https:; " +
            "style-src 'self' 'unsafe-inline'; " +
            "script-src 'self' 'unsafe-inline' 'unsafe-eval';",
          ],
        },
      });
    } else {
      callback({});
    }
  });

  setupIpc();
  createMainWindow();
  createTray();
  setupMenu();
  initExecutor();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    // 不退出，保持托盘运行
    // app.quit();
  }
});

app.on('before-quit', () => { app.isQuitting = true; });

// 未捕获异常
process.on('uncaughtException', (err) => {
  console.error('[uncaughtException]', err);
});
process.on('unhandledRejection', (reason) => {
  console.error('[unhandledRejection]', reason);
});
