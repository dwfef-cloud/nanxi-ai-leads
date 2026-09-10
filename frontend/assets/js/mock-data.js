/* ═══════════════════════════════════════════════════════════
   数据层（真实 API + Mock 回退）
   ───────────────────────────────────────────────────────────
   已完成的 Lead 模块端点直接调用真实后端 API；
   未就绪端点尝试 fetch，失败时回退到内置 mock 数据并标记 TODO。

   接口映射表见文件底部 MOCK_API_MAP。
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  /* ── API 基址配置 ── */
  const API_BASE = 'http://127.0.0.1:8000/api';
  const FETCH_TIMEOUT = 10000; // 10 秒超时

  /* ── 全局加载指示器（顶部进度条，内联样式不依赖 CSS 文件） ── */
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

  /* ── 统一 apiFetch：超时 / HTTP 错误 / 网络错误 ── */
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

  /* ── 模拟延迟（仅 mock 回退时使用） ── */
  const LATENCY = 120;
  const delay = (data) => new Promise((resolve) => setTimeout(() => resolve(data), LATENCY));
  const clone = (o) => JSON.parse(JSON.stringify(o));

  /* ── 全局运行态（安全模式等，页面内可变） ── */
  const runtime = {
    safeMode: { active: false, reason: '', triggeredAt: null, triggerSource: null },
    taskRunning: true,
  };

  /* ════════ 线索来源元数据 ════════ */
  const SOURCE_META = {
    own_comment: { label: '自有视频评论区', cls: 'sent' },
    competitor:  { label: '对标账号监控',   cls: 'throttled' },
    inbound_dm:  { label: '用户主动私信',   cls: 'replied' },
    fan_dm:      { label: '粉丝私信',       cls: 'pending' },
    referral:    { label: '老客转介绍',     cls: 'wechat' },
    lead_card:   { label: '留资卡',         cls: 'mid' },
  };

  /* ════════ 漏斗转化 ════════ */
  const funnel = {
    window: '近 30 天',
    stages: [
      { key: 'exposure', label: '评论区曝光', value: 12840 },
      { key: 'collected', label: '线索入库', value: 342 },
      { key: 'outreach', label: '私信触达', value: 285 },
      { key: 'replied', label: '私信回复', value: 96 },
      { key: 'wechat', label: '加企微 ★', value: 41 },
      { key: 'deal', label: '成交', value: 9 },
    ],
    cost: { totalSpend: 2680, perLead: 7.84, perWechatAdd: 65.4, perDeal: 297.8, avgDealAmount: 18600 },
    wechatTrend: [
      { date: '08-12', v: 1 }, { date: '08-13', v: 2 }, { date: '08-14', v: 0 },
      { date: '08-15', v: 3 }, { date: '08-16', v: 2 }, { date: '08-17', v: 4 },
      { date: '08-18', v: 3 }, { date: '08-19', v: 5 }, { date: '08-20', v: 2 },
      { date: '08-21', v: 4 }, { date: '08-22', v: 6 }, { date: '08-23', v: 3 },
      { date: '08-24', v: 5 }, { date: '08-25', v: 6 },
    ],
  };

  /* ════════ 账号健康度 ════════ */
  const accounts = [
    { id: 'acc-01', nickname: '装修案例·阿明', avatarHue: 152, platform: 'douyin', healthScore: 86, limitStatus: 'healthy', dailyOutreach: 46, dailyLimit: 80, factors: { dailyFreq: 'good', banHistory: 'good', login: 'good', unsubscribe: 'good', complaint: 'good' }, lastBanReason: null, updatedAt: '2026-08-25 09:12' },
    { id: 'acc-02', nickname: '老房改造·莉姐', avatarHue: 28, platform: 'douyin', healthScore: 71, limitStatus: 'healthy', dailyOutreach: 63, dailyLimit: 80, factors: { dailyFreq: 'warn', banHistory: 'good', login: 'good', unsubscribe: 'good', complaint: 'good' }, lastBanReason: null, updatedAt: '2026-08-25 09:12' },
    { id: 'acc-03', nickname: '全屋定制·凯文', avatarHue: 210, platform: 'douyin', healthScore: 54, limitStatus: 'throttled40', dailyOutreach: 40, dailyLimit: 40, factors: { dailyFreq: 'bad', banHistory: 'good', login: 'warn', unsubscribe: 'good', complaint: 'warn' }, lastBanReason: null, r1Note: 'R1 命中：日频达 80，已自动降速至 40/天（第 1 天）', updatedAt: '2026-08-25 08:40' },
    { id: 'acc-04', nickname: '同城装修·班长', avatarHue: 348, platform: 'douyin', healthScore: 38, limitStatus: 'safe_mode', dailyOutreach: 0, dailyLimit: 0, factors: { dailyFreq: 'bad', banHistory: 'bad', login: 'good', unsubscribe: 'warn', complaint: 'bad' }, lastBanReason: '2026-08-23 平台限流 6 小时', r3Note: 'health_score < 40，已联动进入安全模式', updatedAt: '2026-08-25 07:55' },
    { id: 'acc-05', nickname: '设计顾问·小舟', avatarHue: 62, platform: 'douyin', healthScore: 92, limitStatus: 'healthy', dailyOutreach: 21, dailyLimit: 80, factors: { dailyFreq: 'good', banHistory: 'good', login: 'good', unsubscribe: 'good', complaint: 'good' }, lastBanReason: null, updatedAt: '2026-08-25 09:12' },
  ];

  /* ════════ 合规风险 ════════ */
  const compliance = {
    summary: { unsubscribe7d: 7, blacklistTotal: 23, blacklistDelta7d: 2, complaints7d: 1, auditEvents: 156 },
    rules: [
      { id: 'R1', desc: '单账号日频 ≥ 80 → 降速至 40', status: 'hit', hitAt: '08-25 08:40', target: '全屋定制·凯文' },
      { id: 'R2', desc: '话术变体加微转化率 < 8%（样本≥30）→ 自动切换', status: 'hit', hitAt: '08-24 21:03', target: '装修主话术 · 变体B' },
      { id: 'R3', desc: '黑名单日增 > 3% 或账号封禁 → 全量暂停', status: 'standby', hitAt: null, target: null },
    ],
    audit: [
      { time: '08-25 09:12', type: 'safe_mode', level: 'danger', text: '账号「同城装修·班长」health_score=38，联动触发安全模式，全量暂停' },
      { time: '08-25 08:40', type: 'r1_hit', level: 'warn', text: 'R1 命中：账号「全屋定制·凯文」日频达 80，降速至 40/天' },
      { time: '08-24 21:03', type: 'r2_switch', level: 'warn', text: 'R2 命中：装修主话术变体B 转化率 5.9%（样本 51），自动切换至变体A' },
      { time: '08-24 18:31', type: 'unsubscribe', level: 'info', text: '用户 @选择困难症小姐 退订，已加入黑名单，不再触达' },
      { time: '08-24 15:02', type: 'dm_reply', level: 'info', text: '线索「旧房翻新求推荐」回复私信，进入加微引导' },
      { time: '08-23 22:10', type: 'ban', level: 'danger', text: '账号「同城装修·班长」平台限流 6 小时，记 account_limited，健康分 -18' },
      { time: '08-23 14:44', type: 'wechat_added', level: 'ok', text: '线索「客厅改造预算8w」添加企业微信成功（北极星 +1）' },
    ],
  };

  /* ════════ 线索状态元数据 ════════ */
  const LEAD_STATUS = {
    collected:        { label: '已采集',     cls: 'collected' },
    pending_outreach: { label: '待发送',     cls: 'pending' },
    throttled:        { label: '频控暂缓',   cls: 'throttled' },
    sent:             { label: '已私信',     cls: 'sent' },
    replied:          { label: '已回复',     cls: 'replied' },
    wechat_added:     { label: '已加微 ★',   cls: 'wechat' },
    deal_won:         { label: '已成交',     cls: 'deal' },
    send_failed:      { label: '发送失败',   cls: 'failed' },
    rejected:         { label: '已过滤',     cls: 'rejected' },
  };

  /* ════════ 线索数据 ════════ */
  const leads = [
    { id: 'LD-1026', nickname: '老陈的第三套房', hue: 140, intent: 'high', source: 'referral', comment: '朋友推荐来的（叮当一家），110平想装北欧风', video: '—（转介绍回流）', createdAt: '08-25 10:40', status: 'collected', account: null, dealAmount: null, referralNote: '来自成交客户「叮当一家」转介绍，优先跟进', messages: [] },
    { id: 'LD-1025', nickname: '翡翠湾陈先生', hue: 20, intent: 'high', source: 'inbound_dm', comment: '你好，看到你们账号，翡翠湾 96 平全包什么价？', video: '—（主动私信主页）', createdAt: '08-25 09:58', status: 'replied', account: '设计顾问·小舟', dealAmount: null, messages: [{ dir: 'out', text: '您好！96 平全包本月均价 980-1150/平（含主材）。私信放不下明细，加我企微发您三套同小区报价单：[企业微信名片]', time: '08-25 10:05', variant: 'A' }, { dir: 'in', text: '好，先看报价', time: '08-25 10:22' }] },
    { id: 'LD-1024', nickname: '旧房翻新求推荐', hue: 152, intent: 'high', source: 'own_comment', comment: '家里90平老房子想翻新，求靠谱的装修公司，坐标城东', video: '老房改造避坑指南', createdAt: '08-25 09:05', status: 'collected', account: null, dealAmount: null, messages: [] },
    { id: 'LD-1023', nickname: '王女士家半包', hue: 28, intent: 'high', source: 'own_comment', comment: '半包大概什么价位？有具体报价单吗', video: '2026半包全包价目表', createdAt: '08-25 08:52', status: 'pending_outreach', account: '装修案例·阿明', dealAmount: null, messages: [] },
    { id: 'LD-1022', nickname: '阿哲不加班', hue: 210, intent: 'mid', source: 'competitor', comment: '这种风格好看是好看，就是贵吧', video: '（对标账号）奶油风全屋落地', createdAt: '08-25 08:31', status: 'throttled', account: '全屋定制·凯文', dealAmount: null, throttledNote: '账号 R1 降速中，等待队列回放', messages: [] },
    { id: 'LD-1021', nickname: '客厅改造预算8w', hue: 348, intent: 'high', source: 'own_comment', comment: '预算8万左右能做成视频里这样吗？私我', video: '8万块爆改79㎡客厅', createdAt: '08-24 22:18', status: 'wechat_added', account: '装修案例·阿明', dealAmount: null, wechatAddedAt: '08-24 14:44', manual: false, messages: [{ dir: 'out', text: '您好！看到您在「8万块爆改79㎡客厅」下留言～8万在城东可以做全屋定制+硬装了，发您一份同户型的落地案例和明细报价？', time: '08-24 22:40', variant: 'A' }, { dir: 'in', text: '可以的，发来看看', time: '08-24 23:02' }, { dir: 'out', text: '已发您～另外我们企微上有3套同预算的真实工地直播，加一下随时看进度：[企业微信名片]', time: '08-24 23:05', variant: 'A' }, { dir: 'in', text: '加了，通过一下', time: '08-25 09:44' }] },
    { id: 'LD-1020', nickname: '选择困难症小姐', hue: 62, intent: 'mid', source: 'own_comment', comment: '到底选全包还是半包啊纠结死了', video: '全包VS半包终极对比', createdAt: '08-24 19:40', status: 'rejected', account: null, dealAmount: null, rejectReason: '用户退订，已入黑名单', messages: [] },
    { id: 'LD-1019', nickname: '城南李工', hue: 190, intent: 'high', source: 'own_comment', comment: '求推荐靠谱施工队，年底想入住', video: '年底完工工期规划', createdAt: '08-24 16:22', status: 'replied', account: '设计顾问·小舟', dealAmount: null, messages: [{ dir: 'out', text: '您好！年底入住的话工期要倒排了，我们目前在排的工地最快 9 月初开工。您房子多大面积？我按面积帮您排一版工期表～', time: '08-24 16:50', variant: 'A' }, { dir: 'in', text: '110平，毛坯', time: '08-25 08:15' }] },
    { id: 'LD-1018', nickname: '喵喵要装修', hue: 320, intent: 'high', source: 'competitor', comment: '私我私我，正好要装婚房', video: '（对标账号）婚房装修案例', createdAt: '08-24 15:08', status: 'sent', account: '老房改造·莉姐', dealAmount: null, messages: [{ dir: 'out', text: '恭喜恭喜🎉 婚房装修我们有专属的「婚房套餐」，从设计到软装一站式。发您一份本月落地实拍合集和报价区间？', time: '08-24 15:31', variant: 'B' }] },
    { id: 'LD-1017', nickname: '老周说房', hue: 12, intent: 'low', source: 'own_comment', comment: '收藏了慢慢看', video: '装修避坑合集', createdAt: '08-24 11:36', status: 'rejected', account: null, dealAmount: null, rejectReason: '无意向信号，自动过滤', messages: [] },
    { id: 'LD-1016', nickname: '叮当一家', hue: 260, intent: 'high', source: 'own_comment', comment: '三室两厅120平全包多少钱，私信报价', video: '120平全包真实成本', createdAt: '08-23 20:12', status: 'deal_won', account: '装修案例·阿明', dealAmount: 128000, dealAt: '08-25 10:30', messages: [{ dir: 'out', text: '您好！120平三室两厅全包，我们 8 月的均价在 950-1100/平（含主材）。私信里发您三套同户型方案和报价明细～', time: '08-23 20:40', variant: 'A' }, { dir: 'in', text: '好，先看看', time: '08-23 21:02' }, { dir: 'out', text: '方案已发～加我企微，报价单 PDF 和工地实拍都在里面，后续有活动第一时间同步您：[企业微信名片]', time: '08-23 21:05', variant: 'A' }, { dir: 'in', text: '加了', time: '08-24 09:12' }, { dir: 'in', text: '合同签了，8月28号开工', time: '08-25 10:30' }] },
    { id: 'LD-1015', nickname: '北岸小业主', hue: 96, intent: 'mid', source: 'own_comment', comment: '样板间在哪，周末想去看看', video: '城南新样板间开放', createdAt: '08-23 17:55', status: 'wechat_added', account: '设计顾问·小舟', dealAmount: null, wechatAddedAt: '08-24 11:20', manual: true, messages: [{ dir: 'out', text: '样板间在城南红星 3 楼，周末 10:00-18:00 都有人接待～加我企微发您定位和预约通道，到店有礼品：[企业微信名片]', time: '08-23 18:20', variant: 'A' }, { dir: 'in', text: '加你了，发定位', time: '08-24 11:18' }] },
    { id: 'LD-1014', nickname: '装修小白本白', hue: 44, intent: 'mid', source: 'own_comment', comment: '第一次装修啥都不懂，有没有攻略', video: '小白装修30条避坑', createdAt: '08-23 14:30', status: 'sent', account: '全屋定制·凯文', dealAmount: null, messages: [{ dir: 'out', text: '您好！给小白准备了一份《装修全流程日历+预算表》，评论区放不了文件，加我企微发您：[企业微信名片]', time: '08-23 15:02', variant: 'B' }] },
    { id: 'LD-1013', nickname: 'Lynn的宅家计划', hue: 300, intent: 'high', source: 'own_comment', comment: '预算15万，100平，能做日式原木风吗', video: '15万日式原木风全案', createdAt: '08-23 10:22', status: 'wechat_added', account: '老房改造·莉姐', dealAmount: null, wechatAddedAt: '08-23 19:44', manual: false, messages: [{ dir: 'out', text: '可以！100平 15 万做日式原木风，主材用国产一线完全够。发您两个我们刚交付的同预算实拍案例？', time: '08-23 10:50', variant: 'A' }, { dir: 'in', text: '发来看看', time: '08-23 11:15' }, { dir: 'out', text: '已发～细节图太多放企微相册了，加我随时翻：[企业微信名片]', time: '08-23 11:20', variant: 'A' }, { dir: 'in', text: 'ok 加了', time: '08-23 19:44' }] },
    { id: 'LD-1012', nickname: '大雄要努力', hue: 200, intent: 'low', source: 'own_comment', comment: '哈哈哈哈这个翻车现场太好笑了', video: '装修翻车大赏', createdAt: '08-22 21:08', status: 'rejected', account: null, dealAmount: null, rejectReason: '无意向信号，自动过滤', messages: [] },
    { id: 'LD-1011', nickname: '安家·城西', hue: 130, intent: 'high', source: 'own_comment', comment: '城西有没有门店，想去聊方案', video: '城西店开业福利', createdAt: '08-22 18:44', status: 'send_failed', account: '同城装修·班长', dealAmount: null, failNote: '账号安全模式暂停，重试 3 次未发出，已落库等待恢复', messages: [] },
    { id: 'LD-1010', nickname: '半糖主义', hue: 350, intent: 'mid', source: 'fan_dm', comment: '（粉丝私信）关注很久了，想问下全屋定制板材', video: 'ENF级板材科普', createdAt: '08-22 15:20', status: 'replied', account: '装修案例·阿明', dealAmount: null, messages: [{ dir: 'out', text: '您好！我们全屋定制默认 ENF 级板材，可以现场看检测报告。您家大概几平的柜子需求？我帮您估个用量和价～', time: '08-22 15:48', variant: 'A' }, { dir: 'in', text: '大概30平吧，全屋下来多少钱', time: '08-23 09:30' }] },
    { id: 'LD-1009', nickname: '风起云涌', hue: 80, intent: 'high', source: 'own_comment', comment: '旧房翻新求私信，房子在老城区', video: '老城区旧改实录', createdAt: '08-22 11:02', status: 'wechat_added', account: '设计顾问·小舟', dealAmount: null, wechatAddedAt: '08-23 10:10', manual: false, messages: [{ dir: 'out', text: '您好！老城区旧改我们做了 40+ 户，电梯/垃圾清运这些细节都熟。加我企微，发您同小区的改造前后对比：[企业微信名片]', time: '08-22 11:30', variant: 'A' }, { dir: 'in', text: '加了', time: '08-23 10:10' }] },
    { id: 'LD-1008', nickname: '攒钱买大平层', hue: 170, intent: 'mid', source: 'own_comment', comment: '先收藏，明年装', video: '大平层设计方案', createdAt: '08-21 19:33', status: 'collected', account: null, dealAmount: null, messages: [] },
  ];

  /* ════════ 客户与商机 ════════ */
  const STAGE_META = {
    added:       { label: '已加微',   cls: 'wechat',   order: 1 },
    measured:    { label: '已量房',   cls: 'sent',     order: 2 },
    proposal:    { label: '方案中',   cls: 'pending',  order: 3 },
    quoted:      { label: '已报价',   cls: 'throttled',order: 4 },
    negotiating: { label: '谈判中',   cls: 'mid',      order: 5 },
    won:         { label: '已成交',   cls: 'deal',     order: 6 },
    lost:        { label: '已流失',   cls: 'rejected', order: 7 },
  };

  let customers = [
    { id: 'CU-001', leadId: 'LD-1016', name: '叮当一家', hue: 260, source: 'own_comment', stage: 'won', estValue: 128000, dealAmount: 128000, dealAt: '08-25 10:30', wechatAddedAt: '08-24 09:12', nextAction: null, nextAt: null, logs: [{ time: '08-23 21:05', text: '私信引导加微（变体A）', by: '系统' }, { time: '08-24 09:12', text: '已加企微，自动发送欢迎语 + 案例合集', by: '系统' }, { time: '08-24 14:20', text: '电话沟通需求：120平三室两厅，全包预算 12-13 万', by: '老板' }, { time: '08-24 16:00', text: '预约周末量房', by: '老板' }, { time: '08-25 10:30', text: '签约成交 ¥128,000，8月28日开工', by: '老板' }] },
    { id: 'CU-002', leadId: 'LD-1021', name: '客厅改造预算8w', hue: 348, source: 'own_comment', stage: 'measured', estValue: 80000, wechatAddedAt: '08-25 09:44', nextAction: '发送量房后的方案与报价', nextAt: '今天 16:00', logs: [{ time: '08-24 22:40', text: '私信触达（变体A），用户回复要看案例', by: '系统' }, { time: '08-25 09:44', text: '已加企微（自动回调）', by: '系统' }, { time: '08-25 11:00', text: '上午量房完成：79㎡，拆改需求确认', by: '老板' }] },
    { id: 'CU-003', leadId: 'LD-1013', name: 'Lynn的宅家计划', hue: 300, source: 'own_comment', stage: 'quoted', estValue: 150000, wechatAddedAt: '08-23 19:44', nextAction: '报价反馈回访', nextAt: '明天 10:00', logs: [{ time: '08-23 19:44', text: '已加企微，发送日式原木风案例合集', by: '系统' }, { time: '08-24 10:30', text: '出方案：100㎡ 全案 ¥148,000（含主材）', by: '老板' }, { time: '08-24 10:35', text: '报价单已发送，等待反馈', by: '老板' }] },
    { id: 'CU-004', leadId: 'LD-1015', name: '北岸小业主', hue: 96, source: 'own_comment', stage: 'added', estValue: 110000, wechatAddedAt: '08-24 11:20', manual: true, nextAction: 'SOP·D1：发送同预算案例（自动生成）', nextAt: '今天 12:00', logs: [{ time: '08-24 11:20', text: '已加企微（手动标记，到店看样板间意向）', by: '系统' }, { time: '08-24 11:25', text: '发送样板间定位 + 预约通道', by: '系统' }] },
    { id: 'CU-005', leadId: 'LD-1009', name: '风起云涌', hue: 80, source: 'own_comment', stage: 'proposal', estValue: 160000, wechatAddedAt: '08-23 10:10', nextAction: '电话讲解 15 万旧改方案', nextAt: '明天 10:00', logs: [{ time: '08-23 10:10', text: '已加企微，发送同小区改造前后对比', by: '系统' }, { time: '08-24 09:40', text: '老城区 96㎡ 旧改方案设计中', by: '老板' }] },
    { id: 'CU-006', leadId: null, name: '张姐的复式楼', hue: 210, source: 'referral', stage: 'negotiating', estValue: 260000, wechatAddedAt: '08-18 15:02', referrer: '叮当一家（成交客户转介绍）', nextAction: '合同条款确认（工期/付款节点）', nextAt: '今天 18:00', logs: [{ time: '08-18 15:02', text: '「叮当一家」转介绍，直接加微', by: '系统' }, { time: '08-20 10:00', text: '复式 180㎡ 方案报价 ¥268,000', by: '老板' }, { time: '08-24 15:00', text: '第二轮谈判：主材升级方案，待确认合同条款', by: '老板' }] },
    { id: 'CU-007', leadId: 'LD-1025', name: '翡翠湾陈先生', hue: 20, source: 'inbound_dm', stage: 'quoted', estValue: 96000, wechatAddedAt: '08-25 10:40', nextAction: '发送报价二版（按 96㎡ 调整主材）', nextAt: '今天 15:00', logs: [{ time: '08-25 09:58', text: '用户主动私信主页（被动承接，意向最高）', by: '系统' }, { time: '08-25 10:40', text: '已加企微，报价单一版已发', by: '系统' }] },
    { id: 'CU-008', leadId: null, name: '张先生·老城公寓', hue: 330, source: 'competitor', stage: 'added', estValue: 70000, wechatAddedAt: '08-24 20:15', nextAction: 'SOP·D1：首条价值内容', nextAt: '今天 14:00', logs: [{ time: '08-24 20:15', text: '对标账号评论区挖来的线索，已加微', by: '系统' }] },
    { id: 'CU-009', leadId: null, name: '刘女士·阳光郡', hue: 50, source: 'own_comment', stage: 'lost', estValue: 90000, wechatAddedAt: '08-15 12:00', lostReason: '预算不匹配，选了施工队（报价高 15%）', lostAt: '08-22 09:30', nextAction: null, nextAt: null, logs: [{ time: '08-15 12:00', text: '已加企微，快速报价', by: '系统' }, { time: '08-22 09:30', text: '标记流失：预算不匹配', by: '老板' }] },
  ];

  /* ════════ 今日跟进 ════════ */
  let followups = [
    { id: 'FU-01', cuId: 'CU-007', customer: '翡翠湾陈先生', type: '报价跟进', text: '发送报价二版（按 96㎡ 调整主材）', due: '今天 15:00', overdue: false, done: false },
    { id: 'FU-02', cuId: 'CU-002', customer: '客厅改造预算8w', type: '方案推进', text: '量房后方案 + 报价发送', due: '今天 16:00', overdue: false, done: false },
    { id: 'FU-03', cuId: 'CU-006', customer: '张姐的复式楼', type: '谈判', text: '合同条款确认（工期/付款节点）', due: '今天 18:00', overdue: false, done: false },
    { id: 'FU-04', cuId: 'CU-004', customer: '北岸小业主', type: 'SOP·D1', text: '发送同预算案例（培育 SOP 自动生成）', due: '今天 12:00', overdue: true, done: false },
    { id: 'FU-05', cuId: 'CU-008', customer: '张先生·老城公寓', type: 'SOP·D1', text: '欢迎语后首条价值内容', due: '今天 14:00', overdue: true, done: false },
    { id: 'FU-06', cuId: 'CU-003', customer: 'Lynn的宅家计划', type: '报价跟进', text: '报价反馈回访', due: '明天 10:00', overdue: false, done: false },
    { id: 'FU-07', cuId: 'CU-005', customer: '风起云涌', type: '方案讲解', text: '电话讲解 15 万旧改方案', due: '明天 10:00', overdue: false, done: false },
  ];

  /* ════════ 工作台 ════════ */
  const workbench = {
    date: '2026-08-25 · 周二',
    today: { newLeads: 12, pendingSend: 5, sentToday: 46, wechatToday: 6, followupsPending: 7, followupsOverdue: 2, deal: { count: 1, amount: 128000 } },
    alerts: [
      { level: 'danger', text: '账号「同城装修·班长」健康分 38，已联动安全模式，全量暂停', action: '查看账号', href: '#/health' },
      { level: 'warn', text: 'R1 命中：「全屋定制·凯文」日频达 80，已自动降速至 40/天', action: '查看', href: '#/health' },
      { level: 'warn', text: '2 条私信发送失败（账号暂停中），已落库等待恢复', action: '查看队列', href: '#/dm' },
      { level: 'info', text: 'R2 已自动切换：装修主话术变体B 停用，权重转移至变体A', action: '查看话术', href: '#/scripts' },
    ],
    activity: compliance.audit.slice(0, 6),
  };

  /* ════════ 触点归因 ════════ */
  const attribution = {
    window: '近 30 天',
    bySource: [
      { key: 'own_comment', label: '自有视频评论区', leads: 186, wechat: 20, deal: 4, pilot: false },
      { key: 'competitor', label: '对标账号监控', leads: 64, wechat: 5, deal: 1, pilot: false },
      { key: 'inbound_dm', label: '用户主动私信', leads: 42, wechat: 8, deal: 2, pilot: false },
      { key: 'fan_dm', label: '粉丝私信', leads: 31, wechat: 3, deal: 1, pilot: false },
      { key: 'lead_card', label: '留资卡', leads: 17, wechat: 4, deal: 0, pilot: true },
      { key: 'referral', label: '老客转介绍', leads: 2, wechat: 1, deal: 1, pilot: true },
    ],
    topVideos: [
      { title: '8万块爆改79㎡客厅', leads: 38, wechat: 7, dealAmount: 80000 },
      { title: '120平全包真实成本', leads: 29, wechat: 4, dealAmount: 128000 },
      { title: '老城区旧改实录', leads: 21, wechat: 3, dealAmount: 0 },
      { title: '全包VS半包终极对比', leads: 18, wechat: 2, dealAmount: 0 },
      { title: '15万日式原木风全案', leads: 15, wechat: 2, dealAmount: 0 },
    ],
  };

  /* ════════ 私信任务队列 ════════ */
  const dmQueue = [
    { leadId: 'LD-1023', nickname: '王女士家半包', hue: 28, account: '装修案例·阿明', variant: 'A', status: 'pending_outreach', detail: '计划今天 10:40 发送（随机间隔 3-8 分钟）' },
    { leadId: 'LD-1022', nickname: '阿哲不加班', hue: 210, account: '全屋定制·凯文', variant: 'A', status: 'throttled', detail: 'R1 降速中，队列回放预计明天 09:00' },
    { leadId: 'LD-1011', nickname: '安家·城西', hue: 130, account: '同城装修·班长', variant: 'A', status: 'send_failed', detail: '重试 3/3 未发出，等待账号恢复安全模式' },
    { leadId: 'LD-1018', nickname: '喵喵要装修', hue: 320, account: '老房改造·莉姐', variant: 'B', status: 'sent', detail: '已发 08-24 15:31，等待回复' },
    { leadId: 'LD-1014', nickname: '装修小白本白', hue: 44, account: '全屋定制·凯文', variant: 'B', status: 'sent', detail: '已发 08-23 15:02，等待回复' },
    { leadId: 'LD-1019', nickname: '城南李工', hue: 190, account: '设计顾问·小舟', variant: 'A', status: 'replied', detail: '用户 08-25 08:15 回复「110平，毛坯」，待人工跟进' },
    { leadId: 'LD-1010', nickname: '半糖主义', hue: 350, account: '装修案例·阿明', variant: 'A', status: 'replied', detail: '用户 08-23 09:30 询价，待人工跟进' },
  ];

  /* ════════ 话术（含 category 字段用于 Tab 过滤） ════════ */
  const scripts = [
    {
      id: 'SC-001', name: '装修 · 主话术', industry: '装修', category: 'private_message', isMain: true, active: true,
      intro: '评论区高意向用户首次私信，价值钩子 = 同预算真实案例 + 明细报价',
      variants: [
        { id: 'A', text: '您好！看到您在「{视频标题}」下留言～{预算/面积}这个区间我们刚好有一套刚交付的真实案例（含全部主材明细）。评论区放不了文件，发您一份落地实拍+报价单？', weight: 70, status: 'active', sent: 214, replied: 79, wechatAdded: 24, convRate: 11.2, sampleEnough: true },
        { id: 'B', text: '您好！您留言的{需求关键词}我们本月做了 3 户同小区的工地～加我企微发您工地实拍直播和报价区间，还能预约到店量房：[企业微信名片]', weight: 30, status: 'switched_off', sent: 51, replied: 9, wechatAdded: 3, convRate: 5.9, sampleEnough: true, r2Note: 'R2 命中（08-24）：转化率 5.9% < 8%，样本 51 ≥ 30，已自动停用，权重转移至变体A' },
        { id: 'C', text: '（编辑中草稿）您好，看到您的留言，我们专注本地装修 12 年…', weight: 0, status: 'draft', sent: 0, replied: 0, wechatAdded: 0, convRate: null, sampleEnough: false },
      ],
      welcomeMsg: '欢迎～我是 {顾问名}，您的专属装修顾问。这边先发您三样东西：①同预算案例合集 ②报价明细表 ③工地直播入口，有任何问题随时问我。',
    },
    {
      id: 'SC-002', name: '加微后 · 培育 SOP', industry: '装修', category: 'nurture', isMain: false, active: true,
      intro: 'added_wechat=1 后自动排入：欢迎语 → D1 价值内容 → D3 促单',
      variants: [
        { id: 'A', text: '【D1】今天给您发的案例里，第 2 套和您家户型最接近，重点看下厨房那面墙的改法～', weight: 100, status: 'active', sent: 41, replied: 18, wechatAdded: null, convRate: null, sampleEnough: false },
      ],
      welcomeMsg: '（培育欢迎语复用主话术 welcomeMsg）',
    },
    {
      id: 'SC-003', name: '评论区 · 引流回复', industry: '装修', category: 'comment', isMain: false, active: true,
      intro: '高意向评论区直接回复，用价值钩子引导私信或主页',
      variants: [
        { id: 'A', text: '我们老城区旧改做了40+户，加我发您同小区改造前后对比～', weight: 60, status: 'active', sent: 89, replied: 34, wechatAdded: 12, convRate: 13.5, sampleEnough: true },
        { id: 'B', text: '同面积同预算我们刚落地一套，私我发您实拍和报价明细', weight: 40, status: 'active', sent: 56, replied: 18, wechatAdded: 5, convRate: 8.9, sampleEnough: true },
      ],
      welcomeMsg: '',
    },
    {
      id: 'SC-004', name: '微信引导 · 加微话术', industry: '装修', category: 'wechat_guide', isMain: false, active: true,
      intro: '私信回复后引导添加企业微信的转化话术',
      variants: [
        { id: 'A', text: '报价单和案例都在企微相册里，加我随时翻，后续有活动第一时间同步您：[企业微信名片]', weight: 100, status: 'active', sent: 156, replied: 0, wechatAdded: 68, convRate: 43.6, sampleEnough: true },
      ],
      welcomeMsg: '',
    },
    {
      id: 'SC-005', name: '异议处理 · 价格/对比', industry: '装修', category: 'objection', isMain: false, active: true,
      intro: '应对"太贵了""再看看""对比一下"等常见异议',
      variants: [
        { id: 'A', text: '理解您的顾虑～我们报价含主材+施工+售后，同配置比市场价低8%，可以发您明细对比一下', weight: 100, status: 'active', sent: 42, replied: 15, wechatAdded: 6, convRate: 14.3, sampleEnough: false },
      ],
      welcomeMsg: '',
    },
  ];

  const scriptTemplates = [
    { industry: '装修', count: 8, desc: '首次触达×3 / 报价跟进×2 / 加微引导×3', installed: true },
    { industry: '教育', count: 0, desc: 'v2 规划中（强监管行业模板包）', installed: false },
    { industry: '医疗口腔', count: 0, desc: 'v2 规划中', installed: false },
  ];

  /* ════════ 评论回复任务（mock 回退数据） ════════ */
  const commentTasks = [
    { id: 'CT-001', leadId: 'LD-1024', commentContent: '家里90平老房子想翻新，求靠谱的装修公司', videoTitle: '老房改造避坑指南', replyContent: '', status: 'pending', account: '装修案例·阿明', repliedAt: null, userVisited: false, userDm: false, userRepliedComment: false },
    { id: 'CT-002', leadId: 'LD-1023', commentContent: '半包大概什么价位？有具体报价单吗', videoTitle: '2026半包全包价目表', replyContent: '', status: 'pending', account: '装修案例·阿明', repliedAt: null, userVisited: false, userDm: false, userRepliedComment: false },
    { id: 'CT-003', leadId: 'LD-1019', commentContent: '求推荐靠谱施工队，年底想入住', videoTitle: '年底完工工期规划', replyContent: '我们老城区旧改做了40+户，加我发您同小区改造对比', status: 'replied', account: '设计顾问·小舟', repliedAt: '2026-08-25T09:30:00+00:00', userVisited: false, userDm: false, userRepliedComment: false },
    { id: 'CT-004', leadId: 'LD-1021', commentContent: '预算8万左右能做成视频里这样吗？私我', videoTitle: '8万块爆改79㎡客厅', replyContent: '同面积同预算我们刚落地一套，私我发您实拍和报价明细', status: 'user_replied', account: '装修案例·阿明', repliedAt: '2026-08-24T22:40:00+00:00', userVisited: true, userDm: false, userRepliedComment: true },
    { id: 'CT-005', leadId: 'LD-1013', commentContent: '预算15万，100平，能做日式原木风吗', videoTitle: '15万日式原木风全案', replyContent: '可以！100平15万做日式原木风，主材用国产一线完全够，私我发案例', status: 'user_dm', account: '老房改造·莉姐', repliedAt: '2026-08-23T10:50:00+00:00', userVisited: true, userDm: true, userRepliedComment: false },
  ];

  /* ════════ 辅助：mock 回退包装 ════════ */
  function _fallback(label, mockFn) {
    return async function () {
      try {
        return await mockFn.apply(null, arguments);
      } catch (e) {
        console.warn('[API fallback] ' + label + ' 调用失败，使用 mock 数据:', e.message);
        return mockFn.apply(null, arguments);
      }
    };
  }

  /* ── 字段归一化：后端 snake_case → 前端 camelCase（保持 app.js 零改动） ── */
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

  /* ════════ 暴露 API ════════ */
  window.MOCK_API = {

    /* 工作台 → 由 api-dashboard.js 提供真实 API */

    /* ── 获客域 · Lead 模块（已完成，真实对接） ── */
    getLeads: async (params = {}) => {
      const qs = new URLSearchParams();
      if (params.status && params.status !== 'all') qs.set('status', params.status);
      if (params.source) qs.set('source', params.source);
      if (params.keyword) qs.set('keyword', params.keyword);
      if (params.min_score != null) qs.set('min_score', params.min_score);
      const query = qs.toString();
      try {
        const data = await apiFetch('/leads' + (query ? '?' + query : ''));
        return _normalize(data);
      } catch (e) {
        _toast('线索数据加载失败：' + e.message, 'warn');
        // 回退到 mock 过滤
        let list = clone(leads);
        if (params.status && params.status !== 'all') list = list.filter((l) => l.status === params.status);
        if (params.source) list = list.filter((l) => l.source === params.source);
        return list;
      }
    },

    getLeadStatusMeta: async () => {
      try {
        return await apiFetch('/leads/status-meta');
      } catch (e) {
        return clone(LEAD_STATUS); // 元数据静默回退
      }
    },

    getSourceMeta: async () => {
      try {
        return await apiFetch('/leads/source-meta');
      } catch (e) {
        return clone(SOURCE_META);
      }
    },

    getDmQueue: async () => {
      try {
        const data = await apiFetch('/dm/queue');
        return _normalize(data);
      } catch (e) {
        _toast('私信队列加载失败：' + e.message, 'warn');
        return clone(dmQueue);
      }
    },

    markWechatAdded: async (leadId) => {
      try {
        return await apiFetch('/leads/' + leadId + '/wechat_added', {
          method: 'POST',
          body: JSON.stringify({ manual: true }),
        });
      } catch (e) {
        _toast('标记加微失败：' + e.message, 'warn');
        return { ok: false, leadId, manual: true, error: e.message };
      }
    },

    /* ── 评论回复任务（Lead 模块扩展，真实对接） ── */
    getCommentTasks: async (status) => {
      const query = status ? '?status=' + encodeURIComponent(status) : '';
      try {
        const data = await apiFetch('/comments/tasks' + query);
        return _normalize(data);
      } catch (e) {
        _toast('评论任务加载失败：' + e.message, 'warn');
        let list = clone(commentTasks);
        if (status) list = list.filter((t) => t.status === status);
        return list;
      }
    },

    updateCommentTask: async (id, data) => {
      try {
        return await apiFetch('/comments/tasks/' + id, {
          method: 'PATCH',
          body: JSON.stringify(data),
        });
      } catch (e) {
        _toast('更新评论任务失败：' + e.message, 'warn');
        return { ok: false, error: e.message };
      }
    },

    createCommentTask: async (data) => {
      try {
        return await apiFetch('/comments/tasks', {
          method: 'POST',
          body: JSON.stringify(data),
        });
      } catch (e) {
        _toast('创建评论任务失败：' + e.message, 'warn');
        return { ok: false, error: e.message };
      }
    },

    /* 话术库/账号/客户/跟进 → 由 api-business.js 提供真实 API */
    /* 分析域/合规/运行态/上手向导 → 由 api-dashboard.js 提供真实 API */
  };

  /* ═════════════════════════════════════════════════════════
     接口映射表（第八节对照表）
     ─────────────────────────────────────────────────────────
     【工作台】
     getWorkbench()       → GET  /api/workbench                    [mock回退]

     【获客域 · Lead 模块（已完成真实对接）】
     getLeads(params)     → GET  /api/leads?status=...&source=...  [真实API]
     getLeadStatusMeta()  → GET  /api/leads/status-meta             [真实API]
     getSourceMeta()      → GET  /api/leads/source-meta             [真实API]
     getDmQueue()         → GET  /api/dm/queue                      [真实API]
     markWechatAdded(id)  → POST /api/leads/{id}/wechat_added       [真实API]

     【评论回复任务（新增 · 真实对接）】
     getCommentTasks(s)   → GET  /api/comments/tasks?status=...     [真实API]
     updateCommentTask()  → PATCH /api/comments/tasks/{id}           [真实API]
     createCommentTask()  → POST /api/comments/tasks                  [真实API]

     【获客域 · 其他（mock回退）】
     getScripts()         → GET  /api/scripts                        [mock回退]
     getScriptTemplates() → GET  /api/scripts/templates              [mock回退]
     getAccounts()        → GET  /api/accounts                       [mock回退]

     【客户域（mock回退）】
     getCustomers()       → GET  /api/customers                      [mock回退]
     getStageMeta()       → GET  /api/customers/stage-meta           [mock回退]
     getFollowups()       → GET  /api/followups?date=today           [mock回退]
     completeFollowup(id) → POST /api/followups/{id}/complete        [mock回退]
     advanceStage(id,st)  → POST /api/customers/{id}/stage           [mock回退]
     recordDeal(id,amt)   → POST /api/customers/{id}/deal            [mock回退]
     markLost(id,reason)  → POST /api/customers/{id}/lost            [mock回退]

     【分析域（mock回退）】
     getFunnel()          → GET  /api/dashboard/funnel               [mock回退]
     getAttribution()     → GET  /api/dashboard/attribution          [mock回退]
     getCompliance()      → GET  /api/dashboard/compliance           [mock回退]

     【运行态（mock回退）】
     getRuntime()         → GET  /api/runtime                        [mock回退]
     enterSafeMode(...)   → POST /api/compliance/safe-mode/enter     [mock回退]
     exitSafeMode()       → POST /api/compliance/safe-mode/exit      [mock回退]
     pauseAll()/resumeAll → POST /api/compliance/pause | /resume     [mock回退]
     submitWizard(payload)→ POST /api/onboarding/wizard               [mock回退]
     ═════════════════════════════════════════════════════════ */
})();
