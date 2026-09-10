/* ═══════════════════════════════════════════════════════════
   获客系统 v1.0 · 轻量 SPA（纯前端）
   信息架构（业务视角）：
     工作台 · 获客（线索池/私信任务/话术库/抖音账号）
     · 客户管理（客户与商机/今日跟进）
     · 经营分析（转化漏斗/触点归因/账号健康度/合规风险）
     · 设置（上手向导）
   所有数据经 window.MOCK_API 获取（见 mock-data.js），
   本文件不含任何后端请求。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API = window.MOCK_API;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const main = $('#main');
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n) => Number(n).toLocaleString('zh-CN');
  const wan = (n) => (n >= 10000 ? `¥${(n / 10000).toFixed(1)}万` : `¥${fmt(n)}`);

  /* 全局态 */
  const state = {
    safeMode: { active: false, reason: '', triggerSource: null },
    taskRunning: true,
    leads: [],
    leadsFilter: 'all',
    leadStatusMeta: {},
    sourceMeta: {},
    customers: [],
    stageMeta: {},
    custFilter: 'all',
  };

  /* ══════════ 轻提示 ══════════ */
  function toast(msg, type = 'ok') {
    const el = document.createElement('div');
    el.className = `toast toast--${type}`;
    el.textContent = msg;
    $('#toast-region').appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 2400);
    setTimeout(() => el.remove(), 2800);
  }

  /* ══════════ AI 大模型辅助 ══════════ */
  const aiState = { settings: null, loading: false };

  /** 检查 AI 是否已配置 API Key，未配置时 toast 提示并返回 false */
  async function aiEnsureConfigured() {
    if (!aiState.settings) {
      try { aiState.settings = await API.getAISettings(); } catch (e) { aiState.settings = { configured: false }; }
    }
    if (!aiState.settings || !aiState.settings.configured) {
      toast('AI 服务未配置 API Key，请在设置页填写 DASHSCOPE_API_KEY 或 DOUBAO_API_KEY', 'warn');
      return false;
    }
    return true;
  }

  /** 统一处理 AI 接口错误：503 未配置 / 502 上游失败 / 其他 */
  function aiHandleError(e) {
    const msg = e && e.message ? e.message : String(e);
    if (msg.includes('503') || msg.includes('请配置')) {
      toast('AI 服务未配置：请设置 API Key 后再试', 'warn');
    } else if (msg.includes('502') || msg.includes('上游') || msg.includes('超时')) {
      toast('AI 服务调用失败（上游错误/超时），请稍后重试', 'warn');
    } else {
      toast('AI 服务异常：' + msg, 'warn');
    }
  }

  /* ══════════ 图标 / 页头 / 空状态（统一设计元素） ══════════ */
  // 取 index.html 内联 sprite 中的描边图标；颜色继承 currentColor（.ico 已定 --ink-soft）
  function ico(name, cls = '') {
    return `<span class="ico ${cls}" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#${name}"/></svg></span>`;
  }
  // 统一页头：图标锚点 + 标题 + 副标题 + 操作区
  function pageHead(o = {}) {
    return `<div class="page-head">
      ${o.icon ? `<span class="page-head__ico">${ico(o.icon)}</span>` : ''}
      <div class="page-head__title">
        <h1>${esc(o.title)}</h1>
        ${o.desc ? `<span class="page-head__desc">${o.desc}</span>` : ''}
      </div>
      ${o.spacer !== false ? '<span class="page-head__spacer"></span>' : ''}
      ${o.actions ? `<div class="page-head__actions">${o.actions}</div>` : ''}
    </div>`;
  }
  // 统一空状态
  function emptyState(icon, text) {
    return `<div class="empty"><div class="empty__ico">${ico(icon)}</div><p>${esc(text)}</p></div>`;
  }

  /* ══════════ 顶部栏 / 安全模式 联动 ══════════ */
  function renderTopbar() {
    document.body.classList.toggle('status-paused', !state.taskRunning);
    const pillTask = $('#pill-task');
    const pillSafe = $('#pill-safe');
    const banner = $('#safe-mode-banner');

    if (state.safeMode.active) {
      pillTask.innerHTML = '<span class="status-pill__led" style="background:var(--danger)"></span>已全量暂停';
      pillTask.className = 'status-pill status-pill--danger';
      pillSafe.textContent = '安全模式：开';
      pillSafe.className = 'status-pill status-pill--danger';
      banner.hidden = false;
      $('#safe-banner-reason').textContent =
        `触发原因：${state.safeMode.reason} · ${state.safeMode.triggeredAt} · 已发队列只读，新触达全部暂停，需人工确认方可退出`;
    } else {
      pillTask.innerHTML = state.taskRunning
        ? '<span class="status-pill__led"></span>采集运行中'
        : '<span class="status-pill__led" style="background:var(--warn)"></span>已手动暂停';
      pillTask.className = 'status-pill status-pill--running';
      pillSafe.textContent = '安全模式：关';
      pillSafe.className = 'status-pill status-pill--muted';
      banner.hidden = true;
    }
  }

  async function enterSafeMode(source, reason) {
    await API.enterSafeMode(source, reason);
    state.safeMode = { active: true, reason, triggeredAt: '刚刚', triggerSource: source };
    state.taskRunning = false;
    renderTopbar();
    toast('安全模式已启用，全部触达暂停', 'warn');
    route();
  }

  /* 退出安全模式 — 人工确认弹窗（IMP-004 防误恢复） */
  function openExitSafeModeModal() {
    openModal(`
      <h2 id="modal-title">退出安全模式 — 需人工确认</h2>
      <p style="font-size:13px;color:var(--ink-soft)">
        当前触发原因：<strong>${esc(state.safeMode.reason)}</strong><br>
        退出后系统将立即恢复新触达发送。请确认风险已排除（如账号限流已解除、话术已更换、黑名单已复核）。
      </p>
      <div class="confirm-note">
        本次操作将写入审计日志（复用 v0.2 审计表），记录操作人与退出原因，满足合规留痕要求。
      </div>
      <div class="field" style="margin-top:14px">
        <label for="exit-reason">退出原因（必填）</label>
        <textarea id="exit-reason" placeholder="例：账号「同城装修·班长」限流已解除，健康分回升，已复核黑名单"></textarea>
      </div>
      <label style="display:flex;gap:8px;align-items:flex-start;font-size:13px;margin-top:6px">
        <input type="checkbox" id="exit-confirm" style="margin-top:3px;accent-color:var(--danger)">
        <span>我已确认风险排除，明白恢复发送可能带来的封号风险</span>
      </label>
      <div class="modal__foot">
        <button type="button" class="btn" data-close>取消</button>
        <button type="button" class="btn btn--primary" id="btn-confirm-exit" disabled>确认退出并恢复</button>
      </div>
    `);
    const btn = $('#btn-confirm-exit');
    $('#exit-confirm').addEventListener('change', (e) => { btn.disabled = !e.target.checked; });
    btn.addEventListener('click', async () => {
      await API.exitSafeMode();
      state.safeMode = { active: false, reason: '', triggeredAt: null, triggerSource: null };
      state.taskRunning = true;
      closeModal();
      renderTopbar();
      toast('已退出安全模式，触达恢复');
      route();
    });
  }

  /* 一键暂停（不进安全模式，仅暂停） */
  $('#btn-pause-all').addEventListener('click', async () => {
    if (state.safeMode.active) { toast('安全模式中，请先人工确认退出', 'warn'); return; }
    if (!state.taskRunning) {
      await API.resumeAll(); state.taskRunning = true; toast('已恢复运行');
    } else {
      await API.pauseAll(); state.taskRunning = false; toast('已暂停全部采集与发送', 'warn');
    }
    renderTopbar(); route();
  });
  $('#btn-exit-safe-mode').addEventListener('click', openExitSafeModeModal);

  /* ══════════ 配置弹窗 ══════════ */
  $('#btn-settings').addEventListener('click', () => {
    openModal(`
      <h2 id="modal-title">系统配置</h2>
      <div class="grid grid--2" style="gap:14px">
        <div class="field"><label for="cfg-daily">单账号日频上限（R1 阈值）</label>
          <input type="number" id="cfg-daily" value="80"><span class="field__hint">达到后自动降速至 40/天，连续 2 天触顶暂停 24h</span></div>
        <div class="field"><label for="cfg-health">安全模式健康分阈值</label>
          <input type="number" id="cfg-health" value="40"><span class="field__hint">health_score 低于该值自动进入安全模式</span></div>
        <div class="field"><label for="cfg-bl">黑名单日增熔断阈值（R3）</label>
          <input type="number" id="cfg-bl" value="3"><span class="field__hint">日增超过 3% 触发全量暂停 + 人工复核</span></div>
        <div class="field"><label for="cfg-refresh">看板刷新策略（Q6）</label>
          <select id="cfg-refresh">
            <option>健康度预警：近实时（30s）</option>
            <option>漏斗/成本：小时级</option>
            <option>全部 T+1（最省资源）</option>
          </select></div>
        <div class="field"><label for="cfg-wecom">企微侧频控（IMP-030 双侧保护）</label>
          <select id="cfg-wecom">
            <option>培育 SOP：D1/D3 各 1 条（默认）</option>
            <option>严格：仅 D1</option>
          </select><span class="field__hint">过度群发把企微号搞封 = 同类风险，护栏覆盖抖音+企微双侧</span></div>
      </div>
      <div class="confirm-note">配置变更将写入审计日志。CORS 已按账号/Token 隔离（替代 v0.2 的 * 通配）。</div>
      <div class="modal__foot">
        <button type="button" class="btn" data-close>取消</button>
        <button type="button" class="btn btn--primary" data-close onclick="void 0" id="btn-save-cfg">保存</button>
      </div>
    `);
    $('#btn-save-cfg').addEventListener('click', () => { closeModal(); toast('配置已保存（演示态，未落库）'); });
  });

  /* ══════════ 弹窗基础设施 ══════════ */
  function openModal(html) {
    const modal = $('#modal'), bd = $('#modal-backdrop');
    modal.innerHTML = html;
    modal.hidden = false; bd.hidden = false;
    requestAnimationFrame(() => { modal.classList.add('show'); bd.classList.add('show'); });
    $$('[data-close]', modal).forEach((b) => b.addEventListener('click', closeModal));
    bd.onclick = closeModal;
    document.addEventListener('keydown', escClose);
  }
  function closeModal() {
    const modal = $('#modal'), bd = $('#modal-backdrop');
    modal.classList.remove('show'); bd.classList.remove('show');
    setTimeout(() => { modal.hidden = true; bd.hidden = true; }, 220);
    document.removeEventListener('keydown', escClose);
  }
  function escClose(e) { if (e.key === 'Escape') closeModal(); }

  /* ══════════ 抽屉基础设施 ══════════ */
  function openDrawer(html) {
    const drawer = $('#lead-drawer'), bd = $('#drawer-backdrop');
    drawer.innerHTML = html;
    drawer.hidden = false; bd.hidden = false;
    requestAnimationFrame(() => { drawer.classList.add('open'); bd.classList.add('show'); });
    const close = () => {
      drawer.classList.remove('open'); bd.classList.remove('show');
      setTimeout(() => { drawer.hidden = true; bd.hidden = true; }, 300);
    };
    $('.drawer__close', drawer).addEventListener('click', close);
    bd.onclick = close;
    return close;
  }

  /* ══════════ 路由 ══════════ */
  /* ══════════ 通知中心（P4-A） ══════════ */
  const NOTIF_TYPE_META = {
    new_dm:           { label: '新私信' },
    followup_due:     { label: '跟进' },
    followup_overdue: { label: '跟进逾期' },
    deal_won:         { label: '成交' },
    wechat_added:     { label: '加微' },
    account_warning:  { label: '账号预警' },
    system:           { label: '系统' },
  };
  function notifTypeLabel(t) { return (NOTIF_TYPE_META[t] || { label: t }).label; }

  function notifTime(iso) {
    try {
      const d = new Date(iso);
      const diff = (Date.now() - d.getTime()) / 1000;
      if (diff < 60) return '刚刚';
      if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
      if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
      const mm = String(d.getMonth() + 1).padStart(2, '0');
      const dd = String(d.getDate()).padStart(2, '0');
      return mm + '-' + dd;
    } catch (e) { return ''; }
  }

  async function renderBellBadge() {
    try {
      const r = await API.getUnreadCount();
      const n = (r && r.count) || 0;
      const badge = $('#bell-badge');
      if (badge) {
        if (n > 0) { badge.hidden = false; badge.textContent = n > 99 ? '99+' : String(n); }
        else { badge.hidden = true; }
      }
      const navBadge = $('#nav-notif-badge');
      if (navBadge) {
        if (n > 0) { navBadge.hidden = false; navBadge.textContent = n > 99 ? '99+' : String(n); }
        else { navBadge.hidden = true; }
      }
      return n;
    } catch (e) { return 0; }
  }

  function closeBellPanel() {
    const panel = $('#bell-panel');
    if (panel) panel.hidden = true;
    const btn = $('#btn-notifications');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  async function renderBellPanel() {
    const panel = $('#bell-panel');
    panel.innerHTML = '<div class="bell-panel__loading">加载中…</div>';
    let data;
    try { data = await API.getNotifications(false, 20); }
    catch (e) { panel.innerHTML = '<div class="bell-panel__loading">加载失败</div>'; return; }
    const items = (data && data.items) || [];
    if (!items.length) {
      panel.innerHTML = '<div class="bell-panel__empty">' + ico('bell') + '<p>暂无通知</p></div>';
      return;
    }
    const listHtml = items.map((n) => `
      <div class="bell-item ${n.read ? '' : 'bell-item--unread'}" data-notif-id="${esc(n.id)}">
        <span class="bell-item__dot"></span>
        <div class="bell-item__body">
          <div class="bell-item__top">
            <b>${esc(n.title)}</b>
            <span class="bell-item__time">${esc(notifTime(n.createdAt))}</span>
          </div>
          <div class="bell-item__content">${esc(n.content)}</div>
        </div>
      </div>`).join('');
    panel.innerHTML = `
      <div class="bell-panel__head">通知中心</div>
      <div class="bell-panel__list">${listHtml}</div>
      <div class="bell-panel__foot">
        <button type="button" class="btn-link" id="bell-read-all">全部已读</button>
        <button type="button" class="btn-link" id="bell-view-all">查看全部</button>
      </div>`;
    $$('[data-notif-id]', panel).forEach((el) => el.addEventListener('click', async () => {
      const id = el.dataset.notifId;
      try { await API.markNotificationRead(id); } catch (e) {}
      el.classList.remove('bell-item--unread');
      renderBellBadge();
    }));
    $('#bell-read-all', panel).addEventListener('click', async () => {
      try { await API.markAllNotificationsRead(); } catch (e) {}
      toast('已全部标为已读');
      closeBellPanel();
      renderBellBadge();
      if (location.hash === '#/notifications') route();
    });
    $('#bell-view-all', panel).addEventListener('click', () => {
      closeBellPanel();
      location.hash = '#/notifications';
    });
  }

  async function toggleBell() {
    const panel = $('#bell-panel');
    if (panel.hidden) {
      panel.hidden = false;
      $('#btn-notifications').setAttribute('aria-expanded', 'true');
      await renderBellPanel();
    } else {
      closeBellPanel();
    }
  }

  async function viewNotifications() {
    main.innerHTML = '<div class="view"><div class="empty"><p>加载中…</p></div></div>';
    let data;
    try { data = await API.getNotifications(false, 100); }
    catch (e) {
      main.innerHTML = '<div class="view">' + pageHead({ icon: 'bell', title: '通知中心', desc: '' }) + emptyState('bell', '加载失败，请确认后端已启动') + '</div>';
      return;
    }
    const items = (data && data.items) || [];
    const filterState = { type: 'all' };
    const FILTERS = [
      { key: 'all', label: '全部' },
      { key: 'new_dm', label: '新私信' },
      { key: 'followup', label: '跟进' },
      { key: 'deal_won', label: '成交' },
      { key: 'wechat_added', label: '加微' },
      { key: 'account_warning', label: '账号预警' },
      { key: 'system', label: '系统' },
    ];
    function matchFilter(n) {
      if (filterState.type === 'all') return true;
      if (filterState.type === 'followup') return n.type === 'followup_due' || n.type === 'followup_overdue';
      return n.type === filterState.type;
    }
    function renderList() {
      const filtered = items.filter(matchFilter);
      const listEl = $('#notif-list');
      if (!filtered.length) { listEl.innerHTML = emptyState('bell', '没有相关通知'); return; }
      listEl.innerHTML = filtered.map((n) => `
        <div class="notif-item notif-item--${esc(n.level)} ${n.read ? '' : 'notif-item--unread'}" data-notif-id="${esc(n.id)}">
          <span class="notif-item__tag">${esc(notifTypeLabel(n.type))}</span>
          <div class="notif-item__body">
            <div class="notif-item__top">
              <b>${esc(n.title)}</b>
              <span class="notif-item__time">${esc(notifTime(n.createdAt))}</span>
            </div>
            <div class="notif-item__content">${esc(n.content)}</div>
          </div>
        </div>`).join('');
      $$('[data-notif-id]', listEl).forEach((el) => el.addEventListener('click', async () => {
        const id = el.dataset.notifId;
        try { await API.markNotificationRead(id); } catch (e) {}
        el.classList.remove('notif-item--unread');
        const it = items.find((x) => x.id === id);
        if (it) it.read = true;
        renderBellBadge();
      }));
    }
    function renderTabs() {
      const tabsEl = $('#notif-tabs');
      tabsEl.innerHTML = FILTERS.map((f) =>
        `<button type="button" class="notif-tab ${filterState.type === f.key ? 'notif-tab--active' : ''}" data-nf="${f.key}">${f.label}</button>`
      ).join('');
      $$('.notif-tab', tabsEl).forEach((b) => b.addEventListener('click', () => {
        filterState.type = b.dataset.nf;
        renderTabs(); renderList();
      }));
    }
    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'bell', title: '通知中心', desc: '新私信 · 跟进 · 成交 · 加微 · 账号预警，全在这里',
          actions: '<button type="button" class="btn btn--ghost btn--sm" id="notif-read-all">全部已读</button>' })}
        <div class="card">
          <div class="notif-tabs" id="notif-tabs"></div>
          <div class="notif-list" id="notif-list"></div>
        </div>
      </div>`;
    renderTabs();
    renderList();
    $('#notif-read-all').addEventListener('click', async () => {
      try { await API.markAllNotificationsRead(); } catch (e) {}
      items.forEach((n) => { n.read = true; });
      toast('已全部标为已读');
      renderList();
      renderBellBadge();
    });
  }

  /* 通知铃铛事件绑定（P4-A） */
  $('#btn-notifications').addEventListener('click', (e) => { e.stopPropagation(); toggleBell(); });
  document.addEventListener('click', (e) => {
    const wrap = $('#bell-wrap');
    if (wrap && !wrap.contains(e.target)) closeBellPanel();
  });  const routes = {
    workbench: viewWorkbench,
    leads: viewLeads,
    dm: viewDm,
    'dm-inbox': viewDmInbox,
    comments: viewComments,
    scripts: viewScripts,
    accounts: viewAccounts,
    crawl: viewCrawl,
    customers: viewCustomers,
    followups: viewFollowups,
    funnel: viewFunnel,
    reports: viewReports,
    attribution: viewAttribution,
    health: viewHealth,
    risk: viewRisk,
    wizard: viewWizard,
    settings: viewSettings,
    monitor: viewMonitor,
    backup: viewBackup,
    notifications: viewNotifications,
  };
  function route() {
    const hash = (location.hash || '#/workbench').replace('#/', '');
    const name = routes[hash] ? hash : 'workbench';
    $$('[data-nav]').forEach((a) => a.setAttribute('aria-current', a.dataset.nav === name ? 'page' : 'false'));
    main.innerHTML = '';
    routes[name]();
    main.focus({ preventScroll: true });
  }
  window.addEventListener('hashchange', route);

  /* 通用指标卡 */
  function metric(label, value, foot, tone) {
    const color = tone === 'brand' ? 'style="color:var(--brand-deep)"' : tone === 'danger' ? 'style="color:var(--danger)"' : '';
    return `<div class="card metric">
      <span class="metric__label">${esc(label)}</span>
      <span class="metric-value" ${color}>${esc(value)}</span>
      <span class="metric__foot">${foot || ''}</span>
    </div>`;
  }

  /* SVG 折线图（无依赖） */
  function lineChart(points, { h = 180, pad = 26 } = {}) {
    const w = 640, iw = w - pad * 2, ih = h - pad * 2;
    const max = Math.max(...points.map((p) => p.v)) || 1;
    const x = (i) => pad + (i / (points.length - 1)) * iw;
    const y = (v) => pad + ih - (v / max) * ih;
    const path = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');
    const area = `${path} L${x(points.length - 1)},${pad + ih} L${pad},${pad + ih} Z`;
    const grid = [0, 0.5, 1].map((t) => `<line class="chart-grid" x1="${pad}" y1="${pad + t * ih}" x2="${w - pad}" y2="${pad + t * ih}"/><text class="chart-axis" x="${pad - 6}" y="${pad + t * ih + 3}" text-anchor="end">${Math.round(max * (1 - t))}</text>`).join('');
    const dots = points.map((p, i) => `<circle class="chart-dot" cx="${x(i).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3.2"/>`).join('');
    const labels = points.filter((_, i) => i % 3 === 0 || i === points.length - 1)
      .map((p) => { const i = points.indexOf(p); return `<text class="chart-axis" x="${x(i)}" y="${h - 6}" text-anchor="middle">${p.date}</text>`; }).join('');
    return `<svg class="chart-svg" viewBox="0 0 ${w} ${h}" role="img" aria-label="近14天加企微数折线图">
      <defs><linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#1e6e52" stop-opacity=".22"/><stop offset="1" stop-color="#1e6e52" stop-opacity="0"/>
      </linearGradient></defs>
      ${grid}<path class="chart-area" d="${area}"/><path class="chart-line" d="${path}"/>${dots}${labels}
    </svg>`;
  }

  /* ═══════════════════════════════════════════
     视图：工作台（今日运营总览）
     ═══════════════════════════════════════════ */
  async function viewWorkbench() {
    const [wb, fups] = await Promise.all([API.getWorkbench(), API.getFollowups()]);
    const t = wb.today;
    const dotColor = { ok: 'var(--ok)', info: 'var(--info)', warn: 'var(--warn)', danger: 'var(--danger)' };
    const undone = fups.filter((f) => !f.done);
    const todoTop = undone.slice(0, 3);

    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'home', title: '工作台', desc: `${esc(wb.date)} · 今天该做什么，一眼看完` })}

        <div class="grid grid--metrics">
          ${metric('今日新线索', fmt(t.newLeads), '全部触点汇总 · source 归因')}
          ${metric('待发私信', fmt(t.pendingSend), `今日已发 ${t.sentToday} 条`)}
          ${metric('今日加微 ★', fmt(t.wechatToday), '北极星 · 北极星回调实时计数', 'brand')}
          ${metric('今日成交', wan(t.deal.amount), `${t.deal.count} 单 · IMP-029 录入`)}
          ${metric('待跟进', fmt(t.followupsPending), t.followupsOverdue ? `⚠ ${t.followupsOverdue} 条已逾期` : '无逾期', t.followupsOverdue ? 'danger' : '')}
        </div>

        <div class="grid grid--main-side">
          <div class="card">
            <div class="card__head">
              <span class="card__title">需要你处理</span>
              <span class="card__hint">按严重度排序 · 点右侧跳转处理</span>
            </div>
            ${wb.alerts.map((a) => `
              <div class="alert-item">
                <span class="alert-item__dot" style="background:${dotColor[a.level]}"></span>
                <span class="alert-item__text">${esc(a.text)}</span>
                <a class="btn-link" href="${esc(a.href)}">${esc(a.action)} →</a>
              </div>`).join('')}
          </div>

          <div class="col-grid" style="min-width:0">
            <div class="card">
              <div class="card__head">
                <span class="card__title">今日待办</span>
                <a class="btn-link" href="#/followups">全部 ${undone.length} 条 →</a>
              </div>
              <div class="todo">
                ${todoTop.map((f) => `
                  <div class="todo-item ${f.overdue ? 'todo-item--overdue' : ''}">
                    <span class="timeline__dot" style="background:${f.overdue ? 'var(--danger)' : 'var(--brand)'};margin:0"></span>
                    <div class="todo__text"><b>${esc(f.customer)} · ${esc(f.type)}</b>
                      <span class="todo__meta">${esc(f.text)}</span></div>
                    <span class="todo__due ${f.overdue ? 'todo__due--overdue' : ''}">${esc(f.due)}</span>
                  </div>`).join('')}
              </div>
            </div>
            <div class="card">
              <div class="card__head">
                <span class="card__title">实时动态</span>
                <a class="btn-link" href="#/risk">审计日志 →</a>
              </div>
              <ul class="timeline">
                ${wb.activity.slice(0, 4).map((e) => `
                  <li>
                    <span class="timeline__dot" style="background:${dotColor[e.level]}"></span>
                    <span>${esc(e.text)}</span>
                    <span class="timeline__time num">${esc(e.time)}</span>
                  </li>`).join('')}
              </ul>
            </div>
          </div>
        </div>
      </div>`;
  }

  /* ═══════════════════════════════════════════
     视图：线索池（leads 状态机 + source 归因）
     ═══════════════════════════════════════════ */
  async function viewLeads() {
    [state.leadStatusMeta, state.sourceMeta, state.leads] = await Promise.all([
      API.getLeadStatusMeta(), API.getSourceMeta(), API.getLeads(),
    ]);
    renderLeads();
  }

  function renderLeads() {
    const meta = state.leadStatusMeta;
    const srcMeta = state.sourceMeta;
    const filters = [
      { key: 'all', label: '全部', count: state.leads.length },
      ...Object.entries(meta).map(([key, m]) => ({
        key, label: m.label,
        count: state.leads.filter((l) => l.status === key).length,
      })).filter((f) => f.count > 0),
    ];
    const list = state.leadsFilter === 'all' ? state.leads : state.leads.filter((l) => l.status === state.leadsFilter);

    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'leads', title: '线索池', desc: '全部触点汇入同一漏斗 · 状态机：采集 → 待发 → 已发 → 回复 → 加微 ★ → 成交' })}
        <div class="filter-bar" role="group" aria-label="按状态筛选">
          ${filters.map((f) => `
            <button type="button" class="filter-chip" data-filter="${f.key}" aria-pressed="${state.leadsFilter === f.key}">
              ${esc(f.label)}<span class="count num">${f.count}</span>
            </button>`).join('')}
        </div>
        <div class="card">
          <div class="table-wrap">
            <table class="data">
              <thead><tr>
                <th>用户</th><th>评论/留言</th><th>来源触点</th><th>意向</th><th>执行账号</th><th>状态</th><th>时间</th><th></th>
              </tr></thead>
              <tbody>
                ${list.length ? list.map((l) => leadRow(l, meta, srcMeta)).join('')
                  : `<tr><td colspan="8"><div class="empty">${emptyState('leads', '该状态下暂无线索')}</div></td></tr>`}
              </tbody>
            </table>
          </div>
        </div>
      </div>`;

    $$('.filter-chip').forEach((b) => b.addEventListener('click', () => {
      state.leadsFilter = b.dataset.filter;
      renderLeads();
    }));
    $$('[data-lead-id]').forEach((b) => b.addEventListener('click', () => openLeadDrawer(b.dataset.leadId)));
    $$('[data-mark-wechat]').forEach((b) => b.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = b.dataset.markWechat;
      await API.markWechatAdded(id);
      state.leads = await API.getLeads();
      toast('已手动标记加微（manual=true），北极星 +1');
      renderLeads();
    }));
  }

  function leadRow(l, meta, srcMeta) {
    const intentTag = { high: '<span class="tag tag--high">高意向</span>', mid: '<span class="tag tag--mid">中意向</span>', low: '<span class="tag tag--low">低意向</span>' }[l.intent];
    const st = meta[l.status] || { label: l.status, cls: '' };
    const src = srcMeta[l.source] || { label: l.source, cls: 'collected' };
    const isDanger = l.status === 'send_failed' || l.status === 'throttled';
    const canMark = ['sent', 'replied'].includes(l.status);
    return `
      <tr class="${isDanger ? 'row--danger' : ''}" data-lead-id="${l.id}" style="cursor:pointer">
        <td><div class="cell-user">
          <span class="avatar" style="background:hsl(${l.hue} 45% 44%)">${esc(l.nickname.slice(0, 1))}</span>
          <span style="font-weight:600">${esc(l.nickname)}</span>
        </div></td>
        <td class="cell-comment" title="${esc(l.comment)}">${esc(l.comment)}</td>
        <td><span class="tag tag--${src.cls}">${esc(src.label)}</span></td>
        <td>${intentTag}</td>
        <td style="color:var(--ink-soft)">${l.account ? esc(l.account) : '—'}</td>
        <td><span class="tag tag--${st.cls}">${st.label}${l.manual ? ' <small>(手标)</small>' : ''}</span></td>
        <td class="num" style="color:var(--ink-faint)">${esc(l.createdAt)}</td>
        <td>${canMark && !state.safeMode.active ? `<button type="button" class="btn-link" data-mark-wechat="${l.id}" title="IMP-002 降级入口：无直连回调时手动标记">标记加微</button>` : ''}</td>
      </tr>`;
  }

  /* 线索详情抽屉 */
  function openLeadDrawer(leadId) {
    const l = state.leads.find((x) => x.id === leadId);
    if (!l) return;
    const meta = state.leadStatusMeta;
    const src = state.sourceMeta[l.source] || { label: l.source, cls: 'collected' };
    const st = meta[l.status] || { label: l.status, cls: '' };
    const close = openDrawer(`
      <div class="drawer__head">
        <span class="avatar" style="background:hsl(${l.hue} 45% 44%);width:34px;height:34px;font-size:14px">${esc(l.nickname.slice(0, 1))}</span>
        <div class="drawer__title">
          ${esc(l.nickname)}
          <span class="tag tag--${st.cls}" style="margin-left:8px;vertical-align:2px">${st.label}</span>
        </div>
        <button type="button" class="drawer__close" aria-label="关闭">×</button>
      </div>
      <div class="drawer__body">
        <dl class="kv">
          <dt>线索 ID</dt><dd class="num">${esc(l.id)}</dd>
          <dt>来源触点</dt><dd><span class="tag tag--${src.cls}">${esc(src.label)}</span></dd>
          <dt>评论内容</dt><dd>“${esc(l.comment)}”</dd>
          <dt>来源视频</dt><dd>${esc(l.video)}</dd>
          <dt>意向信号</dt><dd>${{ high: '高', mid: '中', low: '低' }[l.intent]}</dd>
          <dt>执行账号</dt><dd>${l.account ? esc(l.account) : '未分配'}</dd>
          <dt>采集时间</dt><dd class="num">${esc(l.createdAt)}</dd>
          ${l.dealAmount ? `<dt>成交金额</dt><dd class="num" style="color:var(--ok);font-weight:700">¥${fmt(l.dealAmount)}</dd>` : ''}
          ${l.wechatAddedAt ? `<dt>加微时间</dt><dd class="num">${esc(l.wechatAddedAt)}${l.manual ? '（手动标记）' : '（自动回调）'}</dd>` : ''}
          ${l.referralNote ? `<dt>转介绍</dt><dd style="color:var(--brand-deep)">${esc(l.referralNote)}</dd>` : ''}
          ${l.throttledNote ? `<dt>暂缓原因</dt><dd style="color:var(--warn)">${esc(l.throttledNote)}</dd>` : ''}
          ${l.failNote ? `<dt>失败原因</dt><dd style="color:var(--danger)">${esc(l.failNote)}</dd>` : ''}
          ${l.rejectReason ? `<dt>过滤原因</dt><dd>${esc(l.rejectReason)}</dd>` : ''}
        </dl>

        ${(l.messages || []).length ? `
          <div>
            <div class="card__title" style="margin-bottom:10px">私信记录</div>
            <div class="msg-list">
              ${(l.messages || []).map((m) => `
                <div class="msg msg--${m.dir === 'out' ? 'out' : 'in'}">
                  ${esc(m.text)}
                  <div class="msg__meta">${m.dir === 'out' ? '系统发送' : '用户'} · ${esc(m.time)}${m.variant ? ` · 变体${m.variant}` : ''}</div>
                </div>`).join('')}
            </div>
          </div>` : emptyState('dm', '暂无私信记录')}

        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
          <button type="button" class="btn btn--ghost" id="drawer-ai-analyze" style="border-color:var(--brand);color:var(--brand-deep)">
            <span class="ico ico--sm" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#i-rocket"/></svg></span>AI 画像分析
          </button>
          ${['sent', 'replied'].includes(l.status) && !state.safeMode.active ? `
            <button type="button" class="btn btn--primary" id="drawer-mark">
              手动标记「已加企微」（IMP-002 降级入口）
            </button>` : ''}
        </div>
        <div id="drawer-ai-result" style="margin-top:10px"></div>
      </div>`);

    const markBtn = $('#drawer-mark');
    if (markBtn) markBtn.addEventListener('click', async () => {
      await API.markWechatAdded(l.id);
      state.leads = await API.getLeads();
      close(); toast('已手动标记加微，北极星 +1'); renderLeads();
    });

    // AI 画像分析
    const aiBtn = $('#drawer-ai-analyze');
    if (aiBtn) aiBtn.addEventListener('click', async () => {
      if (!(await aiEnsureConfigured())) return;
      aiBtn.disabled = true; aiBtn.textContent = '分析中…';
      const resultEl = $('#drawer-ai-result');
      if (resultEl) resultEl.innerHTML = '<div style="color:var(--ink-soft);font-size:13px">AI 正在分析线索画像…</div>';
      try {
        const result = await API.aiAnalyzeLead({
          nickname: l.nickname,
          comment: l.comment || '',
          video_title: l.video || '',
          source_keyword: l.sourceKeyword || '',
        });
        const levelColor = { A: 'var(--danger)', B: 'var(--warn)', C: 'var(--ink-soft)' }[result.intent_level] || 'var(--ink-soft)';
        const tagsHtml = (result.tags || []).map((t) => `<span class="tag tag--low" style="margin:2px 4px 2px 0">${esc(t)}</span>`).join('');
        if (resultEl) resultEl.innerHTML = `
          <div style="background:var(--brand-tint-2);border-radius:10px;padding:12px 14px;font-size:13px;line-height:1.7">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
              <span style="font-weight:700;color:var(--brand-deep)">AI 画像分析结果</span>
              <span style="color:${levelColor};font-weight:700;font-size:15px">意向 ${result.intent_level}</span>
            </div>
            <div><b>核心需求：</b>${esc(result.customer_need || '—')}</div>
            <div style="margin:4px 0"><b>需求标签：</b>${tagsHtml || '—'}</div>
            <div><b>推荐话术：</b>${esc(result.recommended_script_category || '—')}</div>
            <div><b>跟进建议：</b>${esc(result.follow_up_suggestion || '—')}</div>
            ${result.raw_reasoning ? `<div style="color:var(--ink-faint);font-size:11px;margin-top:4px">判断依据：${esc(result.raw_reasoning)}</div>` : ''}
          </div>`;
        toast('画像分析完成，已自动回写线索标签', 'ok');
        // 刷新线索列表
        state.leads = await API.getLeads();
      } catch (e) {
        aiHandleError(e);
        if (resultEl) resultEl.innerHTML = '';
      } finally {
        aiBtn.disabled = false;
        aiBtn.innerHTML = '<span class="ico ico--sm" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#i-rocket"/></svg></span>AI 画像分析';
      }
    });
  }

  /* ═══════════════════════════════════════════
     视图：私信任务（IMP-001 执行器 · 队列+对话）
     ═══════════════════════════════════════════ */
﻿  async function viewDm() {
    // 离开本视图时清理批量轮询
    if (window._dmBatchTimer) { clearInterval(window._dmBatchTimer); window._dmBatchTimer = null; }

    const [queue, meta, cfg, results] = await Promise.all([
      API.getDmQueue(),
      API.getLeadStatusMeta(),
      (API.getDmSenderConfig ? API.getDmSenderConfig() : Promise.resolve(null)),
      (API.fetchDmSendResults ? API.fetchDmSendResults(50) : Promise.resolve([])),
    ]);
    state.leads = await API.getLeads();
    state.leadStatusMeta = meta;

    const cnt = (k) => queue.filter((q) => q.status === k).length;
    const convs = state.leads.filter((l) => (l.messages || []).length > 0);

    // ── 发送器配置（默认值兜底） ──
    const sCfg = cfg || { mode: 'mock', minInterval: 30, maxInterval: 60, dailyLimit: 50 };
    const isMock = sCfg.mode !== 'cdp';

    // ── 发送控制区 ──
    const senderPanel = `
      <div class="card dm-sender">
        <div class="card__head">
          <span class="card__title">发送控制</span>
          <span class="card__hint">私信发送执行器 · P4-D</span>
        </div>
        ${isMock ? '<div class="dm-sender__mockbar">当前为模拟发送（Mock），不会真实发送私信</div>'
                 : '<div class="dm-sender__mockbar dm-sender__mockbar--cdp">当前为 CDP 模式（联调中），真实发送待 Electron 对接</div>'}
        <div class="dm-sender__row">
          <div class="dm-sender__field">
            <label for="dm-sender-mode">发送器模式</label>
            <select id="dm-sender-mode">
              <option value="mock" ${isMock ? 'selected' : ''}>Mock 模拟</option>
              <option value="cdp" ${!isMock ? 'selected' : ''}>CDP（联调中）</option>
            </select>
          </div>
          <div class="dm-sender__field">
            <label for="dm-sender-min">最小间隔(秒)</label>
            <input type="number" id="dm-sender-min" value="${esc(sCfg.minInterval)}" min="0">
          </div>
          <div class="dm-sender__field">
            <label for="dm-sender-max">最大间隔(秒)</label>
            <input type="number" id="dm-sender-max" value="${esc(sCfg.maxInterval)}" min="0">
          </div>
          <div class="dm-sender__field">
            <label for="dm-sender-daily">每日上限</label>
            <input type="number" id="dm-sender-daily" value="${esc(sCfg.dailyLimit)}" min="1">
          </div>
          <div class="dm-sender__field dm-sender__field--btn">
            <button type="button" class="btn" id="dm-sender-save-cfg">保存配置</button>
          </div>
        </div>
        <div class="dm-sender__run">
          <button type="button" class="btn btn--primary" id="dm-batch-start">开始批量发送</button>
          <button type="button" class="btn" id="dm-batch-stop" disabled>停止发送</button>
          <button type="button" class="btn" id="dm-sender-test">测试发送器</button>
          <span class="dm-sender__prog" id="dm-batch-prog">就绪</span>
        </div>
        <div class="dm-sender__bar"><div class="dm-sender__bar-fill" id="dm-batch-bar" style="width:0%"></div></div>
        <div class="dm-sender__counts">
          <span class="dm-count dm-count--ok">成功 <b id="dm-c-ok">0</b></span>
          <span class="dm-count dm-count--fail">失败 <b id="dm-c-fail">0</b></span>
          <span class="dm-count dm-count--thr">频控 <b id="dm-c-thr">0</b></span>
        </div>
      </div>`;

    // ── 最近发送记录 ──
    const leadNick = {};
    state.leads.forEach((l) => { leadNick[l.id] = l.nickname; });
    const _DM_STATUS_CLS = {
      success: 'success', failed: 'failed', throttled: 'throttled',
      need_captcha: 'need_captcha', account_banned: 'account_banned',
    };
    const resultsRows = (results || []).map((r) => {
      const nick = r.leadId === '__test__' ? '（测试）' : (leadNick[r.leadId] || (r.leadId || '').slice(0, 6));
      const stCls = _DM_STATUS_CLS[r.status] || '';
      return `<tr>
        <td style="white-space:nowrap">${esc(_fmtInboxTime(r.createdAt))}</td>
        <td>${esc(nick)}</td>
        <td style="color:var(--ink-soft)">${esc(r.accountId && r.accountId !== '__test__' ? r.accountId.slice(0, 6) : '-')}</td>
        <td style="max-width:220px;color:var(--ink-soft)">${esc((r.content || '').slice(0, 32))}</td>
        <td><span class="dm-status dm-status--${stCls}">${esc(r.status)}</span></td>
        <td style="color:var(--ink-soft)">${esc(r.message)}</td>
      </tr>`;
    }).join('');

    const resultsCard = `
      <div class="card">
        <div class="card__head">
          <span class="card__title">最近发送记录</span>
          <span class="card__hint">发送结果留痕 · 状态颜色：成功绿 / 失败红 / 频控黄 / 验证码橙</span>
        </div>
        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>时间</th><th>线索</th><th>账号</th><th>内容摘要</th><th>状态</th><th>消息</th></tr></thead>
            <tbody>${resultsRows || `<tr><td colspan="6">${emptyState('dm', '暂无发送记录')}</td></tr>`}</tbody>
          </table>
        </div>
      </div>`;

    main.innerHTML = `
      <div class="view">
        ${pageHead({
          icon: 'dm', title: '私信任务', desc: '发送队列 + 对话承接 · 安全模式下全队列只读',
          actions: state.safeMode.active ? '<span class="tag tag--safemode">安全模式中 · 队列冻结</span>' : '',
        })}

        <div class="grid grid--metrics">
          ${metric('待发送', fmt(cnt('pending_outreach')), '按随机间隔拟人发送')}
          ${metric('频控暂缓', fmt(cnt('throttled')), 'R1 降速队列回放')}
          ${metric('失败待恢复', fmt(cnt('send_failed')), '账号恢复后自动重试')}
          ${metric('已发待回复', fmt(cnt('sent')), '超过 48h 无回复转培育')}
          ${metric('待人工回复', fmt(cnt('replied')), '用户已回话，别让线索凉掉', cnt('replied') ? 'danger' : '')}
        </div>

        ${senderPanel}

        <div class="card">
          <div class="card__head">
            <span class="card__title">发送队列</span>
            <span class="card__hint">执行器状态与话术变体分配（IMP-001）</span>
          </div>
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>用户</th><th>执行账号</th><th>话术</th><th>状态</th><th>说明</th><th></th></tr></thead>
              <tbody>
                ${queue.map((q) => {
                  const st = meta[q.status] || { label: q.status, cls: '' };
                  const isDanger = ['throttled', 'send_failed'].includes(q.status);
                  const canSend = q.status === 'pending_outreach' && !state.safeMode.active;
                  return `
                    <tr class="${isDanger ? 'row--danger' : ''}" data-lead-id="${q.leadId}" style="cursor:pointer">
                      <td><div class="cell-user">
                        <span class="avatar" style="background:hsl(${q.hue} 45% 44%)">${esc(q.nickname.slice(0, 1))}</span>
                        <span style="font-weight:600">${esc(q.nickname)}</span>
                      </div></td>
                      <td style="color:var(--ink-soft)">${esc(q.account)}</td>
                      <td><span class="tag tag--low">变体 ${esc(q.variant)}</span></td>
                      <td><span class="tag tag--${st.cls}">${st.label}</span></td>
                      <td style="color:var(--ink-soft);max-width:260px">${esc(q.detail)}</td>
                      <td>${canSend ? `<button type="button" class="btn-link" data-send-now="${q.leadId}">立即发送</button>`
                        : `<button type="button" class="btn-link">查看对话</button>`}</td>
                    </tr>`;
                }).join('')}
              </tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <div class="card__head">
            <span class="card__title">进行中的对话</span>
            <span class="card__hint">点开查看完整私信记录与加微引导过程</span>
          </div>
          ${convs.length ? convs.map((l) => {
            const last = (l.messages || [])[(l.messages || []).length - 1] || {};
            return `
              <div class="dm-conv" data-lead-id="${l.id}">
                <span class="avatar" style="background:hsl(${l.hue} 45% 44%)">${esc(l.nickname.slice(0, 1))}</span>
                <div style="min-width:0">
                  <div style="font-weight:600">${esc(l.nickname)}
                    <span class="msg__meta" style="margin-left:6px">${last.dir === 'out' ? '我方' : '用户'} · ${esc(last.time)}</span></div>
                  <div class="dm-conv__last">${esc(last.text)}</div>
                </div>
                <span class="tag tag--${(state.leadStatusMeta[l.status] || {}).cls}">${esc((state.leadStatusMeta[l.status] || {}).label || l.status)}</span>
              </div>`;
          }).join('') : emptyState('dm', '暂无对话')}
        </div>

        ${resultsCard}
      </div>`;

    // ── 批量进度渲染辅助 ──
    function renderBatchStatus(s) {
      if (!s) return;
      const bar = $('#dm-batch-bar'), prog = $('#dm-batch-prog');
      const ok = $('#dm-c-ok'), fail = $('#dm-c-fail'), thr = $('#dm-c-thr');
      const stopBtn = $('#dm-batch-stop'), startBtn = $('#dm-batch-start');
      if (bar) bar.style.width = s.total ? Math.round((s.done / s.total) * 100) + '%' : '0%';
      if (prog) prog.textContent = s.running
        ? `发送中 ${s.done}/${s.total}`
        : (s.total ? `已完成 ${s.done}/${s.total}` : '就绪');
      if (ok) ok.textContent = s.success || 0;
      if (fail) fail.textContent = s.failed || 0;
      if (thr) thr.textContent = s.throttled || 0;
      if (stopBtn) stopBtn.disabled = !s.running;
      if (startBtn) startBtn.disabled = !!s.running;
    }

    function startBatchPolling() {
      if (window._dmBatchTimer) clearInterval(window._dmBatchTimer);
      window._dmBatchTimer = setInterval(async () => {
        try {
          const s = await API.fetchDmBatchStatus();
          renderBatchStatus(s);
          if (!s.running) {
            clearInterval(window._dmBatchTimer);
            window._dmBatchTimer = null;
          }
        } catch (e) { /* 轮询失败静默，下次重试 */ }
      }, 1200);
    }

    // ── 事件绑定 ──
    $$('[data-send-now]').forEach((b) => b.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (state.safeMode.active) { toast('安全模式中，队列冻结', 'warn'); return; }
      const leadId = b.dataset.sendNow;
      b.disabled = true; b.textContent = '发送中…';
      try {
        const out = await API.sendDmOne(leadId);
        toast('发送结果：' + out.status + (out.message ? '（' + out.message + '）' : ''),
              out.status === 'success' ? 'ok' : 'warn');
      } catch (err) {
        toast('发送失败：' + (err.message || err), 'warn');
      }
      route();
    }));

    $('#dm-batch-start')?.addEventListener('click', async () => {
      if (state.safeMode.active) { toast('安全模式中，队列冻结', 'warn'); return; }
      try {
        const s = await API.sendDmBatch(10);
        renderBatchStatus(s);
        if (s.running) { toast('批量发送已启动（后台执行）'); startBatchPolling(); }
        else { toast(s.message || '没有待发送线索', 'warn'); }
      } catch (err) { toast('启动失败：' + (err.message || err), 'warn'); }
    });

    $('#dm-batch-stop')?.addEventListener('click', async () => {
      try { await API.stopDmBatch(); toast('已请求停止（当前条发完后停）', 'warn'); }
      catch (err) { toast('停止失败：' + (err.message || err), 'warn'); }
    });

    $('#dm-sender-test')?.addEventListener('click', async () => {
      try {
        const out = await API.testDmSender();
        toast('测试发送：' + out.status + (out.message ? '（' + out.message + '）' : ''),
              out.status === 'success' ? 'ok' : 'warn');
      } catch (err) { toast('测试失败：' + (err.message || err), 'warn'); }
    });

    $('#dm-sender-save-cfg')?.addEventListener('click', async () => {
      const payload = {
        mode: $('#dm-sender-mode').value,
        minInterval: parseInt($('#dm-sender-min').value, 10) || 0,
        maxInterval: parseInt($('#dm-sender-max').value, 10) || 0,
        dailyLimit: parseInt($('#dm-sender-daily').value, 10) || 50,
      };
      try {
        await API.updateDmSenderConfig(payload);
        toast('发送器配置已保存');
      } catch (err) { toast('保存失败：' + (err.message || err), 'warn'); }
    });

    $$('[data-lead-id]').forEach((b) => b.addEventListener('click', () => openLeadDrawer(b.dataset.leadId)));
  }

  /* ═══════════════════════════════════════════
     视图：私信收件箱（评论引流转化闭环）
     ═══════════════════════════════════════════ */
  const INBOX_STATUS_META = {
    new: { label: '新会话', cls: 'pending' },
    replied: { label: '已回复', cls: 'sent' },
    wechat_added: { label: '已加微', cls: 'wechat' },
    closed: { label: '已关闭', cls: 'rejected' },
  };

  function _fmtInboxTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const pad = (n) => String(n).padStart(2, '0');
    if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  async function viewDmInbox() {
    const inboxState = {
      conversations: [],
      activeId: null,
      detail: null,
      scripts: [],
      filter: 'all',
    };

    // 加载话术库（私信 + 微信引导类）用于快捷插入
    try {
      const allScripts = await API.getScripts();
      inboxState.scripts = allScripts.filter((s) =>
        ['private_message', 'wechat_guide', 'objection'].includes(s.category)
      );
    } catch (e) {
      inboxState.scripts = [];
    }

    async function loadList() {
      inboxState.conversations = await API.getDmInbox(inboxState.filter === 'all' ? undefined : inboxState.filter);
      renderList();
    }

    async function loadDetail(convId) {
      inboxState.activeId = convId;
      inboxState.detail = await API.getDmConversation(convId);
      renderList();
      renderDetail();
    }

    function renderList() {
      const listEl = $('#inbox-list');
      if (!listEl) return;
      const totalUnread = inboxState.conversations.reduce((s, c) => s + (c.unreadCount || 0), 0);
      const filterTabs = ['all', 'new', 'replied', 'wechat_added', 'closed'].map((k) => {
        const count = k === 'all' ? inboxState.conversations.length
          : inboxState.conversations.filter((c) => c.status === k).length;
        const label = k === 'all' ? '全部' : (INBOX_STATUS_META[k] || {}).label || k;
        return `<button type="button" class="subtab-btn" data-inbox-filter="${k}" aria-selected="${inboxState.filter === k}">${esc(label)}<span class="count">${count}</span></button>`;
      }).join('');

      listEl.innerHTML = `
        <div class="subtabs" role="tablist" style="padding:10px 14px 0">${filterTabs}</div>
        <div style="overflow-y:auto;flex:1">
          ${inboxState.conversations.length ? inboxState.conversations.map((c) => {
            const meta = INBOX_STATUS_META[c.status] || { label: c.status, cls: 'low' };
            const isActive = c.id === inboxState.activeId;
            return `
              <div class="dm-conv" data-inbox-id="${c.id}" style="${isActive ? 'background:var(--brand-tint-2);border-left:3px solid var(--brand-deep)' : 'border-left:3px solid transparent'}">
                <span class="avatar" style="background:hsl(${c.avatarHue || 200} 45% 44%)">${esc((c.nickname || '?').slice(0, 1))}</span>
                <div style="min-width:0;flex:1">
                  <div style="display:flex;align-items:center;gap:6px">
                    <span style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.nickname)}</span>
                    ${c.unreadCount > 0 ? `<span class="nav-badge nav-badge--count" style="background:var(--danger);color:#fff;min-width:18px;height:18px;padding:0 5px;font-size:11px;line-height:18px">${c.unreadCount}</span>` : ''}
                    <span class="tag tag--${meta.cls}" style="margin-left:auto;flex-shrink:0">${esc(meta.label)}</span>
                  </div>
                  <div class="dm-conv__last">${esc(c.lastMessage || '')}</div>
                  <div style="font-size:11px;color:var(--ink-faint);margin-top:2px">${esc(_fmtInboxTime(c.lastMessageAt))}</div>
                </div>
              </div>`;
          }).join('') : `<div class="empty" style="padding:40px 0"><span class="empty__ico">${ico('empty')}</span><p>暂无会话</p></div>`}
        </div>`;

      $$('[data-inbox-id]', listEl).forEach((el) => {
        el.addEventListener('click', () => loadDetail(el.dataset.inboxId));
      });
      $$('[data-inbox-filter]', listEl).forEach((btn) => {
        btn.addEventListener('click', () => {
          inboxState.filter = btn.dataset.inboxFilter;
          loadList();
        });
      });
    }

    function renderDetail() {
      const detailEl = $('#inbox-detail');
      if (!detailEl) return;
      const d = inboxState.detail;
      if (!d) {
        detailEl.innerHTML = `<div class="empty" style="height:100%"><span class="empty__ico">${ico('dm')}</span><p>选择左侧会话查看私信</p></div>`;
        return;
      }
      const meta = INBOX_STATUS_META[d.status] || { label: d.status, cls: 'low' };
      const scriptOptions = inboxState.scripts.length
        ? inboxState.scripts.map((s) => {
            const firstVariant = (s.variants && s.variants[0]) ? s.variants[0].text : (s.intro || '');
            return `<option value="${esc(firstVariant)}" data-script-id="${esc(s.id)}">${esc(s.name)}（${esc(s.category)}）</option>`;
          }).join('')
        : '';

      detailEl.innerHTML = `
        <div style="display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--line);flex-shrink:0">
          <span class="avatar" style="background:hsl(${d.avatarHue || 200} 45% 44%)">${esc((d.nickname || '?').slice(0, 1))}</span>
          <div style="flex:1;min-width:0">
            <div style="font-weight:600">${esc(d.nickname)}</div>
            <div style="font-size:12px;color:var(--ink-soft)"><span class="tag tag--${meta.cls}">${esc(meta.label)}</span></div>
          </div>
          ${d.status !== 'wechat_added' && d.status !== 'closed' ? `
            <button type="button" class="btn btn--primary btn--sm" id="btn-mark-wechat">
              <span class="ico ico--sm" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#i-check"/></svg></span>标记加微转客户
            </button>` : `<span class="tag tag--wechat">已转化为客户</span>`}
        </div>
        <div class="msg-list" id="inbox-msg-list" style="flex:1;overflow-y:auto;padding:16px 18px">
          ${d.messages.map((m) => `
            <div class="msg msg--${m.direction === 'out' ? 'out' : 'in'}">
              ${esc(m.content)}
              <div class="msg__meta">${m.direction === 'out' ? '我方' : '用户'} · ${esc(_fmtInboxTime(m.createdAt))}${m.scriptId ? ' · 话术' : ''}</div>
            </div>`).join('')}
        </div>
        <div style="padding:12px 18px;border-top:1px solid var(--line);flex-shrink:0;background:var(--paper)">
          ${scriptOptions ? `
            <div style="margin-bottom:8px">
              <select id="inbox-script-select" style="width:100%;font-size:12.5px">
                <option value="">— 选择话术快捷插入 —</option>
                ${scriptOptions}
              </select>
            </div>` : ''}
          <div style="display:flex;gap:8px;align-items:flex-end">
            <textarea id="inbox-reply-input" placeholder="输入回复内容…（Enter 发送，Shift+Enter 换行）" style="flex:1;min-height:60px"></textarea>
            <div style="display:flex;flex-direction:column;gap:6px">
              <select id="inbox-ai-style" style="font-size:11px;padding:2px 4px">
                <option value="formal">更正式</option>
                <option value="casual" selected>更口语</option>
                <option value="short">更简短</option>
              </select>
              <button type="button" class="btn btn--ghost btn--sm" id="btn-ai-polish" style="border-color:var(--brand);color:var(--brand-deep);white-space:nowrap">
                <span class="ico ico--sm" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#i-rocket"/></svg></span>AI 润色
              </button>
            </div>
            <button type="button" class="btn btn--primary" id="btn-send-reply" style="height:60px;padding:0 20px">发送</button>
          </div>
        </div>`;

      // 自动滚动到底部
      const msgList = $('#inbox-msg-list', detailEl);
      if (msgList) msgList.scrollTop = msgList.scrollHeight;

      // 发送回复
      $('#btn-send-reply', detailEl)?.addEventListener('click', sendReply);
      const replyInput = $('#inbox-reply-input', detailEl);
      replyInput?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendReply(); }
      });

      // AI 润色
      const polishBtn = $('#btn-ai-polish', detailEl);
      if (polishBtn) polishBtn.addEventListener('click', async () => {
        if (!(await aiEnsureConfigured())) return;
        const styleSel = $('#inbox-ai-style', detailEl);
        const style = styleSel ? styleSel.value : 'casual';
        const currentText = replyInput ? replyInput.value.trim() : '';
        polishBtn.disabled = true; polishBtn.textContent = '润色中…';
        try {
          const result = await API.aiSmartDraft({
            lead_id: d.leadId || d.id || '',
            script_category: 'private_message',
            user_comment: currentText,
            style,
          });
          if (result && result.draft) {
            if (replyInput) { replyInput.value = result.draft; replyInput.focus(); }
            toast('AI 润色完成，可编辑后发送', 'ok');
          }
        } catch (e) {
          aiHandleError(e);
        } finally {
          polishBtn.disabled = false;
          polishBtn.innerHTML = '<span class="ico ico--sm" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#i-rocket"/></svg></span>AI 润色';
        }
      });

      // 话术快捷插入
      $('#inbox-script-select', detailEl)?.addEventListener('change', (e) => {
        if (e.target.value && replyInput) {
          replyInput.value = e.target.value;
          replyInput.focus();
        }
      });

      // 标记加微
      $('#btn-mark-wechat', detailEl)?.addEventListener('click', openMarkWechatModal);
    }

    async function sendReply() {
      const input = $('#inbox-reply-input');
      const content = input ? input.value.trim() : '';
      if (!content) { toast('回复内容不能为空', 'warn'); return; }
      const scriptSelect = $('#inbox-script-select');
      const selectedOption = scriptSelect ? scriptSelect.options[scriptSelect.selectedIndex] : null;
      const scriptId = selectedOption && selectedOption.value ? (selectedOption.dataset.scriptId || null) : null;
      try {
        await API.sendDmReply(inboxState.activeId, content, scriptId);
        toast('回复已发送');
        await loadDetail(inboxState.activeId);
        await loadList();
      } catch (e) {
        toast('发送失败：' + e.message, 'warn');
      }
    }

    function openMarkWechatModal() {
      const d = inboxState.detail;
      openModal(`
        <div class="modal__head">
          <h3 id="modal-title">标记加微 · 转客户</h3>
          <button type="button" class="drawer__close" data-close aria-label="关闭">×</button>
        </div>
        <div class="modal__body">
          <div class="field">
            <label>微信号 <span style="color:var(--danger)">*</span></label>
            <input type="text" id="mw-wechat-id" placeholder="输入客户微信号" value="">
          </div>
          <div class="field">
            <label>客户姓名</label>
            <input type="text" id="mw-customer-name" placeholder="留空则使用昵称：${esc(d.nickname)}" value="${esc(d.nickname)}">
          </div>
          <div class="field">
            <label>手机号（可选）</label>
            <input type="text" id="mw-phone" placeholder="选填">
          </div>
          <p style="font-size:12px;color:var(--ink-soft);margin-top:8px">提交后将：关闭会话 · 线索标记为已加微 · 自动创建客户</p>
        </div>
        <div class="modal__foot" style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
          <button type="button" class="btn btn--ghost" data-close>取消</button>
          <button type="button" class="btn btn--primary" id="mw-submit">确认加微</button>
        </div>`);
      $('#mw-submit').addEventListener('click', async () => {
        const wechatId = $('#mw-wechat-id').value.trim();
        if (!wechatId) { toast('请输入微信号', 'warn'); return; }
        try {
          const res = await API.markDmWechat(inboxState.activeId, {
            wechatId,
            customerName: $('#mw-customer-name').value.trim() || null,
            phone: $('#mw-phone').value.trim() || null,
          });
          closeModal();
          toast('已标记加微，客户已创建');
          setTimeout(() => { location.hash = '#/customers'; }, 600);
        } catch (e) {
          toast('操作失败：' + e.message, 'warn');
        }
      });
    }

    // 初始渲染
    main.innerHTML = `
      <div class="view">
        ${pageHead({
          icon: 'dm', title: '私信收件箱', desc: '用户主动私信承接 · 回复 → 加微 → 转客户，全链路闭环',
          actions: '<button type="button" class="btn btn--ghost btn--sm" id="btn-simulate-dm">模拟收到私信</button>',
        })}
        <div class="card" style="padding:0;display:flex;height:calc(100vh - 220px);min-height:480px;overflow:hidden">
          <div id="inbox-list" style="width:300px;border-right:1px solid var(--line);display:flex;flex-direction:column;flex-shrink:0"></div>
          <div id="inbox-detail" style="flex:1;display:flex;flex-direction:column;min-width:0"></div>
        </div>
      </div>`;

    $('#btn-simulate-dm').addEventListener('click', () => {
      openModal(`
        <div class="modal__head">
          <h3 id="modal-title">模拟收到私信（开发测试）</h3>
          <button type="button" class="drawer__close" data-close aria-label="关闭">×</button>
        </div>
        <div class="modal__body">
          <div class="field"><label>用户昵称</label><input type="text" id="sim-nickname" placeholder="如：装修咨询小王"></div>
          <div class="field"><label>私信内容</label><textarea id="sim-content" placeholder="如：你好，请问多少钱？"></textarea></div>
        </div>
        <div class="modal__foot" style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
          <button type="button" class="btn btn--ghost" data-close>取消</button>
          <button type="button" class="btn btn--primary" id="sim-submit">模拟发送</button>
        </div>`);
      $('#sim-submit').addEventListener('click', async () => {
        const nickname = $('#sim-nickname').value.trim() || '测试用户';
        const content = $('#sim-content').value.trim() || '你好，咨询一下';
        try {
          await API.simulateDmInbound({ nickname, content });
          closeModal();
          toast('已模拟收到私信');
          await loadList();
        } catch (e) {
          toast('模拟失败：' + e.message, 'warn');
        }
      });
    });

    await loadList();
    renderDetail();
  }

  /* ═══════════════════════════════════════════
     视图：客户与商机（IMP-028/029/031）
     ═══════════════════════════════════════════ */
  const STAGE_ORDER = ['added', 'measured', 'proposal', 'quoted', 'negotiating'];
  const STAGE_NEXT_LABEL = {
    added: '已量房（预约量房）', measured: '方案中（出方案）', proposal: '已报价（发报价单）', quoted: '谈判中', negotiating: '成交（签约）',
  };

  async function viewCustomers() {
    [state.customers, state.stageMeta] = await Promise.all([API.getCustomers(), API.getStageMeta()]);
    renderCustomers();
  }

  function renderCustomers() {
    const stages = state.stageMeta;
    const allStages = [...STAGE_ORDER, 'won', 'lost'];
    const custs = state.customers;
    const filtered = state.custFilter === 'all' ? custs : custs.filter((c) => c.stage === state.custFilter);

    const pipeline = allStages.map((k) => {
      const list = custs.filter((c) => c.stage === k);
      const value = list.reduce((s, c) => s + (c.estValue || 0), 0);
      return `
        <button type="button" class="stage-card ${k === 'won' ? 'stage-card--won' : ''} ${k === 'lost' ? 'stage-card--lost' : ''}"
                data-stage="${k}" aria-pressed="${state.custFilter === k}">
          <span class="stage-card__count" style="color:${k === 'won' ? 'var(--brand-deep)' : k === 'lost' ? 'var(--ink-faint)' : 'var(--ink)'}">${list.length}</span>
          <span class="stage-card__label">${esc(stages[k].label)}</span>
          <span class="stage-card__sub">${list.length ? (k === 'lost' ? '累计流失' : wan(value)) : '—'}</span>
        </button>`;
    }).join('');

    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'people', title: '客户与商机', desc: '加微后的成交流程：量房 → 方案 → 报价 → 谈判 → 成交（stage 与获客 status 正交）' })}
        <div class="pipeline" role="group" aria-label="按商机阶段筛选">${pipeline}</div>
        <div class="card">
          <div class="table-wrap">
            <table class="data">
              <thead><tr>
                <th>客户</th><th>来源</th><th>商机阶段</th><th>预估价值</th><th>下一步动作</th><th>最近动态</th><th></th>
              </tr></thead>
              <tbody>
                ${filtered.length ? filtered.map((c) => customerRow(c, stages)).join('')
                  : `<tr><td colspan="7"><div class="empty">${emptyState('people', '该阶段暂无客户')}</div></td></tr>`}
              </tbody>
            </table>
          </div>
        </div>
      </div>`;

    $$('.stage-card').forEach((b) => b.addEventListener('click', () => {
      state.custFilter = state.custFilter === b.dataset.stage ? 'all' : b.dataset.stage;
      renderCustomers();
    }));
    $$('[data-cu-id]').forEach((b) => b.addEventListener('click', () => openCustomerDrawer(b.dataset.cuId)));
  }

  function customerRow(c, stages) {
    const st = stages[c.stage];
    const src = state.sourceMeta[c.source] || { label: c.source, cls: 'collected' };
    const lastLog = c.logs[c.logs.length - 1];
    return `
      <tr data-cu-id="${c.id}" style="cursor:pointer${c.stage === 'lost' ? ';opacity:.6' : ''}">
        <td><div class="cell-user">
          <span class="avatar" style="background:hsl(${c.hue} 45% 44%)">${esc(c.name.slice(0, 1))}</span>
          <span style="font-weight:600">${esc(c.name)}</span>
        </div></td>
        <td><span class="tag tag--${src.cls}">${esc(src.label)}</span></td>
        <td><span class="tag tag--${st.cls}">${esc(st.label)}</span></td>
        <td class="num" style="font-weight:600">${c.dealAmount ? `<span style="color:var(--ok)">${wan(c.dealAmount)}</span>` : wan(c.estValue)}</td>
        <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-soft)">
          ${c.nextAction ? esc(c.nextAction) + ` <small style="color:var(--ink-faint)">(${esc(c.nextAt)})</small>` : '—'}</td>
        <td style="color:var(--ink-faint);font-size:12px">${esc(lastLog.time)} · ${esc(lastLog.text)}</td>
        <td><button type="button" class="btn-link">详情</button></td>
      </tr>`;
  }

  /* 客户详情抽屉（阶段推进 / 成交录入 / 流失标记） */
  function openCustomerDrawer(cuId) {
    const c = state.customers.find((x) => x.id === cuId);
    if (!c) return;
    const stages = state.stageMeta;
    const src = state.sourceMeta[c.source] || { label: c.source, cls: 'collected' };
    const isFinal = ['won', 'lost'].includes(c.stage);
    const nextKey = STAGE_ORDER[Math.min(STAGE_ORDER.indexOf(c.stage) + 1, STAGE_ORDER.length - 1)];

    const stepper = `
      <div class="stepper" aria-label="商机阶段">
        ${STAGE_ORDER.map((k) => {
          const done = stages[k].order < stages[c.stage].order && !isFinal;
          const current = k === c.stage;
          return `<span class="step-pill ${current ? 'step-pill--current' : done ? 'step-pill--done' : ''}">${done ? '✓ ' : ''}${esc(stages[k].label)}</span>`;
        }).join('')}
        ${c.stage === 'won' ? '<span class="step-pill step-pill--current">✓ 已成交</span>'
          : c.stage === 'lost' ? '<span class="step-pill step-pill--lost">已流失</span>'
          : '<span class="step-pill">成交</span>'}
      </div>`;

    const close = openDrawer(`
      <div class="drawer__head">
        <span class="avatar" style="background:hsl(${c.hue} 45% 44%);width:34px;height:34px;font-size:14px">${esc(c.name.slice(0, 1))}</span>
        <div class="drawer__title">
          ${esc(c.name)}
          <span class="tag tag--${stages[c.stage].cls}" style="margin-left:8px;vertical-align:2px">${esc(stages[c.stage].label)}</span>
        </div>
        <button type="button" class="drawer__close" aria-label="关闭">×</button>
      </div>
      <div class="drawer__body">
        ${stepper}
        <dl class="kv">
          <dt>来源触点</dt><dd><span class="tag tag--${src.cls}">${esc(src.label)}</span></dd>
          <dt>加微时间</dt><dd class="num">${esc(c.wechatAddedAt)}</dd>
          <dt>预估价值</dt><dd class="num">${wan(c.estValue)}</dd>
          ${c.dealAmount ? `<dt>成交金额</dt><dd class="num" style="color:var(--ok);font-weight:700">¥${fmt(c.dealAmount)} · ${esc(c.dealAt)}</dd>` : ''}
          ${c.referrer ? `<dt>转介绍人</dt><dd style="color:var(--brand-deep)">${esc(c.referrer)}</dd>` : ''}
          ${c.lostReason ? `<dt>流失原因</dt><dd style="color:var(--danger)">${esc(c.lostReason)}</dd>` : ''}
        </dl>

        ${c.nextAction ? `
          <div class="north-star" style="border-left-color:var(--warn)">
            <span class="north-star__label">下一步 · ${esc(c.nextAt)}</span>
            <span style="font-size:14px;font-weight:600">${esc(c.nextAction)}</span>
          </div>` : ''}

        <div>
          <div class="card__title" style="margin-bottom:10px">跟进记录</div>
          <ul class="timeline">
            ${c.logs.slice().reverse().map((lg) => `
              <li>
                <span class="timeline__dot" style="background:${lg.by === '系统' ? 'var(--brand)' : 'var(--info)'}"></span>
                <span>${esc(lg.text)}<span class="msg__meta" style="margin-left:6px">${esc(lg.by)}</span></span>
                <span class="timeline__time num">${esc(lg.time)}</span>
              </li>`).join('')}
          </ul>
        </div>

        ${!isFinal ? `
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button type="button" class="btn btn--primary" id="cu-advance">推进至「${esc(STAGE_NEXT_LABEL[c.stage] || stages[nextKey].label)}」</button>
            <button type="button" class="btn" id="cu-deal" style="color:var(--ok);font-weight:700">${ico('money', 'ico--ok ico--sm')} 录成交</button>
            <button type="button" class="btn" id="cu-lost">标记流失</button>
          </div>` : c.stage === 'won' ? `
          <div class="confirm-note">已成交客户可发起转介绍请求（IMP-031：回流线索池，最高优先级跟进）</div>
          <button type="button" class="btn btn--primary" id="cu-refer">请求转介绍（演示态）</button>` : ''}
      </div>`);

    const advBtn = $('#cu-advance');
    if (advBtn) advBtn.addEventListener('click', async () => {
      await API.advanceStage(c.id, nextKey);
      close();
      toast(`已推进至「${stages[nextKey].label}」`);
      viewCustomers();
    });

    const dealBtn = $('#cu-deal');
    if (dealBtn) dealBtn.addEventListener('click', () => openDealModal(c, close));

    const lostBtn = $('#cu-lost');
    if (lostBtn) lostBtn.addEventListener('click', () => {
      openModal(`
        <h2 id="modal-title">标记流失 · ${esc(c.name)}</h2>
        <p style="font-size:13px;color:var(--ink-soft);margin-bottom:12px">流失原因将沉淀数据，用于后续流失挽回 SOP（P2）。</p>
        <div class="field"><label for="lost-reason">流失原因</label>
          <select id="lost-reason">
            <option>预算不匹配</option>
            <option>选了竞争对手</option>
            <option>装修计划推迟</option>
            <option>其他</option>
          </select></div>
        <div class="modal__foot">
          <button type="button" class="btn" data-close>取消</button>
          <button type="button" class="btn btn--danger-outline" id="lost-save">确认标记流失</button>
        </div>`);
      $('#lost-save').addEventListener('click', async () => {
        await API.markLost(c.id, $('#lost-reason').value);
        closeModal(); close();
        toast('已标记流失');
        viewCustomers();
      });
    });

    const referBtn = $('#cu-refer');
    if (referBtn) referBtn.addEventListener('click', () => {
      toast('转介绍请求已发送（演示态）——回流线索池 source=referral');
    });
  }

  /* 成交录入弹窗（IMP-029：≤3 次点击） */
  function openDealModal(c, closeDrawer) {
    openModal(`
      <h2 id="modal-title">录入成交 · ${esc(c.name)}</h2>
      <div class="field"><label for="deal-amount">成交金额（元）</label>
        <input type="number" id="deal-amount" value="${c.estValue || 100000}" step="1000">
        <span class="field__hint">只填金额即可，其余自动带出 —— 老板不填表单（IMP-029 设计原则）</span></div>
      <div class="confirm-note">录入后：客户阶段 → 已成交；ROI 看板与「一个加微值多少钱」立即更新；该客户可发起转介绍。</div>
      <div class="modal__foot">
        <button type="button" class="btn" data-close>取消</button>
        <button type="button" class="btn btn--primary" id="deal-save">确认录入</button>
      </div>`);
    $('#deal-save').addEventListener('click', async () => {
      const amount = Number($('#deal-amount').value) || c.estValue;
      await API.recordDeal(c.id, amount);
      closeModal();
      if (closeDrawer) closeDrawer();
      toast(`成交已录入：¥${fmt(amount)}，ROI 账本已更新`);
      viewCustomers();
    });
  }

  /* ═══════════════════════════════════════════
     视图：今日跟进（IMP-028「今日跟进」视图）
     ═══════════════════════════════════════════ */
  async function viewFollowups() {
    const fups = await API.getFollowups();
    const undone = fups.filter((f) => !f.done);
    const overdue = undone.filter((f) => f.overdue);
    const today = undone.filter((f) => !f.overdue && f.due.startsWith('今天'));
    const upcoming = undone.filter((f) => !f.overdue && !f.due.startsWith('今天'));

    const todoItem = (f) => `
      <div class="todo-item ${f.overdue ? 'todo-item--overdue' : ''}">
        <input type="checkbox" class="todo__check" data-fu-done="${f.id}" aria-label="完成 ${esc(f.customer)} 的跟进">
        <div class="todo__text">
          <b>${esc(f.customer)} <span class="tag tag--low" style="margin-left:4px">${esc(f.type)}</span></b>
          <span class="todo__meta">${esc(f.text)}</span>
        </div>
        <span style="display:flex;gap:10px;align-items:center">
          <span class="todo__due ${f.overdue ? 'todo__due--overdue' : ''}">${f.overdue ? '⚠ ' : ''}${esc(f.due)}</span>
          <button type="button" class="btn-link" data-fu-snooze="${f.id}">改期</button>
        </span>
      </div>`;

    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'calendar', title: '今日跟进', desc: '高客单线索跟丢无感知的解药 · SOP 自动生成 + 逾期红色提醒' })}
        <div class="grid grid--metrics">
          ${metric('今日待办', fmt(today.length), 'SOP 自动生成 + 手动添加')}
          ${metric('已逾期', fmt(overdue.length), '逾期越久，成交概率越低', overdue.length ? 'danger' : '')}
          ${metric('明日预告', fmt(upcoming.length), '提前准备资料')}
          ${metric('已完成', fmt(fups.filter((f) => f.done).length), '今天')}
        </div>

        ${overdue.length ? `
          <div class="card" style="border-color:#e5b7b0">
            <div class="card__head"><span class="card__title" style="color:var(--danger)">⚠ 已逾期</span>
              <span class="card__hint">先处理逾期的——装修决策周期 1-2 个月，跟丢即流失</span></div>
            <div class="todo">${overdue.map(todoItem).join('')}</div>
          </div>` : ''}

        <div class="card">
          <div class="card__head"><span class="card__title">今天</span></div>
          <div class="todo">${today.length ? today.map(todoItem).join('') : emptyState('check', '今日待办已清空')}</div>
        </div>

        <div class="card">
          <div class="card__head"><span class="card__title">明天及以后</span></div>
          <div class="todo">${upcoming.length ? upcoming.map(todoItem).join('') : emptyState('calendar', '暂无排期')}</div>
        </div>
      </div>`;

    $$('[data-fu-done]').forEach((cb) => cb.addEventListener('change', async () => {
      await API.completeFollowup(cb.dataset.fuDone);
      toast('跟进已完成');
      viewFollowups();
    }));
    $$('[data-fu-snooze]').forEach((b) => b.addEventListener('click', () => {
      toast('已改期到明天（演示态，未落库）', 'warn');
    }));
  }

  /* ═══════════════════════════════════════════
     视图：转化漏斗（IMP-007 视图一）
     ═══════════════════════════════════════════ */
  async function viewFunnel() {
    main.innerHTML = '<div class="view"><div class="empty"><p>加载中…</p></div></div>';
    const d = await API.getFunnel();
    const [exposure, collected, outreach, replied, wechat, deal] = d.stages.map((s) => s.value);
    const pct = (a, b) => ((b / a) * 100).toFixed(1);
    const rows = d.stages.map((s, i) => {
      const width = Math.max(4, (s.value / exposure) * 100);
      const conv = i === 0 ? null : `${pct(d.stages[i - 1].value, s.value)}%`;
      return `
        <div class="funnel__row">
          <div class="funnel__label">${esc(s.label)}</div>
          <div class="funnel__bar-track"><div class="funnel__bar" style="width:${width}%">${fmt(s.value)}</div></div>
          <div class="funnel__conv">${conv ? `上步转化 <b>${conv}</b>` : '基准'}</div>
        </div>`;
    }).join('');

    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'funnel', title: '转化漏斗', desc: '数据来自真实采集与埋点，替代 v0.2 假报表 · 全部触点统一漏斗' })}
        <div class="grid grid--metrics">
          ${metric('线索入库', fmt(collected), `${d.window} · 意向率 ${pct(exposure, collected)}%`)}
          ${metric('加企微 ★ 北极星', fmt(wechat), `触达→加微 ${pct(outreach, wechat)}%`, 'brand')}
          ${metric('成交', fmt(deal), `加微→成交 ${pct(wechat, deal)}%`)}
          ${metric('单线索成本', `¥${d.cost.perLead}`, `单加微 ¥${d.cost.perWechatAdd} · 单成交 ¥${d.cost.perDeal}`)}
        </div>
        <div class="grid grid--main-side">
          <div class="card">
            <div class="card__head">
              <span class="card__title">转化漏斗（${esc(d.window)}）</span>
              <span class="card__hint">曝光 → 入库 → 触达 → 回复 → 加微 → 成交</span>
            </div>
            <div class="funnel">${rows}</div>
          </div>
          <div class="card">
            <div class="card__head"><span class="card__title">账怎么算</span></div>
            <dl class="kv">
              <dt>投入</dt><dd>¥${fmt(d.cost.totalSpend)}（人力+工具）</dd>
              <dt>客单价</dt><dd>¥${fmt(d.cost.avgDealAmount)}（装修 2-30 万区间）</dd>
              <dt>成交额</dt><dd>¥${fmt(deal * d.cost.avgDealAmount)}</dd>
              <dt>ROI</dt><dd><strong style="color:var(--ok)">${((deal * d.cost.avgDealAmount) / d.cost.totalSpend).toFixed(1)} 倍</strong></dd>
              <dt>一个加微值</dt><dd><strong>${wan(Math.round((deal * d.cost.avgDealAmount) / wechat))}</strong>（IMP-029 成交录入后可精确到实际金额）</dd>
            </dl>
            <p class="card__hint" style="margin-top:10px">
              老板视角：花 ¥${d.cost.perWechatAdd} 换一个企微好友、¥${d.cost.perDeal} 换一单。续费与否看这两个数。
            </p>
          </div>
        </div>
        <div class="card">
          <div class="card__head">
            <span class="card__title">加企微趋势（近 14 天 · wechat_added 埋点）</span>
            <span class="card__hint">IMP-002 北极星核心计数，8-22 起爬升</span>
          </div>
          ${lineChart(d.wechatTrend)}
        </div>
      </div>`;
  }

  /* ═══════════════════════════════════════════
     视图：数据报表（P3 模块 A · 周报/月报/自定义 + 导出）
     ═══════════════════════════════════════════ */
  async function viewReports() {
    main.innerHTML = '<div class="view"><div class="empty"><p>加载中…</p></div></div>';

    // 默认本周
    const today = new Date();
    const monday = new Date(today);
    monday.setDate(today.getDate() - today.getDay() + 1);
    const sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);
    const fmtDate = (d) => d.toISOString().slice(0, 10);

    let rangeStart = fmtDate(monday);
    let rangeEnd = fmtDate(sunday);
    let rangeMode = 'week'; // week | month | custom

    async function loadAndRender() {
      const [report, trend] = await Promise.all([
        API.fetchSummary(rangeStart, rangeEnd),
        API.fetchTrend(14),
      ]);
      render(report, trend);
    }

    function render(r, t) {
      const convPct = (r.conversionRate * 100).toFixed(1);
      // 趋势图数据：近14天新增线索
      const leadPoints = t.points.map((p) => ({ date: p.date.slice(5), v: p.newLeads }));
      const wechatPoints = t.points.map((p) => ({ date: p.date.slice(5), v: p.addedWechat }));

      main.innerHTML = `
        <div class="view">
          ${pageHead({ icon: 'trend', title: '数据报表', desc: `${esc(rangeStart)} ~ ${esc(rangeEnd)} · 经营数据一览与一键导出` })}

          <!-- 时间区间选择器 -->
          <div class="card" style="margin-bottom:16px">
            <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
              <span style="font-weight:600;color:var(--ink)">统计区间</span>
              <button class="btn btn--sm ${rangeMode === 'week' ? 'btn--primary' : ''}" id="rp-week">本周</button>
              <button class="btn btn--sm ${rangeMode === 'month' ? 'btn--primary' : ''}" id="rp-month">本月</button>
              <button class="btn btn--sm ${rangeMode === 'custom' ? 'btn--primary' : ''}" id="rp-custom">自定义</button>
              <span id="rp-custom-fields" style="${rangeMode === 'custom' ? '' : 'display:none'}">
                <input type="date" id="rp-start" value="${esc(rangeStart)}" style="padding:4px 8px;border:1px solid var(--border);border-radius:6px;font-size:13px">
                <span style="margin:0 6px;color:var(--ink-soft)">至</span>
                <input type="date" id="rp-end" value="${esc(rangeEnd)}" style="padding:4px 8px;border:1px solid var(--border);border-radius:6px;font-size:13px">
                <button class="btn btn--sm btn--primary" id="rp-apply" style="margin-left:8px">应用</button>
              </span>
            </div>
          </div>

          <!-- 核心指标卡片 -->
          <div class="grid grid--metrics">
            ${metric('新增线索', fmt(r.newLeads), `${esc(rangeStart)} ~ ${esc(rangeEnd)}`)}
            ${metric('触达数', fmt(r.contacted), '外发消息数')}
            ${metric('回复数', fmt(r.replied), '客户回复消息数')}
            ${metric('加微数', fmt(r.addedWechat), 'wechat_added 埋点', 'brand')}
            ${metric('成交数', fmt(r.deals), 'deal_won 状态')}
            ${metric('成交金额', wan(r.dealAmount), `客单价 ¥${r.deals ? Math.round(r.dealAmount / r.deals).toLocaleString() : 0}`)}
            ${metric('转化率', convPct + '%', `成交 ${fmt(r.deals)} / 线索 ${fmt(r.newLeads)}`, r.conversionRate > 0 ? 'brand' : '')}
          </div>

          <!-- 趋势图 -->
          <div class="grid grid--main-side" style="margin-top:16px">
            <div class="card">
              <div class="card__head">
                <span class="card__title">近 14 天新增线索趋势</span>
                <span class="card__hint">按 created_at 聚合</span>
              </div>
              ${leadPoints.some((p) => p.v > 0) ? lineChart(leadPoints) : '<div class="empty"><p>暂无数据</p></div>'}
            </div>
            <div class="card">
              <div class="card__head">
                <span class="card__title">近 14 天加微趋势</span>
                <span class="card__hint">按 wechat_added_at 聚合</span>
              </div>
              ${wechatPoints.some((p) => p.v > 0) ? lineChart(wechatPoints) : '<div class="empty"><p>暂无数据</p></div>'}
            </div>
          </div>

          <!-- 导出按钮组 -->
          <div class="card" style="margin-top:16px">
            <div class="card__head">
              <span class="card__title">数据导出</span>
              <span class="card__hint">导出当前统计区间数据为 CSV</span>
            </div>
            <div style="display:flex;gap:12px;flex-wrap:wrap">
              <button class="btn btn--primary" id="exp-leads">导出线索 (CSV)</button>
              <button class="btn btn--primary" id="exp-customers">导出客户 (CSV)</button>
              <button class="btn btn--primary" id="exp-deals">导出成交 (CSV)</button>
            </div>
            <div id="exp-result" style="margin-top:12px"></div>
          </div>
        </div>`;

      // 绑定事件
      $('#rp-week').onclick = () => {
        rangeMode = 'week';
        const m = new Date(); m.setDate(m.getDate() - m.getDay() + 1);
        const s = new Date(m); s.setDate(m.getDate() + 6);
        rangeStart = fmtDate(m); rangeEnd = fmtDate(s);
        loadAndRender();
      };
      $('#rp-month').onclick = () => {
        rangeMode = 'month';
        const first = new Date(today.getFullYear(), today.getMonth(), 1);
        const last = new Date(today.getFullYear(), today.getMonth() + 1, 0);
        rangeStart = fmtDate(first); rangeEnd = fmtDate(last);
        loadAndRender();
      };
      $('#rp-custom').onclick = () => {
        rangeMode = 'custom';
        render(r, t); // 重新渲染显示日期输入
      };
      const applyBtn = $('#rp-apply');
      if (applyBtn) {
        applyBtn.onclick = () => {
          const s = $('#rp-start').value;
          const e = $('#rp-end').value;
          if (s && e && s <= e) {
            rangeStart = s; rangeEnd = e;
            loadAndRender();
          } else {
            toast('请选择有效的日期区间', 'warn');
          }
        };
      }

      // 导出按钮
      const expParams = { startDate: rangeStart, endDate: rangeEnd };
      $('#exp-leads').onclick = async () => {
        try {
          const res = await API.exportLeads('csv', expParams);
          showExportResult(res);
        } catch (e) { toast('导出失败：' + e.message, 'warn'); }
      };
      $('#exp-customers').onclick = async () => {
        try {
          const res = await API.exportCustomers('csv', expParams);
          showExportResult(res);
        } catch (e) { toast('导出失败：' + e.message, 'warn'); }
      };
      $('#exp-deals').onclick = async () => {
        try {
          const res = await API.exportDeals('csv', expParams);
          showExportResult(res);
        } catch (e) { toast('导出失败：' + e.message, 'warn'); }
      };
    }

    function showExportResult(res) {
      const url = 'http://127.0.0.1:8000' + res.fileUrl;
      $('#exp-result').innerHTML = `
        <div style="padding:10px 14px;background:var(--bg-soft);border-radius:8px;border:1px solid var(--border)">
          <span style="color:var(--ok)">✓ 导出成功</span>
          <span style="margin-left:12px;color:var(--ink-soft)">${esc(res.fileName)} · ${fmt(res.rowCount)} 行 · ${esc(res.format).toUpperCase()}</span>
          <a href="${esc(url)}" target="_blank" download style="margin-left:12px;color:var(--brand-deep);font-weight:600;text-decoration:underline">点击下载</a>
        </div>`;
      toast('导出成功，共 ' + res.rowCount + ' 行', 'ok');
    }

    try {
      await loadAndRender();
    } catch (e) {
      main.innerHTML = `<div class="view"><div class="empty"><p>加载失败：${esc(e.message)}</p></div></div>`;
    }
  }

  /* ═══════════════════════════════════════════
     视图：触点归因（IMP-032 · source 维度）
     ═══════════════════════════════════════════ */
  async function viewAttribution() {
    main.innerHTML = '<div class="view"><div class="empty"><p>加载中…</p></div></div>';
    const a = await API.getAttribution();
    await API.getSourceMeta().then((m) => { state.sourceMeta = m; });
    const maxLeads = Math.max(...a.bySource.map((s) => s.leads));
    const totalLeads = a.bySource.reduce((s, x) => s + x.leads, 0);
    const totalWechat = a.bySource.reduce((s, x) => s + x.wechat, 0);
    const totalDeal = a.bySource.reduce((s, x) => s + x.deal, 0);

    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'compass', title: '触点归因', desc: '哪条视频、哪个触点、哪套话术带来的线索最成交（IMP-032）· 依赖 leads.source 埋点' })}
        <div class="grid grid--metrics">
          ${metric('线索（' + a.window + '）', fmt(totalLeads), `${a.bySource.length} 个触点在跑`)}
          ${metric('加微 ★', fmt(totalWechat), `整体线索→加微 ${((totalWechat / totalLeads) * 100).toFixed(1)}%`, 'brand')}
          ${metric('成交', fmt(totalDeal), `加微→成交 ${((totalDeal / totalWechat) * 100).toFixed(1)}%`)}
          ${metric('最佳触点', '自有评论区', `${fmt(a.bySource[0].leads)} 线索 · ${a.bySource[0].deal} 成交`)}
        </div>

        <div class="card">
          <div class="card__head">
            <span class="card__title">各触点贡献（${esc(a.window)}）</span>
            <span class="card__hint">单触点失效时其余触点仍在跑 —— 触点组合本身就是防封策略（R4）</span>
          </div>
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>触点</th><th>线索</th><th>加微</th><th>线索→加微</th><th>成交</th><th>状态</th></tr></thead>
              <tbody>
                ${a.bySource.map((s) => `
                  <tr>
                    <td style="font-weight:600">${esc(s.label)}</td>
                    <td><div style="display:flex;align-items:center;gap:8px">
                      <div style="width:110px;height:14px;background:var(--paper-deep);border-radius:4px;overflow:hidden;flex:none">
                        <div style="width:${(s.leads / maxLeads) * 100}%;height:100%;background:var(--brand)"></div>
                      </div><b class="num">${fmt(s.leads)}</b></div></td>
                    <td class="num"><b>${fmt(s.wechat)}</b></td>
                    <td class="num">${((s.wechat / s.leads) * 100).toFixed(1)}%</td>
                    <td class="num">${fmt(s.deal)}</td>
                    <td>${s.pilot ? '<span class="tag tag--mid">试点中</span>' : '<span class="tag tag--healthy">运行中</span>'}</td>
                  </tr>`).join('')}
              </tbody>
            </table>
          </div>
          <p class="card__hint" style="margin-top:10px">
            试点触点（留资卡 / 转介绍）为 v1.1 规划（触点扩展 PRD IMP-024/031），先埋 source 字段再上线，历史数据才不断层。
          </p>
        </div>

        <div class="grid grid--2">
          <div class="card">
            <div class="card__head">
              <span class="card__title">内容榜 · 最能带线索的视频</span>
              <span class="card__hint">指导下一条拍什么</span>
            </div>
            ${a.topVideos.map((v, i) => `
              <div class="bar-row">
                <div class="bar-row__label"><b>${i + 1}</b><span title="${esc(v.title)}">${esc(v.title)}</span></div>
                <div class="bar-row__main">
                  <div class="bar-track"><div class="bar-fill" style="width:${(v.leads / a.topVideos[0].leads) * 100}%"></div></div>
                  <div class="bar-row__sub">加微 ${v.wechat}${v.dealAmount ? ` · 成交 ${wan(v.dealAmount)}` : ''}</div>
                </div>
                <div class="bar-row__value">${fmt(v.leads)}<small>线索</small></div>
              </div>`).join('')}
          </div>
          <div class="card">
            <div class="card__head"><span class="card__title">话术贡献</span></div>
            <dl class="kv">
              <dt>变体A</dt><dd>24 加微 / 214 发送 · 转化率 11.2%（R2 保活在用）</dd>
              <dt>变体B</dt><dd>3 加微 / 51 发送 · 转化率 5.9%（R2 已自动停用）</dd>
            </dl>
            <p class="card__hint" style="margin-top:10px">变体级明细见「话术库」——哪句话在换钱，一目了然。</p>
          </div>
        </div>
      </div>`;
  }

  /* ═══════════════════════════════════════════
     视图：账号健康度（IMP-006 / IMP-007）
     ═══════════════════════════════════════════ */
  async function viewHealth() {
    main.innerHTML = '<div class="view"><div class="empty"><p>加载中…</p></div></div>';
    const accs = await API.getAccounts();
    const avg = Math.round(accs.reduce((s, a) => s + a.healthScore, 0) / accs.length);
    const danger = accs.filter((a) => a.healthScore < 40 || a.limitStatus === 'safe_mode' || a.limitStatus === 'banned');

    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'health', title: '账号健康度', desc: '健康分 = 日频 + 封禁史 + 登录态 + 退订率 + 投诉率（IMP-006）· &lt;40 红色联动安全模式' })}
        <div class="grid grid--metrics">
          ${metric('账号数', fmt(accs.length), '1 人可管 5 号（US4）')}
          ${metric('平均健康分', avg, '<60 黄色 · <40 红色联动安全模式')}
          ${metric('预警账号', danger.length, danger.length ? danger.map((a) => esc(a.nickname)).join('、') : '无', danger.length ? 'danger' : '')}
          ${metric('今日已发送', accs.reduce((s, a) => s + a.dailyOutreach, 0), '总日频上限 ' + fmt(accs.reduce((s, a) => s + (a.dailyLimit || 0), 0)))}
        </div>
        <div class="card">
          <div class="card__head">
            <span class="card__title">账号健康度明细</span>
            <span class="card__hint">R1 命中自动降速 · 健康分低于阈值自动进安全模式</span>
          </div>
          <div class="health-list">
            ${accs.map((a) => healthRow(a)).join('')}
          </div>
        </div>
      </div>`;
  }

  function healthRow(a) {
    const cls = a.healthScore >= 60 ? 'ok' : a.healthScore >= 40 ? 'warn' : 'danger';
    const fill = a.healthScore >= 60 ? 'var(--ok)' : a.healthScore >= 40 ? 'var(--warn)' : 'var(--danger)';
    const statusTag = {
      healthy: '<span class="tag tag--healthy">健康</span>',
      throttled40: '<span class="tag tag--throttled">R1 降速 40/天</span>',
      paused_24h: '<span class="tag tag--throttled">暂停 24h</span>',
      safe_mode: '<span class="tag tag--safemode">安全模式</span>',
      banned: '<span class="tag tag--banned">已封禁</span>',
    }[a.limitStatus] || a.limitStatus;
    const factorLabels = { dailyFreq: '日频', banHistory: '封禁史', login: '登录态', unsubscribe: '退订率', complaint: '投诉率' };
    const factors = Object.entries(a.factors).map(([k, v]) =>
      `<span class="factor factor--${v}">${factorLabels[k]}${v === 'bad' ? '✕' : v === 'warn' ? '△' : '✓'}</span>`).join('');
    const note = a.r1Note || a.r3Note || a.lastBanReason || '';
    return `
      <div class="health-row">
        <div class="cell-user">
          <span class="avatar" style="background:hsl(${a.avatarHue} 42% 42%)">${esc(a.nickname.slice(0, 1))}</span>
          <div><div style="font-weight:600">${esc(a.nickname)}</div>
          <div style="font-size:11.5px;color:var(--ink-faint)">${note ? esc(note) : '更新于 ' + esc(a.updatedAt)}</div></div>
        </div>
        <div class="gauge">
          <span class="gauge__num gauge__num--${cls}">${a.healthScore}</span>
          <div class="gauge__track"><div class="gauge__fill" style="width:${a.healthScore}%;background:${fill}"></div></div>
        </div>
        <div class="factor-grid">${factors}</div>
        <div style="display:flex;flex-direction:column;gap:5px;align-items:flex-end">
          ${statusTag}
          <span style="font-size:11.5px;color:var(--ink-faint)" class="num">今日 ${a.dailyOutreach}/${a.dailyLimit || '—'}</span>
        </div>
      </div>`;
  }

  /* ═══════════════════════════════════════════
     视图：合规风险（IMP-007 / IMP-010）
     ═══════════════════════════════════════════ */
  async function viewRisk() {
    main.innerHTML = '<div class="view"><div class="empty"><p>加载中…</p></div></div>';
    const c = await API.getCompliance();
    const dotColor = { ok: 'var(--ok)', info: 'var(--info)', warn: 'var(--warn)', danger: 'var(--danger)' };
    const ruleTag = (s) => s === 'hit' ? '<span class="tag tag--high">已命中</span>' : '<span class="tag tag--low">待命</span>';

    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'shield', title: '合规风险', desc: '合规护栏可视化 —— 强监管行业的信任底线（差异化护城河）' })}
        <div class="grid grid--metrics">
          ${metric('近7天退订', c.summary.unsubscribe7d, 'user_blocked/unsubscribe 埋点')}
          ${metric('黑名单总数', c.summary.blacklistTotal, `日增均值 ${c.summary.blacklistDelta7d}%（R3 阈值 3%）`)}
          ${metric('近7天投诉', c.summary.complaints7d, 'complaint 埋点')}
          ${metric('审计事件', c.summary.auditEvents, 'v0.2 审计表 · 自托管可导出')}
        </div>
        <div class="grid grid--main-side">
          <div class="card">
            <div class="card__head">
              <span class="card__title">审计事件流（最近）</span>
              <span class="card__hint">全触达可审计 · 支持退订，满足强监管要求（US5）</span>
            </div>
            <ul class="timeline">
              ${c.audit.map((e) => `
                <li>
                  <span class="timeline__dot" style="background:${dotColor[e.level]}"></span>
                  <span>${esc(e.text)}</span>
                  <span class="timeline__time num">${esc(e.time)}</span>
                </li>`).join('')}
            </ul>
          </div>
          <div class="card">
            <div class="card__head"><span class="card__title">决策规则引擎（R1/R2/R3）</span></div>
            <div class="col-grid" style="gap:12px">
              ${c.rules.map((r) => `
                <div style="border:1px solid var(--line);border-radius:10px;padding:11px 13px">
                  <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
                    <strong style="font-family:var(--font-display)">${r.id}</strong>${ruleTag(r.status)}
                  </div>
                  <div style="font-size:12.5px;color:var(--ink-soft)">${esc(r.desc)}</div>
                  ${r.hitAt ? `<div style="font-size:11.5px;color:var(--warn);margin-top:4px">最近命中：${esc(r.hitAt)} · ${esc(r.target)}</div>` : ''}
                </div>`).join('')}
            </div>
            <p class="card__hint" style="margin-top:12px">
              安全模式当前：${state.safeMode.active ? '<b style="color:var(--danger)">已启用（' + esc(state.safeMode.reason) + '）</b>' : '<b style="color:var(--ok)">未启用</b>'}
            </p>
          </div>
        </div>
      </div>`;
  }

  /* ═══════════════════════════════════════════
     视图：话术库（IMP-005 / R2）
     ═══════════════════════════════════════════ */
  /* ══════════ 话术库分类 Tab 状态 ══════════ */
  const scriptTabState = { category: 'all' };
  const SCRIPT_TABS = [
    { key: 'all',             label: '全部' },
    { key: 'comment',         label: '评论话术' },
    { key: 'private_message', label: '私信话术' },
    { key: 'wechat_guide',    label: '微信引导' },
    { key: 'objection',       label: '异议处理' },
    { key: 'nurture',         label: '培育SOP' },
  ];

  async function viewScripts() {
    const [scripts, templates] = await Promise.all([API.getScripts(), API.getScriptTemplates()]);

    function render() {
      const cat = scriptTabState.category;
      const filtered = cat === 'all' ? scripts : scripts.filter((s) => s.category === cat);
      const tabsHtml = SCRIPT_TABS.map((t) => {
        const count = t.key === 'all' ? scripts.length : scripts.filter((s) => s.category === t.key).length;
        return `<button type="button" class="subtab-btn" data-cat="${t.key}" aria-selected="${cat === t.key}">${esc(t.label)}<span class="count">${count}</span></button>`;
      }).join('');

      main.innerHTML = `
        <div class="view">
          ${pageHead({
            icon: 'script', title: '话术库', desc: '主话术 + 变体 AB 自动追踪，R2 命中自动切换（转化率 &lt; 8% 且样本 ≥ 30）',
            actions: '<button type="button" class="btn btn--primary btn--sm" id="btn-new-script">+ 新建话术</button>',
          })}

          <div class="subtabs" role="tablist">
            ${tabsHtml}
          </div>

          ${filtered.length > 0 ? `
          <div class="grid grid--2">
            ${filtered.map((s) => scriptCard(s)).join('')}
          </div>` : `
          <div class="card"><div class="empty">
            <span class="empty__ico">${ico('empty')}</span>
            <p>该分类下暂无话术</p>
          </div></div>`}

          <div class="card">
            <div class="card__head">
              <span class="card__title">行业话术模板包</span>
              <span class="card__hint">H3 验证：v1.0 内置装修包，上线后用行业分布数据复核</span>
            </div>
            <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
              ${templates.map((t) => `
                <div style="border:1px solid var(--line);border-radius:10px;padding:12px 14px;${t.installed ? 'background:var(--brand-tint-2)' : ''}">
                  <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
                    <strong>${esc(t.industry)}</strong>
                    ${t.installed ? '<span class="tag tag--wechat">已安装</span>' : '<span class="tag tag--low">未开放</span>'}
                  </div>
                  <div style="font-size:12px;color:var(--ink-soft)">${esc(t.desc)}</div>
                </div>`).join('')}
            </div>
          </div>
        </div>`;

      // Tab 切换事件
      $$('.subtab-btn', main).forEach((btn) => {
        btn.addEventListener('click', () => {
          scriptTabState.category = btn.dataset.cat;
          render();
        });
      });

      $('#btn-new-script').addEventListener('click', () => {
        openModal(`
          <h2 id="modal-title">新建话术</h2>
          <div class="field"><label for="ns-name">话术名称</label><input type="text" id="ns-name" placeholder="例：装修 · 报价跟进"></div>
          <div class="field"><label for="ns-cat">话术分类</label>
            <select id="ns-cat">
              <option value="comment">评论话术</option>
              <option value="private_message">私信话术</option>
              <option value="wechat_guide">微信引导</option>
              <option value="objection">异议处理</option>
              <option value="nurture">培育SOP</option>
            </select></div>
          <div class="field"><label for="ns-text">话术内容</label>
            <textarea id="ns-text" placeholder="支持变量：{视频标题} {预算/面积} {需求关键词}"></textarea>
            <span class="field__hint">建议同时准备 ≥2 个变体用于 AB 追踪（R2 规则）</span></div>
          <div class="modal__foot">
            <button type="button" class="btn" data-close>取消</button>
            <button type="button" class="btn btn--primary" id="ns-save" data-close>创建（演示态）</button>
          </div>`);
        $('#ns-save').addEventListener('click', () => toast('话术已创建（演示态，未落库）'));
      });
    }

    render();
  }

  /* ═══════════════════════════════════════════
     视图：评论回复任务（评论引流策略核心）
     ═══════════════════════════════════════════ */
  const COMMENT_STATUS_META = {
    pending:      { label: '待回复',   cls: 'pending' },
    replied:      { label: '已回复',   cls: 'sent' },
    user_replied: { label: '对方追评', cls: 'replied' },
    user_dm:      { label: '对方私信 ★', cls: 'wechat' },
    ignored:      { label: '无反应',   cls: 'rejected' },
  };

  async function viewComments() {
    const [allTasks, scriptsAll] = await Promise.all([
      API.getCommentTasks(),
      API.getScripts(),
    ]);
    const commentScripts = scriptsAll.filter((s) => s.category === 'comment');
    const pending = allTasks.filter((t) => t.status === 'pending');
    const tracked = allTasks.filter((t) => ['replied', 'user_replied', 'user_dm', 'ignored'].includes(t.status));

    function taskRow(t, isPending) {
      const meta = COMMENT_STATUS_META[t.status] || { label: t.status, cls: 'pending' };
      const highlight = t.status === 'user_dm' ? 'style="background:var(--ok-tint)"' :
                        t.status === 'user_replied' ? 'style="background:var(--brand-tint-2)"' : '';
      return `<tr ${highlight}>
        <td><div style="font-weight:600;font-size:13px">${esc(t.commentContent || '(无评论内容)')}</div>
          <div style="font-size:11px;color:var(--ink-faint);margin-top:2px">🎬 ${esc(t.videoTitle || '未知视频')}</div></td>
        <td><span class="tag tag--${meta.cls}">${esc(meta.label)}</span></td>
        <td style="font-size:12px">${esc(t.account || '—')}</td>
        <td style="font-size:12px;color:var(--ink-soft)">${t.replyContent ? esc(t.replyContent) : '<span style="color:var(--ink-faint)">未回复</span>'}</td>
        <td style="text-align:right;white-space:nowrap">
          ${isPending ? `<button type="button" class="btn btn--primary btn--sm" data-reply="${t.id}">选择话术回复</button>` :
            `<button type="button" class="btn btn--sm" data-mark="${t.id}" data-status="user_replied">标记追评</button>
             <button type="button" class="btn btn--sm" data-mark="${t.id}" data-status="user_dm" style="margin-left:4px">标记私信★</button>`}
        </td>
      </tr>`;
    }

    main.innerHTML = `
      <div class="view">
        ${pageHead({
          icon: 'dm', title: '评论回复任务', desc: '评论引流策略核心 — 高意向评论一键话术回复，追踪对方追评/私信转化',
          actions: `<span class="tag tag--pending">待回复 ${pending.length}</span>
                    <span class="tag tag--wechat">已转化 ${tracked.filter(t=>t.status==='user_dm').length}</span>`,
        })}

        <!-- 待回复列表 -->
        <div class="card">
          <div class="card__head">
            <span class="card__title">待回复评论</span>
            <span class="card__hint">选择评论话术一键回复，回复后自动进入追踪列表</span>
            <span class="card__spacer"></span>
            <span class="tag tag--pending">${pending.length} 条待处理</span>
          </div>
          ${pending.length > 0 ? `
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>评论内容 / 来源视频</th><th style="width:80px">状态</th><th style="width:110px">负责账号</th><th>回复内容</th><th style="width:140px;text-align:right">操作</th></tr></thead>
              <tbody>${pending.map((t) => taskRow(t, true)).join('')}</tbody>
            </table>
          </div>` : `
          <div class="empty">
            <span class="empty__ico">${ico('check')}</span>
            <p>暂无待回复评论，干得漂亮！</p>
          </div>`}
        </div>

        <!-- 已回复追踪 -->
        <div class="card" style="margin-top:var(--r-md)">
          <div class="card__head">
            <span class="card__title">已回复追踪</span>
            <span class="card__hint">高亮：对方追评（蓝）/ 对方主动私信（绿 ★ 转化成功）</span>
            <span class="card__spacer"></span>
            <span class="tag tag--sent">已回复 ${tracked.filter(t=>t.status==='replied').length}</span>
            <span class="tag tag--replied" style="margin-left:4px">追评 ${tracked.filter(t=>t.status==='user_replied').length}</span>
            <span class="tag tag--wechat" style="margin-left:4px">私信★ ${tracked.filter(t=>t.status==='user_dm').length}</span>
          </div>
          ${tracked.length > 0 ? `
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>评论内容 / 来源视频</th><th style="width:90px">状态</th><th style="width:110px">负责账号</th><th>回复内容</th><th style="width:200px;text-align:right">转化标记</th></tr></thead>
              <tbody>${tracked.map((t) => taskRow(t, false)).join('')}</tbody>
            </table>
          </div>` : `
          <div class="empty">
            <span class="empty__ico">${ico('empty')}</span>
            <p>暂无已回复记录</p>
          </div>`}
        </div>
      </div>`;

    // 绑定回复按钮
    $$('[data-reply]', main).forEach((btn) => {
      btn.addEventListener('click', () => {
        const task = allTasks.find((t) => t.id === btn.dataset.reply);
        openReplyModal(btn.dataset.reply, commentScripts, task || {});
      });
    });

    // 绑定状态标记按钮
    $$('[data-mark]', main).forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = btn.dataset.mark;
        const status = btn.dataset.status;
        const result = await API.updateCommentTask(id, { status });
        if (result && result.ok !== false) {
          toast(status === 'user_dm' ? '已标记对方主动私信 ★ 转化成功！' : '已标记对方追评', 'ok');
          viewComments(); // 重新渲染
        }
      });
    });
  }

  /* 回复话术选择弹窗 */
  function openReplyModal(taskId, commentScripts, task = {}) {
    let selectedScript = null;
    let selectedVariant = null;
    let aiSuggestions = null;
    let aiLoading = false;
    let selectedAiContent = null;

    async function loadAiSuggestions() {
      if (!(await aiEnsureConfigured())) return;
      aiLoading = true; renderModal();
      try {
        const result = await API.aiCommentSuggestion({
          comment: task.commentContent || '',
          video_title: task.videoTitle || '',
          source_keyword: '',
        });
        aiSuggestions = result.suggestions || [];
      } catch (e) {
        aiHandleError(e);
        aiSuggestions = [];
      } finally {
        aiLoading = false; renderModal();
      }
    }

    function renderModal() {
      const scriptsHtml = commentScripts.length > 0 ? commentScripts.map((s) => `
        <div class="radio-card ${selectedScript === s.id ? 'radio-card--checked' : ''}" style="border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px;cursor:pointer;${selectedScript === s.id ? 'border-color:var(--brand);background:var(--brand-tint-2)' : ''}">
          <label style="cursor:pointer;display:block">
            <input type="radio" name="reply-script" value="${s.id}" ${selectedScript === s.id ? 'checked' : ''} style="margin-right:6px">
            <strong>${esc(s.name)}</strong>
            <div style="font-size:11px;color:var(--ink-faint);margin-top:2px">${esc(s.intro || '')}</div>
          </label>
          ${selectedScript === s.id ? `
          <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--line)">
            <div style="font-size:12px;font-weight:600;margin-bottom:6px">选择变体：</div>
            ${s.variants.filter((v) => v.status === 'active').map((v) => `
              <label style="display:block;padding:6px 8px;border-radius:6px;cursor:pointer;margin-bottom:4px;${selectedVariant === v.id ? 'background:var(--brand-tint);border:1px solid var(--brand)' : 'border:1px solid transparent'}">
                <input type="radio" name="reply-variant" value="${v.id}" ${selectedVariant === v.id ? 'checked' : ''} style="margin-right:6px">
                <span style="font-family:var(--font-display);font-weight:700">变体${v.id}</span>
                <span style="font-size:11px;color:var(--ink-faint);margin-left:6px">权重${v.weight}% · 转化率${v.convRate != null ? v.convRate + '%' : '—'}</span>
                <div style="font-size:12px;color:var(--ink);margin-top:4px;line-height:1.5">${esc(v.text)}</div>
              </label>`).join('')}
          </div>` : ''}
        </div>`).join('') : '<p style="color:var(--ink-faint);font-size:13px">暂无评论类话术，请先在话术库创建 category=comment 的话术</p>';

      const aiHtml = aiLoading
        ? '<div style="color:var(--ink-soft);font-size:13px;padding:20px;text-align:center">AI 正在生成回复建议…</div>'
        : aiSuggestions && aiSuggestions.length > 0
          ? aiSuggestions.map((s) => `
            <label style="display:block;padding:10px 12px;border-radius:8px;cursor:pointer;margin-bottom:8px;border:1px solid ${selectedAiContent === s.content ? 'var(--brand)' : 'var(--line)'};background:${selectedAiContent === s.content ? 'var(--brand-tint-2)' : 'transparent'}">
              <input type="radio" name="ai-suggestion" value="${esc(s.content)}" ${selectedAiContent === s.content ? 'checked' : ''} style="margin-right:6px">
              <span class="tag tag--low" style="margin-right:6px">${esc(s.label)}</span>
              <span style="font-size:13px;line-height:1.6">${esc(s.content)}</span>
            </label>`).join('')
          : '<div style="color:var(--ink-faint);font-size:13px;padding:16px;text-align:center">点击上方「AI 生成回复」按钮获取建议</div>';

      const canConfirm = selectedVariant || selectedAiContent;

      openModal(`
        <h2 id="modal-title">选择回复方式</h2>
        <div style="display:flex;gap:8px;margin-bottom:12px">
          <button type="button" class="btn btn--sm ${selectedAiContent ? 'btn--ghost' : 'btn--primary'}" id="tab-script">话术库</button>
          <button type="button" class="btn btn--sm ${selectedAiContent ? 'btn--primary' : 'btn--ghost'}" id="tab-ai" style="border-color:var(--brand);color:var(--brand-deep)">
            <span class="ico ico--sm" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#i-rocket"/></svg></span>AI 生成
          </button>
        </div>
        <div id="panel-script" style="${selectedAiContent ? 'display:none' : ''}">
          <div style="max-height:40vh;overflow-y:auto;margin-bottom:12px">
            ${scriptsHtml}
          </div>
        </div>
        <div id="panel-ai" style="${selectedAiContent ? '' : 'display:none'}">
          <button type="button" class="btn btn--ghost btn--sm" id="btn-ai-generate" style="margin-bottom:10px;width:100%;border-color:var(--brand);color:var(--brand-deep)">
            ${aiLoading ? '生成中…' : '重新生成 AI 回复建议'}
          </button>
          <div style="max-height:35vh;overflow-y:auto">
            ${aiHtml}
          </div>
        </div>
        <div class="modal__foot">
          <button type="button" class="btn" data-close>取消</button>
          <button type="button" class="btn btn--primary" id="reply-confirm" ${!canConfirm ? 'disabled style="opacity:.5;cursor:not-allowed"' : ''}>确认回复</button>
        </div>`);

      // Tab 切换
      $('#tab-script')?.addEventListener('click', () => { selectedAiContent = null; renderModal(); });
      $('#tab-ai')?.addEventListener('click', () => {
        selectedScript = null; selectedVariant = null;
        if (!aiSuggestions && !aiLoading) loadAiSuggestions();
        selectedAiContent = aiSuggestions && aiSuggestions[0] ? aiSuggestions[0].content : null;
        renderModal();
      });
      $('#btn-ai-generate')?.addEventListener('click', loadAiSuggestions);

      // 绑定话术选择
      $$('input[name="reply-script"]').forEach((inp) => {
        inp.addEventListener('change', () => { selectedScript = inp.value; selectedVariant = null; renderModal(); });
      });
      $$('input[name="reply-variant"]').forEach((inp) => {
        inp.addEventListener('change', () => { selectedVariant = inp.value; renderModal(); });
      });
      $$('input[name="ai-suggestion"]').forEach((inp) => {
        inp.addEventListener('change', () => { selectedAiContent = inp.value; renderModal(); });
      });

      const confirmBtn = $('#reply-confirm');
      if (confirmBtn && canConfirm) {
        confirmBtn.addEventListener('click', async () => {
          let replyContent = '';
          if (selectedAiContent) {
            replyContent = selectedAiContent;
          } else {
            const script = commentScripts.find((s) => s.id === selectedScript);
            const variant = script?.variants.find((v) => v.id === selectedVariant);
            if (!variant) return;
            replyContent = variant.text;
          }
          const result = await API.updateCommentTask(taskId, {
            status: 'replied',
            replyContent,
            repliedAt: new Date().toISOString(),
          });
          if (result && result.ok !== false) {
            toast(selectedAiContent ? '评论已回复（AI 生成）' : '评论已回复（话术变体）', 'ok');
            closeModal();
            viewComments();
          }
        });
      }
    }

    renderModal();
  }

  function scriptCard(s) {
    const variants = s.variants.map((v) => {
      const statusLabel = { active: '<span class="tag tag--wechat">使用中</span>', switched_off: '<span class="tag tag--failed">已停用</span>', draft: '<span class="tag tag--low">草稿</span>' }[v.status];
      const conv = v.convRate != null ? `${v.convRate}%` : '—';
      return `
        <div class="variant-row">
          <div style="display:flex;flex-direction:column;gap:4px;align-items:center">
            <span style="font-family:var(--font-display);font-weight:700;font-size:16px">${v.id}</span>
            ${statusLabel}
          </div>
          <div class="variant-row__body">
            <div style="font-size:13px;line-height:1.65;color:var(--ink)">${esc(v.text)}</div>
            <div class="script-meta" style="margin-top:6px">
              <span>权重 <b>${v.weight}%</b></span>
              <span>发送 <b>${fmt(v.sent)}</b></span>
              <span>回复 <b>${fmt(v.replied)}</b></span>
              ${v.wechatAdded != null ? `<span>加微 <b>${fmt(v.wechatAdded)}</b></span>` : ''}
              <span>加微转化率 <b style="${v.convRate != null && v.convRate < 8 ? 'color:var(--danger)' : ''}">${conv}</b></span>
            </div>
            ${v.r2Note ? `<div class="r2-flag" style="margin-top:7px">⚑ R2 自动切换：${esc(v.r2Note)}</div>` : ''}
          </div>
        </div>`;
    }).join('');

    return `
      <div class="card script-card">
        <div class="card__head">
          <span class="card__title">${esc(s.name)}</span>
          ${s.isMain ? '<span class="tag tag--deal">主话术</span>' : ''}
          <span class="card__spacer"></span>
          <span class="card__hint">${esc(s.intro)}</span>
        </div>
        <div>
          <div style="font-size:12px;font-weight:600;color:var(--ink-soft);margin-bottom:6px">企微欢迎语（加微后自动发送）</div>
          <div class="script-text" style="margin-bottom:12px">${esc(s.welcomeMsg)}</div>
        </div>
        <div>${variants}</div>
      </div>`;
  }

  /* ═══════════════════════════════════════════
     视图：抖音账号（IMP-006 详情）
     ═══════════════════════════════════════════ */
  async function viewAccounts() {
    const [accs, schedAccs, schedStats, schedConfig] = await Promise.all([
      API.getAccounts(),
      API.fetchSchedulerAccounts().catch(() => []),
      API.fetchSchedulerStats().catch(() => ({ totalSent: 0, successRate: 0, activeAccounts: 0, perAccount: [] })),
      API.fetchSchedulerConfig().catch(() => ({ strategy: 'round_robin', healthThresholdWarn: 60, healthThresholdCritical: 30, maxConcurrent: 3 })),
    ]);
    // 合并调度状态到账号对象
    const schedMap = {};
    (schedAccs || []).forEach((s) => { schedMap[s.accountId] = s; });
    const merged = accs.map((a) => ({ ...a, sched: schedMap[a.id] || null }));

    main.innerHTML = `
      <div class="view">
        ${pageHead({
          icon: 'phone', title: '抖音账号', desc: '账号健康度 = 日频 + 封禁史 + 登录态 + 退订率 + 投诉率（IMP-006）',
          actions: '<button type="button" class="btn btn--primary btn--sm" id="btn-add-acc">+ 绑定账号</button>',
        })}
        ${schedulerConfigPanel(schedConfig)}
        ${schedulerStatsRow(schedStats)}
        <div class="grid grid--2">
          ${merged.map((a) => accCard(a)).join('')}
        </div>
      </div>`;

    $('#btn-add-acc').addEventListener('click', () => {
      openModal(`
        <h2 id="modal-title">绑定抖音账号</h2>
        <div class="confirm-note" style="margin-bottom:14px">真实发送必须先通过基础防封（IMP-004）+ 安全模式护栏，禁止单独无防护发送（PRD 工程纪律）。</div>
        <div class="field"><label for="acc-cookie">登录凭证（Cookie / 二维码扫码）</label>
          <textarea id="acc-cookie" placeholder="粘贴 MediaCrawler 登录凭证，或点击下方扫码"></textarea></div>
        <div class="field"><label for="acc-daily">该账号日频上限</label><input type="number" id="acc-daily" value="80"></div>
        <div class="modal__foot">
          <button type="button" class="btn" data-close>取消</button>
          <button type="button" class="btn btn--primary" id="acc-save" data-close>绑定（演示态）</button>
        </div>`);
      $('#acc-save').addEventListener('click', () => toast('账号已绑定（演示态，未落库）'));
    });

    bindSchedulerPanel();

    $$('[data-acc-safe]').forEach((b) => b.addEventListener('click', () => {
      openModal(`
        <h2 id="modal-title">安全模式详情（IMP-004）</h2>
        <dl class="kv">
          <dt>触发来源</dt><dd>health_score = 38 &lt; 阈值 40（自动联动）</dd>
          <dt>触发时间</dt><dd>2026-08-25 07:55</dd>
          <dt>影响</dt><dd>全量暂停新触达 · 已发队列只读 · 该账号不再发送</dd>
          <dt>退出条件</dt><dd>需人工确认（防误恢复），写入审计日志</dd>
        </dl>
        <div class="modal__foot"><button type="button" class="btn" data-close>关闭</button></div>`);
    }));
  }

  /* 账号卡（独立布局，不复用 healthRow 的列表网格） */
  function accCard(a) {
    const s = a.sched || {};
    const healthCls = a.healthScore >= 60 ? 'ok' : a.healthScore >= 30 ? 'warn' : 'danger';
    const fill = a.healthScore >= 60 ? 'var(--ok)' : a.healthScore >= 30 ? 'var(--warn)' : 'var(--danger)';
    const statusMap = {
      healthy: ['healthy', '健康'], throttled40: ['throttled', 'R1 降速 40/天'],
      paused_24h: ['throttled', '暂停 24h'], safe_mode: ['safemode', '安全模式'], banned: ['banned', '已封禁'],
    };
    const [tagCls, statusLabel] = statusMap[a.limitStatus] || ['low', a.limitStatus];
    // 调度状态标签
    const schedStatusMap = {
      active: ['healthy', '可调度'], paused: ['low', '已暂停'], throttled: ['throttled', '限流中'],
      safe_mode: ['safemode', '安全模式'], banned: ['banned', '已封禁'],
    };
    const schedStatus = s.status || 'active';
    const [schedTagCls, schedStatusLabel] = schedStatusMap[schedStatus] || ['low', schedStatus];
    const factorLabels = { dailyFreq: '日频', banHistory: '封禁史', login: '登录态', unsubscribe: '退订率', complaint: '投诉率' };
    const factors = Object.entries(a.factors).map(([k, v]) =>
      `<span class="factor factor--${v}">${factorLabels[k]}${v === 'bad' ? '✕' : v === 'warn' ? '△' : '✓'}</span>`).join('');
    const note = a.r1Note || a.r3Note || a.lastBanReason || '';
    const danger = a.healthScore < 30 || a.limitStatus === 'banned';
    const todaySent = s.todaySent !== undefined ? s.todaySent : a.dailyOutreach;
    const remaining = s.remainingQuota !== undefined ? s.remainingQuota : Math.max(0, (a.dailyLimit || 0) - todaySent);
    return `
      <div class="acc-card ${danger ? 'acc-card--danger' : ''}">
        <div class="acc-card__head">
          <span class="avatar" style="background:hsl(${a.avatarHue} 42% 42%)">${esc(a.nickname.slice(0, 1))}</span>
          <div class="acc-card__name">${esc(a.nickname)}
            <span class="acc-card__sub">抖音号 · 更新于 ${esc(a.updatedAt)}</span></div>
          <span class="acc-card__spacer"></span>
          <span class="tag tag--${tagCls}">${statusLabel}</span>
        </div>
        <div class="acc-card__row">
          <span class="gauge__num gauge__num--${healthCls}">${a.healthScore}</span>
          <span class="gauge__track"><span class="gauge__fill" style="display:block;width:${a.healthScore}%;background:${fill}"></span></span>
          <span class="acc-card__meta">今日 ${todaySent}/${a.dailyLimit || '—'} · 剩余 ${remaining}</span>
        </div>
        <div class="acc-card__row" style="margin-top:4px;gap:8px;flex-wrap:wrap">
          <span class="tag tag--${schedTagCls}" style="font-size:11px">调度：${schedStatusLabel}</span>
          <span class="acc-card__meta" style="font-size:11.5px">权重 ${s.weight || a.weight || 1}</span>
          ${s.successRate !== undefined ? `<span class="acc-card__meta" style="font-size:11.5px">成功率 ${s.successRate}%</span>` : ''}
        </div>
        <div class="acc-card__note">${note ? esc(note) : '五因子健康分 · 暂无异动'}</div>
        <div class="acc-card__footer">
          <span class="factor-grid">${factors}</span>
          ${a.limitStatus === 'safe_mode' ? '<button type="button" class="btn btn--sm" data-acc-safe>安全模式详情</button>' : ''}
        </div>
      </div>`;
  }


  /* ═══════════════════════════════════════════
     调度器：配置面板 + 统计卡片（多账号调度模块）
     ═══════════════════════════════════════════ */

  function schedulerConfigPanel(cfg) {
    const strategyLabel = { round_robin: '轮询', weighted: '按权重', health_based: '按健康分' };
    return `
      <details class="card" style="margin-bottom:14px;padding:0;overflow:hidden">
        <summary style="cursor:pointer;padding:14px 18px;font-family:var(--font-display);font-size:14px;display:flex;align-items:center;gap:8px;list-style:none">
          <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--brand)"></span>
          多账号调度配置
          <span style="margin-left:auto;font-size:12px;color:var(--ink-faint);font-family:var(--font-body)">当前策略：${strategyLabel[cfg.strategy] || cfg.strategy} · 最大并发 ${cfg.maxConcurrent}</span>
        </summary>
        <div style="padding:0 18px 16px;border-top:1px solid var(--line)">
          <div class="grid grid--2" style="margin-top:14px">
            <div class="field"><label>调度策略</label>
              <select id="sched-strategy">
                <option value="round_robin" ${cfg.strategy === 'round_robin' ? 'selected' : ''}>轮询（round_robin）</option>
                <option value="weighted" ${cfg.strategy === 'weighted' ? 'selected' : ''}>按权重（weighted）</option>
                <option value="health_based" ${cfg.strategy === 'health_based' ? 'selected' : ''}>按健康分（health_based）</option>
              </select></div>
            <div class="field"><label>最大并发账号数</label>
              <input type="number" id="sched-max-concurrent" value="${cfg.maxConcurrent}" min="1" max="20"></div>
            <div class="field"><label>健康分警告阈值（低于此值降速50%）</label>
              <input type="number" id="sched-threshold-warn" value="${cfg.healthThresholdWarn}" min="0" max="100"></div>
            <div class="field"><label>健康分严重阈值（低于此值不分配）</label>
              <input type="number" id="sched-threshold-critical" value="${cfg.healthThresholdCritical}" min="0" max="100"></div>
          </div>
          <div style="margin-top:12px;text-align:right">
            <button type="button" class="btn btn--primary btn--sm" id="sched-save-config">保存配置</button>
          </div>
        </div>
      </details>`;
  }

  function schedulerStatsRow(stats) {
    if (!stats || !stats.perAccount) return '';
    return `
      <div class="grid grid--3" style="margin-bottom:14px">
        ${metric('今日总发送量', stats.totalSent || 0, '所有账号合计', 'brand')}
        ${metric('发送成功率', (stats.successRate || 0) + '%', '成功 / 总发送', stats.successRate >= 80 ? 'brand' : 'danger')}
        ${metric('活跃可调度账号', stats.activeAccounts || 0, '未限流 / 未达日频 / 健康分达标', 'brand')}
      </div>`;
  }

  function bindSchedulerPanel() {
    const saveBtn = document.getElementById('sched-save-config');
    if (!saveBtn) return;
    saveBtn.addEventListener('click', async () => {
      try {
        await API.updateSchedulerConfig({
          strategy: document.getElementById('sched-strategy').value,
          maxConcurrent: parseInt(document.getElementById('sched-max-concurrent').value, 10),
          healthThresholdWarn: parseInt(document.getElementById('sched-threshold-warn').value, 10),
          healthThresholdCritical: parseInt(document.getElementById('sched-threshold-critical').value, 10),
        });
        toast('调度配置已保存', 'ok');
        viewAccounts();
      } catch (e) {
        toast('保存失败：' + e.message, 'warn');
      }
    });
  }

  /* ═══════════════════════════════════════════
     视图：上手向导（IMP-011）
     ═══════════════════════════════════════════ */
  const WIZARD_STEPS = ['绑定账号', '选行业话术', '设置频控', '启动任务', '看结果'];

  async function viewWizard() {
    const [, templates] = await Promise.all([API.getRuntime(), API.getScriptTemplates()]);
    const wiz = { step: 0, data: {} };

    main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'rocket', title: '上手向导', desc: '非技术老板 30 分钟「填号即跑」（US4 / IMP-011）' })}
        <div class="wizard">
          <div class="wizard__steps">
            ${WIZARD_STEPS.map((s, i) => `
              <div class="wizard__step" data-step="${i}">
                <span class="wizard__step-no">${i + 1}</span><span>${s}</span>
              </div>`).join('')}
          </div>
          <div class="card" style="padding:22px 26px">
            <div id="wiz-panel"></div>
          </div>
        </div>
      </div>`;

    const panel = $('#wiz-panel');

    function renderStep() {
      $$('.wizard__step').forEach((el) => {
        const i = +el.dataset.step;
        el.className = 'wizard__step' + (i < wiz.step ? ' wizard__step--done' : i === wiz.step ? ' wizard__step--active' : '');
        el.querySelector('.wizard__step-no').textContent = i < wiz.step ? '✓' : i + 1;
      });
      panel.innerHTML = `<div class="wizard__panel">${stepHtml(wiz.step)}</div>`;
      bindStep(wiz.step);
    }

    function stepHtml(i) {
      switch (i) {
        case 0: return `
          <h2 style="font-family:var(--font-display);font-size:17px;margin-bottom:6px">第一步 · 绑定抖音账号</h2>
          <p style="font-size:12.5px;color:var(--ink-faint);margin-bottom:16px">扫码或粘贴登录凭证，系统只做采集与合规触达，不碰账号密码</p>
          <div class="field"><label>账号昵称（备注名）</label><input type="text" id="wz-nick" placeholder="例：装修案例·阿明"></div>
          <div class="field"><label>登录方式</label>
            <div class="radio-cards">
              <label class="radio-card radio-card--checked"><input type="radio" name="wz-login" checked>
                <span class="radio-card__body"><span class="radio-card__title">扫码登录（推荐）</span><span class="radio-card__desc">弹出二维码，抖音 App 扫码即完成</span></span></label>
              <label class="radio-card"><input type="radio" name="wz-login">
                <span class="radio-card__body"><span class="radio-card__title">粘贴 Cookie</span><span class="radio-card__desc">适合已在电脑登录的账号</span></span></label>
            </div></div>`;
        case 1: return `
          <h2 style="font-family:var(--font-display);font-size:17px;margin-bottom:6px">第二步 · 选择行业话术包</h2>
          <p style="font-size:12.5px;color:var(--ink-faint);margin-bottom:16px">不懂话术也能跑：选行业即内置高转化模板 + 自动 AB 变体（IMP-005）</p>
          <div class="radio-cards">
            <label class="radio-card radio-card--checked"><input type="radio" name="wz-tpl" checked data-tpl="装修">
              <span class="radio-card__body"><span class="radio-card__title">装修行业包（8 条模板）</span>
              <span class="radio-card__desc">首次触达 ×3 · 报价跟进 ×2 · 加微引导 ×3，含企微欢迎语</span></span></label>
            <label class="radio-card"><input type="radio" name="wz-tpl" disabled>
              <span class="radio-card__body"><span class="radio-card__title">教育 / 医疗（v2 开放）</span>
              <span class="radio-card__desc">强监管行业模板包规划中</span></span></label>
          </div>`;
        case 2: return `
          <h2 style="font-family:var(--font-display);font-size:17px;margin-bottom:6px">第三步 · 设置频控</h2>
          <p style="font-size:12.5px;color:var(--ink-faint);margin-bottom:16px">频控越保守，账号越安全。系统会在风险升高时自动降速</p>
          <div class="grid grid--2">
            <div class="field"><label>单账号每日私信上限</label>
              <select id="wz-freq">
                <option value="40">40 条 / 天（最安全，推荐新号）</option>
                <option value="60" selected>60 条 / 天（均衡）</option>
                <option value="80">80 条 / 天（R1 阈值，达到自动降速）</option>
              </select></div>
            <div class="field"><label>发送间隔</label>
              <select id="wz-gap">
                <option>随机 3-8 分钟（拟人，推荐）</option>
                <option>随机 1-3 分钟（较快，风险略高）</option>
              </select></div>
          </div>
          <div class="confirm-note">安全兜底已默认开启：健康分 &lt; 40 自动进入安全模式 · 黑名单日增 &gt; 3% 全量暂停（R3）</div>`;
        case 3: return `
          <h2 style="font-family:var(--font-display);font-size:17px;margin-bottom:6px">第四步 · 启动任务</h2>
          <p style="font-size:12.5px;color:var(--ink-faint);margin-bottom:16px">确认以下配置，点击启动即开始采集评论区 + 合规私信</p>
          <dl class="kv" style="font-size:14px">
            <dt>账号</dt><dd>${esc(wiz.data.nick || '装修案例·阿明')}（扫码登录）</dd>
            <dt>话术包</dt><dd>装修行业包 · 主话术 + 2 变体自动 AB</dd>
            <dt>频控</dt><dd>${esc(wiz.data.freq || '60')} 条/天 · 随机 3-8 分钟间隔</dd>
            <dt>安全护栏</dt><dd style="color:var(--ok)">健康分预警 + R1/R2/R3 全开 + 一键暂停</dd>
          </dl>`;
        case 4: return `
          <div style="text-align:center;padding:16px 0">
            <div class="empty__ico" style="margin:0 auto 8px">${ico('rocket', 'ico--brand')}</div>
            <h2 style="font-family:var(--font-display);font-size:20px;margin:8px 0 6px">任务已启动！</h2>
            <p style="font-size:13px;color:var(--ink-soft);max-width:400px;margin:0 auto 18px">
              系统正在监控目标视频评论区。发现高意向评论会自动私信引导加企微，
              你可以随时回「工作台」看今天该做什么，或点右上角「一键暂停」。
            </p>
            <div style="display:flex;gap:10px;justify-content:center">
              <a class="btn btn--primary" href="#/workbench">去工作台 →</a>
              <a class="btn" href="#/leads">查看线索</a>
            </div>
          </div>`;
      }
    }

    function bindStep(i) {
      const foot = document.createElement('div');
      foot.className = 'wizard__foot';
      if (i > 0 && i < 4) foot.innerHTML += `<button type="button" class="btn" id="wz-prev">← 上一步</button>`;
      else foot.innerHTML += '<span></span>';
      if (i >= 0 && i <= 2) foot.innerHTML += `<button type="button" class="btn btn--primary btn--lg" id="wz-next">下一步 →</button>`;
      if (i === 3) foot.innerHTML += `<button type="button" class="btn btn--primary btn--lg" id="wz-launch">${ico('rocket', 'ico--lg')} 启动任务</button>`;
      panel.appendChild(foot);

      const next = $('#wz-next');
      if (next) next.addEventListener('click', () => {
        if (wiz.step === 0) wiz.data.nick = ($('#wz-nick')?.value || '').trim();
        if (wiz.step === 2) wiz.data.freq = $('#wz-freq')?.value;
        wiz.step++; renderStep();
      });
      const prev = $('#wz-prev');
      if (prev) prev.addEventListener('click', () => { wiz.step--; renderStep(); });
      const launch = $('#wz-launch');
      if (launch) launch.addEventListener('click', async () => {
        await API.submitWizard(wiz.data);
        wiz.step = 4; renderStep();
        toast('任务已启动（演示态）');
      });

      $$('.radio-card input', panel).forEach((inp) => inp.addEventListener('change', () => {
        $$('.radio-card', panel).forEach((c) => c.classList.remove('radio-card--checked'));
        inp.closest('.radio-card').classList.add('radio-card--checked');
      }));
    }

    renderStep();
  }

  /* ═══════════════════════════════════════════
     视图：采集配置（MediaCrawler 采集任务管理）
     ═══════════════════════════════════════════ */
  const crawlState = { tasks: [], polling: null };

  async function viewCrawl() {
    if (crawlState.polling) { clearInterval(crawlState.polling); crawlState.polling = null; }

    main.innerHTML = `
      <div class="view">
        ${pageHead({
          icon: 'i-rocket',
          title: '采集配置',
          desc: '从抖音评论区自动采集高意向用户，入库为线索',
          actions: `<button type="button" class="btn btn--primary" id="crawl-new-btn">${ico('i-rocket')} 新建采集任务</button>`,
        })}

        <div class="grid grid--main-side">
          <div class="card" id="crawl-list-card">
            <div class="card__head">
              <span class="card__title">采集任务</span>
              <span class="card__hint" id="crawl-list-hint">加载中…</span>
            </div>
            <div id="crawl-task-list">${emptyState('i-empty', '暂无采集任务，点击右上角新建')}</div>
          </div>

          <div class="card" style="min-width:320px">
            <div class="card__head">
              <span class="card__title">新建任务</span>
              <span class="card__hint">至少填写一项采集入口</span>
            </div>
            <div class="field">
              <label for="crawl-name">任务名称（可选）</label>
              <input type="text" id="crawl-name" placeholder="自动生成，如：关键词:装修">
            </div>
            <div class="field">
              <label for="crawl-keyword">搜索关键词</label>
              <input type="text" id="crawl-keyword" placeholder="例：装修 报价">
            </div>
            <div class="field">
              <label for="crawl-competitor">对标账号</label>
              <input type="text" id="crawl-competitor" placeholder="抖音昵称或主页URL">
            </div>
            <div class="field">
              <label for="crawl-video">指定视频URL</label>
              <input type="text" id="crawl-video" placeholder="https://www.douyin.com/video/...">
            </div>
            <div class="field">
              <label for="crawl-source">来源归因</label>
              <select id="crawl-source">
                <option value="own_comment">自有视频评论区</option>
                <option value="competitor">对标账号监控</option>
              </select>
            </div>
            <div class="field">
              <label for="crawl-intent">高意向关键词（逗号分隔，加权评分）</label>
              <input type="text" id="crawl-intent" placeholder="多少钱,报价,加微信,怎么合作">
            </div>
            <div class="field">
              <label for="crawl-max">最大采集条数</label>
              <input type="number" id="crawl-max" value="100" min="1" max="10000">
            </div>
            <button type="button" class="btn btn--primary btn--block" id="crawl-create-btn">创建任务</button>
          </div>
        </div>
      </div>`;

    $('#crawl-new-btn').addEventListener('click', () => {
      $('#crawl-keyword').focus();
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
    });

    $('#crawl-create-btn').addEventListener('click', async () => {
      const payload = {
        name: $('#crawl-name').value.trim(),
        keyword: $('#crawl-keyword').value.trim(),
        competitor_account: $('#crawl-competitor').value.trim(),
        video_url: $('#crawl-video').value.trim(),
        source: $('#crawl-source').value,
        intent_keywords: $('#crawl-intent').value.trim(),
        max_comments: parseInt($('#crawl-max').value, 10) || 100,
        crawl_type: 'comment',
      };
      if (!payload.keyword && !payload.competitor_account && !payload.video_url) {
        toast('至少填写一项：关键词 / 对标账号 / 视频URL', 'warn');
        return;
      }
      try {
        await window.CrawlAPI.createTask(payload);
        toast('采集任务已创建');
        $('#crawl-name').value = '';
        $('#crawl-keyword').value = '';
        $('#crawl-competitor').value = '';
        $('#crawl-video').value = '';
        $('#crawl-intent').value = '';
        await loadCrawlTasks();
      } catch (e) {
        toast('创建失败：' + e.message, 'warn');
      }
    });

    await loadCrawlTasks();

    // 运行中任务自动刷新状态
    crawlState.polling = setInterval(async () => {
      const running = crawlState.tasks.filter((t) => t.status === 'running');
      if (running.length > 0) {
        for (const t of running) {
          try {
            const st = await window.CrawlAPI.getTaskStatus(t.id);
            const idx = crawlState.tasks.findIndex((x) => x.id === t.id);
            if (idx >= 0) {
              crawlState.tasks[idx] = { ...crawlState.tasks[idx], ...st };
            }
          } catch (e) { /* ignore */ }
        }
        renderCrawlTaskList();
      }
    }, 5000);
  }

  async function loadCrawlTasks() {
    try {
      crawlState.tasks = await window.CrawlAPI.listTasks();
      $('#crawl-list-hint').textContent = `共 ${crawlState.tasks.length} 个任务`;
      renderCrawlTaskList();
    } catch (e) {
      $('#crawl-list-hint').textContent = '加载失败';
      $('#crawl-task-list').innerHTML = emptyState('i-alert', '无法连接后端 API：' + e.message);
    }
  }

  function renderCrawlTaskList() {
    const list = $('#crawl-task-list');
    if (!crawlState.tasks.length) {
      list.innerHTML = emptyState('i-empty', '暂无采集任务');
      return;
    }
    const statusMeta = {
      pending: { label: '待启动', cls: 'status-pill--muted' },
      running: { label: '采集中', cls: 'status-pill--running' },
      completed: { label: '已完成', cls: 'status-pill--ok' },
      failed: { label: '失败', cls: 'status-pill--danger' },
    };
    list.innerHTML = crawlState.tasks.map((t) => {
      const sm = statusMeta[t.status] || statusMeta.pending;
      const progress = t.max_comments ? Math.min(100, Math.round((t.collected_count / t.max_comments) * 100)) : 0;
      const entryParts = [];
      if (t.keyword) entryParts.push(`关键词:${esc(t.keyword)}`);
      if (t.competitor_account) entryParts.push(`对标:${esc(t.competitor_account)}`);
      if (t.video_url) entryParts.push('指定视频');
      return `<div class="task-item" data-id="${esc(t.id)}">
        <div class="task-item__head">
          <span class="task-item__title">${esc(t.name || '未命名任务')}</span>
          <span class="status-pill ${sm.cls}">${sm.label}</span>
        </div>
        <div class="task-item__meta">
          ${entryParts.join(' · ') || '评论采集'}
          · 来源:${t.source === 'competitor' ? '对标监控' : '自有评论'}
          · 已采集 <b>${t.collected_count}</b> / 已入库 <b>${t.imported_count}</b>
        </div>
        ${t.status === 'running' ? `
        <div class="progress-bar"><div class="progress-bar__fill" style="width:${progress}%"></div></div>
        <div class="task-item__meta">进度 ${progress}%</div>` : ''}
        ${t.error_message ? `<div class="task-item__error">${esc(t.error_message)}</div>` : ''}
        <div class="task-item__actions">
          ${t.status === 'pending' || t.status === 'failed' ?
            `<button type="button" class="btn btn--primary btn--sm" data-action="start">启动采集</button>` : ''}
          ${t.status === 'running' ?
            `<button type="button" class="btn btn--sm" data-action="stop">停止</button>` : ''}
          <button type="button" class="btn btn--ghost btn--sm" data-action="delete">删除</button>
        </div>
      </div>`;
    }).join('');

    $$('#crawl-task-list [data-action]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        const item = e.target.closest('.task-item');
        const id = item.dataset.id;
        const action = e.target.dataset.action;
        try {
          if (action === 'start') {
            await window.CrawlAPI.startTask(id);
            toast('采集已启动');
          } else if (action === 'stop') {
            await window.CrawlAPI.stopTask(id);
            toast('采集已停止');
          } else if (action === 'delete') {
            await window.CrawlAPI.deleteTask(id);
            toast('任务已删除');
          }
          await loadCrawlTasks();
        } catch (err) {
          toast('操作失败：' + err.message, 'warn');
        }
      });
    });
  }

  /* ══════════ 导航角标 ══════════ */
  async function renderNavBadges() {
    try {
      const [leadsAll, accs, custs, fups, queue] = await Promise.all([
        API.getLeads(), API.getAccounts(), API.getCustomers(), API.getFollowups(), API.getDmQueue(),
      ]);
      $('#nav-leads-badge').textContent = leadsAll.length;

      const dmActionable = queue.filter((q) => ['pending_outreach', 'send_failed'].includes(q.status)).length;
      const dmBadge = $('#nav-dm-badge');
      if (dmActionable > 0) { dmBadge.hidden = false; dmBadge.textContent = dmActionable; }

      const accWarn = accs.filter((a) => a.healthScore < 40).length;
      const accBadge = $('#nav-acc-badge');
      if (accWarn > 0) { accBadge.hidden = false; accBadge.textContent = accWarn; }

      $('#nav-cust-badge').textContent = custs.filter((c) => !['won', 'lost'].includes(c.stage)).length;
      $('#nav-fu-badge').textContent = fups.filter((f) => !f.done).length;
    } catch (e) { /* 静默 */ }
  }

  /* ═══════════════════════════════════════════
     视图：系统设置（配置中心）
     ═══════════════════════════════════════════ */
  async function viewSettings() {
    const SettingsAPI = window.API || {};
    const tabs = [
      { key: 'ai', label: 'AI 设置' },
      { key: 'strategy', label: '话术策略' },
      { key: 'compliance', label: '合规规则' },
      { key: 'crawl', label: '采集设置' },
      { key: 'notification', label: '通知设置' },
    ];
    let activeTab = 'ai';
    let settingsData = null;

    main.innerHTML = pageHead({
      icon: 'i-gear',
      title: '系统设置',
      desc: '配置 AI 模型、话术策略、合规规则、采集参数与通知提醒',
    }) + `
      <div class="card" style="padding:0;overflow:hidden">
        <div class="settings-tabs" id="settings-tabs" style="display:flex;border-bottom:1px solid var(--border);background:var(--bg-soft)">
          ${tabs.map(t => `<button type="button" class="settings-tab-btn" data-tab="${t.key}" style="flex:1;padding:12px 8px;border:none;background:transparent;cursor:pointer;font-size:13px;color:var(--ink-soft);border-bottom:2px solid transparent;transition:all .2s">${t.label}</button>`).join('')}
        </div>
        <div id="settings-tab-content" style="padding:20px"></div>
      </div>
    `;

    // 加载配置
    try {
      settingsData = await SettingsAPI.fetchAllSettings();
    } catch (e) {
      settingsData = {
        ai: { provider: 'doubao', api_key: '', model: '', temperature: 0.7, timeout: 30 },
        strategy: { auto_score_threshold: 60, r2_switch_threshold: 8, daily_frequency_limit: 80 },
        compliance: { r1_threshold: 80, r1_enabled: true, r2_threshold: 8, r2_enabled: true, r3_threshold: 3, r3_enabled: true },
        crawl: { default_keywords: [], crawl_interval_minutes: 30, max_comments_per_video: 100 },
        notification: { new_dm_alert: true, followup_reminder: true, deal_alert: true },
      };
      toast('配置加载失败，使用默认值：' + e.message, 'warn');
    }

    function renderTab() {
      const container = $('#settings-tab-content');
      // 更新 tab 按钮样式
      $$('.settings-tab-btn').forEach(btn => {
        const active = btn.dataset.tab === activeTab;
        btn.style.color = active ? 'var(--brand-deep)' : 'var(--ink-soft)';
        btn.style.borderBottomColor = active ? 'var(--brand-deep)' : 'transparent';
        btn.style.fontWeight = active ? '600' : '400';
        btn.style.background = active ? 'var(--bg)' : 'transparent';
      });

      if (activeTab === 'ai') {
        const ai = settingsData.ai || {};
        container.innerHTML = `
          <div class="field" style="margin-bottom:16px">
            <label for="set-ai-provider">Provider</label>
            <select id="set-ai-provider" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
              <option value="doubao" ${ai.provider === 'doubao' ? 'selected' : ''}>豆包 (Doubao / Volcengine Ark)</option>
              <option value="dashscope" ${ai.provider === 'dashscope' ? 'selected' : ''}>通义千问 (DashScope)</option>
            </select>
          </div>
          <div class="field" style="margin-bottom:16px">
            <label for="set-ai-key">API Key</label>
            <input type="password" id="set-ai-key" value="${esc(ai.api_key || '')}" placeholder="sk-..." style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
          </div>
          <div class="field" style="margin-bottom:16px">
            <label for="set-ai-model">模型名称 / Endpoint ID</label>
            <input type="text" id="set-ai-model" value="${esc(ai.model || '')}" placeholder="如 ep-20240101-default 或 qwen-plus" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
          </div>
          <div class="field" style="margin-bottom:16px">
            <label for="set-ai-temp">温度 (Temperature)：<span id="set-ai-temp-val">${ai.temperature ?? 0.7}</span></label>
            <input type="range" id="set-ai-temp" min="0" max="2" step="0.1" value="${ai.temperature ?? 0.7}" style="width:100%">
          </div>
          <div class="field" style="margin-bottom:16px">
            <label for="set-ai-timeout">超时（秒）</label>
            <input type="number" id="set-ai-timeout" min="1" max="120" value="${ai.timeout ?? 30}" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
          </div>
          <div style="display:flex;gap:10px;align-items:center;margin-top:20px">
            <button type="button" class="btn btn--primary" id="set-ai-save">保存 AI 配置</button>
            <button type="button" class="btn" id="set-ai-test">测试连接</button>
            <span id="set-ai-test-result" style="font-size:12px;color:var(--ink-soft)"></span>
          </div>
        `;
        // 温度滑块实时显示
        $('#set-ai-temp').addEventListener('input', (e) => {
          $('#set-ai-temp-val').textContent = e.target.value;
        });
        // 保存
        $('#set-ai-save').addEventListener('click', async () => {
          const payload = {
            provider: $('#set-ai-provider').value,
            api_key: $('#set-ai-key').value,
            model: $('#set-ai-model').value,
            temperature: parseFloat($('#set-ai-temp').value),
            timeout: parseInt($('#set-ai-timeout').value, 10),
          };
          try {
            const result = await SettingsAPI.updateAISettings(payload);
            settingsData.ai = result;
            toast('AI 配置已保存，热更新生效');
          } catch (e) {
            toast('保存失败：' + e.message, 'warn');
          }
        });
        // 测试连接
        $('#set-ai-test').addEventListener('click', async () => {
          const resultEl = $('#set-ai-test-result');
          resultEl.textContent = '测试中...';
          resultEl.style.color = 'var(--ink-soft)';
          try {
            const result = await SettingsAPI.testAIConnection({
              provider: $('#set-ai-provider').value,
              api_key: $('#set-ai-key').value,
              model: $('#set-ai-model').value,
              timeout: parseInt($('#set-ai-timeout').value, 10) || 10,
            });
            if (result.success) {
              resultEl.textContent = '✓ ' + result.message;
              resultEl.style.color = 'var(--ok)';
            } else {
              resultEl.textContent = '✗ ' + result.message;
              resultEl.style.color = 'var(--danger)';
            }
          } catch (e) {
            resultEl.textContent = '✗ 测试请求失败：' + e.message;
            resultEl.style.color = 'var(--danger)';
          }
        });
      } else if (activeTab === 'strategy') {
        const s = settingsData.strategy || {};
        container.innerHTML = `
          <div class="field" style="margin-bottom:16px">
            <label for="set-st-score">自动评分阈值（≥此值自动标记高意向）</label>
            <input type="number" id="set-st-score" min="0" max="100" step="1" value="${s.auto_score_threshold ?? 60}" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
          </div>
          <div class="field" style="margin-bottom:16px">
            <label for="set-st-r2">R2 话术切换转化率阈值（%）</label>
            <input type="number" id="set-st-r2" min="0" max="100" step="0.5" value="${s.r2_switch_threshold ?? 8}" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
          </div>
          <div class="field" style="margin-bottom:16px">
            <label for="set-st-daily">单账号日频上限</label>
            <input type="number" id="set-st-daily" min="1" max="500" step="1" value="${s.daily_frequency_limit ?? 80}" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
          </div>
          <div style="margin-top:20px">
            <button type="button" class="btn btn--primary" id="set-st-save">保存策略配置</button>
          </div>
        `;
        $('#set-st-save').addEventListener('click', async () => {
          const payload = {
            auto_score_threshold: parseFloat($('#set-st-score').value),
            r2_switch_threshold: parseFloat($('#set-st-r2').value),
            daily_frequency_limit: parseInt($('#set-st-daily').value, 10),
          };
          try {
            const result = await SettingsAPI.updateStrategySettings(payload);
            settingsData.strategy = result;
            toast('话术策略配置已保存');
          } catch (e) {
            toast('保存失败：' + e.message, 'warn');
          }
        });
      } else if (activeTab === 'compliance') {
        const c = settingsData.compliance || {};
        const ruleRow = (id, label, thresholdVal, enabledVal, desc) => `
          <div style="padding:14px 0;border-bottom:1px solid var(--border)">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
              <strong style="font-size:14px">${label}</strong>
              <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
                <input type="checkbox" id="set-c-${id}-enabled" ${enabledVal ? 'checked' : ''} style="accent-color:var(--brand-deep);width:16px;height:16px">
                启用
              </label>
            </div>
            <div style="font-size:12px;color:var(--ink-soft);margin-bottom:8px">${desc}</div>
            <div class="field" style="margin-bottom:0">
              <label for="set-c-${id}-threshold">阈值</label>
              <input type="number" id="set-c-${id}-threshold" step="0.5" value="${thresholdVal}" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
            </div>
          </div>
        `;
        container.innerHTML = `
          ${ruleRow('r1', 'R1 · 单账号日频限流', c.r1_threshold ?? 80, c.r1_enabled ?? true, '单账号日发送量 ≥ 阈值时，自动降速至 40 条/天')}
          ${ruleRow('r2', 'R2 · 话术变体自动切换', c.r2_threshold ?? 8, c.r2_enabled ?? true, '话术变体加微转化率 < 阈值（样本≥30）时，自动切换到更优变体')}
          ${ruleRow('r3', 'R3 · 黑名单激增安全模式', c.r3_threshold ?? 3, c.r3_enabled ?? true, '黑名单日增 > 阈值（%）或账号封禁时，触发全量暂停安全模式')}
          <div style="margin-top:20px">
            <button type="button" class="btn btn--primary" id="set-c-save">保存合规规则</button>
          </div>
        `;
        $('#set-c-save').addEventListener('click', async () => {
          const payload = {
            r1_threshold: parseFloat($('#set-c-r1-threshold').value),
            r1_enabled: $('#set-c-r1-enabled').checked,
            r2_threshold: parseFloat($('#set-c-r2-threshold').value),
            r2_enabled: $('#set-c-r2-enabled').checked,
            r3_threshold: parseFloat($('#set-c-r3-threshold').value),
            r3_enabled: $('#set-c-r3-enabled').checked,
          };
          try {
            const result = await SettingsAPI.updateComplianceSettings(payload);
            settingsData.compliance = result;
            toast('合规规则配置已保存');
          } catch (e) {
            toast('保存失败：' + e.message, 'warn');
          }
        });
      } else if (activeTab === 'crawl') {
        const cr = settingsData.crawl || {};
        const keywords = Array.isArray(cr.default_keywords) ? cr.default_keywords.join(', ') : '';
        container.innerHTML = `
          <div class="field" style="margin-bottom:16px">
            <label for="set-cr-keywords">默认采集关键词（逗号分隔）</label>
            <textarea id="set-cr-keywords" rows="3" placeholder="如：全屋装修,旧房改造,装修报价" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink);resize:vertical">${esc(keywords)}</textarea>
          </div>
          <div class="field" style="margin-bottom:16px">
            <label for="set-cr-interval">采集间隔（分钟）</label>
            <input type="number" id="set-cr-interval" min="1" max="1440" value="${cr.crawl_interval_minutes ?? 30}" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
          </div>
          <div class="field" style="margin-bottom:16px">
            <label for="set-cr-max">单视频最大评论采集数</label>
            <input type="number" id="set-cr-max" min="1" max="10000" value="${cr.max_comments_per_video ?? 100}" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink)">
          </div>
          <div style="margin-top:20px">
            <button type="button" class="btn btn--primary" id="set-cr-save">保存采集配置</button>
          </div>
        `;
        $('#set-cr-save').addEventListener('click', async () => {
          const kwRaw = $('#set-cr-keywords').value;
          const keywords = kwRaw.split(/[,，]/).map(s => s.trim()).filter(Boolean);
          const payload = {
            default_keywords: keywords,
            crawl_interval_minutes: parseInt($('#set-cr-interval').value, 10),
            max_comments_per_video: parseInt($('#set-cr-max').value, 10),
          };
          try {
            const result = await SettingsAPI.updateCrawlSettings(payload);
            settingsData.crawl = result;
            toast('采集配置已保存');
          } catch (e) {
            toast('保存失败：' + e.message, 'warn');
          }
        });
      } else if (activeTab === 'notification') {
        const n = settingsData.notification || {};
        const notifRow = (id, label, desc, checked) => `
          <div style="padding:14px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
            <div>
              <strong style="font-size:14px;display:block;margin-bottom:4px">${label}</strong>
              <span style="font-size:12px;color:var(--ink-soft)">${desc}</span>
            </div>
            <label style="cursor:pointer">
              <input type="checkbox" id="set-n-${id}" ${checked ? 'checked' : ''} style="accent-color:var(--brand-deep);width:18px;height:18px">
            </label>
          </div>
        `;
        container.innerHTML = `
          ${notifRow('dm', '新私信提醒', '收到用户主动私信时通知', n.new_dm_alert ?? true)}
          ${notifRow('followup', '跟进提醒', '今日有待跟进客户时通知', n.followup_reminder ?? true)}
          ${notifRow('deal', '成交提醒', '客户成交时通知', n.deal_alert ?? true)}
          <div style="margin-top:20px">
            <button type="button" class="btn btn--primary" id="set-n-save">保存通知设置</button>
          </div>
        `;
        $('#set-n-save').addEventListener('click', async () => {
          const payload = {
            new_dm_alert: $('#set-n-dm').checked,
            followup_reminder: $('#set-n-followup').checked,
            deal_alert: $('#set-n-deal').checked,
          };
          try {
            const result = await SettingsAPI.updateNotificationSettings(payload);
            settingsData.notification = result;
            toast('通知设置已保存');
          } catch (e) {
            toast('保存失败：' + e.message, 'warn');
          }
        });
      }
    }

    // Tab 切换
    $$('.settings-tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        activeTab = btn.dataset.tab;
        renderTab();
      });
    });

    renderTab();
  }


  /* ═══════════════════════════════════════════
     视图：数据备份与恢复
     ═══════════════════════════════════════════ */
  async function viewBackup() {
    const BK = (window.API || {}).apiBackup || {};
    let backups = [];
    let settings = { enabled: false, retain_count: 7, last_auto_backup_at: null };

    main.innerHTML = pageHead({
      icon: 'i-clock',
      title: '数据备份与恢复',
      desc: '手动备份、自动备份调度、全量数据导出与恢复',
    }) + `
      <div class="backup-page">
        <!-- 操作区 -->
        <div class="card backup-actions">
          <div class="backup-actions__row">
            <button type="button" class="btn btn--primary" id="bk-create">
              <svg width="14" height="14" viewBox="0 0 24 24" style="margin-right:6px"><use href="#i-check"/></svg>立即备份
            </button>
            <button type="button" class="btn" id="bk-export">
              <svg width="14" height="14" viewBox="0 0 24 24" style="margin-right:6px"><use href="#i-arrow"/></svg>导出 JSON 全量
            </button>
            <span id="bk-action-status" class="backup-action-status"></span>
          </div>
        </div>

        <!-- 自动备份设置 -->
        <div class="card" style="margin-top:16px">
          <h3 style="font-size:15px;margin-bottom:14px">自动备份设置</h3>
          <div class="backup-settings-row">
            <label class="backup-toggle">
              <input type="checkbox" id="bk-auto-enabled">
              <span>启用自动备份</span>
            </label>
            <div class="backup-settings-field">
              <label for="bk-retain">保留最近份数</label>
              <input type="number" id="bk-retain" min="1" max="30" value="7" style="width:80px">
            </div>
            <button type="button" class="btn btn--primary btn--sm" id="bk-save-settings">保存设置</button>
          </div>
          <div id="bk-last-backup" style="font-size:12px;color:var(--ink-soft);margin-top:10px"></div>
        </div>

        <!-- 备份列表 -->
        <div class="card" style="margin-top:16px">
          <h3 style="font-size:15px;margin-bottom:14px">备份列表</h3>
          <div id="bk-list-container">
            <div style="text-align:center;padding:40px;color:var(--ink-faint)">加载中...</div>
          </div>
        </div>
      </div>
    `;

    /* ── 加载数据 ── */
    async function loadAll() {
      try {
        settings = await BK.getSettings();
      } catch (e) { /* use defaults */ }
      $('#bk-auto-enabled').checked = !!settings.enabled;
      $('#bk-retain').value = settings.retain_count || 7;
      $('#bk-last-backup').textContent = settings.last_auto_backup_at
        ? `上次自动备份：${settings.last_auto_backup_at}`
        : '尚未执行过自动备份';
      await loadList();
    }

    async function loadList() {
      const container = $('#bk-list-container');
      try {
        const res = await BK.list();
        backups = res.backups || [];
        if (!backups.length) {
          container.innerHTML = emptyState('i-empty', '暂无备份文件');
          return;
        }
        container.innerHTML = `
          <table class="backup-table">
            <thead>
              <tr>
                <th>文件名</th><th>大小</th><th>创建时间</th><th>类型</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              ${backups.map((b, i) => `
                <tr>
                  <td class="backup-filename">${esc(b.filename)}</td>
                  <td>${esc(b.size_display || '')}</td>
                  <td>${esc((b.created_at || '').replace('T', ' ').slice(0, 19))}</td>
                  <td><span class="badge badge--${b.type === 'auto' ? 'info' : b.type === 'pre_restore' ? 'warn' : 'default'}">${esc(b.type)}</span></td>
                  <td class="backup-ops">
                    <button type="button" class="btn btn--warn btn--sm bk-restore" data-idx="${i}">恢复</button>
                    <button type="button" class="btn btn--danger btn--sm bk-delete" data-idx="${i}">删除</button>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
        // 绑定恢复按钮
        $$('.bk-restore', container).forEach(btn => {
          btn.addEventListener('click', () => {
            const b = backups[parseInt(btn.dataset.idx, 10)];
            confirmRestore(b.filename);
          });
        });
        // 绑定删除按钮
        $$('.bk-delete', container).forEach(btn => {
          btn.addEventListener('click', () => {
            const b = backups[parseInt(btn.dataset.idx, 10)];
            confirmDelete(b.filename);
          });
        });
      } catch (e) {
        container.innerHTML = `<div style="color:var(--danger);text-align:center;padding:20px">加载失败：${esc(e.message)}</div>`;
      }
    }

    /* ── 立即备份 ── */
    $('#bk-create').addEventListener('click', async () => {
      const status = $('#bk-action-status');
      status.textContent = '备份中...';
      status.style.color = 'var(--ink-soft)';
      try {
        const result = await BK.create();
        status.textContent = '✓ 备份成功：' + result.filename;
        status.style.color = 'var(--ok)';
        toast('备份创建成功');
        await loadList();
      } catch (e) {
        status.textContent = '✗ 备份失败：' + e.message;
        status.style.color = 'var(--danger)';
      }
    });

    /* ── 导出 JSON ── */
    $('#bk-export').addEventListener('click', async () => {
      const status = $('#bk-action-status');
      status.textContent = '导出中...';
      status.style.color = 'var(--ink-soft)';
      try {
        const result = await BK.exportJson();
        status.textContent = '✓ 导出成功：' + result.filename;
        status.style.color = 'var(--ok)';
        toast('全量导出完成');
        if (result.file_url) {
          window.open(result.file_url, '_blank');
        }
      } catch (e) {
        status.textContent = '✗ 导出失败：' + e.message;
        status.style.color = 'var(--danger)';
      }
    });

    /* ── 保存设置 ── */
    $('#bk-save-settings').addEventListener('click', async () => {
      const payload = {
        enabled: $('#bk-auto-enabled').checked,
        retain_count: parseInt($('#bk-retain').value, 10) || 7,
      };
      try {
        settings = await BK.updateSettings(payload);
        toast('自动备份设置已保存');
        $('#bk-last-backup').textContent = settings.last_auto_backup_at
          ? `上次自动备份：${settings.last_auto_backup_at}`
          : '尚未执行过自动备份';
      } catch (e) {
        toast('保存失败：' + e.message, 'warn');
      }
    });

    /* ── 恢复确认弹窗 ── */
    function confirmRestore(filename) {
      openModal({
        title: '确认恢复备份？',
        body: `<p style="color:var(--ink)">即将从 <strong>${esc(filename)}</strong> 恢复数据库。</p>
               <p style="color:var(--warn);margin-top:10px">⚠ 恢复将覆盖当前数据，系统已自动创建恢复前备份，确定继续？</p>`,
        confirmText: '确认恢复',
        confirmClass: 'btn--danger',
        onConfirm: async () => {
          try {
            const result = await BK.restore(filename);
            if (result.success) {
              toast('恢复成功，建议重启后端服务使数据完全生效');
            } else {
              toast('恢复失败：' + result.message, 'warn');
            }
            closeModal();
            await loadList();
          } catch (e) {
            toast('恢复请求失败：' + e.message, 'warn');
            closeModal();
          }
        },
      });
    }

    /* ── 删除确认 ── */
    function confirmDelete(filename) {
      openModal({
        title: '确认删除备份？',
        body: `<p>即将删除备份文件 <strong>${esc(filename)}</strong>，此操作不可撤销。</p>`,
        confirmText: '确认删除',
        confirmClass: 'btn--danger',
        onConfirm: async () => {
          try {
            await BK.delete(filename);
            toast('备份已删除');
            closeModal();
            await loadList();
          } catch (e) {
            toast('删除失败：' + e.message, 'warn');
            closeModal();
          }
        },
      });
    }

    /* ── 简化弹窗（复用全局 modal） ── */
    function openModal({ title, body, confirmText, confirmClass, onConfirm }) {
      const modal = $('#modal'), bd = $('#modal-backdrop');
      modal.innerHTML = `
        <div class="modal__body" style="padding:24px">
          <h3 style="margin-bottom:14px;font-size:16px">${title}</h3>
          <div style="margin-bottom:20px">${body}</div>
          <div style="display:flex;gap:10px;justify-content:flex-end">
            <button type="button" class="btn" id="modal-cancel">取消</button>
            <button type="button" class="btn ${confirmClass || 'btn--primary'}" id="modal-confirm">${confirmText || '确认'}</button>
          </div>
        </div>
      `;
      modal.hidden = false; bd.hidden = false;
      requestAnimationFrame(() => { modal.classList.add('show'); bd.classList.add('show'); });
      $('#modal-cancel').addEventListener('click', closeModal);
      $('#modal-confirm').addEventListener('click', onConfirm);
      bd.onclick = closeModal;
      document.addEventListener('keydown', escCloseOnce);
    }
    function escCloseOnce(e) {
      if (e.key === 'Escape') { closeModal(); }
    }

    await loadAll();
  }

  /* ═══════════════════════════════════════════
     视图：系统监控（P4-C）
     ═══════════════════════════════════════════ */
  let _monitorTimer = null;
  const MONITOR_STATUS_COLORS = {
    ok: '#22c55e', warning: '#f59e0b', warn: '#f59e0b',
    error: '#ef4444', stopped: '#9ca3af', unknown: '#9ca3af',
  };
  const MONITOR_SEV_COLORS = { danger: '#ef4444', warn: '#f59e0b', info: '#35597e', ok: '#22c55e' };

  function _monUptime(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + '天 ' + h + '时';
    if (h > 0) return h + '时 ' + m + '分';
    return m + '分 ' + (sec % 60) + '秒';
  }
  function _monDot(status) {
    const color = MONITOR_STATUS_COLORS[status] || '#9ca3af';
    return `<span class="mon-dot" style="background:${color}"></span>`;
  }

  async function viewMonitor() {
    if (_monitorTimer) { clearInterval(_monitorTimer); _monitorTimer = null; }

    main.innerHTML = `<div class="view">
      ${pageHead({ icon: 'shield', title: '系统监控', desc: '后端 / 数据库 / AI 配置 / 采集引擎 / 任务与日志 · 每 30 秒自动刷新',
        actions: '<span class="mon-updated" id="monitor-updated-at"></span>' })}
      <div class="empty"><p>加载中…</p></div>
    </div>`;

    function renderMonitor(d) {
      const { health, db, apiStats, accounts, tasks, logs } = d;

      /* ── 系统状态卡片行（5 个） ── */
      const compCards = [
        { label: '后端', c: health.backend, sub: `${esc(health.backend.version)} · 运行 ${esc(_monUptime(health.backend.uptime))}` },
        { label: '数据库', c: health.database, sub: `${esc((health.database.fileSizeMb || 0).toFixed(2))} MB` },
        { label: 'AI 配置', c: health.aiConfig, sub: health.aiConfig.apiKeyConfigured ? esc(health.aiConfig.provider + ' · ' + health.aiConfig.model) : '未配置 API Key' },
        { label: '采集引擎', c: health.crawler, sub: health.crawler.running ? '运行中 · ' + health.crawler.tasksCount + ' 任务' : '未运行' },
        { label: '桌面端', c: health.desktop, sub: '暂未接入' },
      ].map(({ label, c, sub }) => `
        <div class="mon-comp">
          <div class="mon-comp__head">${_monDot(c.status)}<span class="mon-comp__label">${esc(label)}</span></div>
          <div class="mon-comp__status">${esc(c.status)}</div>
          <div class="mon-comp__sub">${sub}</div>
        </div>`).join('');

      /* ── 数据库信息 ── */
      const tableRows = (db.tables || []).map((t) =>
        `<div class="mon-table-row"><span class="mon-table-name">${esc(t.table)}</span><span class="num">${fmt(t.count)}</span></div>`).join('');

      /* ── API 统计 ── */
      const topRows = (apiStats.top5Endpoints || []).map((e) => `
        <div class="mon-ap-row">
          <span class="mon-ap-endpoint">${esc(e.endpoint)}</span>
          <span class="num">${fmt(e.total)} 次</span>
          <span class="num mon-ap-rate">${(e.errorRate || 0).toFixed(1)}% 错误</span>
        </div>`).join('');

      /* ── 账号健康度表格 ── */
      const accRows = accounts.map((a) => {
        const bad = a.healthScore < 60;
        const fillColor = a.healthScore >= 80 ? '#22c55e' : a.healthScore >= 60 ? '#f59e0b' : '#ef4444';
        const limitTag = {
          healthy: '<span class="tag tag--healthy">健康</span>',
          warn: '<span class="tag tag--throttled">预警</span>',
          throttled40: '<span class="tag tag--throttled">降速40</span>',
          safe_mode: '<span class="tag tag--safemode">安全模式</span>',
          banned: '<span class="tag tag--banned">已封禁</span>',
        }[a.limitStatus] || esc(a.limitStatus);
        return `
        <div class="mon-acc-row ${bad ? 'mon-acc-row--bad' : ''}">
          <span class="mon-acc-name">${esc(a.nickname)}</span>
          <span class="mon-acc-gauge">
            <span class="mon-acc-score">${a.healthScore}</span>
            <span class="gauge__track"><span class="gauge__fill" style="width:${a.healthScore}%;background:${fillColor}"></span></span>
          </span>
          <span>${limitTag}</span>
          <span class="num">今日 ${fmt(a.todaySent)}</span>
          <span class="num">预警 ${a.warningCount}</span>
        </div>`;
      }).join('');

      /* ── 最近日志 ── */
      const logRows = logs.map((l) => {
        const color = MONITOR_SEV_COLORS[l.severity] || '#9ca3af';
        const t = (l.createdAt || '').replace('T', ' ').slice(0, 19);
        return `
        <div class="mon-log-row">
          <span class="mon-log-dot" style="background:${color}"></span>
          <span class="mon-log-type">${esc(l.eventType)}</span>
          <span class="mon-log-msg">${esc(l.message)}</span>
          <span class="mon-log-time num">${esc(t)}</span>
        </div>`;
      }).join('');

      main.innerHTML = `
      <div class="view">
        ${pageHead({ icon: 'shield', title: '系统监控', desc: '后端 / 数据库 / AI 配置 / 采集引擎 / 任务与日志 · 每 30 秒自动刷新',
          actions: '<span class="mon-updated" id="monitor-updated-at"></span>' })}

        <div class="mon-comp-grid">${compCards}</div>

        <div class="grid grid--main-side">
          <div class="card">
            <div class="card__head">
              <span class="card__title">数据库信息</span>
              <span class="card__hint">SQLite · 文件 ${fmt(db.tableCount)} 张表 · 共 ${fmt(db.totalRecords)} 条记录</span>
            </div>
            <div class="mon-kv-grid">
              <div class="mon-kv"><span class="mon-kv__k">文件路径</span><span class="mon-kv__v mon-path">${esc(db.filePath)}</span></div>
              <div class="mon-kv"><span class="mon-kv__k">文件大小</span><span class="mon-kv__v">${(db.fileSizeMb || 0).toFixed(2)} MB</span></div>
              <div class="mon-kv"><span class="mon-kv__k">最近备份</span><span class="mon-kv__v">${db.lastBackupAt ? esc(String(db.lastBackupAt).replace('T',' ').slice(0,19)) : '无'}</span></div>
            </div>
            <div class="mon-table-list">${tableRows || '<p class="muted">无表</p>'}</div>
          </div>

          <div class="card">
            <div class="card__head">
              <span class="card__title">API 统计</span>
              <span class="card__hint">内存计数 · 重启清零</span>
            </div>
            <div class="mon-kpi-row">
              <div class="mon-kpi"><span class="mon-kpi__v num">${fmt(apiStats.totalRequests)}</span><span class="mon-kpi__k">总请求</span></div>
              <div class="mon-kpi"><span class="mon-kpi__v num">${(apiStats.errorRate || 0).toFixed(1)}%</span><span class="mon-kpi__k">错误率</span></div>
              <div class="mon-kpi"><span class="mon-kpi__v num">${(apiStats.avgResponseTime || 0).toFixed(0)} ms</span><span class="mon-kpi__k">平均响应</span></div>
            </div>
            <div class="mon-ap-list">${topRows || '<p class="muted">暂无请求</p>'}</div>
          </div>
        </div>

        <div class="card">
          <div class="card__head">
            <span class="card__title">任务运行态</span>
            <span class="card__hint">采集任务 / 私信队列 / 调度器</span>
          </div>
          <div class="mon-task-grid">
            <div class="mon-task-block">
              <div class="mon-task__t">采集任务</div>
              <div class="mon-task__nums">
                <span>运行 <b>${tasks.crawlTasks.running}</b></span>
                <span>完成 <b>${tasks.crawlTasks.completed}</b></span>
                <span>失败 <b>${tasks.crawlTasks.failed}</b></span>
              </div>
            </div>
            <div class="mon-task-block">
              <div class="mon-task__t">私信队列</div>
              <div class="mon-task__nums">
                <span>待发 <b>${tasks.dmQueue.pending}</b></span>
                <span>已发 <b>${tasks.dmQueue.sent}</b></span>
                <span>失败 <b>${tasks.dmQueue.failed}</b></span>
                <span>频控 <b>${tasks.dmQueue.throttled}</b></span>
              </div>
            </div>
            <div class="mon-task-block">
              <div class="mon-task__t">调度器</div>
              <div class="mon-task__nums">
                <span>${_monDot(tasks.scheduler.status === 'running' ? 'ok' : 'stopped')} ${esc(tasks.scheduler.status)}</span>
              </div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card__head">
            <span class="card__title">账号健康度</span>
            <span class="card__hint">健康分 &lt; 60 红色高亮</span>
          </div>
          <div class="mon-acc-head">
            <span>账号</span><span>健康分</span><span>状态</span><span>今日发送</span><span>预警数</span>
          </div>
          <div class="mon-acc-list">${accRows || '<p class="muted">无账号</p>'}</div>
        </div>

        <div class="card">
          <div class="card__head">
            <span class="card__title">最近系统日志</span>
            <span class="card__hint">审计事件 · 时间倒序</span>
          </div>
          <div class="mon-log-list">${logRows || '<p class="muted">暂无日志</p>'}</div>
        </div>
      </div>`;

      const el = $('#monitor-updated-at');
      if (el) el.textContent = '最后更新 ' + new Date().toLocaleTimeString('zh-CN');
    }

    async function loadMonitor() {
      try {
        const data = await Promise.all([
          API.fetchMonitorHealth(),
          API.fetchMonitorDatabase(),
          API.fetchMonitorApiStats(),
          API.fetchMonitorAccounts(),
          API.fetchMonitorTasks(),
          API.fetchMonitorLogs(20),
        ]);
        renderMonitor({
          health: data[0], db: data[1], apiStats: data[2],
          accounts: data[3], tasks: data[4], logs: data[5],
        });
      } catch (e) {
        const el = $('#monitor-updated-at');
        if (el) el.textContent = '刷新失败：' + (e && e.message ? e.message : e);
      }
    }

    await loadMonitor();
    _monitorTimer = setInterval(loadMonitor, 30000);
  }

  /* 离开监控页时清除定时器 */
  window.addEventListener('hashchange', () => {
    const h = (location.hash || '#/workbench').replace('#/', '');
    if (h !== 'monitor' && _monitorTimer) { clearInterval(_monitorTimer); _monitorTimer = null; }
  });


  /* ══════════ 启动 ══════════ */
  (async function init() {
    const rt = await API.getRuntime();
    state.safeMode = rt.safeMode;
    state.taskRunning = rt.taskRunning;
    renderTopbar();
    renderNavBadges();
    renderBellBadge();
    setInterval(renderBellBadge, 30000);
    route();
  })();
})();
