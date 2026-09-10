/* ═══════════════════════════════════════════════════════════
   分析域 + 合规域 · 真实 API 对接模块
   ───────────────────────────────────────────────────────────
   覆盖 mock-data.js 中仍为 mock 回退的 10 个函数：
     分析域：getWorkbench / getFunnel / getAttribution / getCompliance
     运行态：getRuntime
     合规操作：enterSafeMode / exitSafeMode / pauseAll / resumeAll
     上手向导：submitWizard

   本文件在 mock-data.js 之后加载，通过 Object.assign 覆盖
   window.MOCK_API 上对应的 mock 回退函数，已有的真实 API
   函数（getLeads 等）保持不变。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

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

  /* ── 轻提示（复用 app.js 的 toast 机制，不可用时降级为 console） ── */
  function _toast(msg, type) {
    try {
      const region = document.getElementById('toast-region');
      if (region) {
        const el = document.createElement('div');
        el.className = 'toast toast--' + (type || 'warn');
        el.textContent = msg;
        region.appendChild(el);
        setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 2400);
        setTimeout(() => el.remove(), 2800);
        return;
      }
    } catch (e) { /* ignore */ }
    console.warn('[toast]', msg);
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

  /* ── 字段归一化：后端 snake_case → 前端 camelCase（递归） ── */
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
     分析域函数（GET，返回数据经 _normalize 转换）
     ══════════════════════════════════════════════════════════ */

  /**
   * 工作台聚合数据（线索统计/今日待办/转化概览）
   * GET /api/workbench
   */
  async function getWorkbench() {
    const data = await apiFetch('/workbench');
    return _normalize(data);
  }

  /**
   * 转化漏斗数据（六级漏斗 + 成本 + 14天加微趋势）
   * GET /api/dashboard/funnel
   */
  async function getFunnel() {
    const data = await apiFetch('/dashboard/funnel');
    return _normalize(data);
  }

  /**
   * 触点归因数据（6触点归因 + 内容榜）
   * GET /api/dashboard/attribution
   */
  async function getAttribution() {
    const data = await apiFetch('/dashboard/attribution');
    return _normalize(data);
  }

  /**
   * 合规风险数据（风险总览 + R1/R2/R3规则 + 审计日志）
   * GET /api/dashboard/compliance
   */
  async function getCompliance() {
    const data = await apiFetch('/dashboard/compliance');
    return _normalize(data);
  }

  /* ══════════════════════════════════════════════════════════
     运行态函数
     ══════════════════════════════════════════════════════════ */

  /**
   * 运行态数据（任务状态/账号状态/安全模式）
   * GET /api/runtime
   */
  async function getRuntime() {
    const data = await apiFetch('/runtime');
    return _normalize(data);
  }

  /* ══════════════════════════════════════════════════════════
     合规操作函数（POST，成功返回后端响应数据，失败 throw）
     ══════════════════════════════════════════════════════════ */

  /**
   * 进入安全模式（全量暂停）
   * POST /api/compliance/safe-mode/enter
   * @param {string} source - 触发来源：manual | r3 | health_score
   * @param {string} reason - 触发原因
   */
  async function enterSafeMode(source, reason) {
    const data = await apiFetch('/compliance/safe-mode/enter', {
      method: 'POST',
      body: JSON.stringify({ source: source, reason: reason }),
    });
    return _normalize(data);
  }

  /**
   * 退出安全模式（必须人工确认）
   * POST /api/compliance/safe-mode/exit
   */
  async function exitSafeMode() {
    const data = await apiFetch('/compliance/safe-mode/exit', {
      method: 'POST',
    });
    return _normalize(data);
  }

  /**
   * 一键全量暂停
   * POST /api/compliance/pause
   */
  async function pauseAll() {
    const data = await apiFetch('/compliance/pause', {
      method: 'POST',
    });
    return _normalize(data);
  }

  /**
   * 恢复运行
   * POST /api/compliance/resume
   */
  async function resumeAll() {
    const data = await apiFetch('/compliance/resume', {
      method: 'POST',
    });
    return _normalize(data);
  }

  /* ══════════════════════════════════════════════════════════
     上手向导
     ══════════════════════════════════════════════════════════ */

  /**
   * 上手向导提交（5步配置聚合对象）
   * POST /api/onboarding/wizard
   * @param {object} payload - 向导各步配置的聚合对象
   */
  async function submitWizard(payload) {
    const data = await apiFetch('/onboarding/wizard', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return _normalize(data);
  }

  /* ══════════════════════════════════════════════════════════
     挂载到全局 window.MOCK_API（覆盖 mock 回退版本）
     mock-data.js 中已有的真实 API 函数（getLeads 等）保持不变
     ══════════════════════════════════════════════════════════ */
  window.MOCK_API = window.MOCK_API || {};
  Object.assign(window.MOCK_API, {
    /* 分析域 */
    getWorkbench,
    getFunnel,
    getAttribution,
    getCompliance,
    /* 运行态 */
    getRuntime,
    /* 合规操作 */
    enterSafeMode,
    exitSafeMode,
    pauseAll,
    resumeAll,
    /* 上手向导 */
    submitWizard,
  });
})();
