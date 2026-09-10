/* ═══════════════════════════════════════════════════════════
   系统监控 · 真实 API 对接模块（P4-C）
   ───────────────────────────────────────────────────────────
   覆盖 /api/monitor/* 六个端点：
     GET /health       综合健康状态
     GET /database     数据库状态
     GET /api-stats    API 统计
     GET /accounts     账号健康度总览
     GET /tasks        任务运行态
     GET /logs         最近系统日志

   本文件在 mock-data.js 之后、app.js 之前加载，
   将函数挂载到 window.MOCK_API（即 app.js 中的 API 对象）。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 10000;

  /* ── 统一 apiFetch（与 api-scheduler.js 一致） ── */
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

  /* ── 字段归一化：snake_case → camelCase（递归） ── */
  function _camelKey(k) {
    return k.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
  }
  function _normalize(obj) {
    if (Array.isArray(obj)) return obj.map(_normalize);
    if (obj && typeof obj === 'object') {
      const out = {};
      for (const k in obj) {
        if (Object.prototype.hasOwnProperty.call(obj, k)) {
          out[_camelKey(k)] = _normalize(obj[k]);
        }
      }
      return out;
    }
    return obj;
  }

  /* ══════════════════════════════════════════════════════════
     监控 API 函数
     ══════════════════════════════════════════════════════════ */

  /** 综合健康状态 GET /api/monitor/health */
  async function fetchMonitorHealth() {
    return _normalize(await apiFetch('/monitor/health'));
  }

  /** 数据库状态 GET /api/monitor/database */
  async function fetchMonitorDatabase() {
    return _normalize(await apiFetch('/monitor/database'));
  }

  /** API 统计 GET /api/monitor/api-stats */
  async function fetchMonitorApiStats() {
    return _normalize(await apiFetch('/monitor/api-stats'));
  }

  /** 账号健康度总览 GET /api/monitor/accounts */
  async function fetchMonitorAccounts() {
    return _normalize(await apiFetch('/monitor/accounts'));
  }

  /** 任务运行态 GET /api/monitor/tasks */
  async function fetchMonitorTasks() {
    return _normalize(await apiFetch('/monitor/tasks'));
  }

  /** 最近系统日志 GET /api/monitor/logs?lines=N */
  async function fetchMonitorLogs(lines) {
    const qs = lines ? '?lines=' + lines : '';
    return _normalize(await apiFetch('/monitor/logs' + qs));
  }

  /* ══════════════════════════════════════════════════════════
     挂载到全局 window.MOCK_API
     ══════════════════════════════════════════════════════════ */
  window.MOCK_API = window.MOCK_API || {};
  Object.assign(window.MOCK_API, {
    fetchMonitorHealth,
    fetchMonitorDatabase,
    fetchMonitorApiStats,
    fetchMonitorAccounts,
    fetchMonitorTasks,
    fetchMonitorLogs,
  });
})();
