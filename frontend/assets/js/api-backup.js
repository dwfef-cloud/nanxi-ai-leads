/* ═══════════════════════════════════════════════════════════
   数据备份与恢复 API 封装模块
   ───────────────────────────────────────────────────────────
   封装后端 /api/backup 系列接口，挂到 window.API 对象。
   加载顺序：在 app.js 之前加载。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 30000;

  /* ── 统一 fetch 封装 ── */
  async function apiFetch(path, options = {}) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT);
    try {
      const res = await fetch(API_BASE + path, {
        ...options,
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      });
      clearTimeout(timeoutId);
      if (!res.ok) {
        let detail = '';
        try { const j = await res.json(); detail = j.detail || ''; } catch (e) { /* ignore */ }
        const err = new Error('HTTP ' + res.status + (detail ? '：' + detail : ''));
        err.status = res.status;
        throw err;
      }
      if (res.status === 204) return null;
      return res.json();
    } catch (err) {
      clearTimeout(timeoutId);
      if (err.name === 'AbortError') {
        throw new Error('请求超时，请检查后端服务是否启动');
      }
      if (err instanceof TypeError) {
        throw new Error('网络错误，请检查后端服务是否启动（' + err.message + '）');
      }
      throw err;
    }
  }

  /* ════════ 备份操作 ════════ */

  /** 创建手动备份 */
  async function create() {
    return apiFetch('/backup/create', { method: 'POST' });
  }

  /** 获取备份列表 */
  async function list() {
    return apiFetch('/backup/list');
  }

  /** 从备份恢复 */
  async function restore(filename) {
    const enc = encodeURIComponent(filename);
    return apiFetch('/backup/' + enc + '/restore', { method: 'POST' });
  }

  /** 删除备份 */
  async function del(filename) {
    const enc = encodeURIComponent(filename);
    return apiFetch('/backup/' + enc, { method: 'DELETE' });
  }

  /* ════════ 自动备份设置 ════════ */

  /** 获取自动备份设置 */
  async function getSettings() {
    return apiFetch('/backup/settings');
  }

  /** 更新自动备份设置 */
  async function updateSettings(data) {
    const body = JSON.stringify(data || {});
    return apiFetch('/backup/settings', { method: 'PUT', body });
  }

  /* ════════ 全量导出 ════════ */

  /** 全量导出 JSON */
  async function exportJson() {
    return apiFetch('/backup/export-json', { method: 'POST' });
  }

  /* ── 挂载到 window.API ── */
  window.API = window.API || {};
  Object.assign(window.API, {
    apiBackup: {
      create,
      list,
      restore,
      delete: del,
      getSettings,
      updateSettings,
      exportJson,
    },
  });
})();
