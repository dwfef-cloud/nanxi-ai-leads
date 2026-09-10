/* ═══════════════════════════════════════════════════════════
   通知中心 API 对接模块（P4-A）
   ───────────────────────────────────────────────────────────
   覆盖 5 个端点：
     GET    /api/notifications?unreadOnly=&limit=   通知列表（含未读数）
     GET    /api/notifications/unread-count           未读数 { count }
     POST   /api/notifications/{id}/read              标记单条已读
     POST   /api/notifications/read-all               全部已读
     POST   /api/notifications/test                  发送测试通知
   加载顺序：在 app.js 之前，通过 Object.assign 挂载到 window.MOCK_API。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 10000;

  /* ── 统一 apiFetch（与 api-dm-inbox.js 一致） ── */
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

  /* ── snake_case → camelCase ── */
  function _camelKey(k) { return k.replace(/_([a-z])/g, (_, c) => c.toUpperCase()); }
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

  /* ════════ 通知列表 ════════ */

  /**
   * 通知列表（附带未读数）
   * GET /api/notifications?unread_only=&limit=
   * @param {boolean} [unreadOnly] - 仅未读
   * @param {number} [limit] - 条数
   * @returns {Promise<{items: Array, unreadCount: number}>}
   */
  async function getNotifications(unreadOnly, limit) {
    const qs = new URLSearchParams();
    if (unreadOnly) qs.set('unread_only', 'true');
    if (limit) qs.set('limit', String(limit));
    const data = await apiFetch('/notifications' + (qs.toString() ? '?' + qs.toString() : ''));
    return _normalize(data);
  }

  /* ════════ 未读数 ════════ */

  /**
   * 未读通知数（铃铛角标）
   * GET /api/notifications/unread-count
   * @returns {Promise<{count: number}>}
   */
  async function getUnreadCount() {
    const data = await apiFetch('/notifications/unread-count');
    return _normalize(data);
  }

  /* ════════ 标记单条已读 ════════ */

  /**
   * POST /api/notifications/{id}/read
   * @param {string} id - 通知 ID
   */
  async function markNotificationRead(id) {
    return apiFetch('/notifications/' + encodeURIComponent(id) + '/read', { method: 'POST' });
  }

  /* ════════ 全部已读 ════════ */

  /**
   * POST /api/notifications/read-all
   */
  async function markAllNotificationsRead() {
    return apiFetch('/notifications/read-all', { method: 'POST' });
  }

  /* ════════ 测试通知 ════════ */

  /**
   * POST /api/notifications/test
   * @param {Object} [data] - { title?, content?, level? }
   */
  async function sendTestNotification(data) {
    const body = JSON.stringify({
      title: (data && data.title) || '测试通知',
      content: (data && data.content) || '这是一条来自通知中心的测试消息。',
      level: (data && data.level) || 'info',
    });
    return apiFetch('/notifications/test', { method: 'POST', body });
  }

  /* ── 挂载到全局 window.MOCK_API ── */
  window.MOCK_API = window.MOCK_API || {};
  Object.assign(window.MOCK_API, {
    getNotifications,
    getUnreadCount,
    markNotificationRead,
    markAllNotificationsRead,
    sendTestNotification,
  });
})();
