/* ═══════════════════════════════════════════════════════════
   采集任务 API 对接模块
   ───────────────────────────────────────────────────────────
   对接后端 /api/crawl/* 端点：
     GET    /api/crawl/tasks              采集任务列表
     POST   /api/crawl/tasks              创建采集任务
     POST   /api/crawl/tasks/{id}/start   启动采集
     POST   /api/crawl/tasks/{id}/stop    停止采集
     GET    /api/crawl/tasks/{id}/status  采集状态
     DELETE /api/crawl/tasks/{id}         删除采集任务
     POST   /api/crawl/import              采集结果批量入库
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 15000;

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
        throw new Error('HTTP ' + res.status + (detail ? '：' + detail : ''));
      }
      if (res.status === 204) return null;
      return res.json();
    } finally {
      clearTimeout(timeoutId);
    }
  }

  const CrawlAPI = {
    /** 采集任务列表 */
    async listTasks(status) {
      const qs = status ? `?status=${encodeURIComponent(status)}` : '';
      return apiFetch('/crawl/tasks' + qs);
    },

    /** 创建采集任务 */
    async createTask(payload) {
      return apiFetch('/crawl/tasks', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    },

    /** 启动采集 */
    async startTask(taskId) {
      return apiFetch(`/crawl/tasks/${encodeURIComponent(taskId)}/start`, { method: 'POST' });
    },

    /** 停止采集 */
    async stopTask(taskId) {
      return apiFetch(`/crawl/tasks/${encodeURIComponent(taskId)}/stop`, { method: 'POST' });
    },

    /** 采集状态 */
    async getTaskStatus(taskId) {
      return apiFetch(`/crawl/tasks/${encodeURIComponent(taskId)}/status`);
    },

    /** 删除采集任务 */
    async deleteTask(taskId) {
      return apiFetch(`/crawl/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
    },

    /** 采集结果批量入库 */
    async importResults(payload) {
      return apiFetch('/crawl/import', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    },
  };

  window.CrawlAPI = CrawlAPI;
})();
