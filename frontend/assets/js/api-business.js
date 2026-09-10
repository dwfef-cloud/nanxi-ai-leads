/* ═══════════════════════════════════════════════════════════
   业务域真实 API 对接模块（获客域其他 + 客户域）
   ───────────────────────────────────────────────────────────
   覆盖 mock-data.js 中仍为 mock 回退的 11 个函数：
     获客域：getScripts / getScriptTemplates / getAccounts
     客户域：getCustomers / getStageMeta / getFollowups
            completeFollowup / advanceStage / recordDeal
            markLost / createCustomer / createFollowup
   加载顺序：必须在 mock-data.js 之后加载，通过 Object.assign
   覆盖 window.MOCK_API 中对应函数，已有真实函数（getLeads 等）保留。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  /* ── API 基址配置 ── */
  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 10000; // 10 秒超时

  /* ── 全局加载指示器（顶部进度条，与 mock-data.js 行为一致） ── */
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

  /* ── 统一 apiFetch：10秒超时 / HTTP错误 / 网络错误 ── */
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

  /* ── 字段归一化：后端 snake_case → 前端 camelCase ── */
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

  /* ── 请求体字段转换：前端 camelCase → 后端 snake_case ── */
  function _snakeKey(k) {
    return k.replace(/([A-Z])/g, (_, c) => '_' + c.toLowerCase());
  }
  function _snakeify(obj) {
    if (Array.isArray(obj)) return obj.map(_snakeify);
    if (obj && typeof obj === 'object') {
      const out = {};
      for (const k in obj) {
        if (Object.prototype.hasOwnProperty.call(obj, k)) {
          out[_snakeKey(k)] = _snakeify(obj[k]);
        }
      }
      return out;
    }
    return obj;
  }

  /* ════════ 获客域 · 话术库 ════════ */

  /**
   * 话术库列表（含变体统计）
   * GET /api/scripts
   * @returns {Promise<Array>} 话术数组
   */
  async function getScripts() {
    const data = await apiFetch('/scripts');
    return _normalize(data);
  }

  /**
   * 行业话术模板包列表
   * GET /api/scripts/templates
   * @returns {Promise<Array>} 模板包数组
   */
  async function getScriptTemplates() {
    const data = await apiFetch('/scripts/templates');
    return _normalize(data);
  }

  /* ════════ 获客域 · 抖音账号 ════════ */

  /**
   * 抖音账号列表（含健康分和限流状态）
   * GET /api/accounts
   * @returns {Promise<Array>} 账号数组
   */
  async function getAccounts() {
    const data = await apiFetch('/accounts');
    return _normalize(data);
  }

  /* ════════ 客户域 · 客户与商机 ════════ */

  /**
   * 客户列表（含商机阶段）
   * GET /api/customers
   * @param {Object} [params] - 可选筛选参数
   * @param {string} [params.stage] - 按 CustomerStage 筛选
   * @param {string} [params.keyword] - 昵称/备注模糊搜索
   * @returns {Promise<Array>} 客户数组
   */
  async function getCustomers(params = {}) {
    const qs = new URLSearchParams();
    if (params.stage && params.stage !== 'all') qs.set('stage', params.stage);
    if (params.keyword) qs.set('keyword', params.keyword);
    const query = qs.toString();
    const data = await apiFetch('/customers' + (query ? '?' + query : ''));
    return _normalize(data);
  }

  /**
   * 商机 7 阶段元数据（前端筛选器/标签用）
   * GET /api/customers/stage-meta
   * @returns {Promise<Object>} 阶段元数据对象
   */
  async function getStageMeta() {
    const data = await apiFetch('/customers/stage-meta');
    return _normalize(data);
  }

  /**
   * 创建客户（线索加微后自动创建，或手动录入）
   * POST /api/customers
   * @param {Object} data - 客户数据（camelCase，自动转为 snake_case）
   * @returns {Promise<Object>} 创建后的客户对象
   */
  async function createCustomer(data) {
    const body = JSON.stringify(_snakeify(data));
    const res = await apiFetch('/customers', { method: 'POST', body });
    return _normalize(res);
  }

  /**
   * 推进商机阶段（一键点选）
   * POST /api/customers/{id}/stage
   * @param {string} cuId - 客户 ID
   * @param {string} stage - 目标阶段（CustomerStage）
   * @returns {Promise<Object>} { ok: true }
   */
  async function advanceStage(cuId, stage) {
    const body = JSON.stringify({ stage });
    const res = await apiFetch('/customers/' + encodeURIComponent(cuId) + '/stage', { method: 'POST', body });
    return _normalize(res);
  }

  /**
   * 成交录入
   * POST /api/customers/{id}/deal
   * @param {string} cuId - 客户 ID
   * @param {number} amount - 成交金额
   * @returns {Promise<Object>} { ok: true, customerId, amount }
   */
  async function recordDeal(cuId, amount) {
    const body = JSON.stringify({ amount });
    const res = await apiFetch('/customers/' + encodeURIComponent(cuId) + '/deal', { method: 'POST', body });
    return _normalize(res);
  }

  /**
   * 标记流失
   * POST /api/customers/{id}/lost
   * @param {string} cuId - 客户 ID
   * @param {string} reason - 流失原因
   * @returns {Promise<Object>} { ok: true }
   */
  async function markLost(cuId, reason) {
    const body = JSON.stringify({ reason });
    const res = await apiFetch('/customers/' + encodeURIComponent(cuId) + '/lost', { method: 'POST', body });
    return _normalize(res);
  }

  /* ════════ 客户域 · 今日跟进 ════════ */

  /**
   * 跟进待办列表
   * GET /api/followups?date={date}
   * @param {string} [date='today'] - 日期范围：today / overdue / upcoming
   * @returns {Promise<Array>} 跟进待办数组
   */
  async function getFollowups(date = 'today') {
    const data = await apiFetch('/followups?date=' + encodeURIComponent(date));
    return _normalize(data);
  }

  /**
   * 创建跟进待办
   * POST /api/followups
   * @param {Object} data - 跟进数据（camelCase，自动转为 snake_case）
   * @returns {Promise<Object>} 创建后的跟进对象
   */
  async function createFollowup(data) {
    const body = JSON.stringify(_snakeify(data));
    const res = await apiFetch('/followups', { method: 'POST', body });
    return _normalize(res);
  }

  /**
   * 标记跟进完成
   * POST /api/followups/{id}/complete
   * @param {string} id - 跟进待办 ID
   * @param {string} [note] - 完成备注（可选）
   * @returns {Promise<Object>} { ok: true }
   */
  async function completeFollowup(id, note) {
    const body = JSON.stringify(note ? { note } : {});
    const res = await apiFetch('/followups/' + encodeURIComponent(id) + '/complete', { method: 'POST', body });
    return _normalize(res);
  }

  /* ══════════ AI 大模型接口 ══════════ */

  /**
   * 获取 AI 设置（provider / 是否配置 Key / 模型）
   */
  async function getAISettings() {
    try {
      return await apiFetch('/ai/settings');
    } catch (e) {
      return { provider: 'dashscope', configured: false, masked_key: '', model: '' };
    }
  }

  /**
   * 线索画像分析：意向等级 / 需求标签 / 推荐话术 / 跟进建议
   * 成功后后端自动回写 Lead.intent_level / tags / customer_need
   * @param {Object} payload { nickname, comment, video_title, source_keyword }
   */
  async function aiAnalyzeLead(payload) {
    const body = JSON.stringify(payload || {});
    return await apiFetch('/ai/analyze', { method: 'POST', body });
  }

  /**
   * 智能草稿：根据话术类别 + 用户画像 + 评论内容生成个性化回复
   * @param {Object} payload { lead_id, script_category, user_comment?, style? }
   * style: formal | casual | short
   */
  async function aiSmartDraft(payload) {
    const body = JSON.stringify(payload || {});
    return await apiFetch('/ai/draft', { method: 'POST', body });
  }

  /**
   * 评论回复建议：生成 3 条（钩子型/价值型/提问型）
   * @param {Object} payload { comment, video_title?, source_keyword? }
   */
  async function aiCommentSuggestion(payload) {
    const body = JSON.stringify(payload || {});
    return await apiFetch('/ai/comment-suggestion', { method: 'POST', body });
  }

  /* ── 挂载到全局 window.MOCK_API（覆盖 mock 回退版本） ── */
  window.MOCK_API = window.MOCK_API || {};
  Object.assign(window.MOCK_API, {
    /* 获客域 · 话术库 */
    getScripts,
    getScriptTemplates,
    /* 获客域 · 抖音账号 */
    getAccounts,
    /* 客户域 · 客户与商机 */
    getCustomers,
    getStageMeta,
    createCustomer,
    advanceStage,
    recordDeal,
    markLost,
    /* 客户域 · 今日跟进 */
    getFollowups,
    createFollowup,
    completeFollowup,
    /* AI 大模型 */
    getAISettings,
    aiAnalyzeLead,
    aiSmartDraft,
    aiCommentSuggestion,
  });
})();
