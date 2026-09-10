/* ═══════════════════════════════════════════════════════════
   私信收件箱 API 对接模块
   ───────────────────────────────────────────────────────────
   覆盖 5 个端点：
     GET    /api/dm/inbox                      收件箱列表
     GET    /api/dm/inbox/{id}                 会话详情（标记已读）
     POST   /api/dm/inbox/{id}/reply           发送回复
     POST   /api/dm/inbox/{id}/mark-wechat     标记加微转客户
     POST   /api/dm/inbox/simulate             模拟接收私信
   加载顺序：在 mock-data.js 之后、app.js 之前，通过 Object.assign
   挂载到 window.MOCK_API。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 10000;

  /* ── 统一 apiFetch（与 api-business.js 一致） ── */
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

  /* ════════ 收件箱列表 ════════ */

  /**
   * 收件箱会话列表
   * GET /api/dm/inbox?status=
   * @param {string} [status] - new / replied / wechat_added / closed / all
   * @returns {Promise<Array>} 会话列表
   */
  async function getDmInbox(status) {
    const qs = status ? '?status=' + encodeURIComponent(status) : '';
    const data = await apiFetch('/dm/inbox' + qs);
    return _normalize(data);
  }

  /* ════════ 会话详情 ════════ */

  /**
   * 会话详情（同时标记入站消息已读）
   * GET /api/dm/inbox/{id}
   * @param {string} convId - 会话 ID
   * @returns {Promise<Object>} 会话详情 + 消息列表
   */
  async function getDmConversation(convId) {
    const data = await apiFetch('/dm/inbox/' + encodeURIComponent(convId));
    return _normalize(data);
  }

  /* ════════ 发送回复 ════════ */

  /**
   * 发送回复
   * POST /api/dm/inbox/{id}/reply
   * @param {string} convId - 会话 ID
   * @param {string} content - 回复内容
   * @param {string} [scriptId] - 使用的话术 ID
   * @returns {Promise<Object>} 创建的消息
   */
  async function sendDmReply(convId, content, scriptId) {
    const body = JSON.stringify({ content, script_id: scriptId || null });
    const res = await apiFetch('/dm/inbox/' + encodeURIComponent(convId) + '/reply', { method: 'POST', body });
    return _normalize(res);
  }

  /* ════════ 标记加微转客户 ════════ */

  /**
   * 标记加微转客户
   * POST /api/dm/inbox/{id}/mark-wechat
   * @param {string} convId - 会话 ID
   * @param {Object} data - { wechatId, customerName?, phone? }
   * @returns {Promise<Object>} { ok, conversationId, customerId, leadId }
   */
  async function markDmWechat(convId, data) {
    const body = JSON.stringify({
      wechat_id: data.wechatId,
      customer_name: data.customerName || null,
      phone: data.phone || null,
    });
    const res = await apiFetch('/dm/inbox/' + encodeURIComponent(convId) + '/mark-wechat', { method: 'POST', body });
    return _normalize(res);
  }

  /* ════════ 模拟接收私信（开发测试用） ════════ */

  /**
   * 模拟接收私信
   * POST /api/dm/inbox/simulate
   * @param {Object} data - { nickname, content, leadId? }
   * @returns {Promise<Object>} { ok, conversationId, messageId, leadId }
   */
  async function simulateDmInbound(data) {
    const body = JSON.stringify({
      nickname: data.nickname,
      content: data.content,
      lead_id: data.leadId || null,
    });
    const res = await apiFetch('/dm/inbox/simulate', { method: 'POST', body });
    return _normalize(res);
  }

  /* ── 挂载到全局 window.MOCK_API ── */
  window.MOCK_API = window.MOCK_API || {};
  Object.assign(window.MOCK_API, {
    getDmInbox,
    getDmConversation,
    sendDmReply,
    markDmWechat,
    simulateDmInbound,
  });
})();
