/* ═══════════════════════════════════════════════════════════
   私信发送执行器 · 真实 API 对接模块（P4-D）
   ───────────────────────────────────────────────────────────
   覆盖 /api/dm/* 发送执行端点：
     POST /send/{leadId}        发送一条私信
     POST /send-batch           批量发送（后台线程）
     POST /send-batch/stop      停止批量发送
     GET  /send-batch/status    批量发送进度
     GET  /send-results         发送结果列表
     POST /sender/test          测试发送器
     GET  /sender/config        获取配置
     PUT  /sender/config        更新配置

   本文件在 mock-data.js 之后、app.js 之前加载，
   挂载到 window.MOCK_API（即 app.js 中的 API 对象）。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 15000;

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
      if (err.name === 'AbortError') throw new Error('请求超时，请检查后端服务是否启动');
      if (err instanceof TypeError) throw new Error('网络错误，请检查后端服务是否启动（' + err.message + '）');
      throw err;
    } finally {
      _hideLoading();
    }
  }

  function _camelKey(k) {
    return k.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
  }
  function _normalize(obj) {
    if (Array.isArray(obj)) return obj.map(_normalize);
    if (obj && typeof obj === 'object') {
      const out = {};
      for (const k in obj) {
        if (Object.prototype.hasOwnProperty.call(obj, k)) out[_camelKey(k)] = _normalize(obj[k]);
      }
      return out;
    }
    return obj;
  }

  /* ══════════════════════════════════════════════════════════
     私信发送执行器 API
     ══════════════════════════════════════════════════════════ */

  /** 发送一条私信  POST /api/dm/send/{leadId} */
  async function sendDmOne(leadId, content) {
    const body = {};
    if (content) body.content = content;
    const data = await apiFetch('/dm/send/' + encodeURIComponent(leadId), {
      method: 'POST',
      body: JSON.stringify(body),
    });
    return _normalize(data);
  }

  /** 批量发送  POST /api/dm/send-batch */
  async function sendDmBatch(count) {
    const data = await apiFetch('/dm/send-batch', {
      method: 'POST',
      body: JSON.stringify({ count: count || 10 }),
    });
    return _normalize(data);
  }

  /** 停止批量发送  POST /api/dm/send-batch/stop */
  async function stopDmBatch() {
    const data = await apiFetch('/dm/send-batch/stop', { method: 'POST' });
    return _normalize(data);
  }

  /** 批量发送进度  GET /api/dm/send-batch/status */
  async function fetchDmBatchStatus() {
    const data = await apiFetch('/dm/send-batch/status');
    return _normalize(data);
  }

  /** 发送结果列表  GET /api/dm/send-results */
  async function fetchDmSendResults(limit) {
    const data = await apiFetch('/dm/send-results?limit=' + (limit || 50));
    return _normalize(data);
  }

  /** 测试发送器  POST /api/dm/sender/test */
  async function testDmSender() {
    const data = await apiFetch('/dm/sender/test', { method: 'POST' });
    return _normalize(data);
  }

  /** 获取发送器配置  GET /api/dm/sender/config */
  async function getDmSenderConfig() {
    const data = await apiFetch('/dm/sender/config');
    return _normalize(data);
  }

  /** 更新发送器配置  PUT /api/dm/sender/config */
  async function updateDmSenderConfig(data) {
    const body = {};
    if (data.mode !== undefined) body.mode = data.mode;
    if (data.minInterval !== undefined) body.min_interval = data.minInterval;
    if (data.maxInterval !== undefined) body.max_interval = data.maxInterval;
    if (data.dailyLimit !== undefined) body.daily_limit = data.dailyLimit;
    const res = await apiFetch('/dm/sender/config', {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    return _normalize(res);
  }

  /* ══════════════════════════════════════════════════════════
     挂载到全局 window.MOCK_API
     ══════════════════════════════════════════════════════════ */
  window.MOCK_API = window.MOCK_API || {};
  Object.assign(window.MOCK_API, {
    sendDmOne,
    sendDmBatch,
    stopDmBatch,
    fetchDmBatchStatus,
    fetchDmSendResults,
    testDmSender,
    getDmSenderConfig,
    updateDmSenderConfig,
  });
})();
