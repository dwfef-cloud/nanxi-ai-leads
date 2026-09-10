/**
 * 预加载脚本
 * 通过 contextBridge 向渲染进程暴露安全的 API
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('nanxiDesktop', {
  // ── 应用信息 ──
  getAppInfo: () => ipcRenderer.invoke('app:info'),
  openExternal: (url) => ipcRenderer.invoke('app:openExternal', url),
  minimize: () => ipcRenderer.invoke('app:minimize'),
  maximize: () => ipcRenderer.invoke('app:maximize'),
  close: () => ipcRenderer.invoke('app:close'),

  // ── 浏览器面板控制 ──
  browser: {
    open: (url) => ipcRenderer.invoke('browser:open', url),
    back: () => ipcRenderer.invoke('browser:back'),
    forward: () => ipcRenderer.invoke('browser:forward'),
    reload: () => ipcRenderer.invoke('browser:reload'),
    getUrl: () => ipcRenderer.invoke('browser:url'),
    getTitle: () => ipcRenderer.invoke('browser:title'),
    toggle: () => ipcRenderer.invoke('browser:toggle'),
    resize: (dir) => ipcRenderer.invoke('browser:resize', dir),
    isVisible: () => ipcRenderer.invoke('browser:visible'),
  },

  // ── CDP 浏览器控制 ──
  cdp: {
    attach: () => ipcRenderer.invoke('cdp:attach'),
    detach: () => ipcRenderer.invoke('cdp:detach'),
    navigate: (url) => ipcRenderer.invoke('cdp:navigate', url),
    evaluate: (expression) => ipcRenderer.invoke('cdp:evaluate', expression),
    click: (selector) => ipcRenderer.invoke('cdp:click', selector),
    type: (selector, text) => ipcRenderer.invoke('cdp:type', { selector, text }),
    scrapeComments: (opts) => ipcRenderer.invoke('cdp:scrapeComments', opts),
    scroll: () => ipcRenderer.invoke('cdp:scroll'),
    status: () => ipcRenderer.invoke('cdp:status'),
  },

  // ── 自动回复执行器 ──
  executor: {
    sendComment: (videoId, text) => ipcRenderer.invoke('executor:sendComment', { videoId, text }),
    sendDm: (userId, text) => ipcRenderer.invoke('executor:sendDm', { userId, text }),
    getNotifications: () => ipcRenderer.invoke('executor:getNotifications'),
    getCommentList: (videoId) => ipcRenderer.invoke('executor:getCommentList', videoId),
    replyComment: (commentId, text) => ipcRenderer.invoke('executor:replyComment', { commentId, text }),
    status: () => ipcRenderer.invoke('executor:status'),
    switch: (type) => ipcRenderer.invoke('executor:switch', type),
  },

  // ── 采集数据上报 ──
  collector: {
    submitComments: (videoId, comments) => ipcRenderer.invoke('collector:submitComments', { videoId, comments }),
  },

  // ── 平台浏览器（兼容旧 API） ──
  platformBrowser: {
    open: (url) => ipcRenderer.invoke('browser:open', url),
    back: () => ipcRenderer.invoke('browser:back'),
    forward: () => ipcRenderer.invoke('browser:forward'),
    reload: () => ipcRenderer.invoke('browser:reload'),
    getUrl: () => ipcRenderer.invoke('browser:url'),
    cdp: () => ipcRenderer.invoke('cdp:status'),
  },

  // ── 环境标记 ──
  isDesktop: true,
  platform: process.platform,
});
