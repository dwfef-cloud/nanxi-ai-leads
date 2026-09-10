/* ═══════════════════════════════════════════════════════════
   报表域 + 导出域 · 真实 API 对接模块
   ───────────────────────────────────────────────────────────
   覆盖：
     报表：fetchWeeklyReport / fetchMonthlyReport / fetchSummary / fetchTrend
     导出：exportLeads / exportCustomers / exportDeals

   本文件在 mock-data.js 之后加载，通过 Object.assign 挂载到
   window.MOCK_API（app.js 中 const API = window.MOCK_API），
   同时赋值到 window.API 以兼容全局调用。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 15000; // 导出可能较慢，给 15 秒

  /* ── 全局加载指示器（与 api-dashboard.js 行为一致） ── */
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

  /* ── 构建查询字符串 ── */
  function _qs(params) {
    const parts = [];
    for (const k in params) {
      if (params[k] !== undefined && params[k] !== null && params[k] !== '') {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
      }
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  /* ══════════════════════════════════════════════════════════
     报表函数
     ══════════════════════════════════════════════════════════ */

  /**
   * 周报（date 所在周，周一到周日）
   * GET /api/reports/weekly?date=YYYY-MM-DD
   */
  async function fetchWeeklyReport(date) {
    const data = await apiFetch('/reports/weekly' + _qs({ date: date }));
    return _normalize(data);
  }

  /**
   * 月报（自然月）
   * GET /api/reports/monthly?year=2026&month=9
   */
  async function fetchMonthlyReport(year, month) {
    const data = await apiFetch('/reports/monthly' + _qs({ year: year, month: month }));
    return _normalize(data);
  }

  /**
   * 自定义区间汇总
   * GET /api/reports/summary?start_date=&end_date=
   */
  async function fetchSummary(startDate, endDate) {
    const data = await apiFetch('/reports/summary' + _qs({ start_date: startDate, end_date: endDate }));
    return _normalize(data);
  }

  /**
   * 近 N 天趋势（每日新增线索 / 加微）
   * GET /api/reports/trend?days=14
   */
  async function fetchTrend(days) {
    const data = await apiFetch('/reports/trend' + _qs({ days: days || 14 }));
    return _normalize(data);
  }

  /* ══════════════════════════════════════════════════════════
     导出函数（返回 { fileUrl, fileName, rowCount, format }）
     ══════════════════════════════════════════════════════════ */

  /**
   * 导出线索
   * GET /api/export/leads?format=csv&status=&source=&start_date=&end_date=
   * @param {string} format - csv | xlsx
   * @param {object} params - { status, source, startDate, endDate }
   */
  async function exportLeads(format, params) {
    params = params || {};
    const data = await apiFetch('/export/leads' + _qs({
      format: format || 'csv',
      status: params.status,
      source: params.source,
      start_date: params.startDate,
      end_date: params.endDate,
    }));
    return _normalize(data);
  }

  /**
   * 导出客户
   * GET /api/export/customers?format=csv&stage=&start_date=&end_date=
   */
  async function exportCustomers(format, params) {
    params = params || {};
    const data = await apiFetch('/export/customers' + _qs({
      format: format || 'csv',
      stage: params.stage,
      start_date: params.startDate,
      end_date: params.endDate,
    }));
    return _normalize(data);
  }

  /**
   * 导出成交记录
   * GET /api/export/deals?format=csv&start_date=&end_date=
   */
  async function exportDeals(format, params) {
    params = params || {};
    const data = await apiFetch('/export/deals' + _qs({
      format: format || 'csv',
      start_date: params.startDate,
      end_date: params.endDate,
    }));
    return _normalize(data);
  }

  /* ══════════════════════════════════════════════════════════
     挂载到全局
     ══════════════════════════════════════════════════════════ */
  const apiObj = {
    fetchWeeklyReport,
    fetchMonthlyReport,
    fetchSummary,
    fetchTrend,
    exportLeads,
    exportCustomers,
    exportDeals,
  };

  // 挂载到 window.MOCK_API（app.js 使用此对象）
  window.MOCK_API = window.MOCK_API || {};
  Object.assign(window.MOCK_API, apiObj);

  // 同时挂载到 window.API（兼容全局调用）
  window.API = window.MOCK_API;
})();
