/* ═══════════════════════════════════════════════════════════
   多账号调度 · 真实 API 对接模块
   ───────────────────────────────────────────────────────────
   覆盖 /api/scheduler/* 五个端点：
     GET  /config    获取调度配置
     PUT  /config    更新调度配置
     GET  /accounts  获取所有账号调度状态
     POST /assign    分配发送任务
     GET  /stats     调度统计

   本文件在 mock-data.js 之后、app.js 之前加载，
   将函数挂载到 window.MOCK_API（即 app.js 中的 API 对象）。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 10000;

  /* ── 全局加载指示器（与 api-dashboard.js 一致） ── */
  let _loadingCount = 0;
  let _loadingEl = null;
  function _ensureLoadingEl() {
    if (_loadingEl) return _loadingEl;
    _loadingEl = document.createElement('div');
    _loadingEl.id = 'api-loading-bar';
    _loadingEl.style.cssText = 'position:fixed;top:0;left:0;height:3px;background:linear-gradient(90deg,#1e6e52,#3a9d7a);z-index:9999;transition:width .3s ease;box-shadow:0 0 8px rgba(30,110,82,.5);';
    document.body.appendChild(_loadingEl);
    return _loadingEl;
  }
  function _showLoading() {
    _loadingCount++;
    const el = _ensureLoadingEl();
    el.style.width = '70%';
    el.style.opacity = '1';
  }
  function _hideLoading() {
    _loadingCount = Math.max(0, _loadingCount - 1);
    if (_loadingCount === 0 && _loadingEl) {
      _loadingEl.style.width = '100%';
      setTimeout(() => { if (_loadingCount === 0 && _loadingEl) _loadingEl.style.opacity = '0'; }, 200);
    }
  }

  /* ── 统一 apiFetch ── */
  async function apiFetch(path, options = {}) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT);
    _showLoading();
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
    } finally {
      _hideLoading();
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
     调度器 API 函数
     ══════════════════════════════════════════════════════════ */

  /**
   * 获取调度配置
   * GET /api/scheduler/config
   */
  async function fetchSchedulerConfig() {
    const data = await apiFetch('/scheduler/config');
    return _normalize(data);
  }

  /**
   * 更新调度配置
   * PUT /api/scheduler/config
   * @param {object} data - { strategy, healthThresholdWarn, healthThresholdCritical, maxConcurrent }
   */
  async function updateSchedulerConfig(data) {
    const body = {};
    if (data.strategy !== undefined) body.strategy = data.strategy;
    if (data.healthThresholdWarn !== undefined) body.health_threshold_warn = data.healthThresholdWarn;
    if (data.healthThresholdCritical !== undefined) body.health_threshold_critical = data.healthThresholdCritical;
    if (data.maxConcurrent !== undefined) body.max_concurrent = data.maxConcurrent;
    const res = await apiFetch('/scheduler/config', {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    return _normalize(res);
  }

  /**
   * 获取所有账号调度状态
   * GET /api/scheduler/accounts
   */
  async function fetchSchedulerAccounts() {
    const data = await apiFetch('/scheduler/accounts');
    return _normalize(data);
  }

  /**
   * 分配发送任务
   * POST /api/scheduler/assign
   * @param {object} data - { taskType, priority }
   */
  async function assignTask(data) {
    const body = {
      task_type: (data && data.taskType) || 'dm',
      priority: (data && data.priority) || 0,
    };
    const res = await apiFetch('/scheduler/assign', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    return _normalize(res);
  }

  /**
   * 获取调度统计
   * GET /api/scheduler/stats
   */
  async function fetchSchedulerStats() {
    const data = await apiFetch('/scheduler/stats');
    return _normalize(data);
  }

  /* ══════════════════════════════════════════════════════════
     挂载到全局 window.MOCK_API
     ══════════════════════════════════════════════════════════ */
  window.MOCK_API = window.MOCK_API || {};
  Object.assign(window.MOCK_API, {
    fetchSchedulerConfig,
    updateSchedulerConfig,
    fetchSchedulerAccounts,
    assignTask,
    fetchSchedulerStats,
  });
})();
