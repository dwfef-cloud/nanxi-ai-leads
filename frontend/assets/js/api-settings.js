/* ═══════════════════════════════════════════════════════════
   系统配置中心 API 封装模块
   ───────────────────────────────────────────────────────────
   封装后端 /api/settings 系列接口，挂到 window.API 对象。
   加载顺序：在 app.js 之前加载。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 15000;

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

  /* ════════ 全部配置 ════════ */

  /**
   * 获取所有分类配置
   * GET /api/settings
   * @returns {Promise<Object>} { ai, strategy, compliance, crawl, notification }
   */
  async function fetchAllSettings() {
    return apiFetch('/settings');
  }

  /**
   * 部分更新配置
   * PUT /api/settings
   * @param {Object} data - { category: { field: value, ... } }
   */
  async function updateSettings(data) {
    const body = JSON.stringify(data || {});
    return apiFetch('/settings', { method: 'PUT', body });
  }

  /* ════════ AI 配置 ════════ */

  async function fetchAISettings() {
    return apiFetch('/settings/ai');
  }

  async function updateAISettings(data) {
    const body = JSON.stringify(data || {});
    return apiFetch('/settings/ai', { method: 'PUT', body });
  }

  /* ════════ 话术策略配置 ════════ */

  async function fetchStrategySettings() {
    return apiFetch('/settings/strategy');
  }

  async function updateStrategySettings(data) {
    const body = JSON.stringify(data || {});
    return apiFetch('/settings/strategy', { method: 'PUT', body });
  }

  /* ════════ 合规规则配置 ════════ */

  async function fetchComplianceSettings() {
    return apiFetch('/settings/compliance');
  }

  async function updateComplianceSettings(data) {
    const body = JSON.stringify(data || {});
    return apiFetch('/settings/compliance', { method: 'PUT', body });
  }

  /* ════════ 采集配置 ════════ */

  async function fetchCrawlSettings() {
    return apiFetch('/settings/crawl');
  }

  async function updateCrawlSettings(data) {
    const body = JSON.stringify(data || {});
    return apiFetch('/settings/crawl', { method: 'PUT', body });
  }

  /* ════════ 通知配置 ════════ */

  async function fetchNotificationSettings() {
    return apiFetch('/settings/notification');
  }

  async function updateNotificationSettings(data) {
    const body = JSON.stringify(data || {});
    return apiFetch('/settings/notification', { method: 'PUT', body });
  }

  /* ════════ AI 连接测试 ════════ */

  /**
   * 测试 AI 连接
   * POST /api/settings/test-ai
   * @param {Object} data - { provider, api_key, model, timeout }
   * @returns {Promise<Object>} { success, message, latency_ms }
   */
  async function testAIConnection(data) {
    const body = JSON.stringify(data || {});
    return apiFetch('/settings/test-ai', { method: 'POST', body });
  }

  /* ── 挂载到 window.API ── */
  window.API = window.API || {};
  Object.assign(window.API, {
    fetchAllSettings,
    updateSettings,
    fetchAISettings,
    updateAISettings,
    fetchStrategySettings,
    updateStrategySettings,
    fetchComplianceSettings,
    updateComplianceSettings,
    fetchCrawlSettings,
    updateCrawlSettings,
    fetchNotificationSettings,
    updateNotificationSettings,
    testAIConnection,
  });
})();
