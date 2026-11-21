/**
 * demo-bridge.js —— 浏览器演示版专用
 * 原版前端依赖 Electron 主进程（window.ipcRenderer）。
 * 本文件在浏览器环境里模拟这层桥，用本地演示数据让界面完整可浏览。
 * 演示数据均为虚构，不连接任何真实服务。
 */
(function () {
  if (window.ipcRenderer) return; // 已在 Electron 环境中则不干预

  const now = Date.now();
  const DAY = 86400000;
  const iso = (t) => new Date(t).toISOString();
  const fmt = (t) => {
    const d = new Date(t);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  const rid = () => Math.random().toString(36).slice(2, 10);

  /* ---------------- 授权（演示：试用体验版） ---------------- */
  const license = {
    success: true, isFree: false, isTrial: true, type: "试用体验版",
    expireTime: fmt(now + 30 * DAY), planType: "trial", licenseTier: "basic",
    licenseTierLabel: "试用体验版", maxConcurrentAccounts: 3, aiQuota: 500,
    entitlementVerified: false, savedCode: "", deviceId: "DEMO-" + rid().toUpperCase(),
    runtimeConfig: {}, offline: false,
  };

  /* ---------------- 账号池 ---------------- */
  const accounts = [
    { id: "acc-01", platform: "douyin", name: "获客小助手1号", nickname: "小助优选", status: "online", loginState: "logged_in", remark: "主力号", createdAt: iso(now - 40 * DAY) },
    { id: "acc-02", platform: "douyin", name: "获客小助手2号", nickname: "好物分享日记", status: "online", loginState: "logged_in", remark: "评论区互动", createdAt: iso(now - 35 * DAY) },
    { id: "acc-03", platform: "douyin", name: "获客小助手3号", nickname: "家居好物推荐官", status: "online", loginState: "logged_in", remark: "", createdAt: iso(now - 28 * DAY) },
    { id: "acc-04", platform: "douyin", name: "获客小助手4号", nickname: "优选生活馆", status: "offline", loginState: "logged_in", remark: "今日限额已用完", createdAt: iso(now - 20 * DAY) },
    { id: "acc-05", platform: "douyin", name: "获客小助手5号", nickname: "源头工厂直供", status: "online", loginState: "logged_in", remark: "", createdAt: iso(now - 12 * DAY) },
    { id: "acc-06", platform: "douyin", name: "获客小助手6号", nickname: "小助严选", status: "offline", loginState: "logged_in", remark: "待重新登录", createdAt: iso(now - 5 * DAY) },
  ];

  /* ---------------- 任务数据 ---------------- */
  const accName = (id) => accounts.find(a => a.id === id) || {};
  const mkTask = (id, name, status, days, leads, extra = {}) => ({
    id, name, status,
    createdAt: iso(now - days * DAY),
    updatedAt: iso(now - days * DAY + 3600e3),
    startedAt: iso(now - days * DAY),
    endedAt: status === "running" ? null : iso(now - days * DAY + 7200e3),
    endReason: status === "running" ? null : "completed",
    leadCount: leads,
    runs: [{ accountId: "acc-01", automationTaskId: id, nickname: accName("acc-01").nickname, name: accName("acc-01").name, startedAt: iso(now - days * DAY), endedAt: status === "running" ? null : iso(now - days * DAY + 7200e3) }],
    stats: { videos: 20 + (leads % 30), leadsTotal: leads, likes: Math.round(leads * 0.9), replies: Math.round(leads * 0.45), videoComments: Math.round(leads * 0.3), profileComments: Math.round(leads * 0.2), follows: Math.round(leads * 0.15), messages: Math.round(leads * 0.1), profileShares: 2, commentShares: 1 },
    accountStats: [{ accountId: "acc-01", accountName: accName("acc-01").nickname, total: leads, success: Math.round(leads * 0.85), failed: leads - Math.round(leads * 0.85) }],
    configSnapshot: { sourceTypes: ["comment"], selectedAccounts: ["acc-01"], keyword: "好物推荐", ...extra },
  });

  const leadgenTasks = [
    mkTask("lg-01", "好物推荐-评论区获客-周一", "completed", 1, 128, { keyword: "家居好物" }),
    mkTask("lg-02", "护肤关键词-评论采集", "running", 0, 64, { keyword: "护肤推荐" }),
    mkTask("lg-03", "美食探店-评论获客", "completed", 2, 233),
    mkTask("lg-04", "母婴用品-评论获客", "stopped", 3, 57),
  ];
  const entityTasks = [
    mkTask("eg-01", "博主主页-线索采集", "completed", 1, 96, { sourceTypes: ["profile"] }),
    mkTask("eg-02", "热门视频-评论点赞", "running", 0, 41, { sourceTypes: ["comment_like"] }),
    mkTask("eg-03", "吞评检测-定时任务", "completed", 2, 18, { sourceTypes: ["comment_visibility"] }),
  ];
  const monitorTasks = [
    mkTask("mt-01", "竞品账号-作品监控", "running", 0, 22),
    mkTask("mt-02", "关键词-新视频监控", "completed", 1, 87),
  ];
  const selfWarmupTasks = [
    mkTask("sw-01", "老客户-自动回复维护", "running", 0, 35),
    mkTask("sw-02", "新粉丝-互动养号", "completed", 1, 112),
  ];
  const workPublishTasks = [
    { id: "wp-01", name: "好物合集-每日更新", status: "completed", mediaType: "video", createdAt: iso(now - 1 * DAY), endedAt: iso(now - 1 * DAY + 5400e3), items: [{ accountId: "acc-01", title: "家居好物 TOP5 合集", status: "published" }, { accountId: "acc-02", title: "平价护肤实测", status: "published" }] },
    { id: "wp-02", name: "周末探店系列", status: "running", mediaType: "video", createdAt: iso(now), endedAt: null, items: [{ accountId: "acc-05", title: "周末去哪儿｜宝藏小店", status: "publishing" }] },
  ];

  /* ---------------- 线索库 ---------------- */
  const NICKS = ["小红薯用户", "爱生活的莉莉", "装修老王", "宝妈小艾", "数码控阿凯", "温柔学姐", "厨娘日记", "健身达人Leo", "旅行家小周", "带货小妹"];
  const LOC = ["广东", "浙江", "江苏", "四川", "山东", "北京", "上海", "河南"];
  const INTENT = ["高意向", "中意向", "低意向"];
  const leads = Array.from({ length: 48 }, (_, i) => {
    const t = now - i * 5 * 3600e3 - rid().length * 1000;
    const like = Math.random() < 0.5, reply = Math.random() < 0.35, follow = Math.random() < 0.2, msg = Math.random() < 0.15;
    return {
      leadId: "lead-" + String(1000 + i), key: "lead-" + String(1000 + i),
      nickname: NICKS[i % NICKS.length], avatar: "",
      douyinId: "dy_" + rid(), location: LOC[i % LOC.length],
      intention: INTENT[i % 3 === 0 ? 0 : i % 3], commentLevel: i % 4 === 0 ? "强意向评论" : "普通评论",
      comment: "这个真的好用吗？想了解一下价格", commentContent: "这个真的好用吗？想了解一下价格",
      accountId: accounts[i % 5].id, accountName: accounts[i % 5].name, account: accounts[i % 5].name,
      entrySource: ["评论获客", "线索采集", "监控任务"][i % 3],
      capturedAt: iso(t), timestamp: t, time: fmt(t),
      touchCounts: { like: like ? 1 : 0, reply: reply ? 1 : 0, profileComment: 0, follow: follow ? 1 : 0, message: msg ? 1 : 0 },
      liked: like, replied: reply, followed: follow, messaged: msg,
      batchFollowStatus: follow ? "已关注" : "未关注",
      touchLog: [], touchTotal: (like ? 1 : 0) + (reply ? 1 : 0) + (follow ? 1 : 0) + (msg ? 1 : 0),
      keyword: "好物推荐",
    };
  });
  const leadStats = { total: 48, likes: leads.filter(l => l.liked).length, replies: leads.filter(l => l.replied).length, messages: leads.filter(l => l.messaged).length, follows: leads.filter(l => l.followed).length, touched: leads.filter(l => l.touchTotal > 0).length, untouched: leads.filter(l => !l.touchTotal).length, profileComments: 0 };

  /* ---------------- 账号数据看板 ---------------- */
  const dayEntry = (a, delta) => ({
    followers: 1200 + a.id.charCodeAt(4) * 37 + delta, following: 300 + (a.id.charCodeAt(4) % 60), works: 45 + (a.id.charCodeAt(4) % 30),
    snapshotAt: fmt(now), likes: 40 + delta, collections: 12 + delta % 7, videoShares: 3, replies: 8, profileComments: 4,
    videoComments: 6, follows: 5, messages: 2,
  });
  const accountDataDashboard = {
    success: true,
    scheduleTime: "00:00",
    lastCollectAt: now - 3600e3,
    collecting: false,
    days: [0, 1, 2, 3, 4, 5, 6].map(i => fmt(now - i * DAY).slice(0, 10)),
    errors: {},
    baselines: Object.fromEntries(accounts.map(a => [a.id, { ...dayEntry(a, 0), snapshotAt: fmt(now - 7 * DAY) }])),
    latest: Object.fromEntries(accounts.map(a => [a.id, dayEntry(a, 20)])),
    accounts: accounts.map(a => ({
      accountId: a.id,
      days: Object.fromEntries([0, 1, 2, 3].map(i => [fmt(now - i * DAY).slice(0, 10), dayEntry(a, 20 - i * 4)])),
    })),
  };

  /* ---------------- 系统信息 ---------------- */
  const announcements = { success: true, items: [{ id: "an-1", title: "演示版说明", content: "当前为浏览器演示版：界面与原版一致，数据为本地虚构演示数据。", level: "info", createdAt: iso(now) }] };
  const systemPerformance = { success: true, cpuUsage: 12, memoryUsage: 38, memoryUsedMB: 3050, memoryTotalMB: 8192, diskFreeGB: 128, uptimeHours: 3.2, status: "good" };
  const runtimeState = { success: true, state: "ready", ready: true };
  const versionInfo = { success: true, version: "3.9.66", platform: "win32", arch: "x64", electron: "demo", demo: true };
  const storageOk = { success: true, state: "ok" };
  const runningIds = (t) => t.filter(x => x.status === "running").map(x => x.id);
  const quotas = { success: true, data: { used: 12, total: 50, remain: 38, aiQuota: license.aiQuota } };

  /* ---------------- 通道处理 ---------------- */
  const handlers = {
    "check-auth": () => license,
    "activate": () => ({ ...license, success: true, msg: "演示模式：授权码校验已在本地模拟" }),
    "refresh-auth": () => license,
    "get-device-id": () => license.deviceId,
    "get-version-info": () => versionInfo,
    "get-app-info": () => versionInfo,
    "app-version": () => versionInfo.version,
    "get-runtime-state": () => runtimeState,
    "get-runtime-config-status": () => ({ success: true, state: "ready" }),
    "get-runtime-feature-defaults": () => ({ success: true, features: {}, defaults: {} }),
    "get-system-announcements": () => announcements,
    // 线索库导出 + 批量跟进（演示模式：只回结构，不发真实请求）
    "export-leads": () => ({ items: [], total: 0 }),
    "get-batch-follow-tasks": () => ({ success: true, items: [], total: 0 }),
    "save-batch-follow-task": () => ({ success: true, id: "bf_demo_" + rid() }),
    "control-batch-follow-task": () => ({ success: true }),
    "get-batch-follow-runs-page": () => ({ items: [], total: 0 }),
    "get-batch-follow-run-details-page": () => ({ run: null, items: [], total: 0 }),
    "get-batch-follow-user-history": () => ({ success: true, items: [], total: 0, hasMore: false }),
    "clear-batch-follow-runs": () => ({ success: true }),
    "delete-batch-follow-runs": () => ({ success: true }),
    "clear-batch-follow-run-logs": () => ({ success: true }),
    "cancel-batch-follow-user-history": () => ({ success: true }),
    "delete-failed-batch-follow-leads": () => ({ success: true, deleted: 0 }),
    "test-batch-follow-proxy": () => ({ success: false, message: "演示模式不发起真实代理请求" }),
    "start-batch-action": () => ({ ok: false, reason: "演示模式不执行真实动作" }),
    // 自动回复：演示模式不发真实私信，如实说明
    "send-autoreply-dm": () => ({ success: false, status: "skipped", code: "demo_mode",
      error: "演示模式不发送真实私信" }),
    "submit-faq-question": () => ({ success: true, id: "FAQ-DEMO-" + rid().toUpperCase() }),
    "read-faq-clipboard-image": () => ({
      success: true,
      base64: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
      bytes: 68,
    }),
    "get-system-performance": () => systemPerformance,
    "get-pending-update": () => ({ success: true, update: null }),
    "get-last-update-download-path": () => ({ success: true, path: "" }),
    "support-bootstrap": () => ({ success: true, enabled: false }),
    "chat-reminder-diagnostic": () => ({ success: true, ok: true }),
    "get-chat-unread-state": () => ({ success: true, unread: {} }),
    "get-chat-notification-config": () => ({ success: true, config: {} }),
    "get-account-pool": () => accounts,
    "save-account-pool": () => ({ success: true }),
    "get-account-data-dashboard": () => accountDataDashboard,
    "save-account-data-schedule": (a) => ({ success: true, time: (a && a.time) || "09:00" }),
    "get-leadgen-tasks": () => leadgenTasks,
    "get-leadgen-tasks-storage-status": () => storageOk,
    "get-leadgen-schedules": () => ({ success: true, schedules: [] }),
    "save-leadgen-schedules": () => ({ success: true }),
    "get-entity-leadgen-tasks": () => entityTasks,
    "get-entity-leadgen-tasks-storage-status": () => storageOk,
    "get-entity-leadgen-task": (a) => entityTasks.find(t => t.id === (a && a.id)) || null,
    "get-entity-leadgen-schedules": () => ({ success: true, schedules: [] }),
    "save-entity-leadgen-schedules": () => ({ success: true }),
    "get-entity-leadgen-concurrency-limit": () => ({ success: true, recommended: 3, runningAccountCount: 1 }),
    "get-running-entity-leadgen-task-ids": () => runningIds(entityTasks),
    "get-entity-leadgen-task-runtime-logs": () => ({ success: true, logs: [] }),
    "get-monitor-tasks": () => monitorTasks,
    "get-monitor-tasks-storage-status": () => storageOk,
    "get-monitor-schedules": () => ({ success: true, schedules: [] }),
    "save-monitor-schedules": () => ({ success: true }),
    "get-monitor-lease-pauses": () => ({}),
    "get-running-monitor-task-ids": () => runningIds(monitorTasks),
    "get-monitor-task-runtime-logs": () => ({ success: true, logs: [] }),
    "get-monitor-live-views": () => ({ success: true, views: [] }),
    "consume-video-monitor-live-preview": () => null,
    "get-self-warmup-tasks": () => selfWarmupTasks,
    "get-self-warmup-tasks-storage-status": () => storageOk,
    "get-self-warmup-schedules": () => ({ success: true, schedules: [] }),
    "save-self-warmup-schedules": () => ({ success: true }),
    "get-self-warmup-lease-pauses": () => ({}),
    "get-running-self-warmup-task-ids": () => runningIds(selfWarmupTasks),
    "get-running-self-warmup-manual-retry-task-ids": () => [],
    "get-self-warmup-task": () => selfWarmupTasks[0],
    "save-nurture-config": () => ({ success: true }),
    "get-nurture-config": () => ({ success: true, config: {} }),
    "get-task-config": () => ({ success: true, config: {} }),
    "get-work-publish-tasks": () => workPublishTasks,
    "get-work-publish-quota": () => quotas,
    "get-running-work-publish-task-ids": () => runningIds(workPublishTasks),
    "get-work-publish-task-runtime-logs": () => ({ success: true, logs: [] }),
    "consume-work-publish-live-preview": () => null,
    "consume-entity-leadgen-live-preview": () => null,
    "get-batch-runtime-state": () => ({ success: true, state: "idle" }),
    "get-entity-leadgen-monitor-view-keys": () => [],
    "get-leads-page": (a) => {
      const page = (a && a.page) || 1, size = (a && a.pageSize) || 20;
      return { items: leads.slice((page - 1) * size, page * size), total: leads.length, runtime: "sqlite" };
    },
    "get-leads-pool-stats": () => ({ stats: leadStats, runtime: "sqlite" }),
    "get-leads-filter-options": () => ({ accounts: accounts.map(a => ({ id: a.id, name: a.name, nickname: a.nickname })), keywords: ["好物推荐", "家居好物", "护肤推荐"], locations: LOC }),
    "get-active-automation-tasks": () => [],
    "set-message-center-session": () => ({ success: true }),
    "set-creator-view-attached": () => ({ success: true }),
    "set-chat-view-attached": () => ({ success: true }),
    "set-views-visible": () => ({ success: true }),
    "destroy-creator-view": () => ({ success: true }),
    "destroy-chat-view": () => ({ success: true }),
    "stop-monitor": () => ({ success: true }),
    "stop-task": () => ({ success: true }),
    "generate": () => ({ success: true, text: "（演示模式）AI 生成内容示例：这款家居好物真的改变了我的生活质量，性价比超高！" }),

    /* ---- 明细类（演示：返回带数据的明细页，避免一进去就空表） ---- */
    "get-leadgen-task-detail": (a) => {
      const t = leadgenTasks.find(x => x.id === (a && a.id)) || leadgenTasks[0];
      return { success: true, task: t, leads: leads.slice(0, 12), total: t.stats.leadsTotal };
    },
    "get-entity-leadgen-task-leads": (a) => {
      const page = (a && a.page) || 1, size = (a && a.pageSize) || 20;
      return { success: true, items: leads.slice((page - 1) * size, page * size), total: leads.length };
    },
    "get-comment-like-results": () => ({ success: true, items: leads.slice(0, 15), total: 15 }),
    "get-monitor-task-matches": () => ({ success: true, items: leads.slice(0, 10), total: 10 }),
    "get-processed-videos": () => ({
      success: true, total: 6, items: [
        { id: "v1", title: "家居好物开箱｜这5件真的值得买", author: "家居研究所", createTime: fmt(now - 6 * 3600e3), collectedAt: fmt(now - 5 * 3600e3), account: "小助优选", state: "已互动", actions: { like: 1, reply: 1 } },
        { id: "v2", title: "30㎡小户型收纳改造全过程", author: "小空间大改造", createTime: fmt(now - 1 * DAY), collectedAt: fmt(now - 1 * DAY + 3600e3), account: "好物分享日记", state: "已互动", actions: { like: 1, reply: 1 } },
        { id: "v3", title: "平价护肤实测第3期", author: "成分党小林", createTime: fmt(now - 2 * DAY), collectedAt: fmt(now - 2 * DAY + 1800e3), account: "小助优选", state: "待处理", actions: {} },
      ],
    }),
    "get-monitor-comment-seen-stats": () => ({ success: true, stats: { seen: 342, matched: 28, replied: 12 } }),
    "get-monitor-manual-retry-task-ids": () => [],
    "get-self-warmup-events": () => ({ success: true, events: [] }),
    "get-self-warmup-task-logs": () => ({ success: true, logs: [] }),
    "get-work-publish-templates": () => ({ success: true, items: [
      { id: "tpl-1", name: "好物合集模板", title: "{关键词} TOP5 合集｜第{n}期", desc: "整理不易，点赞收藏～" },
      { id: "tpl-2", name: "探店模板", title: "周末去哪儿｜{城市}宝藏小店", desc: "地址在评论区～" },
    ] }),
    "open-chat-view": () => ({ success: false, msg: "演示模式：无法内嵌真实浏览器画面" }),
    "open-creator-view": () => ({ success: false, msg: "演示模式：无法内嵌真实浏览器画面" }),
    "refresh-leadgen-task-stats": (a) => {
      const t = leadgenTasks.find(x => x.id === (a && a.id)) || leadgenTasks[0];
      return { success: true, stats: t.stats, total: t.stats.leadsTotal };
    },
    "save-self-warmup-task": () => ({ success: true }),
    "start-self-warmup-task-runtime": () => ({ success: false, msg: "演示模式：不会真正执行自动化任务" }),
    "get-leadgen-task-leads": (a) => {
      const page = (a && a.page) || 1, size = (a && a.pageSize) || 20;
      return { success: true, items: leads.slice((page - 1) * size, page * size), total: leads.length };
    },
    "get-leadgen-task-video-comments": () => ({ success: true, items: [], total: 0 }),
    "update-self-warmup-task": () => ({ success: true }),
  };

  window.__chan = { handled: {}, unknown: {} };
  function invoke(ch, ...args) {
    try {
      const h = handlers[ch];
      const bucket = h ? window.__chan.handled : window.__chan.unknown;
      bucket[ch] = (bucket[ch] || 0) + 1;
      if (h) return Promise.resolve(h(args[0], args[1]));
      return Promise.resolve({ success: true });
    } catch (e) {
      return Promise.resolve({ success: false, msg: String(e && e.message || e) });
    }
  }

  window.ipcRenderer = {
    invoke,
    send: () => {},
    on: () => () => {},
    once: () => () => {},
    removeListener: () => {},
    removeAllListeners: () => {},
  };
  window.__DEMO_BRIDGE__ = { license, accounts, leads };
  console.info("[demo-bridge] 浏览器演示桥已启用，数据为本地虚构演示数据");
})();
