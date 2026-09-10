/**
 * 应用菜单构建
 * 文件 / 编辑 / 视图 / 工具 / 帮助
 */

const { app, shell, BrowserWindow } = require('electron');

function buildMenu(mainWindow, actions = {}) {
  const isMac = process.platform === 'darwin';

  const template = [
    // ── 文件 ──
    {
      label: '文件',
      submenu: [
        {
          label: '重新加载前端',
          accelerator: 'CmdOrCtrl+R',
          click: () => mainWindow?.webContents.reload(),
        },
        {
          label: '重新加载浏览器标签',
          accelerator: 'CmdOrCtrl+Shift+R',
          click: () => actions.reloadBrowser?.(),
        },
        { type: 'separator' },
        {
          label: '打开前端目录',
          click: () => actions.openFrontendDir?.(),
        },
        {
          label: '打开后端地址',
          click: () => shell.openExternal('http://127.0.0.1:8000'),
        },
        { type: 'separator' },
        isMac ? { role: 'close', label: '关闭窗口' } : { role: 'quit', label: '退出' },
      ],
    },
    // ── 编辑 ──
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' },
      ],
    },
    // ── 视图 ──
    {
      label: '视图',
      submenu: [
        {
          label: '切换浏览器面板',
          accelerator: 'CmdOrCtrl+B',
          click: () => actions.toggleBrowser?.(),
        },
        {
          label: '放大浏览器面板',
          accelerator: 'CmdOrCtrl+]',
          click: () => actions.resizeBrowser?.(1),
        },
        {
          label: '缩小浏览器面板',
          accelerator: 'CmdOrCtrl+[',
          click: () => actions.resizeBrowser?.(0),
        },
        { type: 'separator' },
        { role: 'reload', label: '重新加载' },
        { role: 'forceReload', label: '强制重新加载' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '重置缩放' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    // ── 工具 ──
    {
      label: '工具',
      submenu: [
        {
          label: 'CDP 调试器',
          submenu: [
            { label: '附加 CDP', click: () => actions.cdpAttach?.() },
            { label: '分离 CDP', click: () => actions.cdpDetach?.() },
            { label: '打开浏览器 DevTools', click: () => actions.openBrowserDevTools?.() },
          ],
        },
        { type: 'separator' },
        { label: '执行器状态', click: () => actions.showExecutorStatus?.() },
        { label: '切换 Mock/真实执行器', click: () => actions.toggleExecutor?.() },
      ],
    },
    // ── 帮助 ──
    {
      label: '帮助',
      submenu: [
        {
          label: '关于 南溪AI获客',
          click: () => {
            const aboutWin = new BrowserWindow({
              width: 420, height: 320, title: '关于',
              resizable: false, minimizable: false, maximizable: false,
              parent: mainWindow, modal: true,
              webPreferences: { contextIsolation: true, nodeIntegration: false },
            });
            aboutWin.setMenuBarVisibility(false);
            aboutWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`
              <html><body style="font-family:system-ui;margin:0;padding:32px;background:#f4f7f7;text-align:center">
                <div style="font-size:28px;font-weight:700;color:#0f3d3e;margin-bottom:4px">南溪 AI 获客</div>
                <div style="color:#666;margin-bottom:24px">桌面工作区 v${app.getVersion()}</div>
                <div style="font-size:13px;color:#888;line-height:1.8">
                  抖音获客 → 私信 → 企微 → 成交<br>
                  内置浏览器 · CDP 控制 · 自动回复<br><br>
                  Electron ${process.versions.electron} · Chrome ${process.versions.chrome}
                </div>
              </body></html>
            `)}`);
          },
        },
        { label: '后端 API 文档', click: () => shell.openExternal('http://127.0.0.1:8000/docs') },
        { label: '报告问题', click: () => shell.openExternal('https://github.com/nanxi-ai/lead-desktop/issues') },
        { type: 'separator' },
        { label: '开发者工具（前端）', accelerator: 'F12', click: () => mainWindow?.webContents.toggleDevTools() },
      ],
    },
  ];

  return template;
}

module.exports = { buildMenu };
