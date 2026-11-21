/**
 * 真机采集执行器（跑在桌面壳主进程）
 *
 * 为什么必须在壳里做
 * ----------------
 * 采集要读的是**已登录账号**看到的评论，登录态存在 Electron 的 session 里。
 * 后端（Python）碰不到 DOM，所以由后端下发指令、壳执行并把结果回传：
 *   GET  /desktop/command  ← 取一条指令（keyword / videoMax）
 *   POST /desktop/result   → 回传 comments + stats + 诊断
 *
 * 本版能力与边界
 * --------------
 * 指令类型：
 *   collect-comments   搜索关键词 → 逐个视频读评论（评论获客）
 *   search-videos      搜索关键词 → 视频 + 作者（线索采集）
 *   read-comments      指定视频 → 当前评论（监控）
 *   read-author-works  作者主页 → 最近作品（监听主播）
 *   warmup-watch       推荐页真实浏览（滚动/停留），点赞按配额且只点未赞的（养号）
 * 采集指令只读；互动须通过已确认清单及签名范围，始终由用户完成登录。
 */
const http = require("http");
const fs = require("fs");
const path = require("path");
const { BrowserWindow, WebContentsView, View, net, session } = require("electron");
const { createWorkerSurface } = require("./worker-surface");
const { resolvePartition } = require("./session-partition");
const { classifyDiggReceipts } = require("./write-commands");
const { rejectLiveAction } = require("./action-executor");

const BASE = "http://127.0.0.1:8899";
const POLL_MS = 1200;

// 实例标识：后端按它分开统计取指令次数。多开时能立刻看出
// 「指令被另一个实例拿走了」，而不是对着持续增长的轮询数误判成正常。
const AGENT_ID = "shell-" + process.pid + "-" + Date.now().toString(36).slice(-4);

let timer = null;
// 并发执行模型（2026-11-18）：同一账号的常驻窗口一次只跑一条指令（同账号
// 串行）；不同账号的窗口互不干扰（跨账号并行，吞评检测「所选账号同时运行」
// 依赖它）。inflightAccounts 记账「哪个账号正在跑」。后端派发时同样按账号
// 过滤，这里再加一道防线：万一两边判断有时间差，指令经 /desktop/command-requeue
// 放回队列，不丢、不假成功。
const MAX_PARALLEL = Math.max(1, Math.min(6, Number(process.env.ELECTRON_MAX_PARALLEL_COMMANDS) || 4));
const inflightAccounts = new Set();
let polling = false;
// 当前正在执行的采集视图集合。
// 为什么用集合而不是单个变量：多账号可以并行跑，A 跑完把共享变量清空会让
// B 的循环误判成「已被停止」。stop() 时整体 clear() 即可中断所有账号。
const liveViews = new Set();
// 本次壳生命周期内已经点过赞的视频地址：同一个视频再点一次就是「取消赞」，
// 跨轮次也要挡住（分页/推荐会重复推到同一条）。
const warmupLiked = new Set();

/* --------------------------------------------------------------- 诊断日志 */
/**
 * 埋点写到文件里（_verify/agent.log）。
 * 为什么不只靠 console：主进程的 stdout 在被打包/重定向时可能拿不到，
 * 排查「指令取走了但没回执」这类问题时，光看后端 side 只会看到 results=0，
 * 分不清是壳没收到、页面没执行、还是回传失败。文件日志一路留下才定得了位。
 */
// 实现搬到 diag-log.js：写动作（关注/私信/点赞）的现场诊断要写同一个文件，
// 而它们不该反过来 require 这个十几万行的采集模块。
const { trace, LOG_FILE } = require("./diag-log");

/**
 * 采集窗口全程不主动弹。
 *
 * 为什么：无人值守时段（排期）突然弹出抖音窗口，会打断用户正在做的事，
 * 而且窗口一旦被看见，用户很容易顺手关掉——那会连带毁掉采集。
 * 需要人工处理（验证码）时只发通知，由用户在托盘里自己决定要不要看。
 */
let noticeHandler = null;
function onNotice(cb) {
  noticeHandler = typeof cb === "function" ? cb : null;
}
function notify(title, content) {
  trace("[通知]", title, "|", content);
  if (!noticeHandler) return;
  try { noticeHandler({ title: title, content: content }); } catch (e) { /* 通知失败不影响采集 */ }
}

/* --------------------------------------------------------------- HTTP 小工具 */
/**
 * 直连 session。
 *
 * 本机有个透明代理（HTTP_PROXY=127.0.0.1:49626）。Chromium 网络栈默认可能把
 * 127.0.0.1:8899 也交给代理，所以显式给一个 mode:"direct" 的独立 session，
 * 只用于访问本机桥接。不共享 cookie，也不落缓存。
 */
let directSession = null;
function getDirectSession() {
  if (directSession) return directSession;
  directSession = session.fromPartition("bridge-direct", { cache: false });
  try { directSession.setProxy({ mode: "direct" }); } catch (e) { trace("设置直连失败:", (e && e.message) || e); }
  return directSession;
}

/** 走 Chromium 网络栈发请求（Electron 的 net 模块） */
function requestViaNet(method, urlPath, body) {
  return new Promise((resolve) => {
    const data = body ? JSON.stringify(body) : null;
    let settled = false;
    let timer = null;
    let clientReq = null;
    const done = (v, why) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      // 超时后必须 abort：Chromium 的请求不会自己结束，socket 挂着不放，
      // 后端会为这条连接占一个读取线程。壳每 1.2s 泄漏一条，几分钟就能把
      // 后端线程池占满 —— 那时连 /bridge-info 都超时，看起来像后端挂了。
      if (why) {
        try { clientReq && clientReq.abort(); } catch (e) { /* 已结束 */ }
        trace("net 失败:", method, urlPath, why);
      }
      resolve(v);
    };
    try {
      const req = net.request({
        method,
        url: BASE + urlPath,
        session: getDirectSession(),
        useSessionCookies: false,
      });
      clientReq = req;
      if (data) {
        req.setHeader("Content-Type", "application/json");
        // 不要自己设 Content-Length：Chromium 侧会自己算，手设会直接
        // 报 net::ERR_INVALID_ARGUMENT，POST 全部失败（GET 不受影响）。
      }
      req.on("response", (res) => {
        let raw = "";
        res.on("data", (c) => (raw += c));
        res.on("end", () => {
          try { done(JSON.parse(raw || "{}")); }
          catch (e) { done({}, "响应非 JSON"); }
        });
        res.on("error", (e) => done({}, (e && e.message) || "response-error"));
      });
      req.on("error", (e) => done({}, (e && e.message) || "request-error"));
      req.on("abort", () => done({}, "aborted"));
      // 本机请求正常是几毫秒级；3.5s 已经是非常宽松的上限。
      // 不要设太长：一个卡住的 tick 会占住轮询，期间后端下发的指令全排不上。
      timer = setTimeout(() => done({}, "timeout-3.5s"), 3500);
      if (data) req.write(data);
      req.end();
    } catch (e) {
      done({}, "throw:" + ((e && e.message) || e));
    }
  });
}

/** 走 Node 原生 http（兜底路径） */
function requestViaNodeHttp(method, urlPath, body) {
  return new Promise((resolve) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request(
      BASE + urlPath,
      {
        method,
        agent: false, // 不复用连接：uvicorn keep-alive 到期先关，复用会吃 RST
        headers: data
          ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) }
          : { Connection: "close" },
      },
      (res) => {
        let raw = "";
        res.on("data", (c) => (raw += c));
        res.on("end", () => {
          try { resolve(JSON.parse(raw || "{}")); }
          catch (e) { resolve({}); }
        });
      }
    );
    req.on("error", (e) => {
      trace("node-http 失败:", method, urlPath, (e && e.code) || (e && e.message) || e);
      resolve({});
    });
    req.setTimeout(2500, () => {
      trace("node-http 超时:", method, urlPath);
      req.destroy();
      resolve({});
    });
    if (data) req.write(data);
    req.end();
  });
}

/**
 * 发本机请求：先 Chromium 栈，失败再退回 Node 原生。
 *
 * 为什么不能只用 Node 的 http：壳里一旦打开抖音页面（采集窗口），主进程的
 * Node socket 就收不到本机后端的响应了 —— 实测同一时刻 curl 往返 24ms、后端
 * 也回了 200，但壳这边 8s 都等不到 data，只能 destroy 掉吃 ECONNRESET。
 * 结果就是「指令被取走、轮询数照涨、后端 results 永远是 0」。
 * Chromium 网络栈与页面同栈，压力下仍然稳定，所以让它做首选；Node http 留作兜底。
 */
async function request(method, urlPath, body) {
  const res = await requestViaNet(method, urlPath, body);
  if (res && Object.keys(res).length > 0) return res;
  // net 没拿到东西（或后端真返回空对象），换条路再试一次
  return requestViaNodeHttp(method, urlPath, body);
}

/* ------------------------------------------------------------------ 页面脚本 */
/** 搜索结果页：收集视频链接（滚动加载更多） */
const SEARCH_EXTRACT_JS = `(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const seen = new Set();
  const out = [];
  const grab = () => {
    document.querySelectorAll('a[href*="/video/"]').forEach(a => {
      const href = a.href || '';
      const m = /\\/video\\/(\\d{6,})/.exec(href);
      if (m && !seen.has(m[1])) {
        seen.add(m[1]);
        const wrap = a.closest('li') || a.parentElement;
        const title = (wrap && (wrap.innerText || '')).replace(/\\s+/g, ' ').trim().slice(0, 80);
        out.push({ videoId: m[1], url: 'https://www.douyin.com/video/' + m[1], title });
      }
    });
  };
  grab();
  for (let i = 0; i < 5; i++) {
    window.scrollBy(0, window.innerHeight * 0.9);
    await sleep(900);
    grab();
    if (out.length >= 24) break;
  }
  let loginWall = !!document.querySelector('#login-pannel, [data-e2e="login-guide"], .login-mask');
  const bodyText = document.body.innerText || '';
  // 抖音新版登录浮层没有上面那些选择器（2026-11-14 真机实锤：文本是
  // 「登录后即可搜索更多精彩视频 / 扫码登录」，任务曾因此「正常完成」但 0 条）。
  // 文本特征兜底：登录引导话术 + 一条视频都没解析到才算登录墙。
  if (!loginWall && out.length === 0) {
    loginWall = /登录后即可搜索|扫码登录\\s*如何扫码|验证码登录\\s*密码登录/.test(bodyText.replace(/\\n+/g, ''));
  }
  const emptyHint = /暂无搜索结果|没找到|无结果/.test(bodyText);
  return { videos: out, loginWall, emptyHint, url: location.href };
})()`;

/** 视频详情页：展开并读取评论区 */
const COMMENT_EXTRACT_JS = `(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const diag = { selectors: {}, scrolled: 0, vis: document.visibilityState };
  const windowMinutes = __COMMENT_WINDOW_MINUTES__;
  const expandSubComments = __EXPAND_SUB_COMMENTS__;

  // 1) 仅在评论面板确实收起时才点图标。已展开的零评论面板再点一次会被关掉。
  const commentList = document.querySelector('[data-e2e="comment-list"]');
  const listRect = commentList && commentList.getBoundingClientRect();
  const listShown = !!(listRect && listRect.width > 100 && listRect.height > 20 &&
    listRect.right > 0 && listRect.left < (window.innerWidth || 1280));
  const visibleCommentItems = Array.from(document.querySelectorAll('[data-e2e="comment-item"]'))
    .some(el => { const r = el.getBoundingClientRect();
      return r.width > 8 && r.height > 8 && r.right > 0 && r.left < (window.innerWidth || 1280); });
  const pageText = String(document.body && document.body.innerText || '');
  const emptyCommentPanel = /(?:全部评论|评论)\\s*[（(]\\s*0\\s*[）)]/.test(pageText) &&
    /暂无评论|没有评论|还没有评论/.test(pageText);
  const panelAlreadyOpen = listShown || visibleCommentItems || emptyCommentPanel;
  if (!panelAlreadyOpen) {
    const iconSelectors = [
      '[data-e2e="comment-icon"]',
      '[data-e2e="feed-comment-icon"]',
      'span[data-e2e*="comment"]'
    ];
    for (const sel of iconSelectors) {
      const el = document.querySelector(sel);
      if (el) { try { el.click(); diag.clickedIcon = sel; } catch (e) {} break; }
    }
    await sleep(1200);
  } else {
    diag.commentPanelAlreadyOpen = true;
  }

  if (expandSubComments) {
    const expanders = Array.from(document.querySelectorAll('button,[role="button"],span,div'))
      .filter(el => el.offsetParent && /展开\s*(?:\d+\s*条)?\s*(?:回复|评论)/.test(
        String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()))
      .slice(0, 40);
    for (const el of expanders) {
      try { el.click(); } catch (_) {}
    }
    if (expanders.length) await sleep(700);
    diag.expandedSubComments = expanders.length;
  }

  // 2) 找「真正会滚动」的容器，再增量往下滚，把懒加载的评论滚出来。
  //    2026-11-19 真机取证（video 7659638318140788003）：
  //      [data-e2e="comment-list"] 自身 scrollHeight === clientHeight（802/802），根本不可滚；
  //      真正滚动的是它的祖先 .route-scroll-container（scrollHeight 2452 → 6091）。
  //    旧写法把 scrollTop 设在这个不可滚节点上，二次再 size 到 scrollHeight 也是空转 ——
  //    首屏 6 条之后不再加载，用户在采集窗口里看到的就是「屏幕压根没往下滚」。
  //    增量滚动实测：6 → 16 → 26 → 34 → 37 条，滚到底才能读全。
  const listSelectors = [
    '[data-e2e="comment-list"]',
    '[data-e2e="comment-list-container"]',
    'div[class*="comment-list"]',
    'div[class*="CommentList"]'
  ];
  let list = null;
  for (const sel of listSelectors) {
    const el = document.querySelector(sel);
    diag.selectors[sel] = el ? (el.querySelectorAll('*').length) : 0;
    if (el && !list) list = el;
  }
  const canScroll = (el) => !!el && el.scrollHeight > el.clientHeight + 20;
  // 收起/关闭状态的评论区在文档里还占着节点，但宽高是 0 或被推到视口外。
  // 滚一个不可见的容器等于空转，还会让 diag 看起来「滚过了」。
  const shownBox = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8;
  };
  // 评论面板是右侧窄栏（真机 336px）。比它宽得多的一定是整页/外层滚动容器，
  // 滚那种容器会把详情浮层顶掉（实测浮层宽度直接变 0，评论再也读不到）。
  const panelWidth = (() => {
    const r = list ? list.getBoundingClientRect() : null;
    return r && r.width > 200 ? r.width : 620;
  })();
  // 独立视频页（评论区在视频下方、很宽）与详情浮层（右侧窄栏）的滚动目标不同
  const wideLayout = !!(list && list.getBoundingClientRect().width > 500);
  const isPanelScroller = el => {
    if (!shownBox(el) || !canScroll(el)) return false;
    const r = el.getBoundingClientRect();
    // 独立视频页评论区很宽（真机 880px），整页容器（1280）也算合法滚动目标；
    // 只有浮层窄栏才需要把「整页容器」排除掉，否则滚它会把浮层顶掉。
    if (wideLayout) return r.height > 120;
    return r.width <= Math.max(panelWidth + 40, 620) && r.height > 120;
  };
  const findScroller = () => {
    // 先向上找可滚祖先：评论面板的滚动条挂在列表外层容器上
    for (let p = list; p && p !== document.body; p = p.parentElement) {
      if (isPanelScroller(p)) return p;
    }
    // 再向下找：有的版本把滚动条放在列表内部的 wrapper 上
    if (list) {
      const inner = Array.from(list.querySelectorAll("*")).find(el => isPanelScroller(el));
      if (inner) return inner;
    }
    // 兜底：整页可滚才滚整页 —— 但详情浮层（搜索页上盖一层）不能滚整页：
    // 真机 2026-11-19 实测滚完搜索页，浮层宽度直接变 0（被顶掉），评论再也读不到。
    // 只在独立视频页（/video/<id>）才允许整页兜底。
    const onVideoPage = /\\/video\\/\\d/.test(String(location.pathname || ""));
    if (onVideoPage && canScroll(document.scrollingElement)) return document.scrollingElement;
    return null;
  };
  let scroller = findScroller();
  // 保存进入评论区前的视频位置。独立视频页的评论区位于视频下方，
  // 阅读结束后要回到视频；右侧评论浮层则留在评论面板顶部，不能滚动整页。
  const videoTop = scroller ? scroller.scrollTop : 0;
  // 骨架态：comment-item 节点已经有了，但文本还没渲染（真机实测：面板刚展开时
  // 恰好 2 个空 item）。按「有文字的条数」计数，否则骨架会被当成「读到 2 条且
  // 不再增长」，两轮就退出，结果 count=0，而用户在屏幕上明明看得到评论。
  const rendered = el => String((el && el.innerText) || '').trim().length > 2;
  const itemCount = () => Array.from(document.querySelectorAll('[data-e2e="comment-item"]'))
    .filter(rendered).length;
  // 先等第一批评论出文字（最多 6s）再开始翻阅
  for (let w = 0; w < 15 && itemCount() === 0; w++) await sleep(400);
  // 面板刚展开时尺寸还在动画中，等完文本再确认一次滚动容器
  scroller = scroller || findScroller();
  // 独立视频页的评论区在视频下方（真机实测 top=920 > 视口 900）：
  // 先像人一样把视线落到评论区，再开始逐屏翻。否则「读评论」是在屏幕外空读，
  // 采集窗口里也看不到任何滚动动作。
  if (wideLayout && list) {
    try { list.scrollIntoView({ block: "start" }); } catch (e) {}
    await sleep(700);
    scroller = findScroller() || scroller;
  }
  // 浮层回到评论面板顶部；独立视频页回到阅读评论前的视频位置。
  const commentTop = scroller ? scroller.scrollTop : 0;
  const restoreTop = wideLayout ? videoTop : commentTop;
  const scrollMaxRounds = __COMMENT_SCROLL_ROUNDS__;
  const wantItems = __COMMENT_LIMIT__;
  const jitter = (min, max) => min + Math.floor(Math.random() * Math.max(1, max - min + 1));
  // 档位（2026-11-20 全网行为数据口径）：真人大多数只扫一眼热评就走——
  // 视线研究实测「每次打开评论区平均只停 2.71 秒、每次会话平均 5.5 次滚动」，
  // 只有评论又多又有看头时才会驻足 20-40 秒。档位按视频评论数定：
  //   <20 条 → 瞄一眼（1-4 秒，首屏看完就走）
  //   20-500 → 扫一圈（8-20 秒，前两屏——平台也把优质热评压在前两屏）
  //   >500 或读不到评论数 → 驻足（20-40 秒，读满本次上限）
  const parseCountText = (t) => {
    const m = /([\d.]+)\s*(万|w|亿)?/i.exec(String(t || ''));
    if (!m) return null;
    const n = parseFloat(m[1]);
    if (!Number.isFinite(n)) return null;
    return Math.round(n * (m[2] === '亿' ? 1e8 : (m[2] ? 1e4 : 1)));
  };
  const railCount = (() => {
    const el = document.querySelector('[data-e2e="comment-count"]');
    if (el) {
      const n = parseCountText(el.getAttribute('data-count') || el.textContent);
      if (n != null) return n;
    }
    for (const node of document.querySelectorAll('[aria-label]')) {
      const label = node.getAttribute('aria-label') || '';
      if (!/评论/.test(label)) continue;
      const n = parseCountText(label);
      if (n != null) return n;
    }
    return null;
  })();
  let tierName = 'scan';
  let stopItems = wantItems;
  let tierBudgetS = jitter(8, 20);
  if (railCount != null && railCount < 20) {
    tierName = 'glance'; stopItems = Math.min(wantItems, 8); tierBudgetS = jitter(1, 4);
  } else if (railCount != null && railCount > 500) {
    tierName = 'stay'; tierBudgetS = jitter(20, 40);
  } else {
    stopItems = Math.min(wantItems, jitter(25, 30));
  }
  const browseStartedAt = Date.now();
  diag.browseTier = tierName + (railCount != null ? '@' + railCount : '@unknown');
  diag.browseBudgetS = tierBudgetS;
  let lastCount = itemCount();
  let still = 0;
  let peak = lastCount;
  let pauses = 0;
  diag.itemsFirst = lastCount;
  diag.scrollerCls = scroller ? String(scroller.className || "").slice(0, 60) : "";
  // 人式翻阅（用户 2026-11-19 口径）：不是匀速电机式滚到底，而是
  // 「滚一段 → 停下来看 → 再滚一段 → 再停」，看几屏之后停久一点。
  // 步长和停顿都随机化，避免每轮节奏完全一模一样（那是最容易被识别成脚本的形态）。
  for (let i = 0; i < scrollMaxRounds; i++) {
    if (!scroller) break;
    // 档位时间预算：到点就走，不再恋战（均值 20 秒左右的直接来源）
    if (Date.now() - browseStartedAt > tierBudgetS * 1000) { diag.budgetStop = true; break; }
    const step = jitter(480, 760);
    try { scroller.scrollTop = Math.min(scroller.scrollTop + step, scroller.scrollHeight); } catch (e) {}
    // 有的版本懒加载只监听 scroll（直接改 scrollTop 不会自动派发）
    try { scroller.dispatchEvent(new Event('scroll', { bubbles: true })); } catch (e) {}
    await sleep(jitter(800, 1500));
    // 视线被某条评论「停住」：高赞/有意思的内容会多看 1.5-3 秒（视线研究口径）
    if (Math.random() < 0.2) { pauses += 1; await sleep(jitter(1500, 3000)); }
    // 每滚三屏算「看了一段」：停下来多读一会，再继续往下
    if ((i + 1) % 3 === 0) { pauses += 1; await sleep(jitter(2000, 3000)); }
    // 追加内容时虚拟列表可能重建容器：每轮重新确认一次
    if (!canScroll(scroller)) scroller = findScroller() || scroller;
    const now = itemCount();
    peak = Math.max(peak, now);
    diag.scrolled = i + 1;
    diag.scrollTop = scroller ? Math.round(scroller.scrollTop) : 0;
    diag.scrollHeight = scroller ? scroller.scrollHeight : 0;
    if (now >= stopItems) break;                  // 够本轮要的条数就停
    if (now <= lastCount) { still += 1; } else { still = 0; }
    lastCount = Math.max(lastCount, now);
    if (still >= 2) break;                        // 连续两轮没有新增：已经到底
  }
  diag.itemsPeak = peak;
  diag.readPauses = pauses;
  // 「看得差不多了往上翻」：分几段退回顶部，不是一下跳上去。
  // 主评输入框在面板顶部，上翻顺带把编辑器带回可见区 —— 之后才能留评论。
  let backSteps = 0;
  while (scroller && scroller.scrollTop > restoreTop + 12 && backSteps < 14) {
    backSteps += 1;
    try { scroller.scrollTop = Math.max(restoreTop, scroller.scrollTop - jitter(560, 900)); } catch (e) {}
    try { scroller.dispatchEvent(new Event('scroll', { bubbles: true })); } catch (e) {}
    await sleep(jitter(380, 650));
  }
  if (scroller) { try { scroller.scrollTop = restoreTop; } catch (e) {} await sleep(jitter(220, 420)); }
  diag.backSteps = backSteps;
  diag.videoTop = Math.round(videoTop);
  diag.commentTop = Math.round(commentTop);
  diag.restoredTop = scroller ? Math.round(scroller.scrollTop) : null;

  // 3) 抓评论项：优先 data-e2e 精确项，抓不到内容再退 class 模糊项
  const e2eItems = Array.from(document.querySelectorAll('[data-e2e="comment-item"]'));
  const fuzzyItems = Array.from(document.querySelectorAll(
      'div[class*="comment-item"]:not([class*="avatar"]), div[class*="CommentItem"]'));
  diag.selectors['[data-e2e="comment-item"]'] = e2eItems.length;
  diag.selectors['div[class*="comment-item"]'] = fuzzyItems.length;
  diag.selectors['[data-e2e="comment-level-1"]'] =
      document.querySelectorAll('[data-e2e="comment-level-1"]').length;

  // ⚠️ 提取口径取自交付包 douyin_automation_pkg/core.py（2026-09-09 真机修正）：
  //    抖音改版后行首可能是 '...'，直接取第一个非空行会把内容全部提成占位符。
  //    规则：优先内容元素 → 回退取「剔掉昵称/UI 词/时间/纯数字后最长的一行」。
  const SKIP = ['分享', '回复', '作者', '展开', '收起', '翻译', '举报', '赞',
    '作者赞过', '作者已赞', '...', '…'];
  const TIME_RE = /(\\d+分钟前|\\d+小时前|\\d+天前|\\d+周前|\\d+月前|\\d+年前|刚刚|今天|昨天)/;
  const ageMinutes = value => {
    const text = String(value || '');
    if (/刚刚|今天/.test(text)) return 0;
    if (/昨天/.test(text)) return 24 * 60;
    const match = /(\\d+)\\s*(分钟|小时|天|周|月|年)前/.exec(text);
    if (!match) return null;
    const n = Number(match[1]);
    const unit = { '分钟': 1, '小时': 60, '天': 1440, '周': 10080,
      '月': 43200, '年': 525600 }[match[2]];
    return Number.isFinite(n) && unit ? n * unit : null;
  };

  /**
   * 楼中楼是「comment-item 里再嵌一层 comment-item」（2026-11-18 真机实测：
   * 新版没有 comment-level-2 这个标记，也没有「回复 @昵称」文案，
   * 父子关系只能从 DOM 嵌套推）。
   * 不标深度的话，父评论和它的回复会被拍平成两条互不相干的记录，
   * 「谁回复了谁」就永远判不出来 —— watchReplies 卡在这。
   */
  const depthOf = (node) => {
    let d = 0, p = node.parentElement;
    while (p && d < 6) {
      if (p.getAttribute && p.getAttribute('data-e2e') === 'comment-item') d++;
      p = p.parentElement;
    }
    return d;
  };
  const parentItemOf = (node) => {
    let p = node.parentElement;
    while (p) {
      if (p.getAttribute && p.getAttribute('data-e2e') === 'comment-item') return p;
      p = p.parentElement;
    }
    return null;
  };

  const extract = (nodes) => {
    const out = [];
    const idxOf = new Map();
    nodes.forEach((n, i) => idxOf.set(n, i));
    for (const node of nodes.slice(0, __COMMENT_EXTRACT_CAP__)) {
      const raw = node.innerText || '';
      const lines = raw.split('\\n').map(s => s.trim()).filter(Boolean);
      const own = selector => Array.from(node.querySelectorAll(selector))
        .find(e => e.closest('[data-e2e="comment-item"]') === node);
      const info = own('.comment-item-info-wrap');
      const nameEl = own(
          '[data-e2e="comment-username-1"], a[href*="/user/"], span[class*="name"]');
      // 作者徽章会被一起读进昵称（实测「AI学长小马 作者」）。徽章要单独拎出来做
      // isAuthor 标记，昵称里必须剥掉 —— 否则后端按昵称对「谁回复了我」永远对不上。
      const rawName = ((nameEl && nameEl.innerText) || lines[0] || '')
          .replace(/\\s+/g, ' ').trim();
      const isAuthor = /(?:^|\\s)(?:作者赞过|作者已赞|作者)$/.test(rawName);
      const nickname = rawName
          .replace(/\\s*(?:作者赞过|作者已赞|作者)$/, '').trim().slice(0, 40);
      const timeMatch = raw.match(TIME_RE);
      let content = '';
      const cEl = (info && info.nextElementSibling) || own(
          '[data-e2e="comment-level-1"], [class*="comment-content"],' +
          ' [class*="CommentContent"], p[class*="content"], span[class*="content"]');
      if (cEl) content = (cEl.innerText || '').trim();
      if (!content || SKIP.includes(content)) {
        const cands = lines.slice(1).map(l => l.trim()).filter(t => {
          if (!t || SKIP.includes(t)) return false;
          if (/^\\d+$/.test(t)) return false;
          if (timeMatch && t.includes(timeMatch[0])) return false;
          if (t === nickname) return false;
          return true;
        });
        if (cands.length) content = cands.sort((a, b) => b.length - a.length)[0];
      }
      if (content && content.length >= 2) {
        // 内容元素会把 UI 行一起带出来（实测「为什么不用原声␤作者赞过」），
        // 按行剔掉 UI 词再拼，否则「作者赞过」会混进正文被当成回复内容。
        content = content.split('\\n').map(s => s.trim())
            .filter(s => s && !SKIP.includes(s)).join(' ').trim();
        if (!content || SKIP.includes(content)) continue;
        const marker = own('[id^="tooltip_"]');
        const commentId = marker && /^tooltip_(\\d{6,})$/.exec(marker.id);
        const userLink = own('a[href*="/user/"]');
        const userId = userLink && /\\/user\\/([^/?#]+)/.exec(userLink.href);
        // 只声明一次：同一块作用域里再 const 一个同名标识符会让整段注入脚本
        // SyntaxError（Identifier 'publishedText' has already been declared），
        // 表现是「评论一条都读不到，报 Script failed to execute」。
        const publishedText = String(info?.nextElementSibling?.nextElementSibling?.innerText || '').slice(0, 60);
        const age = ageMinutes(publishedText || timeMatch?.[0] || '');
        if (windowMinutes > 0 && (age == null || age > windowMinutes)) {
          diag.timeFiltered = (diag.timeFiltered || 0) + 1;
          continue;
        }
        // 父引用只在本轮读取内有效（同一批 nodes 的下标）：跨轮去重靠子项
        // 自己的 commentId，父下标只用来判「这条回复挂在谁下面」。
        const parentNode = parentItemOf(node);
        out.push({ nickname: nickname || '未知用户', text: content.slice(0, 300),
          commentId: commentId ? commentId[1] : '', userId: userId ? userId[1] : '',
          publishedText, ageMinutes: age,
          depth: depthOf(node),
          parentIdx: parentNode && idxOf.has(parentNode) ? idxOf.get(parentNode) : -1,
          parentNick: parentNode && idxOf.has(parentNode) ? String(
            ((parentNode.querySelector('[data-e2e="comment-username-1"], a[href*="/user/"]') || {})
              .innerText || '')).replace(/\\s*(?:作者赞过|作者已赞|作者)$/, '').trim().slice(0, 40) : '',
          isAuthor: !!isAuthor });
      }
    }
    return out;
  };

  let comments = extract(e2eItems);
  if (!comments.length) {
    comments = extract(fuzzyItems.length ? fuzzyItems
                     : (list ? Array.from(list.children).slice(0, 40) : []));
  }
  const nodes = e2eItems.length ? e2eItems : fuzzyItems;

  const loginWall = !!document.querySelector('#login-pannel, [data-e2e="login-guide"], .login-mask');
  const caption = (document.querySelector('[data-e2e="video-desc"], h1') || {}).innerText || document.title || '';
  diag.itemCount = nodes.length;
  diag.bodyLen = (document.body.innerText || '').length;
  return { comments, loginWall, caption: String(caption).slice(0, 60), diag, url: location.href };
})()`;

function commentExtractScript(options = {}) {
  const minutes = Number(options.commentWindowMinutes);
  const safeMinutes = Number.isFinite(minutes) ? Math.max(0, Math.min(4320, Math.floor(minutes))) : 0;
  // 滚动轮数与抽取上限跟着本次要的条数走：只取 4 条时没必要滚 10 屏，
  // 要 60 条时又必须多滚几轮（实测 37 条要滚 7 轮）。
  const limit = Math.max(4, Math.min(parseInt(options.limit, 10) || 40, 200));
  const rounds = Math.max(5, Math.min(12, Math.ceil(limit / 6) + 3));
  return COMMENT_EXTRACT_JS
    .replace('__COMMENT_WINDOW_MINUTES__', String(safeMinutes))
    .replace('__EXPAND_SUB_COMMENTS__', options.expandSubComments === true ? 'true' : 'false')
    .replace('__COMMENT_SCROLL_ROUNDS__', String(rounds))
    .replace('__COMMENT_LIMIT__', String(limit))
    .replace('__COMMENT_EXTRACT_CAP__', String(Math.min(limit, 120)));
}

/** 搜索结果页：视频 + 作者（线索采集要的是「谁在发什么」，不只是链接） */
const SEARCH_ITEMS_JS = `(async () => {
  const targetLimit = __LEADGEN_SEARCH_LIMIT__;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const linkCount = () => document.querySelectorAll('a[href*="/video/"]').length;

  // 抖音搜索页是 SPA：did-finish-load 只代表 HTML 到了，卡片还在异步渲染。
  // 这里等的是「网格稳定」，不是「出现第一条」——2026-11-19 真机实锤：
  // 任务里同一个关键词有时只解析到 3–5 条，而同一页面手工采样是 26 条，
  // 差别就在抓取时机：links>0 就开抓时首屏还在增量渲染，随后滚动又触发不了
  // 懒加载，连续几轮「没新增」就退出，于是每次拿到的都是同一小段头部结果。
  // 判据：条数连续两次采样不再增长（且已有结果）才算稳定，最多等 12s。
  let waited = 0;
  let prevLinks = -1;
  let stable = 0;
  while (waited < 12000) {
    if (/验证|captcha|安全验证/i.test(document.title || '')) break;
    const links = linkCount();
    stable = (links === prevLinks && links > 0) ? stable + 1 : 0;
    prevLinks = links;
    if (links > 0 && stable >= 2) break;
    await sleep(600);
    waited += 600;
  }

  const seen = new Set();
  const out = [];
  const clean = (t) => String(t || '').replace(/\\s+/g, ' ').trim();

  /**
   * 找到「这一个视频」的卡片容器。
   * 不能一路往上找带作者链接的节点 —— 搜索结果的外层列表也带作者链接，
   * 一路追上去会把整页文本当成每条的标题（实测 3 条标题完全相同、
   * 作者全空）。这里优先认 li / data-e2e 卡片，找不到就回到最近父节点。
   */
  const cardOf = (a) => {
    let n = a;
    for (let i = 0; i < 8 && n && n !== document.body; i++) {
      const tag = (n.tagName || '').toLowerCase();
      const e2e = (n.getAttribute && n.getAttribute('data-e2e')) || '';
      if (tag === 'li' || /scroll-list-item|video-card|search-result/i.test(e2e)) return n;
      n = n.parentElement;
    }
    return a.parentElement || a;
  };

  const grab = () => {
    document.querySelectorAll('a[href*="/video/"]').forEach(a => {
      const href = a.href || '';
      const m = /\\/video\\/(\\d{6,})/.exec(href);
      if (!m || seen.has(m[1])) return;
      seen.add(m[1]);
      const card = cardOf(a);
      const cardText = clean(card.innerText);
      const userA = card.querySelector('a[href*="/user/"]');
      let author = clean((userA && userA.innerText) || '').split('\\n')[0].slice(0, 40);
      // 搜索结果卡片不一定有作者主页链接，但文案里通常带 @昵称。
      // 线索采集要「谁在发」，拿不到就退一步从文案里取。
      if (!author) {
        const at = cardText.match(/@([^\\s#@，,。:：]{1,20})/);
        if (at) author = at[1];
      }
      const descEl = card.querySelector(
        '[data-e2e="video-desc"], [data-e2e="search-card-desc"], p');
      let title = clean((descEl && descEl.innerText) || '');
      if (!title) title = cardText.length && cardText.length <= 240 ? cardText : clean(a.innerText || '');
      // 计数兜底：搜索卡常常只有图标 + 数字，文本里没有「点赞」二字。
      // 先看 data-e2e，再看 aria-label（抖音给互动按钮加了「点赞 1.2万」），
      // 最后才用旧的文本正则。三条都落空就留空 —— 后端会去详情页补，
      // 而不是把「没读到」当成「低于阈值」直接跳过。
      const statOf = (names) => {
        for (const n of names) {
          const el = card.querySelector('[data-e2e="' + n + '"]');
          const v = el && (el.getAttribute('data-count') || el.textContent || '').trim();
          if (v && /^[0-9]+(?:\\.[0-9]+)?[万wW亿]?$/.test(v)) return v;
        }
        for (const el of card.querySelectorAll('[aria-label]')) {
          const label = el.getAttribute('aria-label') || '';
          if (!/点赞|喜欢|赞/.test(label)) continue;
          const m = label.match(/[0-9]+(?:\\.[0-9]+)?[万wW亿]?/);
          if (m) return m[0];
        }
        return '';
      };
      const likeCount = statOf(['like-count', 'video-like-count']);
      const likeM = cardText.match(/(\\d+(?:\\.\\d+)?\\s*[万wW]?)\\s*(?:点赞|赞)/);
      const adBadge = card.querySelector('[data-e2e*="ad"], [aria-label*="广告"], [class*="ad-badge"]');
      const adContext = (cardText + ' ' + String((adBadge &&
        (adBadge.innerText || adBadge.getAttribute('aria-label'))) || '')).replace(/\\s+/g, ' ');
      out.push({
        videoId: m[1],
        url: 'https://www.douyin.com/video/' + m[1],
        author,
        title: title.slice(0, 80),
        isAd: /(^|[\\s·])广告($|[\\s·])/.test(adContext),
      likeText: likeCount || (likeM ? likeM[1] : ''),
      likeCount: likeCount || '',
      commentCount: statOf(['comment-count', 'video-comment-count']),
      collectCount: statOf(['collect-count']),
      shareCount: statOf(['share-count']),
      });
    });
  };
  grab();
  const itemsFirst = out.length;
  // 找真正的滚动容器：搜索页多数情况是整页滚，但列表被包进内部滚动层时
  // window.scrollBy 完全空转（视频详情页就是这样，评论那次已经踩过）。
  // 先向上找可滚祖先，找不到再退回整页。
  const scroller = (() => {
    const first = document.querySelector('a[href*="/video/"]');
    let n = first;
    for (let i = 0; n && i < 10; i++, n = n.parentElement) {
      if (n.scrollHeight > n.clientHeight + 40) return n;
    }
    return document.scrollingElement || document.documentElement;
  })();
  const pageLevel = scroller === document.scrollingElement || scroller === document.documentElement;
  const scrollPos = () => Math.round(pageLevel ? window.scrollY : scroller.scrollTop);
  const scrollToY = (y) => {
    if (pageLevel) window.scrollTo(0, y);
    else scroller.scrollTop = y;
  };
  const stepPx = Math.max(400, Math.round(window.innerHeight * 0.9));
  let stagnant = 0;
  let stoppedBy = "limit";
  let rounds = 0;
  for (let i = 0; i < __LEADGEN_SEARCH_ROUNDS__; i++) {
    rounds = i + 1;
    if (out.length >= targetLimit) { stoppedBy = "limit"; break; }
    const before = out.length;
    const yBefore = scrollPos();
    const hBefore = document.documentElement.scrollHeight;
    scrollToY(yBefore + stepPx);
    // 部分懒加载靠 scroll/wheel 事件触发，只改 scrollTop 不冒泡就永远不加载
    try {
      scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
      window.dispatchEvent(new WheelEvent("wheel", { deltaY: stepPx, bubbles: true }));
    } catch (e) {}
    await sleep(900);
    grab();
    const grew = out.length > before || document.documentElement.scrollHeight > hBefore;
    stagnant = grew ? 0 : stagnant + 1;
    if (stagnant >= 3) { stoppedBy = scrollPos() > yBefore ? "no-more" : "scroll-stuck"; break; }
  }
  if (out.length >= targetLimit) stoppedBy = "limit";
  // 读完回顶：搜索页后续可能被复用（同一窗口继续搜别的词），别停在半截
  try { scrollToY(0); } catch (e) {}
  let loginWall = !!document.querySelector('#login-pannel, [data-e2e="login-guide"], .login-mask');
  const bodyText = document.body.innerText || '';
  // 抖音新版登录浮层不带上面那些类名/选择器（2026-11-14 真机实锤：
  // 页面文本是「登录后即可搜索更多精彩视频 / 扫码登录」，选择器全部落空，
  // 任务于是「正常完成」但 0 条）。文本特征兜底：出现登录引导话术且一条视频都没解析到。
  if (!loginWall && out.length === 0) {
    loginWall = /登录后即可搜索|扫码登录\\s*如何扫码|验证码登录\\s*密码登录/.test(bodyText.replace(/\\n+/g, ''));
  }
  const emptyHint = /暂无搜索结果|没找到|无结果/.test(bodyText);
  const verifyHint = /验证|captcha|安全验证/i.test(document.title || '');
  return {
    items: out, loginWall, emptyHint, url: location.href,
    // 诊断：解析到 0 条时靠这几项区分「还在加载 / 被拦 / 真空结果」
    diag: {
      waitedMs: waited,
      videoLinks: linkCount(),
      bodyLen: bodyText.length,
      title: document.title,
      verifyHint,
      loginWall,
      vis: document.visibilityState,
      itemsFirst,
      itemsPeak: out.length,
      rounds,
      stoppedBy,
      scrollY: scrollPos(),
      scrollHeight: document.documentElement.scrollHeight,
      scroller: pageLevel ? "page" : String(scroller.className || scroller.tagName || "").slice(0, 60),
      sample: bodyText.slice(0, 120),
    },
  };
})()`;

/** 作者主页：最近作品链接（监听主播 / 采集账号作品用） */
const AUTHOR_WORKS_JS = `(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const filterPinned = __FILTER_PINNED_AUTHOR_WORKS__;
  const seen = new Set();
  const out = [];
  let pinnedSkipped = 0;
  const grab = () => {
    document.querySelectorAll('a[href*="/video/"]').forEach(a => {
      const m = /\\/video\\/(\\d{6,})/.exec(a.href || '');
      if (!m || seen.has(m[1])) return;
      seen.add(m[1]);
      const wrap = a.closest('li') || a.parentElement || a;
      const text = String(wrap.innerText || '').replace(/\\s+/g, ' ').trim();
      const pinned = /置顶/.test(text);
      if (filterPinned && pinned) { pinnedSkipped++; return; }
      out.push({
        videoId: m[1],
        url: 'https://www.douyin.com/video/' + m[1],
        title: text.slice(0, 80),
        pinned,
      });
    });
  };
  grab();
  for (let i = 0; i < 4; i++) {
    window.scrollBy(0, window.innerHeight);
    await sleep(900);
    grab();
    if (out.length >= 24) break;
  }
  const loginWall = !!document.querySelector('#login-pannel, [data-e2e="login-guide"], .login-mask');
  const nameEl = document.querySelector('[data-e2e="user-detail"] h1, h1');
  return { items: out, loginWall, pinnedSkipped, author: nameEl ? String(nameEl.innerText || '').trim() : '', url: location.href };
})()`;

function authorWorksScript(options = {}) {
  return AUTHOR_WORKS_JS.replace('__FILTER_PINNED_AUTHOR_WORKS__',
    options.filterPinnedAuthorWorks === true ? 'true' : 'false');
}

// The platform may restart a paused video asynchronously. Keep stopped workers
// paused until another warmup begins or navigation replaces this document.
const WARMUP_STOP_JS = `(() => {
  const commandId = __COMMAND_ID__;
  if (window.__warmupCommandId !== commandId) return false;
  if (window.__warmupReleasePause) window.__warmupReleasePause();
  window.__warmupStop = true;
  const media = window.HTMLMediaElement && window.HTMLMediaElement.prototype;
  const originalPlay = media && media.play;
  const held = () => window.__warmupCommandId === commandId && window.__warmupStop;
  const guardedPlay = function (...args) {
    if (held()) { this.pause(); return Promise.resolve(); }
    return originalPlay.apply(this, args);
  };
  const release = () => {
    document.removeEventListener('play', guard, true);
    if (media && media.play === guardedPlay) media.play = originalPlay;
    if (window.__warmupReleasePause === release) delete window.__warmupReleasePause;
  };
  const guard = event => {
    if (!held()) { release(); return; }
    if (event.target && event.target.tagName === 'VIDEO') event.target.pause();
  };
  if (media && typeof originalPlay === 'function') media.play = guardedPlay;
  window.__warmupReleasePause = release;
  document.addEventListener('play', guard, true);
  document.querySelectorAll('video').forEach(v => v.pause());
  return true;
})()`;

/**
 * 视频入口（信息流）：关注 / 推荐两条流是「一屏一条」的竖向轮播，
 * 页面里只有当前这一屏带 data-e2e-vid，拿不到列表。所以只能像真人一样
 * 一屏一屏翻（点右侧下一条箭头），每屏记一遍 id + 标题 + 作者。
 * 只读：不点赞、不关注、不评论。
 */
const FEED_LIST_JS = `(async () => {
  try {
    const limit = __LIMIT__;
    const dwell = __DWELL__;
    // 内部截止时间：推荐流的翻页要等视频起播才换 vid，偶发一次卡十几秒。
    // 以前只有外层 evaluate 的 120s 硬超时 —— 一超时整段脚本作废，返回 0 条，
    // 任务直接被判「读取视频入口失败 / 启动不了」。现在到点就收工，已有条目照常带回。
    const budget = __BUDGET__;
    const t0 = Date.now();
    const over = () => Date.now() - t0 > budget;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const visible = el => {
      if (!el) return false;
      const r = el.getBoundingClientRect();
      const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
      return r.width >= Math.min((window.innerWidth || 1280) * 0.22, 180) &&
        r.height >= Math.min((window.innerHeight || 900) * 0.42, 240) &&
        r.right > 0 && r.left < (window.innerWidth || 1280) &&
        r.bottom > (window.innerHeight || 900) * 0.12 && r.top < (window.innerHeight || 900) * 0.88 &&
        (!style || (style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0.05));
    };
    const idOf = el => {
      if (!el || !el.getAttribute) return '';
      for (const name of ['data-e2e-vid','data-video-id','data-aweme-id','aweme-id','data-id']) {
        const value = String(el.getAttribute(name) || '');
        if (/^\\d{6,}$/.test(value)) return value;
      }
      const cls = String(el.className && (el.className.baseVal || el.className) || '');
      const match = /video[_-](\\d{6,})/.exec(cls);
      return match ? match[1] : '';
    };
    const activeInfo = () => {
      const marked = document.querySelector('[data-e2e="feed-active-video"]');
      if (marked) {
        let root = marked, id = '';
        for (let el = marked, depth = 0; el && depth < 10; el = el.parentElement, depth++) {
          id = idOf(el) || id;
          if (!id && el.querySelectorAll) {
            const link = Array.from(el.querySelectorAll('a[href*="/video/"]'))
              .map(a => /\\/video\\/(\\d{6,})/.exec(String(a.href || a.getAttribute('href') || '')))
              .find(Boolean);
            if (link) id = link[1];
          }
          if (id) { root = el; break; }
        }
        return { root: marked, id };
      }
      const vw = window.innerWidth || 1280, vh = window.innerHeight || 900;
      const players = Array.from(document.querySelectorAll('video')).filter(visible)
        .sort((a, b) => (Number(!a.paused && a.readyState >= 2) - Number(!b.paused && b.readyState >= 2)) ||
          (b.getBoundingClientRect().width * b.getBoundingClientRect().height -
           a.getBoundingClientRect().width * a.getBoundingClientRect().height));
      const video = players[0];
      if (!video) return null;
      let root = video, id = '';
      for (let el = video, depth = 0; el && depth < 12; el = el.parentElement, depth++) {
        const candidate = idOf(el);
        if (candidate) { root = el; id = candidate; break; }
        if (el.querySelectorAll) {
          const link = Array.from(el.querySelectorAll('a[href*="/video/"]'))
            .map(a => /\\/video\\/(\\d{6,})/.exec(String(a.href || a.getAttribute('href') || '')))
            .find(Boolean);
          if (link) { root = el; id = link[1]; break; }
        }
        root = el;
      }
      const pathId = /\\/video\\/(\\d{6,})/.exec(location.pathname || '');
      id = id || (pathId && pathId[1]) || '';
      return { root, id, video, visiblePlayers: players.length, viewport: { width: vw, height: vh } };
    };
    const active = () => activeInfo()?.root || null;
    const arrow = () => document.querySelector('[data-e2e="video-switch-next-arrow"]');
    // 首页先渲染导航再异步挂视频流，直接找卡片永远是 0 条。
    // 后台窗口偶发渲染慢，等到 30 秒再判「没有流」。
    for (let i = 0; i < 60 && !activeInfo(); i++) await sleep(500);
    if (!activeInfo()) {
      // 兜底：点顶部「推荐」把页面切回推荐流（首页有时落在精选网格页，
      // 那里根本没有 feed-active-video，只能靠导航切回来）。
      const nav = Array.from(document.querySelectorAll('a,div,span'))
        .find(e => /^推荐$/.test(String(e.textContent || '').trim()) && e.offsetParent);
      if (nav) { try { nav.click(); } catch (e) { /* 忽略 */ } await sleep(4000); }
    }
    if (!activeInfo()) {
      return { items: [], error: '未进入视频信息流（可能是登录墙或空推荐）',
               diag: { feed: 0, bodyLen: (document.body.innerText || '').length,
                       href: location.href,
                       activeMarker: !!document.querySelector('[data-e2e="feed-active-video"]'),
                       visiblePlayers: Array.from(document.querySelectorAll('video')).filter(visible).length,
                       loginWall: /扫码登录|登录后查看|请先登录/.test((document.body.innerText || '').slice(0, 3000)) } };
    }
    if (document.querySelector('#captcha_container,[data-e2e="verify-dialog"]')) {
      return { items: [], verifyHint: true, error: '被安全验证页拦住' };
    }
    const items = [], seen = {};
    let stuck = 0;
    for (let round = 0; round < limit * 4 && items.length < limit; round++) {
      if (over()) break;
      const current = activeInfo();
      const box = current && current.root;
      if (!box) break;
      const vid = String(current.id || idOf(box) || '');
      if (!/^\\d{6,}$/.test(vid)) {
        const diag = { activeMarker: !!document.querySelector('[data-e2e="feed-active-video"]'),
          visiblePlayers: current.visiblePlayers || 1, href: location.href,
          bodyLen: (document.body.innerText || '').length };
        if (!items.length) return { items: [], error: '识别到可见视频，但页面没有提供可核对的视频编号', diag };
        break;
      }
      if (vid && !seen[vid]) {
        seen[vid] = 1;
        // 标题优先取 video-desc（干净正文）；退回到 video-info 时前面会带
        // 「@作者 · 6天前」，下面按这个模式剥掉。
        const desc = box.querySelector('[data-e2e="video-desc"]');
        const info = box.querySelector('[data-e2e="video-info"]');
        let title = desc ? (desc.innerText || '') : (info ? (info.innerText || '') : '');
        title = String(title).replace(/\\s+/g, ' ').trim();
        const cut = title.indexOf('合集：');
        if (cut > 0) title = title.slice(0, cut).trim();
        if (!desc) title = title.replace(/^@[^·]{1,24}·\\s*\\S*?前\\s*/, '').trim();
        const links = Array.from(box.querySelectorAll('a[href*="/user/"]'));
        const who = links.find(a => (a.innerText || '').trim()) || links[0] || null;
        const href = who ? String(who.getAttribute('href') || '') : '';
        // 头像链接里往往只有 <img>，昵称读不到。抖音把「@昵称 · 时间」放在
        // video-info 首行，退一步从这里抠昵称 —— 排除作者要靠它才生效。
        let author = who ? (who.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40) : '';
        if (!author && info) {
          const head = String(info.innerText || '').replace(/\\s+/g, ' ').trim();
          const m = /^@([^\\s·]{1,40})/.exec(head);
          if (m) author = m[1];
        }
        author = String(author || '').replace(/^@+/, '').trim();
        const when = String(box.innerText || '').match(/(\\d{4}年\\d{1,2}月\\d{1,2}日|\\d{1,2}月\\d{1,2}日|\\d+\\s*(?:分钟|小时|天|周|个月|年)前|刚刚)/)
          || String(box.innerText || '').match(/(\\d{4}-\\d{1,2}-\\d{1,2})/)
          || String(box.innerText || '').match(/(\\d{1,2}-\\d{1,2})/);
        const adMarker = box.querySelector('[data-e2e*="ad"], [aria-label*="广告"], [class*="ad-badge"]');
        const adContext = (String((info && info.innerText) || '') + ' ' +
          String((adMarker && (adMarker.innerText || adMarker.getAttribute('aria-label'))) || '') + ' ' +
          String(box.innerText || '')).replace(/\\s+/g, ' ');
        items.push({
          videoId: vid,
          url: 'https://www.douyin.com/video/' + vid,
          title: title.slice(0, 200),
          author,
          authorUrl: href.indexOf('//') === 0 ? 'https:' + href : href,
          publishedText: when ? when[0] : '',
          isAd: /(^|[\\s·])广告($|[\\s·])/.test(adContext),
        });
        stuck = 0;
      } else {
        // 翻页偶尔不生效（视频还在起播）：同一屏连着 3 次没换就收工，别空转
        stuck += 1;
        if (stuck >= 3) break;
      }
      // 达到本次读取数量就停在当前视频。尤其 limit=1 的单遍流程，
      // 不能先切到下一条再把刚读到的这条重新打开。
      if (items.length >= limit) break;
      const n = arrow();
      if (!n) break;
      // 翻页不是「点一下就到」：视频要起播才换 data-e2e-vid，直接固定等待
      // 经常读到同一条（推荐流尤其慢）。这里轮询到 vid 真的变了为止。
      try { n.click(); } catch (e) { break; }
      await sleep(dwell);
      let moved = false;
      for (let t = 0; t < 8; t++) {
        if (over()) break;
        const now = activeInfo();
        if (now && now.id && String(now.id) !== vid) { moved = true; break; }
        if (t === 3) { try { arrow() && arrow().click(); } catch (e) { /* 忽略 */ } }
        await sleep(700);
      }
      if (!moved) {
        stuck += 1;
        if (stuck >= 2) break;
      }
    }
    return { items, truncated: over() && items.length < limit,
             diag: { items: items.length, budgetMs: budget, elapsedMs: Date.now() - t0,
             activeMarker: !!document.querySelector('[data-e2e="feed-active-video"]'),
             visiblePlayers: Array.from(document.querySelectorAll('video')).filter(visible).length,
             cards: document.querySelectorAll('[data-e2e="feed-item"]').length } };
  } catch (e) { return { items: [], error: String((e && e.message) || e) }; }
})()`;

/**
 * 视频入口（网格页）：喜欢列表 / 话题页是瀑布流卡片，卡片本身就是
 * /video/ 链接，滚动加载即可。卡片正面写的是「点赞数 + 标题」，
 * 所以要把开头那个数字去掉，否则标题会变成「412.8万 下个路口…」。
 */
const GRID_LIST_JS = `(async () => {
  try {
    const limit = __LIMIT__;
    const boxSel = __BOX__;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    let box = null;
    for (let i = 0; i < 20 && !box && boxSel; i++) {
      box = document.querySelector(boxSel);
      if (!box) await sleep(500);
    }
    const root = box || document;
    const items = [], seen = {};
    for (let s = 0; s < 10 && items.length < limit; s++) {
      const links = Array.from(root.querySelectorAll('a[href*="/video/"]'));
      for (const a of links) {
        if (items.length >= limit) break;
        // 页脚「相关推荐」也是 /video/ 链接，混进来会把话题页读成一堆无关视频
        if (a.closest('[data-e2e="page-footer"]')) continue;
        const href = String(a.getAttribute('href') || '');
        const m = /\\/video\\/(\\d{6,})/.exec(href);
        if (!m || seen[m[1]]) continue;
        seen[m[1]] = 1;
        const card = a.closest('li') || a.parentElement || a;
        let title = String((card.innerText || a.innerText || '')).replace(/\\s+/g, ' ').trim();
        title = title.replace(/^[\\d.]+\\s*[万wW]?\\s+/, '').trim();
        items.push({ videoId: m[1], url: 'https://www.douyin.com/video/' + m[1],
                     title: title.slice(0, 200), author: '', authorUrl: '', publishedText: '' });
      }
      if (items.length >= limit) break;
      window.scrollBy(0, 1200);
      await sleep(900);
    }
    return { items, diag: { items: items.length,
             cards: root.querySelectorAll('a[href*="/video/"]').length, boxFound: !!box } };
  } catch (e) { return { items: [], error: String((e && e.message) || e) }; }
})()`;

/** 推荐页：真实浏览（滚动 + 停留），可选点赞；只做被允许的次数，不多点一次 */
const WARMUP_JS = `(async () => {
  // 整段包一层 try：页面里任何一次 DOM 访问抛错，Electron 只会回
  // 「Script failed to execute」，既没有行号也没有原因，只能盲猜。
  try {
  const commandId = window.__warmupCommandId;
  const stopped = () => window.__warmupStop || window.__warmupCommandId !== commandId;
  const checkStopped = () => {
    if (stopped()) throw Object.assign(new Error('已手动停止'), { stopped: true });
  };
  const sleep = async (ms) => {
    const until = Date.now() + ms;
    while (Date.now() < until) {
      checkStopped();
      await new Promise(r => setTimeout(r, Math.min(100, until - Date.now())));
    }
    checkStopped();
  };
  const scrolls = Math.max(1, Math.min(__SCROLLS__, 20));
  const dwell = Math.max(400, Math.min(__DWELL__, 30000));
  const deadlineMs = __DEADLINE__;
  const likeMax = Math.max(0, Math.min(__LIKE_MAX__, 5));
  // 页面「养号」设置的是点赞概率（%）。没给概率时退回旧的「每轮上限」语义：
  // likeMax=0 表示不点赞；给了概率则每屏掷一次骰，上限默认等于屏数。
  const likeProb = Math.max(0, Math.min(__LIKE_PROB__, 100));
  const likeCap = likeProb > 0 ? (likeMax > 0 ? likeMax : scrolls) : likeMax;

  // 抖音首页会先渲染导航、再异步挂视频流；精选页甚至没有视频卡片。
  // 不等就直接找点赞按钮，结论永远是「没找到」—— 这里先等视频出现，最多 10s。
  const hasVideo = () => !!document.querySelector('[data-e2e="feed-active-video"], [data-e2e="feed-live"]');
  for (let w = 0; w < 20 && !hasVideo(); w++) await sleep(500);
  if (!hasVideo()) {
    // 兜底：点顶部「推荐」把页面切回推荐流。
    // 只认「推荐」——以前 /^(推荐|精选)$/ 会命中「精选」标签，把页面从推荐流
    // 切到精选页，那里根本没有点赞控件，于是每轮都是「无点赞按钮」。
    const nav = Array.from(document.querySelectorAll('a,div,span'))
      .find(e => /^推荐$/.test(String(e.textContent || '').trim()) && e.offsetParent);
    checkStopped();
    if (nav) { try { nav.click(); } catch (e) {} await sleep(3500); }
  }

  const acts = [];
  const watchedIds = new Set();
  let liked = 0;
  let needDetail = 0; // 骰子中了要点赞，但信息流上没有控件 → 交给详情页
  let unconfirmed = 0; // 连续多少屏没确认视频在播（单屏跳过，连续 3 屏才判失败）
  for (let i = 1; i <= scrolls; i++) {
    checkStopped();
    if (deadlineMs && Date.now() + dwell + 600 > deadlineMs) break;
    const active = document.querySelector('[data-e2e="feed-active-video"]');
    const next = document.querySelector('[data-e2e="video-switch-next-arrow"]');
    if (!active) {
      if (document.querySelector('[data-e2e="feed-live"]') && next) {
        next.click(); await sleep(800); continue;
      }
      return {actions:acts,liked:0,error:'未进入可验证的视频推荐流，不把网格页滚动计入浏览'};
    }
    const videoId = active.getAttribute('data-e2e-vid');
    const player = active.querySelector('video');
    if (!videoId || !player || !next) return {actions:acts,liked:0,error:'推荐流视频或切换控件未核实'};
    window.__warmupProgress = { phase: 'watching', videoId, startedAt: Date.now() };
    if (!window.__warmupFirstVideoAt) window.__warmupFirstVideoAt = Date.now();
    if (player.paused) { try { await player.play(); } catch (_) {} }
    const playbackStart = player.currentTime;
    await sleep(dwell);
    const nowActive = document.querySelector('[data-e2e="feed-active-video"]');
    const sameVideo = nowActive && nowActive.getAttribute('data-e2e-vid') === videoId;
    const played = player.readyState >= 2 && !player.paused &&
      (player.currentTime > playbackStart + 0.1 || player.currentTime < playbackStart);
    if (!sameVideo || !played || watchedIds.has(videoId)) {
      // 一屏没确认播放就整轮失败太重：后台窗口偶发自动播放被拒、视频刚好播完，
      // 都会让 2 分钟的养号在第 4 屏就中断（真机连续两次撞上）。
      // 这里跳过这一屏、多等一拍再切下一条；连续 3 屏都确认不了才判本轮失败。
      // 期间一屏都不计入浏览数，不伪造数据。
      unconfirmed += 1;
      if (unconfirmed >= 3) {
        return {actions:acts,liked:0,error:'连续 3 屏都没确认视频在播放，停止本轮'};
      }
      try { next.click(); } catch (e) { /* 忽略 */ }
      await sleep(900);
      continue;
    }
    watchedIds.add(videoId);
    let likeNote = '';
    if (likeProb > 0) {
      // 先掷骰子再判断有没有控件：以前先判 likeBtn，控件一缺就整轮 6 屏全是
      // 「无点赞按钮」，概率到底有没有生效根本看不出来（用户对着日志对数对不上）。
      if (liked + needDetail >= likeCap) {
        likeNote = 'cap-reached';
      } else if (Math.random() * 100 >= likeProb) {
        likeNote = 'skip-by-probability';
      } else {
        // 信息流的脚本 click 不可信，也不能据其 DOM 变化计成功。
        // 真正的点赞只在详情页用 sendInputEvent，并核对回执或已赞状态。
        likeNote = 'need-detail';
        needDetail += 1;
      }
    } else if (liked + needDetail < likeCap) {
      // 兼容旧配置：仍要进入详情页验证，不能 click 后直接加计数。
      likeNote = 'need-detail';
      needDetail += 1;
    }
    const title = (active.querySelector('[data-e2e="video-desc"]') || {}).innerText || '';
    acts.push({ round: i, videoId, dwellMs: dwell, like: likeNote, caption: String(title).slice(0, 50) });
    if (deadlineMs && Date.now() + 600 > deadlineMs) break;
    checkStopped();
    next.click();
    let changed = false;
    for (let wait = 0; wait < 12; wait++) {
      await sleep(200);
      const current = document.querySelector('[data-e2e="feed-active-video"]');
      if ((current && current.getAttribute('data-e2e-vid') !== videoId) ||
          document.querySelector('[data-e2e="feed-live"]')) { changed = true; break; }
      if (deadlineMs && Date.now() >= deadlineMs) break;
    }
    if (!changed) return {actions:acts,liked:0,error:'切换后未检测到新视频，停止浏览'};
  }
  const loginWall = !!document.querySelector('#login-pannel, [data-e2e="login-guide"], .login-mask');
  // 这轮有没有「该点赞但信息流上摸不到控件」的屏：交给详情页去点。
  // 精选页/推荐流用的都是混淆类名，唯一稳定能点的位置是视频详情页。
  const videoLinks = Array.from(new Set(
    Array.from(document.querySelectorAll('a[href*="/video/"]'))
      .map(e => String(e.href || ''))
      // 注意：这里是模板字符串，正则里的反斜杠会被吃掉一层（\\d 才等于 \d），
      // 所以这里用 indexOf 判断，别再写 /\/video\/\d{6,}/ —— 那会变成语法错误，
      // Electron 只回一句 "Script failed to execute"，白查半小时。
      .filter(u => u.indexOf('/video/') >= 0)
  )).slice(0, 8);
  // 点赞按钮找不到时，把页面上真实存在的 data-e2e 带回去：
  // 否则后端只能写"没找到按钮"，没法判断是选择器过期还是页面类型不对。
  let probe = null;
  if ((likeProb > 0 || likeMax > 0) && liked === 0 && needDetail > 0) {
    const names = Array.from(document.querySelectorAll('[data-e2e]'))
      .map(e => String(e.getAttribute('data-e2e') || '')).filter(Boolean);
    // 把页面上所有像按钮的元素摊出来：抖音精选页的点赞控件没有 data-e2e，
    // 只看 data-e2e 会一直得出"没有按钮"的错误结论。
    const buttons = Array.from(document.querySelectorAll(
      'button, [role="button"], [class*="like"], [class*="Like"], [class*="praise"], [class*="digg"]'))
      .slice(0, 24)
      .map(e => ({
        tag: e.tagName,
        cls: String(e.className || '').slice(0, 56),
        aria: String(e.getAttribute('aria-label') || '').slice(0, 16),
        title: String(e.getAttribute('title') || '').slice(0, 16),
        text: String(e.innerText || '').trim().slice(0, 10),
      }));
    probe = {
      url: location.href,
      title: String(document.title || "").slice(0, 60),
      videoCount: document.querySelectorAll('video').length,
      e2e: Array.from(new Set(names)).slice(0, 30),
      buttons,
      bodyLen: (document.body ? document.body.innerText.length : 0),
      // 精选页/推荐流是单页应用：卡片未必是 <a href="/video/...">，
      // 所以要把「所有带视频地址的锚点」和「页面自己的 data-e2e 名」摊出来，
      // 才能判断该点哪儿进详情页，而不是一直卡在「没解析到视频链接」。
      anchors: Array.from(document.querySelectorAll('a'))
        .map(a => String(a.getAttribute('href') || ''))
        .filter(h => h.indexOf('/video') >= 0 || h.indexOf('modal_id') >= 0)
        .slice(0, 8),
      e2eAll: Array.from(new Set(names)).slice(0, 60),
      // 卡片结构样本：精选页没有 /video/ 锚点，只能靠这段 HTML 判断
      // 「点击该落在哪个节点上」，否则点一次没反应就只能干瞪眼。
      cardHtml: (() => {
        const v = Array.from(document.querySelectorAll('video')).find(x => x.offsetParent);
        if (!v) return '';
        let p = v;
        for (let k = 0; k < 4 && p.parentElement; k++) p = p.parentElement;
        return String(p.outerHTML || '').slice(0, 700);
      })(),
    };
  }
  return { actions: acts, liked, needDetail, videoLinks, loginWall, probe,
           url: location.href };
  } catch (e) {
    return { actions: [], liked: 0, error: String((e && e.message) || e).slice(0, 300),
             stopped: !!e.stopped,
             stack: String((e && e.stack) || "").slice(0, 400), url: location.href };
  }
})()`;

/**
 * 点赞探测（养号用）：只读，返回按钮位置 + 当前签名，真正的点击由 Node 侧发。
 *
 * 为什么点击不能写在页面脚本里：抖音的点赞处理器会忽略脚本合成的 click
 * （isTrusted=false），点了 class/计数都不变 —— 日志里就是 click-no-effect，
 * 用户以为「点赞没生效」。真机验证过：只有 webContents.sendInputEvent 这种
 * 走 Chromium 输入管道的鼠标事件才算真实点击。
 *
 * 选择器：video-player-digg 是详情页真正的点赞按钮（真机取到的 data-e2e），
 * 比 class*="likeIcon" 稳得多。
 */
const LIKE_PROBE_JS = `(() => {
  const SEL = '[data-e2e="video-player-digg"], [data-e2e="video-like-icon"], ' +
              '[data-e2e="like-icon"], [class*="likeIcon"], [aria-label="点赞"], [title="点赞"]';
  const vw = window.innerWidth, vh = window.innerHeight;
  // 挑按钮的判据不是「在不在 innerWidth 内」，而是**点得到吗**。
  // 详情浮层打开后，页面底层与横向预加载的卡片里也挂着点赞控件，querySelector
  // 取到的第一个可能是屏幕外那个（真机实测 rect 是 2011,672，而窗口只有 1280；
  // 隐藏窗口的 innerWidth 在 Electron 里并不可靠，所以用命中测试兜底）。
  // 对屏幕外坐标 elementFromPoint 返回 null —— 这类候选一律排除。
  const cands = Array.from(document.querySelectorAll(SEL)).map((el) => {
    const r = el.getBoundingClientRect();
    const cx = Math.round(r.left + r.width / 2);
    const cy = Math.round(r.top + r.height / 2);
    let hit = null;
    try { hit = document.elementFromPoint(cx, cy); } catch (e) { hit = null; }
    const reachable = !!hit && (hit === el || el.contains(hit) || hit.contains(el));
    return { el, r, cx, cy, hit, reachable,
             sized: r.width >= 4 && r.height >= 4 };
  });
  // 优先「点得到且尺寸正常」的；其次「在视口内」的。
  // 不能退到视口外的候选：推荐流是纵向堆叠的，屏幕外的按钮 rect y 会到 1300+，
  // 点过去等于点在窗口外——日志显示「点了」但平台什么都没发生（真机踩过）。
  const inView = (c) => c.cy >= 0 && c.cy <= vh && c.cx >= 0 && c.cx <= vw;
  const pick = cands.find((c) => c.sized && c.reachable)
            || cands.find((c) => c.sized && inView(c));
  if (!pick) {
    const e2e = Array.from(new Set(Array.from(document.querySelectorAll('[data-e2e]'))
      .map(e => String(e.getAttribute('data-e2e') || '')).filter(Boolean))).slice(0, 20);
    return { found: false, url: location.href, e2e,
             count: cands.length,
             rects: cands.map((c) => [Math.round(c.r.left), Math.round(c.r.top),
                                      Math.round(c.r.width), Math.round(c.r.height)]).slice(0, 4),
             viewport: [vw, vh] };
  }
  const btn = pick.el;
  const r = pick.r;
  const x = pick.cx, y = pick.cy;
  const hit = pick.hit;
  const inside = pick.reachable;
  let p = btn;
  for (let k = 0; k < 3 && p.parentElement; k++) p = p.parentElement;
  // 点前识别是否已赞，避免把第二次点击（取消赞）误记为成功。
  // 注入脚本不用正则字面量，避免模板字符串求值时发生转义问题。
  const ownClass = String(btn.className || '').toLowerCase().split(' ');
  const parentClass = String((btn.parentElement || {}).className || '').toLowerCase().split(' ');
  const state = String(btn.getAttribute('data-state') || '').toLowerCase();
  const pressed = String(btn.getAttribute('aria-pressed') || '').toLowerCase();
  const svg = btn.querySelector('svg, path');
  let fill = '';
  try {
    fill = String((svg && (svg.getAttribute('fill') || getComputedStyle(svg).fill)) || '').toLowerCase();
  } catch (e) { /* 忽略 */ }
  const activeClass = ownClass.concat(parentClass).some((v) =>
    v === 'active' || v === 'liked' || v === 'selected' || v.indexOf('is-liked') >= 0);
  const activeFill = fill.indexOf('#fe2c55') >= 0 || fill.indexOf('254, 44, 85') >= 0;
  const already = pressed === 'true' || state === 'active' || state === 'liked'
               || state === 'selected' || activeClass || activeFill;
  return {
    found: true,
    x, y,
    sig: String(btn.className || '') + '|' + String(p.className || '') + '|' + String(p.innerText || ''),
    already,
    state: { pressed, state, fill: fill.slice(0, 40), activeClass },
    hitOk: inside,
    hitCls: hit ? String(hit.tagName + '.' + String(hit.className || '').slice(0, 60)) : '',
    html: String(btn.outerHTML || '').slice(0, 160),
    rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    count: cands.length,
    viewport: [vw, vh],
    title: String(document.title || '').slice(0, 40),
    url: location.href,
  };
})()`;

/* ------------------------------------------------------- 点赞接口监听（真实性判据） */
/**
 * 挂 fetch / XHR 监听，把点赞接口（/aweme/v1/web/commit/item/digg/）的**回执**
 * 记到 window.__diggLog 里。
 *
 * 为什么不靠 DOM 判断：真机上点完赞，按钮的 class/计数文本经常不变（样式走 svg
 * fill、计数在别的兄弟节点），只看 DOM 就会把「真的点上了」误判成
 * click-no-effect —— 用户看到的就是「点赞概率 100% 却 0 赞」。接口回执
 * status_code=0 才是平台认账的硬证据。
 *
 * 注意：模板字符串会吃掉 `\d`、`\/` 这类转义，这里的匹配全用 indexOf，
 * 不写正则字面量（2026-11-14 踩过：`/\/video\/\d{6,}/` 注入后直接语法错）。
 */
// 点击事件探针：只统计「页面到底收没收到我的鼠标事件」。
// 「点了没反应」有两种完全不同的原因——事件没进页面（窗口/坐标问题），
// 或事件进去了但平台不认（遮挡/非可信/状态不对）。没有这组计数就只能猜。
const INPUT_SPY_JS = `(() => {
  const blank = () => ({ wd: 0, wu: 0, wc: 0, dd: 0, du: 0, dc: 0, last: '', vis: '',
                         paused: null, hasVideo: false, login: false });
  // 顺手采样播放器状态：vis=hidden 时视频会被暂停，点赞处理器可能直接短路，
  // 只有把「事件进没进页面」和「播放器是否在播」分开看，才知道该修哪一头。
  const sample = (st) => {
    const v = document.querySelector('video');
    st.hasVideo = !!v;
    st.paused = v ? !!v.paused : null;
    st.login = !!document.querySelector('[data-e2e="login-guide"], .login-mask, [class*="login-guide"]');
    st.vis = String(document.visibilityState || '');
    return st;
  };
  if (window.__likeInput) {
    const st = window.__likeInput;
    st.wd = st.wu = st.wc = st.dd = st.du = st.dc = 0; st.last = '';
    sample(st);
    return true;
  }
  const st = blank();
  st.vis = String(document.visibilityState || '');
  window.__likeInput = st;
  // 同时挂 window 与 document 两级捕获：如果 window 有、document 没有，
  // 说明页面在 window 捕获阶段就 stopPropagation 了（抖音有反自动化处理），
  // 那不是坐标问题，得换触发方式。
  const note = (scope, kind) => (e) => {
    st[scope + kind] += 1;
    const t = e.target || {};
    st.last = scope + kind + ':' + String(t.tagName || '') + '.' + String(t.className || '').slice(0, 40);
  };
  for (const k of ['d', 'u', 'c']) {
    const ev = k === 'd' ? 'pointerdown' : (k === 'u' ? 'pointerup' : 'click');
    window.addEventListener(ev, note('w', k), true);
    document.addEventListener(ev, note('d', k), true);
  }
  return true;
})()`;

const DIGG_HOOK_JS = `(() => {
  if (window.__diggHook) return { ok: true, already: true };
  window.__diggLog = [];
  const push = (url, status, body) => {
    try {
      window.__diggLog.push({ url: String(url).slice(0, 160), status: Number(status) || 0,
                              body: String(body || '').slice(0, 400), ts: Date.now() });
      if (window.__diggLog.length > 20) window.__diggLog.shift();
    } catch (e) { /* 忽略 */ }
  };
  const isDigg = (u) => {
    const s = String(u || '');
    return s.indexOf('/digg') >= 0 || s.indexOf('digg/') >= 0
           || s.indexOf('like/write') >= 0 || s.indexOf('commit/item/digg') >= 0;
  };
  try {
    const origFetch = window.fetch;
    window.fetch = function (input, init) {
      const url = (typeof input === 'string') ? input : ((input && input.url) || '');
      const p = origFetch.apply(this, arguments);
      if (isDigg(url)) {
        p.then((res) => {
          try {
            res.clone().text().then((t) => push(url, res.status, t)).catch(() => {});
          } catch (e) { /* 忽略 */ }
        }).catch(() => push(url, -1, 'fetch-error'));
      }
      return p;
    };
  } catch (e) { /* 页面锁了 fetch 也不致命，还有 XHR */ }
  try {
    const oOpen = XMLHttpRequest.prototype.open;
    const oSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) {
      this.__diggUrl = u;
      return oOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      if (isDigg(this.__diggUrl)) {
        this.addEventListener('load', () => push(this.__diggUrl, this.status, this.responseText));
      }
      return oSend.apply(this, arguments);
    };
  } catch (e) { /* 忽略 */ }
  window.__diggHook = true;
  return { ok: true, already: false };
})()`;

/* ------------------------------------------------------------------ 视图管理 */
/**
 * 采集用的窗口。
 *
 * 账号只持有一个原生 WebContentsView，登录、大屏与执行器共用 webContents。
 * 大屏收起后挂回常驻隐藏窗口；始终有宿主，避免未挂载视图停止渲染。
 *
 * 多账号：每个账号一个 partition（cookie/登录态各存各的），一个账号一个常驻窗口。
 * 单账号场景走默认 session —— 现有登录态就在那里，不能平白让用户重扫一次。
 */
function createWorkerWindow(accountId) {
  // resolvePartition（而非 partitionOf）：存量账号的登录态在默认 session，
  // 直接按 id 开新分区会把人「隔」下线 —— 登录墙 0 条（2026-11-14 真机实锤）。
  const partition = resolvePartition(accountId);
  const webPreferences = {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    backgroundThrottling: false, // 后台窗口不节流定时器/动画
    spellcheck: false,
  };
  if (partition) webPreferences.partition = partition; // 不设 = 默认 session
  const win = createWorkerSurface({BrowserWindow, WebContentsView, View, webPreferences});
  // 页面里抛的错只会反映成 "Script failed to execute"，真实原因在渲染进程控制台。
  win.webContents.on("console-message", (_e, level, message, line, sourceId) => {
    const lv = Number(level);
    if (lv >= 2 || /error|uncaught/i.test(String(message))) {
      trace("页面控制台:", String(message).slice(0, 300),
            `line=${line}`, String(sourceId || "").slice(0, 90));
    }
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    trace("渲染进程退出:", (details && details.reason) || "unknown",
          "exit=" + ((details && details.exitCode) ?? ""));
  });
  // 抖音的卡片是「点开就 new tab」的：不接管的话，详情页开在我们看不见的新窗口里，
  // 当前页面一点变化都没有 —— 表现就是「点了卡片但页面里没出现点赞控件」。
  // 这里直接把新窗口的地址在同一窗口里打开，点击才能真的进到视频详情页。
  win.webContents.setWindowOpenHandler(({ url }) => {
    trace("页面请求新窗口:", String(url || "").slice(0, 120));
    if (/^https?:\/\/([a-z0-9-]+\.)?douyin\.com\//i.test(String(url || ""))) {
      setTimeout(() => { win.loadURL(url).catch(() => {}); }, 50);
    }
    return { action: "deny" };
  });
  const { blockNonWebNavigation, installProtocolGuard } = require("./external-url");
  win.webContents.on("will-navigate", blockNonWebNavigation);
  win.webContents.on("will-redirect", blockNonWebNavigation);
  installProtocolGuard(win.webContents);
  trace(`采集窗口已创建（账号 ${accountId || "default"} / ${partition || "默认会话"}）`);
  return win;
}

/** 关掉采集窗口（BrowserWindow 没有 webContents.destroy，要整窗销毁） */
function closeWorker(win) {
  try {
    win.destroy();
  } catch (e) {}
}

/* ------------------------------------------------- 直播采集专用窗口（可见但屏幕外） */
/**
 * 直播页只有在「可见」状态下才渲染聊天区：隐藏窗口里 document.hidden=true，
 * 抖音直播页的虚拟列表永远是空的，采集结果是「已读 0 条」，看起来像坏了。
 *
 * 所以直播采集单独用一个窗口：移到屏幕外（-20000）、不抢焦点、不进任务栏，
 * 页面照常渲染但用户看不到。按账号复用，避免每次采集重建窗口把登录态/指纹打乱。
 */
const liveWindows = new Map(); // 账号 key -> 直播采集窗口

function getLiveWindow(accountId) {
  const key = accountKey(accountId);
  const existing = liveWindows.get(key);
  if (existing && !existing.isDestroyed()) return existing;
  const partition = resolvePartition(accountId);
  const webPreferences = {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    backgroundThrottling: false,
    spellcheck: false,
  };
  if (partition) webPreferences.partition = partition;
  const win = new BrowserWindow({
    show: true,               // 必须可见，否则页面不渲染聊天区
    x: -20000, y: -20000,     // 放到屏幕外，用户看不到
    width: 1280, height: 900,
    skipTaskbar: true,        // 不进任务栏
    focusable: false,         // 不抢焦点
    frame: false,
    hasShadow: false,
    webPreferences,
  });
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  const { blockNonWebNavigation, installProtocolGuard } = require("./external-url");
  win.webContents.on("will-navigate", blockNonWebNavigation);
  win.webContents.on("will-redirect", blockNonWebNavigation);
  installProtocolGuard(win.webContents);
  trace(`直播采集窗口已创建（账号 ${accountId || "default"} / ${partition || "默认会话"}）`);
  liveWindows.set(key, win);
  return win;
}

function closeLiveWindows() {
  for (const win of liveWindows.values()) {
    try { win.destroy(); } catch (e) { /* 已销毁 */ }
  }
  liveWindows.clear();
}

/* ------------------------------------- 直播查看窗（真·可见，供人工核对控件） */
/**
 * 「点赞入口 / 福袋抽奖到底在不在页面上」这类问题只能人眼看。
 * 采集窗口虽然 show:true，但位置在屏幕外（-20000），用户看不见；
 * 这里单独开一个**正常摆在屏幕内**的窗口：有系统标题栏、可拖动缩放、可手动点击。
 *
 * 与采集窗口刻意分开（不复用 liveWindows）：
 *   1) 人工点来点去会打乱采集节奏，两者共用会互相干扰；
 *   2) 采集窗口 focusable:false，用户点不动；
 *   3) 用户关掉查看窗时不能连带把正在跑的采集窗口销毁。
 */
const inspectWindows = new Map(); // 账号 key -> 直播查看窗

function getInspectWindow(accountId) {
  const key = accountKey(accountId) || "default";
  const existing = inspectWindows.get(key);
  if (existing && !existing.isDestroyed()) return existing;
  const partition = resolvePartition(accountId);
  const webPreferences = {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    backgroundThrottling: false,
    spellcheck: false,
  };
  if (partition) webPreferences.partition = partition;
  let bounds = { x: 80, y: 60, width: 1280, height: 860 };
  try {
    const { screen } = require("electron");
    const area = screen.getPrimaryDisplay().workArea;
    const width = Math.min(1320, Math.max(960, Math.round(area.width * 0.7)));
    const height = Math.min(900, Math.max(660, Math.round(area.height * 0.88)));
    bounds = { x: Math.round(area.x + (area.width - width) / 2), y: area.y + 8,
      width, height };
  } catch (e) { /* 拿不到屏幕尺寸就用默认值 */ }
  const win = new BrowserWindow({
    ...bounds,
    show: true,
    frame: true,
    resizable: true,
    skipTaskbar: false,
    focusable: true,
    autoHideMenuBar: true,
    title: "抖音直播间 · 人工核对",
    webPreferences,
  });
  win.setMenuBarVisibility(false);
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  const { blockNonWebNavigation, installProtocolGuard } = require("./external-url");
  win.webContents.on("will-navigate", blockNonWebNavigation);
  win.webContents.on("will-redirect", blockNonWebNavigation);
  installProtocolGuard(win.webContents);
  win.on("closed", () => inspectWindows.delete(key));
  trace(`直播查看窗口已创建（账号 ${accountId || "default"} / ${partition || "默认会话"}）`);
  inspectWindows.set(key, win);
  return win;
}

// 控件雷达：把视口内「像按钮的小元素」全部吐出来（含纯图标按钮）。
// 抖音直播间的点赞/福袋多是没文字、没 aria-label 的 icon，只能靠坐标+尺寸认。
const INSPECT_CONTROLS_JS = `(() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const seen = new Set();
  const items = [];
  const alive = (el) => {
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) > 0.05
      && s.pointerEvents !== 'none';
  };
  const nameOf = (el) => {
    const c = el.className;
    return String(typeof c === 'string' ? c : (c && c.baseVal) || '').slice(0, 60);
  };
  for (const el of document.querySelectorAll('button,[role="button"],[data-e2e],a,div,span,i,svg,img')) {
    const r = el.getBoundingClientRect();
    if (r.width < 18 || r.height < 18 || r.width > 200 || r.height > 200) continue;
    if (r.right < 0 || r.bottom < 0 || r.left > vw || r.top > vh) continue;
    if (!alive(el)) continue;
    if (el.querySelectorAll('*').length > 8) continue;   // 跳过容器，只留叶子控件
    const key = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)].join(',');
    if (seen.has(key)) continue;
    seen.add(key);
    const tag = el.tagName.toLowerCase();
    items.push({
      tag,
      cls: nameOf(el),
      e2e: el.getAttribute('data-e2e') || '',
      label: (el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0, 20),
      txt: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 20),
      icon: tag === 'svg' || !!el.querySelector('svg'),
      x: Math.round(r.left), y: Math.round(r.top),
      w: Math.round(r.width), h: Math.round(r.height),
    });
    if (items.length >= 140) break;
  }
  const body = (document.body && document.body.innerText) || '';
  const m = /([0-9][0-9,.\\s]*[万亿]?)\\s*本场点赞/.exec(body);
  const hit = (re) => items.filter((i) => re.test(i.txt + i.label + i.e2e + i.cls))
    .map((i) => ({ txt: i.txt, label: i.label, e2e: i.e2e, x: i.x, y: i.y, w: i.w, h: i.h }));
  return {
    url: location.href, title: document.title, vw, vh, bodyLen: body.length,
    likeCountText: m ? m[1] : '',
    flags: {
      hasLikeText: /本场点赞/.test(body),
      hasLucky: /福袋|抽奖|幸运/.test(body),
      needsLogin: /需先登录|请先登录|登录后参与/.test(body),
      hasPlayer: !!document.querySelector('[data-e2e="living-container"],.living-container,video'),
    },
    likeCandidates: hit(/赞|like|digg/i),
    luckyCandidates: hit(/福袋|抽奖|幸运|lucky/i),
    items,
  };
})()`;

/** 打开直播间（真可见窗口）→ 扫描控件 → 截图存档。只读，不点任何东西。 */
async function inspectLiveUi(cmd) {
  const url = String(cmd.liveUrl || "").trim();
  if (!url) return { ok: false, error: "缺少直播间地址" };
  const view = getInspectWindow(cmd.accountId);
  const notes = [];
  await loadPage(view, url, Math.max(1500, Number(cmd.waitMs) || 9000));
  await new Promise((r) => setTimeout(r, 2500));
  let first = null;
  try {
    first = await evaluate(view, INSPECT_CONTROLS_JS, 20000);
  } catch (e) {
    // 直播页常见 did-finish-load 不来 / 页面还没稳定：停掉再试一次
    try { view.webContents.stop(); } catch (_) { /* 无进行中的加载 */ }
    await new Promise((r) => setTimeout(r, 1200));
    try {
      first = await evaluate(view, INSPECT_CONTROLS_JS, 20000);
    } catch (e2) {
      return { ok: false, error: "页面脚本执行失败：" + String((e2 && e2.message) || e2).slice(0, 160) };
    }
  }
  // 悬停播放器中部：不少版本把工具栏（含点赞）藏在 hover 里
  let hover = null;
  try {
    const sc = await viewScale(view);
    const vp = (await evaluate(view, "({ w: innerWidth, h: innerHeight })", 6000)) || { w: 1600, h: 900 };
    view.webContents.sendInputEvent({ type: "mouseMove",
      x: Math.round(vp.w * 0.42 * sc.x), y: Math.round(vp.h * 0.55 * sc.y) });
    await new Promise((r) => setTimeout(r, 1200));
    hover = await evaluate(view, INSPECT_CONTROLS_JS, 20000).catch(() => null);
  } catch (e) {
    notes.push("hover:" + String((e && e.message) || e).slice(0, 80));
  }
  const keyOf = (i) => [i.tag, i.x, i.y, i.w, i.h, i.e2e, i.label].join("|");
  const before = new Set(((first && first.items) || []).map(keyOf));
  const added = ((hover && hover.items) || []).filter((i) => !before.has(keyOf(i)));
  let shot = "";
  try {
    const image = await view.webContents.capturePage();
    shot = path.join(__dirname, "_verify", "live-inspect-" + Date.now() + ".png");
    fs.writeFileSync(shot, image.toPNG());
  } catch (e) {
    notes.push("shot:" + String((e && e.message) || e).slice(0, 60));
  }
  try { view.show(); view.focus(); } catch (e) { /* 无窗口管理器时忽略 */ }
  return { ok: true, url: view.webContents.getURL(), shot,
    first, hoverAdded: added, note: notes.join("；") };
}

/**
 * 常驻采集窗口（按账号分池）。
 *
 * 为什么不再「每次指令新建一个窗口」：抖音会按窗口/指纹判断风险，
 * 一次采集一个全新窗口，第二次开始就大概率被丢到「验证码中间页」
 * （实测：第一次拿到 3 条真数据，紧接着再跑就只有 videoLinks=0、bodyLen=4）。
 * 复用同一个窗口后 cookie、UA、设备指纹都稳定，才像正常用户在浏览。
 * 内存代价是一个隐藏窗口（百兆级），换来的是采集可用性。
 *
 * 为什么按账号分：不同账号的 cookie 不能混在同一个 session 里，否则
 * A 的登录态会被 B 覆盖，多账号等于摆设。
 */
const workers = new Map(); // 账号 key -> 常驻原生执行页面（带后台宿主）
let lastWorkerKey = "default";

function accountKey(accountId) {
  const id = String(accountId || "").trim();
  return id || "default";
}

function getWorker(accountId) {
  const key = accountKey(accountId);
  const cur = workers.get(key);
  if (cur && !cur.isDestroyed()) {
    lastWorkerKey = key;
    return cur;
  }
  if (cur) cur.destroy();
  const win = createWorkerWindow(key);
  workers.set(key, win);
  lastWorkerKey = key;
  return win;
}

/** 释放全部常驻窗口（只在停止执行器时调用） */
function releaseWorker() {
  for (const win of workers.values()) closeWorker(win);
  workers.clear();
}

/**
 * 把采集窗口弹到前台，等人处理验证。
 *
 * 为什么必须弹：验证只能人对着页面做，窗口藏在后台等于让整条任务无限空等，
 * 用户只看到「卡住了」却找不到该点哪里。拦截本身不代过、不绕过，只是把
 * 需要人处理的那一页直接摆出来。
 * 只弹不发：不点、不输入、不刷新，页面停在验证页原样等人。
 */
function popForVerify(accountId, where) {
  // 调用方可能传账号 id，也可能直接把 surface 传进来（验证拦截都发生在拿着
  // 这个 surface 干活的地方），两种都认。
  let key = "";
  for (const [k, w] of workers) {
    if (w && (w === accountId || w.view === accountId)) { key = k; break; }
  }
  if (!key) key = accountKey(typeof accountId === "string" ? accountId : lastWorkerKey);
  const win = workers.get(key);
  if (!win || win.isDestroyed()) {
    // 没有常驻窗口时退到最近用过的那个：被拦的账号通常就是它。
    const fallback = workers.get(lastWorkerKey);
    if (!fallback || fallback.isDestroyed()) return false;
    try { fallback.setSkipTaskbar(false); fallback.show(); fallback.focus(); return true; } catch (_) { return false; }
  }
  try {
    win.setSkipTaskbar(false);
    win.show();
    win.focus();
    trace(`验证拦截${where ? "（" + where + "）" : ""}：采集窗口已弹到前台，账号 ${key}`);
    return true;
  } catch (e) {
    trace("验证弹窗失败:", String((e && e.message) || e));
    return false;
  }
}

/**
 * 页面被风控拦到验证页时的统一处理。
 *
 * 合规边界：不代过验证、不破解、不绕过——只把「需要人处理」这件事告诉用户，
 * 并把原因回传，让任务日志写清楚是被拦了，而不是「采到 0 条」。
 * 窗口直接弹到前台（2026-11-18 用户要求）：以前藏在后台 + 托盘提示，
 * 用户看到通知还得自己去托盘找窗口，验证这步本来就只能当场做，藏起来没意义。
 */
function verifyBlocked(view, diag, where) {
  const popped = popForVerify(view, where);
  notify(
    "抖音要求安全验证",
    `采集在「${where}」被拦下（需要人工过验证）。采集窗口已弹到前台，未做任何点击，` +
    `请在弹出的窗口里完成验证，完成后任务会自动继续。`,
  );
  return {
    ok: false,
    needVerify: true,
    popped,
    diag: diag || {},
    error: `抖音要求安全验证（${where}）：采集窗口已弹出，请在该窗口完成验证后任务自动继续`,
  };
}

/**
 * 执行页面脚本（带超时）。
 * 为什么必须带超时：executeJavaScript 在页面卡住时**永不 resolve**
 * （验证码中间页、被拦的导航、渲染进程挂起都会这样），而 tick 的取指循环
 * 会卡在 await 处不前进 → 该账号的槽位永远占着，后端给它的后续指令全石沉大海，
 * 表现为「点开始没反应 / 采集超时」。这里用 race 保证任何情况都会返回。
 */
function evaluate(view, code, timeoutMs = 45000) {
  // 页面里抛错时 Electron 只会回一句 "Script failed to execute …"，看不出哪一行、
  // 也分不清是被拦、页面被冻结还是脚本自身问题。这里在注入代码外再包一层
  // try/catch，把真实错误当成返回值带回来，日志里才追得下去。
  const wrapped = `(async () => {
    try { return await (${code}); }
    catch (e) {
      return { __evalError: String((e && (e.stack || e.message)) || e) };
    }
  })()`;
  let timer = null;
  return Promise.race([
    view.webContents.executeJavaScript(wrapped, true).then((res) => {
      if (res && typeof res === "object" && res.__evalError) {
        throw new Error("页面脚本异常：" + String(res.__evalError).slice(0, 400));
      }
      return res;
    }),
    new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(`页面脚本执行超时（${Math.round(timeoutMs / 1000)}s）`)),
        timeoutMs);
    }),
  ]).finally(() => { if (timer) clearTimeout(timer); });
}

async function loadPage(view, url, waitMs, reload = false) {
  trace("加载页面:", url);
  return new Promise((resolve) => {
    let done = false;
    const finish = (why) => {
      if (done) return;
      done = true;
      trace("页面就绪:", why, String(url).slice(0, 70));
      setTimeout(resolve, waitMs);
    };
    view.webContents.once("did-finish-load", () => finish("did-finish-load"));
    view.webContents.once("did-fail-load", (_e, code, desc) => finish("did-fail-load:" + code + " " + desc));
    if(reload)view.webContents.reloadIgnoringCache();
    else view.webContents.loadURL(url).catch((e) => finish("loadURL-err:" + ((e && e.message) || e)));
    setTimeout(() => finish("timeout-guard"), waitMs + 8000); // 兜底，避免卡死
  });
}

/* --------------------------------------------------------------- 采集主流程 */
async function collectComments(cmd) {
  const keyword = String(cmd.keyword || "").trim();
  const videoMax = Math.max(1, Math.min(parseInt(cmd.videoMax, 10) || 6, 20));
  if (!keyword) return { ok: false, error: "指令缺少关键词" };

  const view = getWorker(cmd.accountId);
  liveViews.add(view);
  const stats = { videos: 0, comments: 0, keyword };
  const notes = [];
  const diag = {};
  try {
    const searchUrl = `https://www.douyin.com/search/${encodeURIComponent(keyword)}?type=video`;
    await loadPage(view, searchUrl, 3500);
    const found = await evaluate(view, SEARCH_EXTRACT_JS, 45000);
    diag.search = { videos: (found.videos || []).length, loginWall: found.loginWall, empty: found.emptyHint };
    if (found.loginWall) {
      return {
        ok: false, loginWall: true,
        error: "抖音账号未登录（搜索页弹出登录墙）。请到「账号池」找到该账号重新登录，登录后任务会自动恢复采集",
      };
    }
    const videos = (found.videos || []).slice(0, videoMax);
    if (!videos.length) {
      return { ok: true, comments: [], stats, note: "搜索页未找到视频", diag };
    }

    const comments = [];
    const seen = new Set();
    for (const v of videos) {
      if (!liveViews.has(view)) break; // 已被 stop 释放
      await loadPage(view, v.url, 3200);
      const page = await evaluate(view, commentExtractScript(cmd), 90000);
      stats.videos += 1;
      diag["v" + v.videoId] = page.diag || {};
      if (page.loginWall) {
        notes.push("中途出现登录墙，已停止");
        break;
      }
      for (const c of page.comments || []) {
        const key = `${c.nickname}|${c.text}`;
        if (seen.has(key)) continue;
        seen.add(key);
        comments.push({ ...c, videoId: v.videoId, videoUrl: v.url });
      }
      await new Promise((r) => setTimeout(r, 1200)); // 视频之间留间隔
    }
    stats.comments = comments.length;
    if (!comments.length) {
      notes.push("视频页没读到评论：可能评论区未展开或选择器需要按真实 DOM 校准");
    }
    return { ok: true, comments, stats, diag, note: notes.join("；") };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e), stats, diag };
  } finally {
    // 窗口常驻，不在这里销毁（见 getWorker）
    liveViews.delete(view);
  }
}

/* ------------------------------------------------- 搜索视频 / 读评论 / 浏览 */
async function withView(waitMs, fn, accountId) {
  // 复用常驻窗口，不再每次新建（见 getWorker 的说明）
  const view = getWorker(accountId);
  liveViews.add(view);
  try {
    return await fn(view);
  } finally {
    liveViews.delete(view);
  }
}

/* ------------------------------------------------------------ DOM 采样（只读） */
/**
 * 采样脚本：把「这个页面的结构长什么样」如实带回来，供写选择器用。
 *
 * 为什么需要它：直播监控、群成员、首作互动这些采集器只能靠真实 DOM 校准。
 * 以前靠猜类名（.comment-item、.chat-item 之类）写选择器，线上全落空，
 * 日志只剩「读到 0 条」，分不清是页面没渲染、被风控拦了还是选择器压根不对。
 *
 * 只读：不点、不输入、不发请求，纯遍历 document。
 */
const SAMPLE_DOM_JS = `(() => {
  const out = { title: document.title || "", url: location.href, readyState: document.readyState };
  const bodyText = (document.body && document.body.innerText) || "";
  out.bodyLen = bodyText.length;
  out.loginWall = /扫码登录|登录后查看|请先登录/.test(bodyText.slice(0, 4000));
  const cls = (el) => String((el.className && el.className.baseVal !== undefined
    ? el.className.baseVal : el.className) || "").trim().slice(0, 90);
  const e2e = (el) => el.getAttribute && el.getAttribute("data-e2e") || "";

  // 1) data-e2e 标记：抖音自己的测试锚点，比类名稳。
  const e2es = [];
  document.querySelectorAll("[data-e2e]").forEach((el) => {
    if (e2es.length >= 160) return;
    const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
    e2es.push({ tag: el.tagName.toLowerCase(), e2e: e2e(el), cls: cls(el), text: t.slice(0, 60) });
  });
  out.e2e = e2es;

  // 2) 可滚动容器：聊天/评论流一定是可滚动且子节点多的那个。
  const scrollers = [];
  document.querySelectorAll("div,ul,section,main").forEach((el) => {
    if (el.scrollHeight - el.clientHeight > 40 && el.children.length >= 3) {
      scrollers.push({ tag: el.tagName.toLowerCase(), cls: cls(el), e2e: e2e(el),
        scrollH: el.scrollHeight, clientH: el.clientHeight, children: el.children.length,
        text: (el.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 50) });
    }
  });
  out.scrollers = scrollers.slice(0, 12);

  // 3) 「同构子节点」容器：一个父节点下面挂着 N 个结构相同的子节点，
  //    评论流、聊天流、礼物提示都是这个形状。按子节点数量排序最像的排前面。
  const groups = new Map();
  document.querySelectorAll("div,ul,li,section").forEach((el) => {
    if (el.children.length < 6) return;
    const sig = Array.from(el.children).slice(0, 4).map((c) =>
      c.tagName + "." + cls(c).split(/\\s+/)[0]).join("|");
    if (!sig || sig === "|||") return;
    const key = sig;
    const hit = groups.get(key) || { sig: key, count: 0, parentCls: cls(el), parentE2e: e2e(el),
      childTag: el.children[0].tagName.toLowerCase(), childCls: cls(el.children[0]),
      sample: [] };
    const n = el.children.length;
    if (n > hit.count) {
      hit.count = n; hit.parentCls = cls(el); hit.parentE2e = e2e(el);
      hit.childTag = el.children[0].tagName.toLowerCase();
      hit.childCls = cls(el.children[0]);
    }
    if (hit.sample.length < 3) {
      hit.sample.push(Array.from(el.children).slice(-2)
        .map((c) => (c.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 70)));
    }
    groups.set(key, hit);
  });
  out.rows = Array.from(groups.values()).sort((a, b) => b.count - a.count).slice(0, 10);

  // 4) 末尾若干条叶子文本 + 它的祖先链：用来认清「一条消息」由哪几段组成
  //    （昵称 / 正文 / 等级牌 / 时间），以及各段的类名。
  const chain = (el) => {
    const parts = [];
    let p = el;
    for (let i = 0; i < 7 && p && p !== document.body; i++) {
      parts.unshift(p.tagName.toLowerCase() + (cls(p) ? "." + cls(p).split(/\\s+/).slice(0, 2).join(".") : ""));
      p = p.parentElement;
    }
    return parts.join(" > ");
  };
  const leaves = [];
  const all = document.querySelectorAll("span,div,p,a,li");
  for (let i = all.length - 1; i >= 0 && leaves.length < 70; i--) {
    const el = all[i];
    if (el.children.length !== 0) continue;
    const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
    if (!t || t.length > 90) continue;
    leaves.push({ text: t.slice(0, 80), chain: chain(el) });
  }
  out.leaves = leaves;
  return out;
})()`;

/**
 * 只读 DOM 采样。
 * cmd.url 给了就先跳过去；cmd.script 给了就跑那段脚本（返回值原样带回），
 * 否则跑内置的结构采样。任何情况下都不点、不输入、不写。
 */
async function sampleDom(cmd) {
  const waitMs = Math.max(800, Math.min(parseInt(cmd.waitMs, 10) || 6000, 30000));
  return withView(600, async (view) => {
    // 采样必须采「真渲染出来的页面」：worker 窗口默认是隐藏的，而抖音的列表 / 弹层
    // 在 document.hidden 时压根不渲染 —— 采回来的是空骨架，照着它校准选择器必然错。
    // 只读通道同样需要可见渲染，所以这里统一先 showQuiet（屏幕内 + opacity 0，不抢焦点）。
    if (cmd.visible !== false && typeof view.showQuiet === "function") {
      try { view.showQuiet(); } catch (_) { /* 无窗口管理器时按原样采样 */ }
      await new Promise((r) => setTimeout(r, 350));
    }
    if (cmd.url) await loadPage(view, String(cmd.url), waitMs);
    else await new Promise((r) => setTimeout(r, waitMs));
    // 可选：在页面坐标 (clickX, clickY) 上做一次**真实**鼠标点击，再跑 script。
    // 为什么不用脚本里的 element.click()：抖音是 React + 自研事件层，
    // 脚本合成的 MouseEvent（isTrusted=false）会被直接忽略 —— 精选页点赞踩过这个坑
    // （点了三下 URL 没变、也没有新窗口请求）。顶栏「通知 / 消息」这类弹层同样只认
    // Chromium 输入管道，所以这里走 sendInputEvent。
    if (cmd.clickX != null && cmd.clickY != null) {
      await clickPoint(view, cmd.clickX, cmd.clickY,
                     { wait: Math.max(300, Number(cmd.clickWait) || 1800) });
    }
    const code = String(cmd.script || "").trim() || SAMPLE_DOM_JS;
    const data = await evaluate(view, code, Math.max(8000, waitMs + 8000));
    const pageUrl = view.webContents.getURL();
    if (cmd.script) return { ok: true, url: pageUrl, data };
    return { ok: true, url: pageUrl, ...data };
  }, cmd.accountId);
}

/* ------------------------------------------------------------ 直播监控采集 */
/**
 * 直播间聊天区读取（只读，不点、不输入、不发弹幕）。
 *
 * 直播间 DOM 的类名是带 hash 的（如 `S3vewJ9R Ij9il8sm webcast-chatroom___list`），
 * 但 `webcast-chatroom___*` 这段 BEM 前缀是稳定的，所以按「前缀 + 结构」定位，
 * 不写死 hash 类名，改版后不至于整片采集落空。
 *
 * 返回状态（live / ended / login_required / unknown）与最近若干条事件：
 *   comment 评论、enter 进房、like 点赞、follow 关注、gift 礼物。
 * 后端只把 comment 当线索，其余只计数。
 */
const LIVE_EVENTS_JS = `(() => {
  const out = { title: document.title || "", url: location.href };
  const body = (document.body && document.body.innerText) || "";
  const head = body.slice(0, 4000);
  out.roomTitle = String(out.title || "").replace(/\\s*-\\s*抖音直播\\s*$/, "").trim();
  const list = document.querySelector('[class*="webcast-chatroom___list"]');
  const itemsBox = document.querySelector('[class*="webcast-chatroom___items"]') || list;
  const ended = /直播已结束|已下播|直播结束|主播已离开/.test(head);
  const onAir = !!document.querySelector('[data-e2e="living-container"], .living-container, video');
  const loginWall = /登录后(参与|查看|发表)评论|请先登录/.test(head);
  out.status = ended ? "ended"
    : (loginWall && !itemsBox) ? "login_required"
    : (itemsBox || onAir) ? "live" : "unknown";
  out.listFound = !!list;
  out.itemCount = 0;
  const events = [];
  // 聊天区是虚拟列表：___list > 定位层 > 真正的消息列表 > 消息项。
  // 先沿「单子节点」往下走到出现多个兄弟的那一层（真正的行容器）；
  // 不行再退回按 ___item 前缀找（注意排除 ___items 容器）。
  const pickItems = (root) => {
    if (!root) return [];
    let node = root;
    for (let i = 0; i < 6 && node; i++) {
      if (node.children.length > 1) return Array.from(node.children);
      node = node.firstElementChild;
    }
    const direct = Array.from(root.querySelectorAll('[class*="___item"]'))
      .filter((el) => String(el.className || "").indexOf("___items") < 0);
    if (direct.length) return direct;
    return root ? Array.from(root.children) : [];
  };
  if (itemsBox) {
    const src = pickItems(itemsBox);
    out.itemCount = src.length;
    let lastKey = "";
    for (const el of src.slice(-60)) {
      const raw = (el.innerText || "").replace(/\\s+/g, " ").trim();
      if (!raw) continue;
      const cls = String(el.className || "");
      const nickEl = el.querySelector('[class*="___nickname"]') ||
        el.querySelector('[class*="___user-name"]') || el.querySelector('[class*="nickname"]');
      const textEl = el.querySelector('[class*="___content"]') ||
        el.querySelector('[class*="___content-with-emoji"]') || el.querySelector('[class*="content"]');
      let nickname = nickEl ? (nickEl.innerText || "").trim() : "";
      let content = textEl ? (textEl.innerText || "").trim() : "";
      if (!nickname || !content) {
        const parts = raw.split(/[:：]/);
        if (!nickname) nickname = (parts[0] || "").trim();
        if (!content) content = parts.slice(1).join("：").trim() || raw;
      }
      let kind = "comment";
      if (/___member|___enter|__enter/i.test(cls) || /(来了|进入了?直播间|加入直播间)/.test(raw)) kind = "enter";
      else if (/关注了主播|关注了你/.test(raw)) kind = "follow";
      else if (/(为主播)?点赞/.test(raw)) kind = "like";
      else if (/送出|赠送|礼物/.test(raw) && /送/.test(raw)) kind = "gift";
      else if (/^欢迎(来到|大家)|欢迎来到直播间/.test(raw)) kind = "system";
      if (kind !== "comment") { nickname = nickname || ""; content = raw; }
      // 用户标识：建单侧「用户类型 / 粉丝数量」筛选要按人去主页核对，弹幕行
      // 里的 data-uid / data-user-id 是唯一可靠锚点，没有就只能保留并提示无法核对
      const uidEl = el.matches('[data-uid],[data-user-id]') ? el
        : el.querySelector('[data-uid],[data-user-id]');
      const uid = uidEl ? (uidEl.getAttribute('data-uid') || uidEl.getAttribute('data-user-id') || '') : '';
      // 虚拟列表会把同一条消息渲染成相邻两份，按相邻去重（保留用户后续真的重复发言）
      const sig = kind + "|" + nickname + "|" + content;
      if (sig === lastKey) continue;
      lastKey = sig;
      events.push({ kind, uid: String(uid || '').slice(0, 64),
        nickname: String(nickname).slice(0, 40),
        content: String(content).slice(0, 300), raw: raw.slice(0, 300) });
    }
  }
  out.events = events.map((it) => Object.assign({}, it, {
    key: (it.kind + "|" + it.nickname + "|" + it.content).slice(0, 140),
  }));
  return out;
})()`;

// 调试采样：只在 cmd.debug 时执行，用来现场核对聊天行的用户标识与可点控件
// （直播间的点赞 / 关注 / 福袋按钮没有稳定的 data-e2e，必须按实际 DOM 校准）。
const LIVE_DEBUG_JS = `(() => {
  const itemsBox = document.querySelector('[class*="webcast-chatroom___items"]');
  const list = document.querySelector('[class*="webcast-chatroom___list"]');
  const host = itemsBox || list;
  let rows = [];
  if (host) {
    let n = host;
    for (let i = 0; i < 6 && n; i++) { if (n.children.length > 1) { rows = Array.from(n.children); break; } n = n.firstElementChild; }
  }
  const out = { rowCount: rows.length, rows: [], controls: [],
    loginHint: /需先登录|请先登录|登录后参与/.test((document.body && document.body.innerText) || "") };
  for (const el of rows.slice(-3)) {
    out.rows.push({
      html: el.innerHTML.slice(0, 700),
      links: Array.from(el.querySelectorAll("a[href]")).map((a) => a.getAttribute("href")).slice(0, 4),
      dataAttrs: Array.from(el.querySelectorAll("*[data-uid],*[data-user-id],[data-e2e]"))
        .slice(0, 6)
        .map((n) => (n.getAttribute("data-e2e") || "") + "|" + (n.getAttribute("data-uid") || n.getAttribute("data-user-id") || "")),
    });
  }
  document.querySelectorAll('[data-e2e],button,[role="button"],[class*="like"],[class*="Like"],[class*="lucky"],[class*="Lucky"]').forEach((el) => {
    if (out.controls.length > 40) return;
    const r = el.getBoundingClientRect();
    if (r.width < 8) return;
    const txt = (el.innerText || "").replace(/\\s+/g, " ").trim();
    const e2e = el.getAttribute("data-e2e") || "";
    const label = el.getAttribute("aria-label") || el.getAttribute("title") || "";
    if (!/赞|关注|福袋|抽奖|参与|领取/.test(txt + e2e + label)) return;
    out.controls.push({ e2e, cls: String(el.className || "").slice(0, 44),
      txt: txt.slice(0, 14), label: label.slice(0, 14),
      x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) });
  });
  return out;
})()`;

// 控件雷达：把右侧工具栏/操作区里「可点的小图标」全部报出来。
// 直播间的点赞、福袋很多是纯图标按钮（没有文字也没有 aria-label），
// 只能按位置和尺寸定位，先看清实际布局再写选择器。
const LIVE_POINTERS_JS = `(() => {
  const out = [];
  document.querySelectorAll("div,button,span").forEach((el) => {
    if (out.length > 60) return;
    const cs = getComputedStyle(el);
    if (cs.cursor !== "pointer") return;
    const r = el.getBoundingClientRect();
    if (r.width < 16 || r.width > 120 || r.height < 16 || r.height > 120) return;
    if (r.left < 600) return;
    if (el.children.length > 2) return;
    out.push({ cls: String(el.className || "").slice(0, 46), tag: el.tagName,
      txt: (el.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 12),
      x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
      svg: el.querySelectorAll("svg").length });
  });
  return { pointers: out };
})()`;

// 用户卡片探测：点一条聊天行的昵称，看弹出层里有没有「私信」入口和用户主页链接。
// 私信必须拿到能定位到人的标识（sec_uid / 主页链接），拿不到就只能放弃这条路径。
const LIVE_CLICK_NICK_JS = `(() => {
  const itemsBox = document.querySelector('[class*="webcast-chatroom___items"]');
  const list = document.querySelector('[class*="webcast-chatroom___list"]');
  const host = itemsBox || list;
  let rows = [];
  if (host) { let n = host; for (let i = 0; i < 6 && n; i++) { if (n.children.length > 1) { rows = Array.from(n.children); break; } n = n.firstElementChild; } }
  // 从最后一条往前找：必须是一条「昵称：内容」的真人发言（末尾常有系统提示行）
  let row = null, target = null;
  for (let i = rows.length - 1; i >= 0 && !row; i--) {
    const el = rows[i];
    const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
    if (!t.includes("：") && !t.includes(":")) continue;
    row = el;
    for (const cand of el.querySelectorAll("span,div,a")) {
      const ct = (cand.innerText || "").replace(/\\s+/g, " ").trim();
      if (!ct || ct.length > 24 || ct.includes("：")) continue;
      if (getComputedStyle(cand).cursor !== "pointer") continue;
      target = cand; break;
    }
  }
  if (!row) return { ok: false, reason: "no-comment-row" };
  const box = (target || row).getBoundingClientRect();
  return { ok: true, x: box.left + Math.min(box.width / 2, 30), y: box.top + box.height / 2,
    picked: target ? (target.innerText || "").trim().slice(0, 20) : "(row)" };
})()`;

const LIVE_POPUP_DUMP_JS = `(() => {
  const out = { links: [], buttons: [], text: "" };
  document.querySelectorAll('a[href*="/user/"]').forEach((a) => {
    if (out.links.length < 8) out.links.push(a.getAttribute("href"));
  });
  document.querySelectorAll('button,[role="button"],[class*="card"],[class*="Card"]').forEach((el) => {
    const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
    if (!t || t.length > 12 || out.buttons.length > 20) return;
    const r = el.getBoundingClientRect();
    if (r.width < 8) return;
    out.buttons.push({ txt: t, cls: String(el.className || "").slice(0, 34) });
  });
  out.text = ((document.body && document.body.innerText) || "").replace(/\\s+/g, " ").slice(-260);
  return out;
})()`;

/**
 * 主播主页 → 开播检测（监控主播直播用）。
 * 主播开播时主页会出现 live.douyin.com/<webRid> 的入口链接 + 「直播中」标记；
 * 两个证据任一成立才算开播，链接缺失时退回只认标记（点进直播间的路径交给壳跟随跳转）。
 */
const AUTHOR_LIVE_JS = `(() => {
  let roomUrl = "";
  for (const a of document.querySelectorAll('a[href*="live.douyin.com"], a[href*="/live/"]')) {
    const href = String(a.getAttribute('href') || '');
    const m = /live\\.douyin\\.com\\/(\\d{6,})/.exec(href) || /\\/live\\/(\\d{6,})/.exec(href);
    if (m) { roomUrl = 'https://live.douyin.com/' + m[1]; break; }
  }
  let liveBadge = false;
  for (const el of document.querySelectorAll('[class*="live"], [data-e2e], span, div')) {
    const t = (el.innerText || '').trim();
    if (t === '直播中' || t === '正在直播') {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) { liveBadge = true; break; }
    }
  }
  let nickname = "";
  for (const sel of ['[data-e2e="user-info"] [class*="nickname"]', 'h1', '[class*="nickname"]']) {
    const el = document.querySelector(sel);
    const t = (el && el.innerText || '').trim();
    if (t && t.length <= 30) { nickname = t; break; }
  }
  return { isLive: !!(roomUrl || liveBadge), roomUrl, liveBadge, nickname, url: location.href };
})()`;

/**
 * 直播监控：逐个直播间打开并读取聊天区。
 *
 * 为什么不用采集工人窗口（getWorker）：工人窗口是隐藏的，`document.hidden=true`
 * 时抖音直播页不渲染聊天区 —— 实测同一房间在隐藏窗口里 chatroom 列表永远是空的，
 * 「已读 0 条」会被误判成采集器坏了。这里单独开一个「屏幕外但可见」的窗口：
 * 坐标 -20000、skipTaskbar、focusable:false，页面照常渲染，又不打扰用户。
 *
 * 监控主播直播（author_live）：authorUrls 传的是主播主页链接。先开主页检测是否
 * 开播 —— 开播则解析出直播间地址并入本轮采集；未开播回 status:"not_live"，
 * 由后端记「主播未开播」后跳过（下轮检查间隔由任务配置决定）。
 */
async function readLiveEvents(cmd) {
  const authors = (Array.isArray(cmd.authorUrls) ? cmd.authorUrls : [])
    .map((u) => (typeof u === "string" ? u : (u && u.url) || ""))
    .map((u) => String(u || "").trim())
    .filter(Boolean).slice(0, 8);
  const urls = (Array.isArray(cmd.liveUrls) ? cmd.liveUrls : [])
    .map((u) => (typeof u === "string" ? u : (u && u.url) || ""))
    .map((u) => String(u || "").trim())
    .filter(Boolean).slice(0, 5);
  if (!urls.length && !authors.length) return { ok: false, error: "缺少直播间地址" };
  const view = getLiveWindow(cmd.accountId);
  const rooms = [];
  const notes = [];
  liveViews.add(view);
  try {
    // 先解析主播主页：开播的并入 urls 一起读，未开播的直接回 not_live
    const liveAuthors = [];
    for (const authorUrl of authors) {
      if (!liveViews.has(view) || view.isDestroyed()) throw new Error("直播采集已停止");
      await loadPage(view, authorUrl, 9000);
      let info = null;
      try {
        info = await evaluate(view, AUTHOR_LIVE_JS, 15000);
      } catch (first) {
        try { view.webContents.stop(); } catch (e) { /* 无进行中的加载 */ }
        await new Promise((r) => setTimeout(r, 1200));
        try {
          info = await evaluate(view, AUTHOR_LIVE_JS, 15000);
        } catch (second) {
          notes.push("主播主页读取失败：" + String((second && second.message) || second));
          rooms.push(Object.assign({ url: authorUrl, finalUrl: view.webContents.getURL(),
            status: "not_live", note: "主页读取失败，按未开播处理" }, {}));
          continue;
        }
      }
      if (info && info.isLive && info.roomUrl) {
        liveAuthors.push(info.roomUrl);
      } else {
        rooms.push(Object.assign({ url: authorUrl, finalUrl: view.webContents.getURL(),
          status: "not_live",
          nickname: (info && info.nickname) || "",
          note: info && info.isLive ? "检测到直播中但未取到直播间地址" : "主播未开播" }, {}));
      }
    }
    for (const url of urls.concat(liveAuthors)) {
      if (!liveViews.has(view) || view.isDestroyed()) throw new Error("直播采集已停止");
      await loadPage(view, url, 9000);
      let data = null;
      try {
        data = await evaluate(view, LIVE_EVENTS_JS, 20000);
      } catch (first) {
        // 页面还在 provisional load 时 executeJavaScript 会一直排队：停掉再试一次
        try { view.webContents.stop(); } catch (e) { /* 无进行中的加载 */ }
        await new Promise((r) => setTimeout(r, 1200));
        try {
          data = await evaluate(view, LIVE_EVENTS_JS, 20000);
        } catch (second) {
          notes.push(String(url).slice(0, 60) + "：" + String((second && second.message) || second));
        }
      }
      if (cmd.debug) {
        try {
          const dbg = await evaluate(view, LIVE_DEBUG_JS, 15000);
          const ptr = await evaluate(view, LIVE_POINTERS_JS, 15000).catch(() => null);
          data = Object.assign({}, data, { debug: Object.assign({}, dbg, ptr || {}) });
          if (cmd.debug === "shot") {
            // 截图存档：用于现场核对控件位置（只读，不改页面状态）
            try {
              const image = await view.webContents.capturePage();
              const file = path.join(__dirname, "_verify", "live-shot-" + Date.now() + ".png");
              require("fs").writeFileSync(file, image.toPNG());
              data.debug.shot = file;
            } catch (e) {
              data.debug.shot = "failed: " + String((e && e.message) || e).slice(0, 60);
            }
          }
          if (cmd.debug === "popup") {            const pick = await evaluate(view, LIVE_CLICK_NICK_JS, 15000);
            if (pick && pick.ok) {
              const sc = await viewScale(view);
              const point = { x: Math.round(pick.x * sc.x), y: Math.round(pick.y * sc.y) };
              for (const type of ["mouseMove", "mouseDown", "mouseUp"])
                view.webContents.sendInputEvent({ type, ...point, button: "left", clickCount: 1 });
              await new Promise((r) => setTimeout(r, 2600));
              data.debug.popup = Object.assign({ picked: pick.picked },
                await evaluate(view, LIVE_POPUP_DUMP_JS, 15000).catch(() => null));
            } else {
              data.debug.popup = { error: (pick && pick.reason) || "click-failed" };
            }
          }
        } catch (e) { /* 调试信息拿不到不影响采集 */ }
      }
      rooms.push(Object.assign({ url, finalUrl: view.webContents.getURL() }, data || {}));
    }
    return { ok: true, rooms, note: notes.join("；") };
  } finally {
    liveViews.delete(view);
  }
}

/* ------------------------------------------------------------ 群成员采集 */
/**
 * 监控群成员：打开抖音消息页 → 按群名找到群聊 → 设置 → 完整成员面板读取。
 *
 * DOM 链路（2026-11-17 真机侦察，AI智能体教师交流群(4)）：
 *   会话条目 conversationConversationItem{wrapper,rowArea2}（虚拟列表，找不到要滚动）
 *   → 右侧「设置」tab（semi-tabs-tab，文本=「设置」）
 *   → GroupParticipantsoverviewheaderArea（「群聊成员 N/上限」）
 *   → GroupParticipantsdetailparticipantsArea：
 *       detailitemcontainer{ detailitemtitle「群主/管理员(N人)」|「群成员(N人)」,
 *                            detailitemitem「昵称 [群主|管理员]」 }
 * 会话点击必须用完整 mouse 事件序列（mouseover→mousedown→mouseup→click），
 * el.click() 在该页面不生效（同直播间昵称卡片）。
 */
function groupMembersJs(name) {
  return `(async () => {
  const GROUP_NAME = ${JSON.stringify(name)};
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const dispatch = (el) => {
    const rect = el.getBoundingClientRect();
    for (const type of ["mouseover", "mousedown", "mouseup", "click"])
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window,
        clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2 }));
  };
  const findConversation = () => {
    for (const el of document.querySelectorAll(
        '[class*="conversationConversationItemrowArea2"], [class*="conversationConversationItemwrapper"]')) {
      if ((el.innerText || "").includes(GROUP_NAME)) return el;
    }
    return null;
  };
  // 虚拟列表：群会话不在视口时逐屏滚动左栏找（最多 10 屏，防呆）
  let item = findConversation();
  if (!item) {
    let scrollBox = null;
    for (const el of document.querySelectorAll('[class*="onversation"], [class*="essionList"]')) {
      if (el.scrollHeight > el.clientHeight + 80) { scrollBox = el; break; }
    }
    for (let i = 0; i < 10 && !item; i++) {
      if (scrollBox) scrollBox.scrollTop += 420;
      await sleep(500);
      item = findConversation();
    }
  }
  if (!item) return { found: false, reason: "会话列表里没有找到该群（虚拟列表滚动后仍未命中）" };
  item.scrollIntoView({ block: "center" });
  await sleep(500);
  dispatch(item);
  await sleep(2500);
  // 群名标题确认真的切过去了（防止点错成普通会话）
  let opened = false;
  for (const el of document.querySelectorAll('[class*="RightPanelHeadertitle"]')) {
    if ((el.innerText || "").includes(GROUP_NAME)) { opened = true; break; }
  }
  if (!opened) return { found: false, reason: "点击后右侧会话标题未出现群名" };
  // 「设置」tab
  let settingTab = null;
  for (const el of document.querySelectorAll('[class*="semi-tabs-tab"]')) {
    if ((el.innerText || "").trim() === "设置") { settingTab = el; break; }
  }
  if (!settingTab) return { found: true, reason: "没找到设置入口", members: [] };
  settingTab.click();
  await sleep(2200);
  // 「群聊成员 N/上限」概览头部 → 完整成员面板
  const header = document.querySelector('[class*="GroupParticipantsoverviewheaderArea"]')
    || document.querySelector('[class*="GroupParticipantsoverviewdescription"]');
  if (!header) return { found: true, reason: "没找到群成员入口", members: [] };
  dispatch(header);
  await sleep(2500);
  // 读完整成员面板：按 detailitemcontainer 分组，title 里有身份分组名
  const members = [];
  let currentRole = "member";
  for (const box of document.querySelectorAll('[class*="detailitemcontainer"]')) {
    const title = (box.querySelector('[class*="detailitemtitle"]') || {}).innerText || "";
    if (/群主/.test(title)) currentRole = "owner";
    else if (/管理员/.test(title)) currentRole = "admin";
    else if (/群成员/.test(title)) currentRole = "member";
    for (const it of box.querySelectorAll('[class*="detailitemitem"]')) {
      const text = (it.innerText || "").replace(/\\s+/g, " ").trim();
      if (!text) continue;
      let role = currentRole;
      let nick = text;
      const m = /^(.*?)\\s*(群主|管理员)$/u.exec(text);
      if (m) { nick = m[1].trim(); role = m[2] === "群主" ? "owner" : "admin"; }
      if (!nick || nick === "邀请") continue;
      if (members.some((x) => x.nickname === nick)) continue;
      members.push({ nickname: nick, role });
    }
  }
  // 关掉成员面板（点头部右上角关闭图标），避免影响下一个群
  const detailHeader = document.querySelector('[class*="GroupParticipantsdetailheaderArea"]');
  if (detailHeader) {
    const closeBtn = detailHeader.querySelector('svg, [class*="semi-icon"], [class*="close"]');
    if (closeBtn) { dispatch(closeBtn); await sleep(800); }
  }
  const countText = ((document.querySelector('[class*="GroupParticipantsoverviewcount"]') || {}).innerText || "").trim();
  return { found: true, members, countText };
})()`;
}

async function readGroupMembers(cmd) {
  const names = (Array.isArray(cmd.groupNames) ? cmd.groupNames : [])
    .map((g) => (typeof g === "string" ? g : (g && (g.name || g.groupName || g.title)) || ""))
    .map((g) => String(g || "").trim())
    .filter(Boolean).slice(0, 20);
  if (!names.length) return { ok: false, error: "缺少群名" };
  // 与直播采集同款「屏幕外但可见」窗口：消息页虚拟列表在隐藏窗口可能不渲染
  const view = getLiveWindow(cmd.accountId);
  liveViews.add(view);
  const groups = [];
  const notes = [];
  try {
    await loadPage(view, "https://www.douyin.com/chat?isPopup=1", 10000);
    const login = await evaluate(view, LOGIN_STATE_JS, 10000).catch(() => null);
    if (login && login.loginWall && !login.loggedIn) {
      return { ok: false, error: "需要登录才能读群聊：请在该账号窗口登录后重试" };
    }
    for (const name of names) {
      if (!liveViews.has(view) || view.isDestroyed()) break;
      let data = null;
      try {
        data = await evaluate(view, groupMembersJs(name), 45000);
      } catch (first) {
        try { view.webContents.stop(); } catch (e) { /* 无进行中的加载 */ }
        await new Promise((r) => setTimeout(r, 1200));
        try {
          data = await evaluate(view, groupMembersJs(name), 45000);
        } catch (second) {
          notes.push(`群「${name}」读取失败：${String((second && second.message) || second)}`);
          groups.push({ name, found: false, members: [], note: "读取失败" });
          continue;
        }
      }
      const row = Object.assign({ name }, data || {});
      groups.push(row);
    }
    return { ok: true, groups, note: notes.join("；") };
  } finally {
    liveViews.delete(view);
  }
}

/* ------------------------------------------------------------ 私信读取（自动回复） */
/**
 * 自动回复的监听源之一：私信。
 *
 * 为什么不能靠「轮询接口」：抖音私信没有公开的 HTTP 接口，未读数与消息正文
 * 只在已登录会话的页面里。壳能拿到登录态，所以读动作放在壳里做、后端只收结构化结果。
 *
 * DOM 依据（2026-11-17 真机采样，账号 1794886618996）：
 *   会话列表  [data-e2e="conversation-item"]
 *             标题 .conversationConversationItemtitle（注意：*ItemtitleWrapper 也命中 * 选择器，必须精确类名）
 *             时间 .ConversationItemTagNextToTitletimeStr
 *             摘要 .ConversationItemHinttextBox
 *   消息面板  .messageMessageListlist > .messageMessageBoxmessageBox（每条）
 *             正文 .MessageItemTextbubbleTextContent
 *             方向 .MessageItemTextisFromMe（有=我发的；没有=对方）
 *             时间 .MessageBoxTimetimeLayout
 *   输入框    .editor-kit-container[contenteditable="true"]（宽 772，可见）
 *
 * 列表是虚拟列表：只渲染视口附近，滚动才能把首屏外的会话带出来。
 */
const DM_CHAT_URL = "https://www.douyin.com/chat?isPopup=1";

/** 会话列表：按滚动屏数累积去重，返回 昵称/时间/摘要/头像数（头像数>1 说明是群）。 */
function dmConversationsJs(scrollScreens) {
  return `(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cls = (el) => String((el.className && el.className.baseVal !== undefined
    ? el.className.baseVal : el.className) || "").trim();
  const has = (el, name) => cls(el).split(/\\s+/).includes(name);
  const byClass = (root, name) => [...root.querySelectorAll("*")].filter((e) => has(e, name));
  const read = (it) => {
    const t = byClass(it, "conversationConversationItemtitle")[0];
    const tm = byClass(it, "ConversationItemTagNextToTitletimeStr")[0];
    const pv = byClass(it, "ConversationItemHinttextBox")[0];
    const avatars = it.querySelectorAll('[class*="avatar"] img, [class*="Avatar"] img').length;
    return {
      title: t ? (t.innerText || "").trim() : "",
      time: tm ? (tm.innerText || "").trim() : "",
      preview: pv ? (pv.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 80) : "",
      avatarImgs: avatars,
    };
  };
  const seen = new Map();
  const collect = () => {
    for (const it of document.querySelectorAll('[data-e2e="conversation-item"]')) {
      const row = read(it);
      if (!row.title) continue;
      const key = row.title + "|" + row.time + "|" + row.preview;
      if (!seen.has(key)) seen.set(key, row);
    }
  };
  collect();
  let box = null;
  for (const el of document.querySelectorAll('[class*="conversationConversationList"], [class*="onversation"]')) {
    if (el.scrollHeight > el.clientHeight + 40) { box = el; break; }
  }
  for (let i = 0; i < ${scrollScreens} && box; i++) {
    box.scrollTop += 380;
    await sleep(450);
    collect();
  }
  if (box) box.scrollTop = 0;
  return { url: location.href, conversations: [...seen.values()], scrolled: !!box };
})()`;
}

/**
 * 群聊列表：会话列表里逐行判定「是不是群」，按滚动屏数累积去重。
 *
 * 判定依据（2026-11-18 真机采样，账号 1794886618996）：
 *   群会话头像  <img class="commonConversationIconnoDrag">（IM 群图标），行内没有 .semi-avatar；
 *   单聊头像    <span class="semi-avatar …-img">，行内 .semi-avatar 恰好 1 个。
 * 另有标题带成员数的形式「群名(305)」作为兜底信号。
 * 三条任一命中即算群；信号都带回读取结果，判错时能看出是哪条生效的。
 */
function groupConversationsJs(scrollScreens) {
  return `(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cls = (el) => String((el.className && el.className.baseVal !== undefined
    ? el.className.baseVal : el.className) || "").trim();
  const has = (el, name) => cls(el).split(/\\s+/).includes(name);
  const byClass = (root, name) => [...root.querySelectorAll("*")].filter((e) => has(e, name));
  const read = (it) => {
    const t = byClass(it, "conversationConversationItemtitle")[0];
    const tm = byClass(it, "ConversationItemTagNextToTitletimeStr")[0];
    const pv = byClass(it, "ConversationItemHinttextBox")[0];
    const title = t ? (t.innerText || "").trim() : "";
    const groupIcon = !!it.querySelector('img[class*="commonConversationIcon"]');
    const semiAvatar = it.querySelectorAll(".semi-avatar").length;
    const countSuffix = /\\(\\d+\\)\\s*$/.test(title);
    return {
      name: title,
      time: tm ? (tm.innerText || "").trim() : "",
      preview: pv ? (pv.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 80) : "",
      groupIcon,
      semiAvatar,
      countSuffix,
      // 只认「群图标」与「标题带成员数」：semi-avatar 缺失也可能是头像还没渲染
      // （懒加载），单靠它会把未渲染的单聊误判成群聊。
      isGroup: groupIcon || countSuffix,
    };
  };
  const seen = new Map();
  const seenAll = new Map();
  const collect = () => {
    for (const it of document.querySelectorAll('[data-e2e="conversation-item"]')) {
      const row = read(it);
      if (!row.name) continue;
      if (!seenAll.has(row.name)) seenAll.set(row.name, row);
      if (!row.isGroup) continue;
      if (!seen.has(row.name)) seen.set(row.name, row);
    }
  };
  // 列表是虚拟列表：首屏外的会话要滚动才会渲染
  for (let i = 0; i < 30; i++) {
    if (document.querySelector('[data-e2e="conversation-item"]')) break;
    await sleep(500);
  }
  collect();
  let box = null;
  for (const el of document.querySelectorAll('[class*="conversationConversationList"], [class*="onversation"]')) {
    if (el.scrollHeight > el.clientHeight + 40) { box = el; break; }
  }
  for (let i = 0; i < ${scrollScreens} && box; i++) {
    box.scrollTop += 380;
    await sleep(450);
    collect();
  }
  if (box) box.scrollTop = 0;
  const rows = [...document.querySelectorAll('[data-e2e="conversation-item"]')].length;
  return { url: location.href, groups: [...seen.values()],
           all: [...seenAll.values()].slice(0, 60), rows, scrolled: !!box };
})()`;
}

async function readGroupList(cmd) {
  const view = getLiveWindow(cmd.accountId);
  liveViews.add(view);
  try {
    await loadPage(view, DM_CHAT_URL, 9000);
    const login = await evaluate(view, LOGIN_STATE_JS, 10000).catch(() => null);
    if (login && login.loginWall && !login.loggedIn) {
      return { ok: false, error: "需要登录才能读群聊：请在该账号窗口登录后重试" };
    }
    const screens = Math.max(0, Math.min(parseInt(cmd.scrollScreens, 10) || 6, 10));
    const data = await evaluate(view, groupConversationsJs(screens), 45000);
    const groups = (data && data.groups) || [];
    return { ok: true, url: (data && data.url) || "", groups,
      all: (data && data.all) || [],
      rows: (data && data.rows) || 0,
      note: groups.length ? "" : "会话列表里没有识别到群聊：确认账号已加入群聊，或页面还没渲染完" };
  } finally {
    liveViews.delete(view);
  }
}

/** 单个会话：打开 → 读最近 limit 条文本消息（系统卡片不计入）。 */
function dmThreadJs(title, limit) {
  return `(async () => {
  const TITLE = ${JSON.stringify(title)};
  const LIMIT = ${limit};
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cls = (el) => String((el.className && el.className.baseVal !== undefined
    ? el.className.baseVal : el.className) || "").trim();
  const has = (el, name) => cls(el).split(/\\s+/).includes(name);
  const byClass = (root, name) => [...root.querySelectorAll("*")].filter((e) => has(e, name));
  const dispatch = (el) => {
    const rect = el.getBoundingClientRect();
    for (const type of ["mouseover", "mousedown", "mouseup", "click"])
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window,
        clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2 }));
  };
  // 会话列表异步渲染：刚导航过来时列表还是空的，先等最多 15 秒
  for (let i = 0; i < 30; i++) {
    if (document.querySelector('[data-e2e="conversation-item"]')) break;
    await sleep(500);
  }
  const find = () => {
    for (const it of document.querySelectorAll('[data-e2e="conversation-item"]')) {
      const t = byClass(it, "conversationConversationItemtitle")[0];
      if (t && (t.innerText || "").trim() === TITLE) return it;
    }
    return null;
  };
  let item = find();
  if (!item) {
    let box = null;
    for (const el of document.querySelectorAll('[class*="conversationConversationList"], [class*="onversation"]')) {
      if (el.scrollHeight > el.clientHeight + 40) { box = el; break; }
    }
    for (let i = 0; i < 10 && !item; i++) {
      if (box) box.scrollTop += 420;
      await sleep(500);
      item = find();
    }
  }
  if (!item) return { found: false, title: TITLE, reason: "会话列表里没有找到该会话（滚动后仍未命中）" };
  item.scrollIntoView({ block: "center" });
  await sleep(400);
  dispatch(item);
  await sleep(2600);
  const headTitle = [...document.querySelectorAll('[class*="RightPanelHeadertitle"]')]
    .map((e) => (e.innerText || "").trim()).filter(Boolean);
  const opened = headTitle.some((t) => t === TITLE || t.indexOf(TITLE) === 0);
  // 群聊判定（2026-11-17 真机校准）：群会话右侧标题带成员数「群名(305)」，
  // 且面板有 groupTabs/groupTabPane（聊天/公告/收藏/设置）、群公告卡片。
  // 三条任一命中即判群 —— 自动回复默认不碰群聊，误判成单聊会往 300 人群里发话术。
  const isGroup = !!document.querySelector(
      '[class*="componentsRightPanelgroupTabs"], [class*="groupTabPane"], [class*="MessageItemGroupNotice"]')
    || /\\(\\d+\\)\\s*$/.test(headTitle[0] || "");
  const list = document.querySelector('[class*="messageMessageListlist"]') || document;
  const boxes = [...list.querySelectorAll('[class*="messageMessageBoxmessageBox"]')];
  const messages = [];
  for (const b of boxes) {
    const body = b.querySelector('[class*="MessageItemTextbubbleTextContent"]');
    if (!body) continue;
    const timeEl = b.querySelector('[class*="MessageBoxTimetimeLayout"]');
    const text = (body.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 500);
    if (!text) continue;
    messages.push({
      time: timeEl ? (timeEl.innerText || "").trim() : "",
      fromMe: !!b.querySelector('[class*="MessageItemTextisFromMe"]'),
      text,
    });
  }
  // 页面 DOM 是「新在上、旧在下」（真机核对：刚发的消息出现在列表最前面）。
  // 这里统一翻成「旧 → 新」，后端只用 messages[-1] 判最后一句是谁说的。
  messages.reverse();
  // 对方主页标识：会话面板里跳主页的 /user/<sec_uid> 锚点。关键词规则勾了
  // 「关注用户」时后端要用它下发 follow-user；读不到就为空，后端会跳过关注
  // （不按昵称猜人）。先在右侧面板里找，找不到再全页兜底一次。
  let uid = '';
  const panel = document.querySelector('[class*="RightPanel"]');
  const scanAnchors = (root) => {
    for (const a of root.querySelectorAll('a[href*="/user/"]')) {
      const m = String(a.href || '').match(/\/user\/([^/?#]+)/);
      if (m && m[1]) { uid = m[1]; return true; }
    }
    return false;
  };
  if (!panel || !scanAnchors(panel)) scanAnchors(document);
  return { found: true, title: TITLE, opened, isGroup, headTitle, uid,
    messages: messages.slice(-LIMIT), total: messages.length, url: location.href };
})()`;
}

async function readDmConversations(cmd) {
  const view = getLiveWindow(cmd.accountId);
  liveViews.add(view);
  try {
    await loadPage(view, DM_CHAT_URL, 9000);
    const login = await evaluate(view, LOGIN_STATE_JS, 10000).catch(() => null);
    if (login && login.loginWall && !login.loggedIn) {
      return { ok: false, error: "需要登录才能读私信：请在该账号窗口登录后重试" };
    }
    const screens = Math.max(0, Math.min(parseInt(cmd.scrollScreens, 10) || 0, 8));
    const data = await evaluate(view, dmConversationsJs(screens), 45000);
    const list = (data && data.conversations) || [];
    return { ok: true, url: (data && data.url) || "", conversations: list,
      note: list.length ? "" : "私信会话列表为空：可能是该账号没有会话，或页面还没渲染完" };
  } finally {
    liveViews.delete(view);
  }
}

async function readDmThreads(cmd) {
  const titles = (Array.isArray(cmd.titles) ? cmd.titles : [])
    .map((t) => String(t || "").trim()).filter(Boolean).slice(0, 8);
  if (!titles.length) return { ok: false, error: "缺少会话名（titles）" };
  const limit = Math.max(4, Math.min(parseInt(cmd.limit, 10) || 20, 60));
  const view = getLiveWindow(cmd.accountId);
  liveViews.add(view);
  const threads = [];
  const notes = [];
  try {
    await loadPage(view, DM_CHAT_URL, 9000);
    const login = await evaluate(view, LOGIN_STATE_JS, 10000).catch(() => null);
    if (login && login.loginWall && !login.loggedIn) {
      return { ok: false, error: "需要登录才能读私信：请在该账号窗口登录后重试" };
    }
    for (const title of titles) {
      if (!liveViews.has(view) || view.isDestroyed()) break;
      try {
        threads.push(await evaluate(view, dmThreadJs(title, limit), 45000));
      } catch (first) {
        // 页面卡住时停一次再试：虚拟列表偶发长时间不 idle
        try { view.webContents.stop(); } catch (e) { /* 无进行中的加载 */ }
        await new Promise((r) => setTimeout(r, 1200));
        try {
          threads.push(await evaluate(view, dmThreadJs(title, limit), 45000));
        } catch (second) {
          notes.push(`会话「${title}」读取失败：${String((second && second.message) || second)}`);
          threads.push({ found: false, title, reason: "读取失败" });
        }
      }
    }
    return { ok: true, threads, note: notes.join("；") };
  } finally {
    liveViews.delete(view);
  }
}

/* ------------------------------------------------------------ 登录状态检测 */
/**
 * 账号登录态检测（只读，供账号池「检测账号 / 状态同步」用）。
 *
 * 两层判定：
 *  1) Cookie 层：账号分区里有没有 sessionid / sid_guard 等 session cookie ——
 *     没有就直接判未登录，不导航（快，且不惊动页面）。
 *  2) 页面层：有 session cookie 也不一定有效（登出后可能残留 / 过期），
 *     打开抖音首页看有没有登录墙，没有才算真登录。
 *
 * 为什么必须走壳：cookie 在 Electron session（含 httpOnly）里，后端读不到；
 * 页面层的登录墙标记也只有真浏览器里才有。
 */
/**
 * 账号数据采样：只读个人主页的公开计数（关注 / 粉丝 / 获赞 / 作品）。
 *
 * 为什么不用固定选择器：抖音主页改版频繁，data-e2e 有时有有时没有。
 * 这里走三层退让：data-e2e 锚点 → 「关注/粉丝/获赞」文本邻近节点 → 整页文本正则。
 * 三个都拿不到就如实返回空值，绝不把 0 当成「这个账号真的 0 粉丝」。
 */
const ACCOUNT_STATS_JS = `(() => {
  const text = (document.body && document.body.innerText) || '';
  const pick = (sels) => {
    for (const s of sels) {
      const el = document.querySelector(s);
      const v = el && (el.getAttribute('data-count') || el.textContent || '').trim();
      if (v && /^[0-9]+(\\.[0-9]+)?[万wW亿]?$/.test(v)) return v;
    }
    return '';
  };
  const near = (label) => {
    const nodes = [...document.querySelectorAll('div,span,p')].filter(
      el => el.children.length === 0 && (el.innerText || '').trim() === label);
    for (const el of nodes) {
      const box = el.parentElement;
      if (!box) continue;
      const nums = (box.innerText || '').match(/[0-9]+(\\.[0-9]+)?[万wW亿]?/g) || [];
      const other = nums.filter(n => n !== label);
      if (other.length) return other[1] || other[0];
      const sib = el.nextElementSibling;
      const t = sib && (sib.innerText || '').trim();
      if (t && /^[0-9]+(\\.[0-9]+)?[万wW亿]?$/.test(t)) return t;
    }
    return '';
  };
  const fromText = (label) => {
    const m = text.match(new RegExp(label + '\\\\s*[:：]?\\\\s*([0-9]+(?:\\\\.[0-9]+)?[万wW亿]?)'));
    return m ? m[1] : '';
  };
  const nickname = (document.querySelector('[data-e2e="user-name"]')
    || document.querySelector('h1'))?.textContent?.trim() || '';
  const idMatch = text.match(/抖音号\\\\s*[:：]?\\\\s*([A-Za-z0-9_.-]{2,40})/);
  return {
    url: location.href,
    nickname,
    douyinId: idMatch ? idMatch[1] : '',
    following: pick(['[data-e2e="following-count"]']) || near('关注') || fromText('关注'),
    followers: pick(['[data-e2e="fans-count"]', '[data-e2e="followers-count"]']) || near('粉丝') || fromText('粉丝'),
    likes: pick(['[data-e2e="like-count"]', '[data-e2e="total-like"]']) || near('获赞') || fromText('获赞'),
    works: pick(['[data-e2e="work-count"]']) || near('作品') || fromText('作品'),
    loginWall: /扫码登录|登录后查看|请先登录/.test(text.slice(0, 3000)),
    textLen: text.length,
  };
})()`;

const LOGIN_STATE_JS = `(() => {
  const loginWall = !!document.querySelector('#login-pannel, [data-e2e="login-guide"], .login-mask, [class*="login-guide"]');
  // 顶栏出现「登录」按钮 = 未登录；出现头像/昵称 = 已登录。两个证据都拿，取并集。
  let loginButton = false;
  for (const el of document.querySelectorAll('button,span,div,a')) {
    const t = (el.innerText || '').trim();
    if ((t === '登录' || t === '登录/注册') && el.children.length <= 2) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.width < 160 && r.height > 0 && r.height < 60) { loginButton = true; break; }
    }
  }
  let avatar = false;
  for (const el of document.querySelectorAll('[class*="avatar"], img[class*="avatar"]')) {
    const r = el.getBoundingClientRect();
    if (r.width > 18 && r.height > 18 && r.top >= 0 && r.top < 120) { avatar = true; break; }
  }
  return { loginWall, loginButton, avatar,
           loggedIn: !loginWall && (avatar || !loginButton) };
})()`;

/**
 * 读账号公开数据（关注/粉丝/获赞/作品）。
 *
 * 走的是这个账号自己的分区：默认账号落在默认 session，其余账号落在
 * persist:douyin-<id>，所以读到的必须是「这个账号」的主页，不会串号。
 */
async function readAccountStats(cmd) {
  return withView(2500, async (view) => {
    const url = String(cmd.profileUrl || "https://www.douyin.com/user/self");
    await loadPage(view, url, 4200);
    let stats = await evaluate(view, ACCOUNT_STATS_JS, 15000).catch(() => null);
    if (!stats) return { ok: false, error: "主页读取超时（可能被风控或网络异常）" };
    if (stats.loginWall) {
      return { ok: false, code: "login_required", error: "该账号未登录，读不到主页数据", diag: stats };
    }
    // 主页计数是异步渲染的：一次拿不全就刷一次再读
    if (!stats.followers && !stats.likes) {
      await loadPage(view, url, 4000, true);
      stats = await evaluate(view, ACCOUNT_STATS_JS, 15000).catch(() => stats);
    }
    const filled = ["following", "followers", "likes", "works"].filter(k => stats && stats[k]);
    if (!filled.length) {
      return { ok: false, error: "页面上没有读到关注/粉丝/获赞计数（选择器需按真机校准）",
               diag: { url: stats.url, textLen: stats.textLen, nickname: stats.nickname } };
    }
    return { ok: true, ...stats };
  }, cmd.accountId);
}

async function checkLogin(cmd) {
  const accountId = String(cmd.accountId || "");
  const { session } = require("electron");
  const partition = resolvePartition(accountId);
  const ses = partition ? session.fromPartition(partition) : session.defaultSession;
  let hasSessionCookie = false;
  let cookieNote = "";
  try {
    const cookies = await ses.cookies.get({});
    const names = new Set(cookies.map((c) => c.name));
    hasSessionCookie = ["sessionid", "sid_guard", "sid_ucp_virtual"].some((n) => names.has(n));
    cookieNote = `session cookie: ${hasSessionCookie ? "有" : "无"}`;
  } catch (e) {
    cookieNote = "cookie 读取失败：" + String((e && e.message) || e).slice(0, 120);
  }
  if (!hasSessionCookie) {
    return { ok: true, loggedIn: false, note: cookieNote, diag: { partition: partition || "default" } };
  }
  // cookie 层过了，再用真页面确认（否则「登录了但状态没同步」会换成「残留 cookie 假在线」）
  return withView(500, async (view) => {
    const url = view.webContents.getURL();
    if (!/douyin\.com/.test(url)) await loadPage(view, "https://www.douyin.com/", 3200);
    else await new Promise((r) => setTimeout(r, 1200));
    const st = await evaluate(view, LOGIN_STATE_JS, 12000);
    const loginWall = !!(st && st.loginWall);
    if (loginWall && cmd.reloadOnWall !== false) {
      // 页面可能停在旧状态：强制刷新一次再判，避免「窗口还停在登录页」误判
      await loadPage(view, "https://www.douyin.com/", 3200, true);
      const st2 = await evaluate(view, LOGIN_STATE_JS, 12000);
      return { ok: true, loggedIn: !!(st2 && st2.loggedIn), note: cookieNote,
               diag: { partition: partition || "default", page: st2 || null, firstPage: st } };
    }
    return { ok: true, loggedIn: !!(st && st.loggedIn), note: cookieNote,
             diag: { partition: partition || "default", page: st || null } };
  }, accountId);
}

/* --------------------------------------------- 搜索联想词（采集关键词扩词） */
/**
 * 搜索联想词：在搜索框里输入一个词，读下拉联想列表。
 *
 * 背景：页面「采集关键词」弹窗（Settings 里的 keyword-collector）是前端早就做好
 * 的功能，它按「关键词+a」到「关键词+z」逐次查询、收集联想词去重。但后端 6 个
 * keyword-collector-* 通道此前一个都没注册，按钮点了必失败。本指令补的是最底下
 * 那一段：从真实页面里把联想下拉读回来。
 *
 * 为什么必须真实键盘：联想下拉由 React 事件层触发，脚本 setValue / dispatch
 * 合成 input 事件（isTrusted=false）不会弹出浮层 —— 和点赞同源的坑。所以走
 * showForCheck（可见 + 短暂置顶，躲开 DWM 遮挡判定）+ clickPoint + insertText。
 */
const SUGGEST_INPUT_JS = `(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (!r || r.width < 40 || r.height < 14) return false;
    const s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity) > 0.1;
  };
  const hint = (el) => ((el.getAttribute && el.getAttribute("data-e2e")) || "") + " " +
    ((el.getAttribute && (el.getAttribute("placeholder") || el.getAttribute("aria-label"))) || "") + " " +
    String(el.className || "");
  const cands = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]')).filter(vis);
  if (!cands.length) return { found: false, diag: { inputs: 0, title: document.title || "" } };
  const scored = cands.map((el) => {
    const r = el.getBoundingClientRect();
    const e2e = String((el.getAttribute && el.getAttribute("data-e2e")) || "");
    return { el, score: (/search|搜索/i.test(hint(el)) ? 10 : 0)
      + (e2e === "searchbar-input" ? 20 : 0) + (r.top < window.innerHeight * 0.4 ? 2 : 0) };
  }).sort((a, b) => b.score - a.score);
  const best = scored[0];
  if (!best || best.score < 5) {
    return { found: false, diag: { inputs: cands.length,
      sample: cands.slice(0, 4).map((el) => ({ tag: el.tagName, hint: hint(el).trim().slice(0, 60) })) } };
  }
  const el = best.el;
  const r = el.getBoundingClientRect();
  el.setAttribute("data-wb-suggest-input", "1");
  return { found: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
    rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
    info: { tag: el.tagName, hint: hint(el).trim().slice(0, 80) } };
})()`;

/**
 * 读下拉联想列表。
 * 定位思路：不猜类名（抖音这套类名会漂移），改成「几何判据」——
 * 浮层贴着输入框底边、宽度接近输入框、里面挂着若干行短文本。
 * 取「行数最多」的那个容器，再按文本去重，保留最短的内层节点。
 */
const SUGGEST_READ_JS = `(() => {
  const norm = (s) => String(s || "").replace(/\\s+/g, " ").trim();
  const box = document.querySelector('[data-wb-suggest-input="1"]');
  if (!box) return { items: [], diag: { error: "输入框锚点丢失（页面已跳转？）" } };
  const ir = box.getBoundingClientRect();
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (!r || r.width < 60 || r.height < 10) return false;
    const s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity) > 0.05;
  };
  const cands = [];
  document.querySelectorAll("div,ul,section").forEach((el) => {
    if (!vis(el)) return;
    const r = el.getBoundingClientRect();
    if (r.top < ir.bottom - 14 || r.top > ir.bottom + 48) return;
    // 必须和输入框左对齐：真机取证发现顶部导航栏（精选/推荐/…）高度 484、
    // 子节点 21 个，几何上完全满足「宽 + 高」，只有 x 对不齐。少了这一条
    // 就会把整条导航当成联想词返回。
    if (Math.abs(r.left - ir.left) > Math.max(36, ir.width * 0.28)) return;
    if (r.width < ir.width * 0.5 || r.width > ir.width * 2.4) return;
    if (r.height < 26 || r.height > 760) return;
    const text = norm(el.innerText);
    if (!text || text.length > 800) return;
    cands.push({ el, r, text, kids: el.children.length, cls: String(el.className || "").slice(0, 80) });
  });
  cands.sort((a, b) => (b.kids - a.kids) || (b.text.length - a.text.length));
  const best = cands[0] || null;
  const items = [];
  if (best) {
    const rows = Array.from(best.el.querySelectorAll("li, [role='option'], div, span"))
      .filter((el) => vis(el) && norm(el.innerText));
    const seen = new Map();
    for (const el of rows) {
      const t = norm(el.innerText);
      if (!t || t.length > 60) continue;
      const prev = seen.get(t);
      if (!prev || norm(prev.innerText).length > t.length) seen.set(t, el);
    }
    for (const t of seen.keys()) items.push(t);
  }
  return { items, diag: {
    candidates: cands.length,
    top: cands.slice(0, 3).map((c) => ({ cls: c.cls, kids: c.kids, h: Math.round(c.r.height), text: c.text.slice(0, 90) })),
    picked: best ? best.cls : null,
    e2e: Array.from(document.querySelectorAll("[data-e2e]"))
      .map((el) => String(el.getAttribute("data-e2e") || ""))
      .filter((v) => /suggest|search/i.test(v)).slice(0, 12),
    visibility: document.visibilityState || "",
  } };
})()`;

async function suggestKeywords(cmd) {
  const keyword = String(cmd.keyword || "").trim();
  if (!keyword) return { ok: false, error: "指令缺少关键词" };
  return withView(3000, async (view) => {
    // 采集要连查 26 次。每次重新 loadPage 白等 3 秒以上，26 条就是 1 分半的纯等待，
    // 所以允许后端传 reuse：当前页已经是抖音首页就直接复用，只在第一次加载。
    const here = String((view.webContents && view.webContents.getURL && view.webContents.getURL()) || "");
    const reusable = cmd.reuse !== false && /^https:\/\/www\.douyin\.com\/?(\?|$)/.test(here);
    if (!reusable) await loadPage(view, "https://www.douyin.com/", 3200);
    const quick = await evaluate(view, `(() => ({ title: document.title || "",
      bodyLen: ((document.body && document.body.innerText) || "").length }))()`, 8000).catch(() => null);
    if (quick && /验证|captcha|安全验证/i.test(quick.title)) return verifyBlocked(view, quick, "搜索页");

    // 联想下拉只在「真正可见」的页面里弹；opacity:0 的窗口会被 DWM 判遮挡，
    // 可信输入直接丢。先 showForCheck（可见 + 短暂置顶），读完整页再让位。
    const shown = typeof view.showForCheck === "function" ? view.showForCheck()
      : (typeof view.showQuiet === "function" ? view.showQuiet() : false);
    try {
      await new Promise((r) => setTimeout(r, 450));
      let vis = await evaluate(view, "document.visibilityState", 5000).catch(() => "");
      if (String(vis) !== "visible") {
        // 一次重试：冷启动首帧经常还没被系统认成可见
        try { view.show(); view.focus(); } catch (e) { /* 无窗口管理器时忽略 */ }
        await new Promise((r) => setTimeout(r, 600));
        vis = await evaluate(view, "document.visibilityState", 5000).catch(() => "");
      }
      const box = await evaluate(view, SUGGEST_INPUT_JS, 8000).catch(() => null);
      if (!box || !box.found) return { ok: false, error: "未找到搜索输入框", diag: box || {} };

      await clickPoint(view, box.x, box.y, { wait: 450 });
      // 清掉可能残留的上一次输入，否则会拼成「获客a获客b」
      const key = (keyCode, modifiers = []) => {
        view.webContents.sendInputEvent({ type: "keyDown", keyCode, modifiers });
        view.webContents.sendInputEvent({ type: "keyUp", keyCode, modifiers });
      };
      key("A", ["control"]);
      await new Promise((r) => setTimeout(r, 120));
      key("Backspace");
      await new Promise((r) => setTimeout(r, 200));
      view.webContents.insertText(keyword);
      await new Promise((r) => setTimeout(r, 1100));

      const read = await evaluate(view, SUGGEST_READ_JS, 12000)
        .catch((e) => ({ items: [], diag: { error: String((e && e.message) || e) } }));
      const value = await evaluate(view, `(() => { const el = document.querySelector('[data-wb-suggest-input="1"]');
        return el ? String(el.value || el.textContent || "") : ""; })()`, 5000).catch(() => "");
      const items = (read.items || []).filter((t) => t && t !== keyword);
      return { ok: true, keyword, items, inputValue: value,
               diag: { ...(read.diag || {}), input: box.info, rect: box.rect, visibility: String(vis) },
               note: items.length ? "" : "输入后没读到联想下拉（浮层未渲染，或页面有遮挡）" };
    } finally {
      if (typeof view.releaseTopmost === "function") view.releaseTopmost();
      if (shown && typeof view.hideQuiet === "function") {
        await new Promise((r) => setTimeout(r, 200));
        view.hideQuiet();
      }
    }
  }, cmd.accountId);
}

/** 搜索关键词，拿回视频 + 作者（线索采集） */
async function searchVideos(cmd) {
  const keyword = String(cmd.keyword || "").trim();
  const limit = Math.max(1, Math.min(parseInt(cmd.limit, 10) || 12, 200));
  if (!keyword) return { ok: false, error: "指令缺少关键词" };
  return withView(3500, async (view) => {
    const url = `https://www.douyin.com/search/${encodeURIComponent(keyword)}?type=video`;
    // 搜索结果同样是懒加载：窗口 hidden 时首屏只渲染几张卡片且不再追加
    // （2026-11-19 真机对比：hidden 3–5 条 vs 安静显示 26 条）。
    // 用户正在看采集画面时不动它，只在没人看时安静显示（opacity 0，不抢焦点）。
    const watchable = typeof view.isShown === "function" ? !!view.isShown() : false;
    const shown = !watchable && typeof view.showQuiet === "function" ? !!view.showQuiet() : false;
    if (shown) await new Promise((r) => setTimeout(r, 350));
    try {
    await loadPage(view, url, 3200);

    // 先用一个极轻的脚本判断当前是什么页面。
    // 为什么要先探一次：验证码中间页会让页面内的定时器停走，SEARCH_ITEMS_JS
    // 里的 await sleep 永远不 resolve，一路挂到 45s evaluate 超时 —— 明明是
    // 「被风控拦了」，日志里却只写成「页面脚本执行超时」，用户根本不知道要
    // 去过验证。先探标题就能立刻分辨，也不用白等。
    const quick = await evaluate(
      view,
      `(() => ({ title: document.title || "",
                 links: document.querySelectorAll('a[href*="/video/"]').length,
                 bodyLen: (document.body.innerText || "").length }))()`,
      8000
    ).catch(() => null);
    if (quick && /验证|captcha|安全验证/i.test(quick.title)) {
      return verifyBlocked(view, { ...quick, verifyHint: true }, "搜索页");
    }

    // SPA navigation can outlive did-finish-load's timeout guard. Observe the
    // requested search page before running its extraction script; a slow load
    // is not evidence of a CAPTCHA and must not be reported as one.
    const readyDeadline = Date.now() + 60000;
    let readiness = null;
    while (Date.now() < readyDeadline && liveViews.has(view)) {
      readiness = await evaluate(view, `(() => ({
        url: location.href, title: document.title || '',
        links: document.querySelectorAll('a[href*="/video/"]').length,
        text: (document.body && document.body.innerText || '').slice(0, 800),
        verify: !!document.querySelector('#captcha_container,[data-e2e="verify-dialog"]')
      }))()`, 5000).catch(() => null);
      if (readiness && (readiness.verify || /验证|captcha|安全验证/i.test(readiness.title))) {
        return verifyBlocked(view, { verifyHint: true }, "搜索页");
      }
      if (readiness && readiness.url.includes('/search/' + encodeURIComponent(keyword)) &&
          (readiness.links > 0 || /暂无搜索结果|登录后即可搜索|扫码登录/.test(readiness.text))) break;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    let page;
    try {
      // 滚动轮数按目标条数给：每轮约 900ms，够读到 limit 就提前退出
      const rounds = Math.min(40, Math.max(8, Math.ceil(limit / 4) + 6));
      page = await evaluate(view, SEARCH_ITEMS_JS
        .replace('__LEADGEN_SEARCH_LIMIT__', String(limit))
        .replace('__LEADGEN_SEARCH_ROUNDS__', String(rounds)), 75000);
    } catch (e) {
      // 大脚本超时也要给出可读原因，而不是干巴巴一句超时
      return {
        ok: false,
        diag: quick || {},
        error: `搜索页加载或读取超时（${(e && e.message) || e}），未确认搜索结果；未发现验证码证据`,
        needVerify: false,
      };
    }
    if (page.loginWall) return { ok: false, error: "抖音账号未登录（检测到登录墙）", loginWall: true };
    if (page.diag && page.diag.verifyHint) return verifyBlocked(view, page.diag, "搜索页");
    const items = (page.items || []).slice(0, limit);
    return {
      ok: true, items, keyword, diag: page.diag || {},
      note: items.length ? "" : (page.emptyHint ? "搜索无结果"
              : (page.diag && page.diag.verifyHint) ? "被安全验证页拦住，请在壳里手动过一次验证"
              : "搜索页未解析到视频卡片"),
    };
    } finally {
      if (shown && typeof view.hideQuiet === "function") {
        await new Promise((r) => setTimeout(r, 200));
        view.hideQuiet();
      }
    }
  }, cmd.accountId);
}

/**
 * 视频入口：关注流 / 推荐流 / 喜欢列表 / 话题页 → 视频清单（只读）。
 *
 * 四种入口在页面结构上是两类：关注、推荐是竖向一屏一条的信息流（只能翻页取），
 * 喜欢、话题是瀑布流卡片（滚一滚就有链接）。所以这里分两条路走，但对外只暴露
 * 一个指令，后端不必关心页面长什么样。
 */
async function readSourceVideos(cmd) {
  const source = String(cmd.source || "").trim();
  const keyword = String(cmd.keyword || "").trim();
  const limit = Math.max(1, Math.min(parseInt(cmd.limit, 10) || 12, 30));
  const FEED = { follow: "https://www.douyin.com/follow",
                 recommend: "https://www.douyin.com/?recommend=1&from_nav=1" };
  const GRID = { like: { url: "https://www.douyin.com/user/self?showTab=like",
                         box: '[data-e2e="user-like-list"]' },
                 hashtag: { url: "", box: "" } };
  if (!FEED[source] && !GRID[source]) {
    return { ok: false, error: "不支持的视频来源：" + (source || "空") };
  }
  if (source === "hashtag") {
    if (!keyword) return { ok: false, error: "话题来源缺少关键词" };
    GRID.hashtag.url = "https://www.douyin.com/hashtag/" + encodeURIComponent(keyword);
    // 网页版话题页对普通账号经常只回一个错误页 + 页脚推荐位，正文里没有视频列表。
    // 先按话题页读；读不到就退回「话题关键词搜索」，并在 note 里说明走了哪条路。
    GRID.hashtag.box = '[data-e2e="hashtag-video-list"], [data-e2e="scroll-list"]';
  }
  if (FEED[source]) {
    return withView(4000, async (view) => {
      await loadPage(view, FEED[source], 3500);
      // 抓取脚本自带 85s 预算，到点带着已抓到的条目返回；
      // evaluate 的外层超时放宽到 180s（含首次空流重载重试），
      // 免得一次翻页卡顿就把整轮判成「读取入口失败」。
      const budget = 85000;
      const runFeed = () => evaluate(view, FEED_LIST_JS
        .replace("__LIMIT__", String(limit))
        .replace("__DWELL__", String(source === "follow" ? 1600 : 2100))
        .replace("__BUDGET__", String(budget)), 180000);
      let page = await runFeed();
      // 只对真正的空白骨架重载一次；精选网格、登录墙或可见但未识别的
      // 播放器都不能靠反复 loadURL 修好，重载只会让画面回到同一页。
      const diag = page.diag || {};
      const blankSkeleton = !(page.items || []).length && !!page.error &&
        !diag.loginWall && !diag.activeMarker && !diag.visiblePlayers && Number(diag.bodyLen || 0) < 1200;
      if (blankSkeleton) {
        await loadPage(view, FEED[source], 4000, true);
        page = await runFeed();
      }
      if (page.verifyHint) return verifyBlocked(view, { verifyHint: true }, "视频流页");
      if (page.error) return { ok: false, error: page.error, diag: page.diag || {} };
      const items = (page.items || []).filter(i => /^\d{6,}$/.test(String(i.videoId || "")));
      // 部分结果也算成功：宁可先跑手上这几条，也不要把「翻页慢」判成「启动失败」。
      // note 会随回执带回页面/日志，用户知道为什么条数少了。
      const note = items.length
        ? (page.truncated
            ? `信息流翻页超过 ${Math.round(budget / 1000)} 秒，只拿到 ${items.length}/${limit} 条；可减少视频数量或稍后重试`
            : "")
        : "信息流没有翻到可用的视频（可能停在直播或空推荐）";
      return { ok: true, source, url: view.webContents.getURL(), items,
               truncated: !!page.truncated, diag: page.diag || {}, note };
    }, cmd.accountId);
  }
  const cfg = GRID[source];
  return withView(3500, async (view) => {
    await loadPage(view, cfg.url, 4000);
    // 喜欢列表要先等 tab 激活才渲染卡片；等不到就空扫一遍，把诊断带回去
    const page = await evaluate(view, GRID_LIST_JS
      .replace("__LIMIT__", String(limit))
      .replace("__BOX__", JSON.stringify(cfg.box || "")), 120000);
    if (page.error) return { ok: false, error: page.error };
    const items = (page.items || []).filter(i => /^\d{6,}$/.test(String(i.videoId || "")));
    if (source === "hashtag" && !items.length) {
      // 话题页没有真列表：改成「#话题」搜索，至少拿到平台按话题匹配的视频。
      // 这是降级路径，note 里写清楚，避免用户以为读的就是话题页本身。
      const found = await searchVideos({ keyword: "#" + keyword, limit, accountId: cmd.accountId });
      if (found.ok === false) {
        // 话题搜索本身失败（验证码 / 未登录 / 超时）：如实报出去，
        // 不要写成「话题里没有视频」——那是完全不同的两件事。
        return { ok: false, diag: page.diag || {},
                 error: "话题页没有视频列表，话题搜索也没成功：" + (found.error || "未知原因") };
      }
      const alt = (found.items || []).filter(i => /^https:\/\/www\.douyin\.com\/video\/\d{6,}$/.test(i.url || ""));
      return { ok: true, source, url: view.webContents.getURL(),
               items: alt.map(i => ({ ...i, videoId: (/\/video\/(\d{6,})/.exec(i.url) || [])[1] || "" })),
               diag: { ...(page.diag || {}), fallback: "search" },
               note: alt.length ? "话题页未返回视频列表，已改用话题关键词搜索"
                                : "话题页与话题搜索都没读到视频" };
    }
    return { ok: true, source, url: view.webContents.getURL(), items,
             diag: page.diag || {},
             note: items.length ? "" : (source === "like"
               ? "喜欢列表没有读到视频：可能未登录或列表为空"
               : "话题页没有读到视频卡片") };
  }, cmd.accountId);
}

/**
 * 推荐页扫描（吞评检测「推荐页随机评论」专用）：像真人一样逐屏翻推荐流，
 * 每屏读当前视频的评论数徽标，收集评论数少于上限的视频。
 * 只读：不点赞、不关注、不评论 —— 发表评论由后端签名指令单独下发。
 *
 * 评论数徽标的位置：feed 右侧操作栏的评论图标（feed-comment-icon / comment-icon），
 * 数字通常是图标的兄弟节点，沿父层向上找第一个「纯数字或 N万」的文本。
 * 读不到数字的视频不计入候选（没法核实 <200，宁可少收不硬凑）。
 */
const RECOMMEND_SCAN_JS = `(async () => {
  try {
    const want = __COUNT__;
    const maxComments = __MAX_COMMENTS__;
    const maxRounds = __MAX_ROUNDS__;
    const dwell = __DWELL__;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const active = () => document.querySelector('[data-e2e="feed-active-video"]');
    const arrow = () => document.querySelector('[data-e2e="video-switch-next-arrow"]');
    // 首页先渲染导航再异步挂视频流（与 FEED_LIST_JS 同坑），等 30 秒
    for (let i = 0; i < 60 && !active(); i++) await sleep(500);
    if (!active()) {
      const nav = Array.from(document.querySelectorAll('a,div,span'))
        .find(e => /^推荐$/.test(String(e.textContent || '').trim()) && e.offsetParent);
      if (nav) { try { nav.click(); } catch (e) {} await sleep(4000); }
    }
    if (!active()) {
      return { ok: false, items: [], error: '未进入推荐视频流（可能是登录墙或空推荐）',
               diag: { loginWall: /扫码登录|登录后查看|请先登录/.test((document.body.innerText || '').slice(0, 3000)),
                       href: location.href } };
    }
    if (document.querySelector('#captcha_container,[data-e2e="verify-dialog"]')) {
      return { ok: false, items: [], verifyHint: true, error: '被安全验证页拦住' };
    }
    const parseCount = (t) => {
      const s = String(t || '').replace(/[,，\\s]/g, '');
      const m = /^([\\d.]+)(万|w|W)?$/.exec(s);
      if (!m) return null;
      const n = parseFloat(m[1]);
      if (!isFinite(n) || n < 0) return null;
      return Math.round(n * (m[2] ? 10000 : 1));
    };
    const readCommentCount = (box) => {
      const nodes = box.querySelectorAll('[data-e2e="feed-comment-icon"], [data-e2e="comment-icon"], span[data-e2e*="comment"]');
      for (const el of nodes) {
        let cur = el;
        for (let up = 0; up < 4 && cur; up++, cur = cur.parentElement) {
          const text = String(cur.innerText || '').trim();
          // 徽标容器里只有「图标(无字) + 数字」；带别的正文说明爬过头了
          if (text.length <= 12) {
            const n = parseCount(text);
            if (n !== null) return n;
          }
        }
      }
      return null;
    };
    const items = [], seen = {};
    let skippedBusy = 0, skippedUnreadable = 0, stuck = 0;
    for (let round = 0; round < maxRounds && items.length < want; round++) {
      const box = active();
      if (!box) break;
      const vid = String(box.getAttribute('data-e2e-vid') || '');
      if (vid && !seen[vid]) {
        seen[vid] = 1;
        const n = readCommentCount(box);
        if (n === null) {
          skippedUnreadable += 1;
        } else if (n >= maxComments) {
          skippedBusy += 1;
        } else {
          const desc = box.querySelector('[data-e2e="video-desc"]');
          const info = box.querySelector('[data-e2e="video-info"]');
          let title = desc ? (desc.innerText || '') : (info ? (info.innerText || '') : '');
          title = String(title).replace(/\s+/g, ' ').trim();
          const cut = title.indexOf('合集：');
          if (cut > 0) title = title.slice(0, cut).trim();
          if (!desc) title = title.replace(/^@[^·]{1,24}·\s*\S*?前\s*/, '').trim();
          items.push({
            videoId: vid,
            videoUrl: 'https://www.douyin.com/video/' + vid,
            commentCount: n,
            title: title.slice(0, 200),
          });
        }
        stuck = 0;
      } else {
        stuck += 1;
        if (stuck >= 3) break;
      }
      const next = arrow();
      if (!next) break;
      // 翻页要等视频起播才换 data-e2e-vid（与 FEED_LIST_JS 同解）
      try { next.click(); } catch (e) { break; }
      await sleep(dwell);
      let moved = false;
      for (let t = 0; t < 8; t++) {
        const now = active();
        if (now && String(now.getAttribute('data-e2e-vid') || '') !== vid) { moved = true; break; }
        if (t === 3) { try { arrow() && arrow().click(); } catch (e) {} }
        await sleep(700);
      }
      if (!moved) {
        stuck += 1;
        if (stuck >= 2) break;
      }
    }
    return { ok: true, items,
             diag: { visited: Object.keys(seen).length, skippedBusy, skippedUnreadable,
                     enough: items.length >= want } };
  } catch (e) { return { ok: false, items: [], error: String((e && e.message) || e) }; }
})()`;

/** 推荐页扫描：收集评论数少于上限的候选视频（吞评检测「推荐页随机评论」） */
async function recommendScan(cmd) {
  const want = Math.max(1, Math.min(parseInt(cmd.count, 10) || 5, 100));
  const maxComments = Math.max(1, Math.min(parseInt(cmd.maxComments, 10) || 200, 10000));
  const dwell = Math.max(600, Math.min(parseInt(cmd.dwellMs, 10) || 1800, 8000));
  const script = RECOMMEND_SCAN_JS
    .replace('__COUNT__', JSON.stringify(want))
    .replace('__MAX_COMMENTS__', JSON.stringify(maxComments))
    .replace('__MAX_ROUNDS__', JSON.stringify(Math.max(want * 8, 60)))
    .replace('__DWELL__', JSON.stringify(dwell));
  return withView(4000, async (view) => {
    await loadPage(view, "https://www.douyin.com/?recommend=1&from_nav=1", 3000);
    const res = await evaluate(view, script, 240000);
    if (!res || typeof res !== "object") {
      return { ok: false, items: [], error: "推荐页扫描脚本无返回" };
    }
    const items = (res.items || []).filter(i => /^https:\/\/www\.douyin\.com\/video\/\d{6,}$/.test(i.videoUrl || ""));
    return { ...res, items,
             url: view.webContents.getURL(),
             note: items.length ? "" : "推荐页没有扫到符合条件的视频（评论数低于上限且数字可读）" };
  }, cmd.accountId);
}

// 详情浮层（点封面 / 点「下一个」进来的那种）评论面板默认是收起的：
// 不点评论图标，页面上既没有 comment-list 也没有 comment-item，读到 0 条。
// 视频页则默认展开。这里没开就点开，跟人点评论一个动作。
/** 读取指定视频的评论（监控 / 线索采集） */
async function readComments(cmd) {
  const { VIDEO_STATE_JS } = require("./leadgen-read");
  const urls = Array.isArray(cmd.videoUrls) ? cmd.videoUrls
    : (cmd.videoUrl ? [cmd.videoUrl] : []);
  const limit = Math.max(4, Math.min(parseInt(cmd.limit, 10) || 40, 200));
  if (!urls.length) return { ok: false, error: "指令缺少视频地址" };
  return withView(3200, async (view) => {
    const out = [];
    const diag = {};
    // 评论区是懒加载的：parking 窗口隐藏时 document.visibilityState=hidden，
    // 抖音不会追加下一屏评论，滚动完全白做（2026-11-19 真机：hidden 时恒定 6 条，
    // 安静显示后同一页 6→16→26→34→37）。
    // 但用户正在「查看采集画面」里盯着的时候不能再 showQuiet —— 那会把窗口压成
    // opacity 0，用户眼里就是「画面突然没了」。所以只在没被看见时才安静显示。
    const watchable = typeof view.isShown === "function" ? !!view.isShown() : false;
    const shown = !watchable && typeof view.showQuiet === "function" ? !!view.showQuiet() : false;
    if (shown) await new Promise((r) => setTimeout(r, 350));
    try {
      for (const url of urls.slice(0, 12)) {
        if (!liveViews.has(view)) break;
        const targetId = /\/video\/(\d{6,})/.exec(String(url || ""))?.[1] || "";
        // 当前画面是哪条视频：点「下一个」/点封面进来的是详情浮层，抖音不改地址，
        // 只认 URL 会以为「没进到目标视频」，于是又整页加载一遍——用户看到的
        // 就是同一个视频反复加载。
        const here = await evaluate(view, VIDEO_STATE_JS, 8000).catch(() => null);
        const currentId = String((here && here.id) || "");
        const immersiveFeed = !!(here && here.immersiveFeed);
        // 浮层关掉后视频节点还留在 DOM 里（宽高为 0），只比 id 会误判「已经在这条
        // 视频上」→ 不去重新打开 → 评论图标点不到 → 读到 0 条。必须同时要求画面可见。
        const reusedCurrentPage = !!targetId && targetId === currentId && here.stageShown !== false;
        if (immersiveFeed && !reusedCurrentPage) {
          return { ok: false, comments: out, diag,
            error: "沉浸式视频流中的当前视频与目标视频无法核对；为保持浏览界面，未跳转到视频详情页",
            navigation: "preserved-immersive-feed",
            currentVideoId: currentId || null, targetVideoId: targetId || null };
        }
        if (!reusedCurrentPage) await loadPage(view, url, 2800);
        // 浮层里评论面板是收起的：点开再读，否则永远是 0 条。
        // 必须用主进程真实鼠标点击（脚本 click 会被 React 事件层忽略）。
        const readPanel = () => evaluate(view, require("./leadgen-read").COMMENT_OPEN_JS, 8000)
          .catch(() => null);
        // 浮层刚打开时图标还在滑入动画里，第一次取到的坐标可能点空。
        // 这里最多点两次，每次点完重新取坐标；仍打不开就按 0 条如实返回。
        await new Promise((r) => setTimeout(r, 600));
        let panelState = await readPanel();
        // 独立视频页的评论区在视频下方，列表可能还在渲染（首帧读到 list=null，
        // 会被当成「面板没开、图标也不可见」→ 白跑一次整页重载）。等最多 2s 再判。
        for (let w = 0; w < 4 && panelState && !panelState.open && !panelState.click; w++) {
          await new Promise((r) => setTimeout(r, 500));
          panelState = await readPanel();
        }
        for (let attempt = 0; attempt < 2 && panelState && !panelState.open; attempt++) {
          if (!panelState.click) break;
          try { await clickPoint(view, panelState.click.x, panelState.click.y, { wait: 2000 }); }
          catch (_) { break; }
          await new Promise((r) => setTimeout(r, 700));
          panelState = await readPanel();
        }
        // 浮层已经被顶掉（评论 DOM 还在但不可见、评论图标也点不到）：
        // 重新打开这条视频，再按「点图标」的流程走一遍。不重来一次的话
        // 会读到那几条残留 DOM，把「面板没开」当成「这条视频只有 2 条评论」。
        if (panelState && !panelState.open && !panelState.click) {
          // 浮层场景（地址还是 /search/...）：只把页面滚回顶部等图标出现，
          // 绝不 loadPage —— 重新加载等于「又把目标视频网址输一遍」，
          // 正是用户明确否掉的做法。
          const isOverlay = /\/search\//.test(String(view.webContents.getURL() || ""));
          if (isOverlay) {
            await evaluate(view, "window.scrollTo(0,0);document.documentElement.scrollTop=0;true", 5000)
              .catch(() => false);
            for (let w = 0; w < 6 && panelState && !panelState.open && !panelState.click; w++) {
              await new Promise((r) => setTimeout(r, 500));
              panelState = await readPanel();
            }
            if (panelState && !panelState.open && panelState.click) {
              try { await clickPoint(view, panelState.click.x, panelState.click.y, { wait: 2000 }); }
              catch (_) {}
              await new Promise((r) => setTimeout(r, 700));
              panelState = await readPanel();
            }
          }
          // 真读不到（浮层已被顶掉 / 图标就是不出来）：详情页可按地址恢复，
          // 但沉浸流必须留在当前页面，不能悄悄跳成视频详情页。
          if (!immersiveFeed && panelState && !panelState.open && !panelState.click) {
            await loadPage(view, url, 2800);
            await new Promise((r) => setTimeout(r, 800));
            panelState = await readPanel();
            if (panelState && !panelState.open && panelState.click) {
              try { await clickPoint(view, panelState.click.x, panelState.click.y, { wait: 2000 }); }
              catch (_) {}
              await new Promise((r) => setTimeout(r, 700));
              panelState = await readPanel();
            }
          }
        }
        if (immersiveFeed && panelState && !panelState.open) {
          return { ok: false, comments: out, diag,
            error: "沉浸式视频流内未能确认评论区已展开；为保持当前页面，未跳转到视频详情页",
            navigation: "preserved-immersive-feed",
            currentVideoId: currentId || null, targetVideoId: targetId || null };
        }
        if (panelState) diag[targetId || url] = { ...(diag[targetId || url] || {}),
          panel: { open: !!panelState.open, items: panelState.items,
                   visibleItems: panelState.visibleItems, note: panelState.note || "",
                   click: panelState.click || null } };
        // 90s：人式翻阅要「滚一段停一下、每三屏多停一会、再分段往上翻」，
        // 60 条上限时实测约 30s，留够余量给慢网络下的懒加载。
        let page = await evaluate(view, commentExtractScript({ ...cmd, limit }), 90000);
        // 兜底：窗口明明 show 了但被别的窗口盖住（远程桌面 / 息屏唤醒常见），
        // Chromium 仍判 hidden，懒加载照样不触发。这时短暂真显示 + 置顶逼它认一帧，
        // 重读一次；置顶立即还回去，不挡用户。
        if (page && page.diag && page.diag.vis === "hidden") {
          try { if (typeof view.showForCheck === "function") view.showForCheck(); } catch (_) {}
          await new Promise((r) => setTimeout(r, 450));
          try { if (typeof view.releaseTopmost === "function") view.releaseTopmost(); } catch (_) {}
          page = await evaluate(view, commentExtractScript({ ...cmd, limit }), 90000);
        }
        if (immersiveFeed) {
          const panelAfterRead = await readPanel();
          if (panelAfterRead && panelAfterRead.open && panelAfterRead.toggle) {
            try { await clickPoint(view, panelAfterRead.toggle.x, panelAfterRead.toggle.y, { wait: 900 }); }
            catch (_) { /* 下一步会再次核对评论面板和视频身份 */ }
          }
        }
        const videoId = targetId;
        diag[videoId || url] = { ...(page.diag || {}), ...(diag[videoId || url] || {}),
          navigation: reusedCurrentPage ? "reused-current-video" : "loaded-target-video",
          currentVideoId: currentId || null };
        if (page.loginWall) return { ok: false, error: "抖音账号未登录（检测到登录墙）", loginWall: true };
        for (const c of page.comments || []) {
          out.push({ ...c, videoId, videoUrl: url, caption: page.caption || "" });
          if (out.length >= limit) break;
        }
        if (out.length >= limit) break;
        await new Promise((r) => setTimeout(r, 900));
      }
    } finally {
      if (shown && typeof view.hideQuiet === "function") { try { view.hideQuiet(); } catch (_) {} }
    }
    return { ok: true, comments: out, diag,
             note: out.length ? "" : "视频页没读到评论：评论区可能未展开或选择器需按当前页面校准" };
  }, cmd.accountId);
}

/* ----------------------------------------- 人式浏览：点封面进视频 / 点「下一个」 */
// 2026-11-19 用户口径：正常人是「输关键词 → 点进一条视频看评论区 → 留言 → 点下一个」，
// 不是每条都把目标视频网址重新输一遍；同一个视频也不该被反复加载。
// 所以这里只做两件事：点搜索结果里的封面进视频、点平台自己的「下一个」切换控件。
// 不构造推荐流、不点右侧「相关推荐」（那会把页面带去跟关键词无关的内容）。
// 状态探针（认画面里正在播的那条）在 leadgen-read.js 里，和视频元数据读法同源。

/** 点「下一个」：切换成功后回新视频的地址/标题；地址跟不跟变只作诊断 */
async function nextVideo(cmd) {
  const { VIDEO_STATE_JS, COMMENT_OPEN_JS } = require("./leadgen-read");
  return withView(1200, async (view) => {
    const readState = () => evaluate(view, VIDEO_STATE_JS, 10000).catch(() => null);
    const waitForVideo = async (previousId, attempts = 10) => {
      let state = null;
      for (let i = 0; i < attempts; i++) {
        if (!liveViews.has(view)) return { stopped: true };
        await new Promise((resolve) => setTimeout(resolve, 300));
        state = await readState();
        if (state && state.verify) return { verify: true, state };
        if (state && state.id && (!previousId || state.id !== previousId)) return { state };
      }
      return { state };
    };
    const scrollFeed = async (strategy = "wheel") => {
      const shown = typeof view.isShown === "function" ? !!view.isShown() : false;
      const temporarilyShown = !shown && typeof view.showQuiet === "function" && !!view.showQuiet();
      try {
        if (strategy !== "wheel") {
          const keyCode = strategy === "page-down" ? "PAGEDOWN" : "DOWN";
          view.webContents.sendInputEvent({ type: "keyDown", keyCode });
          view.webContents.sendInputEvent({ type: "keyUp", keyCode });
          return { temporarilyShown };
        }
        const scale = await viewScale(view);
        const vp = scale && scale.vp;
        if (!vp || !view.webContents || typeof view.webContents.sendInputEvent !== "function") {
          if (temporarilyShown && typeof view.hideQuiet === "function") view.hideQuiet();
          return null;
        }
        view.webContents.sendInputEvent({ type: "mouseWheel",
          x: Math.round(vp.w * 0.48 * scale.x), y: Math.round(vp.h * 0.5 * scale.y),
          deltaY: Math.round(vp.h * 0.72), wheelTicksY: 4, canScroll: true });
        return { temporarilyShown };
      } catch (_) {
        if (temporarilyShown && typeof view.hideQuiet === "function") view.hideQuiet();
        return null;
      }
    };
    const recoverNonVideo = async (initial) => {
      if (!cmd.feedRecovery) return { ok: false, nonVideo: true,
        error: "当前不在视频页，未启用信息流恢复" , diag: initial };
      if (!initial || initial.verify) return { ok: false, needVerify: !!(initial && initial.verify),
        nonVideo: true, error: initial && initial.verify ? "当前页需要人工验证" : "当前页面读不到状态",
        diag: initial };

      let state = initial;
      const onDouyin = /^https?:\/\/(?:www\.)?douyin\.com\//i.test(String(state.url || ""))
        || /^https?:\/\/live\.douyin\.com\//i.test(String(state.url || ""));
      if (!onDouyin) return { ok: false, nonVideo: true, error: "当前页面不是抖音信息流，停止恢复",
        diag: state };

      // 直播间常作为覆盖页打开：先按平台常规返回键关闭覆盖，再回信息流跳过直播卡片。
      if (state.liveRoom) {
        try {
          view.webContents.sendInputEvent({ type: "keyDown", keyCode: "ESC" });
          view.webContents.sendInputEvent({ type: "keyUp", keyCode: "ESC" });
        } catch (_) { /* 后续仍尝试页面内恢复 */ }
        const returned = await waitForVideo("", 6);
        if (returned.stopped) return { ok: false, stopped: true, error: "已停止" };
        if (returned.verify) return { ok: false, needVerify: true, nonVideo: true,
          error: "退出直播间时遇到人工验证", diag: returned.state };
        if (returned.state && returned.state.id) {
          return { ok: true, videoId: String(returned.state.id),
            videoUrl: "https://www.douyin.com/video/" + returned.state.id,
            navigation: "live-room-return", recoveredFromNonVideo: true,
            title: returned.state.title || "", author: returned.state.author || "",
            publishedText: returned.state.publishedText || "", url: returned.state.url,
            diag: { recoveredFrom: "live-room", afterId: returned.state.id } };
        }
        state = returned.state || state;
      }

      // 非视频卡片/直播页的恢复分层尝试滚轮、方向键和整页翻动；
      // 每步都必须重新核验视频编号，未核验成功就交由后端退避后继续恢复。
      const strategies = ["wheel", "wheel", "wheel", "down", "page-down", "wheel"];
      for (let attempt = 0; attempt < strategies.length; attempt++) {
        if (!liveViews.has(view)) return { ok: false, stopped: true, error: "已停止" };
        const strategy = strategies[attempt];
        const scroll = await scrollFeed(strategy);
        if (!scroll) return { ok: false, nonVideo: true,
          error: "检测到非视频内容，但无法滚动信息流", diag: state };
        let moved;
        try { moved = await waitForVideo("", 8); }
        finally {
          if (scroll.temporarilyShown && typeof view.hideQuiet === "function") view.hideQuiet();
        }
        if (moved.stopped) return { ok: false, stopped: true, error: "已停止" };
        if (moved.verify) return { ok: false, needVerify: true, nonVideo: true,
          error: "跳过非视频内容时遇到人工验证", diag: moved.state };
        if (moved.state && moved.state.id) {
          return { ok: true, videoId: String(moved.state.id),
            videoUrl: "https://www.douyin.com/video/" + moved.state.id,
            navigation: "non-video-recovery-" + strategy, recoveredFromNonVideo: true,
            title: moved.state.title || "", author: moved.state.author || "",
            publishedText: moved.state.publishedText || "", url: moved.state.url,
            diag: { recoveredFrom: state.liveRoom ? "live-room" : "non-video-feed-item",
              strategy, attempts: attempt + 1, afterId: moved.state.id } };
        }
        state = moved.state || state;
      }
      return { ok: false, nonVideo: true, error: "连续跳过 3 屏仍未发现可核验的视频",
        diag: state };
    };
    let before = await evaluate(view, VIDEO_STATE_JS, 12000).catch(() => null);
    if (!before) return { ok: false, error: "当前页面读不到状态，未点「下一个」" };
    if (before.verify) return { ok: false, needVerify: true, error: "当前页需要人工验证", diag: before };
    if (!before.id) return recoverNonVideo(before);
    if (before.immersiveFeed) {
      const panel = await evaluate(view, COMMENT_OPEN_JS, 8000).catch(() => null);
      if (panel && panel.open) {
        if (!panel.toggle) return { ok: false, error: "评论面板仍打开但找不到收起入口，未滚动视频流", diag: before };
        try { await clickPoint(view, panel.toggle.x, panel.toggle.y, { wait: 800 }); }
        catch (_) { return { ok: false, error: "评论面板未能收起，未滚动视频流", diag: before }; }
        before = await evaluate(view, VIDEO_STATE_JS, 10000).catch(() => null);
        if (!before || before.verify || !before.immersiveFeed || !before.id) {
          return { ok: false, error: "收起评论区后无法确认仍在沉浸式视频流", diag: before };
        }
      }
    }
    let clicked = false;
    if (before.hasArrow) clicked = await evaluate(view, `(() => {
      const a = document.querySelector('[data-e2e="video-switch-next-arrow"]');
      if (!a) return false;
      try { a.click(); return true; } catch (e) { return false; }
    })()`, 8000).catch(() => false);
    let scrolled = false;
    let temporarilyShown = false;
    if (!clicked && before.immersiveFeed) {
      const watchable = typeof view.isShown === "function" ? !!view.isShown() : false;
      temporarilyShown = !watchable && typeof view.showQuiet === "function" && !!view.showQuiet();
      const scale = await viewScale(view);
      const vp = scale && scale.vp;
      if (!vp || !view.webContents || typeof view.webContents.sendInputEvent !== "function") {
        if (temporarilyShown && typeof view.hideQuiet === "function") view.hideQuiet();
        return { ok: false, error: "无法向沉浸式视频流发送滚动操作", diag: before };
      }
      try {
        view.webContents.sendInputEvent({ type: "mouseWheel",
          x: Math.round(vp.w * 0.48 * scale.x), y: Math.round(vp.h * 0.5 * scale.y),
          deltaY: Math.round(vp.h * 0.72), wheelTicksY: 4, canScroll: true });
        scrolled = true;
      } catch (error) {
        if (temporarilyShown && typeof view.hideQuiet === "function") view.hideQuiet();
        return { ok: false, error: "沉浸式视频流滚动失败：" + String(error && error.message || error), diag: before };
      }
    }
    if (!clicked && !scrolled) return { ok: false, error: "当前页没有可用的视频流切换控件", diag: before };
    let after = null;
    let changed = false;
    try {
      for (let i = 0; i < 40; i++) {
        if (!liveViews.has(view)) return { ok: false, stopped: true, error: "已停止" };
        await new Promise((r) => setTimeout(r, 300));
        after = await evaluate(view, VIDEO_STATE_JS, 10000).catch(() => null);
        if (after && after.verify) return { ok: false, needVerify: true, error: "切换时遇到人工验证", diag: after };
        if (after && after.id && after.id !== before.id) { changed = true; break; }
      }
    } finally {
      if (temporarilyShown && typeof view.hideQuiet === "function") view.hideQuiet();
    }
    if (!changed) return { ok: false, error: "切换后未检测到新视频", diag: { before, after } };
    // 画面先换、标题后到：真机上刚切过去时 desc 还是上一条的文案（2026-11-19 实测），
    // 直接读会把上一条标题记到下一条头上。多等几拍，等标题真的变了再回。
    let settled = after;
    for (let i = 0; i < 6; i++) {
      const now = String((settled && settled.title) || "").trim();
      if (now && now !== String(before.title || "").trim()) break;
      await new Promise((r) => setTimeout(r, 350));
      const next = await evaluate(view, VIDEO_STATE_JS, 8000).catch(() => null);
      if (next && next.id === after.id) settled = next;
    }
    const id = String(after.id);
    const urlChanged = String(after.urlId || "") === id;
    return { ok: true, videoId: id, videoUrl: "https://www.douyin.com/video/" + id,
             navigation: scrolled ? "feed-wheel" : "next-arrow",
             title: (settled && settled.title) || "", author: (settled && settled.author) || "",
             isAd: !!(settled && settled.isAd),
             publishedText: (settled && settled.publishedText) || "",
             url: after.url, urlChanged,
             diag: { beforeId: before.id, afterId: id, urlChanged,
                     domChanged: !!after.domId && after.domId !== before.domId,
                     titleSettled: String((settled && settled.title) || "") !== String(before.title || "") } };
  }, cmd.accountId);
}

/**
 * 进入指定视频：当前页已经是它就直接复用；否则先在搜索结果里点它的封面
 * （人点卡片），点不到再退回按地址打开。两种情况都不会重复加载同一个视频。
 */
async function openVideo(cmd) {
  const { VIDEO_STATE_JS } = require("./leadgen-read");
  const status = /\/video\/(\d{6,})/.exec(String(cmd.videoUrl || ""));
  if (!status) return { ok: false, error: "视频地址无效" };
  const id = status[1];
  return withView(1500, async (view) => {
    const cur = await evaluate(view, VIDEO_STATE_JS, 10000).catch(() => null);
    if (cur && cur.verify) return { ok: false, needVerify: true, error: "当前页需要人工验证", diag: cur };
    // 只剩残留 DOM 时（浮层已关、节点宽高 0）不能算「已经在这条视频上」，
    // 否则直接返回 reused，评论面板永远点不开。
    if (cur && cur.id === id && cur.stageShown !== false) {
      return { ok: true, videoId: id, videoUrl: cmd.videoUrl, mode: "reused-current-video",
               isAd: cur.isAd === true, diag: { url: cur.url } };
    }
    if (cur && cur.id === id && cur.stageShown === false) {
      // 浮层被关掉了：按地址重新打开这条视频（最稳，等同于用户点进来）
      await loadPage(view, cmd.videoUrl, 2800);
      const back = await evaluate(view, VIDEO_STATE_JS, 10000).catch(() => null);
      return { ok: !!(back && back.id === id && back.stageShown !== false), videoId: id,
               videoUrl: cmd.videoUrl, mode: "reopened-after-close",
               title: (back && back.title) || "", isAd: !!(back && back.isAd),
               url: (back && back.url) || "" };
    }
    // 搜索结果页：点这条视频的封面进去（相当于用户手点），不用重新输网址。
    const clicked = await evaluate(view, `(() => {
      const links = Array.from(document.querySelectorAll('a[href*="/video/${id}"]'))
        .filter(e => e.offsetParent !== null);
      if (!links.length) return false;
      const card = links[0];
      card.scrollIntoView({ block: 'center' });
      try { card.click(); return true; } catch (e) { return false; }
    })()`, 8000).catch(() => false);
    if (clicked) {
      for (let i = 0; i < 30; i++) {
        if (!liveViews.has(view)) return { ok: false, stopped: true, error: "已停止" };
        await new Promise((r) => setTimeout(r, 300));
        const now = await evaluate(view, VIDEO_STATE_JS, 10000).catch(() => null);
        if (now && now.verify) return { ok: false, needVerify: true, error: "进入视频时遇到人工验证" };
        if (now && now.id === id) {
          return { ok: true, videoId: id, videoUrl: cmd.videoUrl, mode: "clicked-card",
                   title: now.title || "", isAd: now.isAd === true, url: now.url };
        }
      }
    }
    if (cmd.preserveSearch) {
      return { ok: false, videoId: id, videoUrl: cmd.videoUrl,
        error: "关键词结果页未找到可核验的视频卡片；为保留搜索任务，未打开视频详情地址" };
    }
    // 点不到（不在搜索结果页 / 卡片没渲染）：按地址打开，保证任务不中断。
    await loadPage(view, cmd.videoUrl, 2800);
    const after = await evaluate(view, VIDEO_STATE_JS, 10000).catch(() => null);
    return { ok: !!(after && after.id === id), videoId: id, videoUrl: cmd.videoUrl,
             mode: "loaded-url", url: (after && after.url) || "",
             isAd: !!(after && after.isAd),
             error: after && after.id === id ? undefined : "打开后未确认停在目标视频" };
  }, cmd.accountId);
}

async function inspectLeadgenVideo(cmd) {
  const {VIDEO_METADATA_JS,VIDEO_STATE_JS,verifiedPage}=require('./leadgen-read');
  if(!/^https:\/\/www\.douyin\.com\/video\/\d{6,}$/.test(cmd.videoUrl||''))
    return {ok:false,error:'视频地址无效'};
  return withView(1000,async view=>{
    const {createCommandControl}=require('./command-control');
    const control=createCommandControl({
      check:()=>request('GET','/desktop/command-status/'+encodeURIComponent(cmd.id||'')),
      interrupt:async()=>{
        liveViews.delete(view);
        try{view.webContents.stop();}catch(_){}
        await evaluate(view,"document.querySelector('video')?.pause()",1500).catch(()=>{});
      }
    });
    const wait=p=>control.wait(p);
    try {
    // 不重复加载同一个视频：点「下一个」进来、刚读完评论区、或刚执行完动作时，
    // 当前页已经停在目标视频上，再 loadURL 一次只会整页重刷（用户看到的就是
    // 「同一个视频反复加载」）。只有真不在目标页时才导航。
    const targetId=/\/video\/(\d{6,})/.exec(String(cmd.videoUrl||''))?.[1]||'';
    const hereState=await evaluate(view,VIDEO_STATE_JS,8000).catch(()=>null);
    const hereId=String((hereState&&hereState.id)||'');
    const reused=!!targetId&&targetId===hereId;
    if(!reused)await wait(loadPage(view,cmd.videoUrl,2500));
    let item;
    let tries=0;
    for(let i=0;i<20&&liveViews.has(view);i++){
      tries++;
      item=await wait(evaluate(view,VIDEO_METADATA_JS,8000));
      if(item?.verify)return {ok:false,needVerify:true,error:'视频页需要人工验证',diag:{tries}};
      if(item?.loginWall)return {ok:false,error:'账号未登录',diag:{tries}};
      if(verifiedPage(item,cmd.videoUrl)&&item.title)break;
      await wait(new Promise(resolve=>setTimeout(resolve,500)));
    }
    // 回执必须带诊断：以前「页面打开了但读不到标题」只回 ok + 空 title，
    // 后端只能说「未读到视频标题」，用户根本不知道是页面没渲染完还是选择器变了。
    const meta={tries,titleMissing:!String(item?.title||'').trim(),titleSource:String(item?.titleSource||''),
      navigation:reused?'reused-current-video':'loaded-target-video'};
    if(!verifiedPage(item,cmd.videoUrl))
      return {ok:false,error:'未能核对目标视频',
        diag:{...meta,pageVerified:false,verify:!!item?.verify,loginWall:!!item?.loginWall}};
    if(cmd.type==='watch-video'){
      const seconds=Number(cmd.seconds);
      if(!Number.isFinite(seconds)||seconds<0||seconds>3600)return {ok:false,error:'停留时间无效'};
      control.check();
      await wait(evaluate(view,"document.querySelector('video')?.play().catch(()=>{})",8000));
      const start=Date.now();
      while(Date.now()-start<seconds*1000){
        if(!liveViews.has(view))return {ok:false,stopped:true,error:'视频停留已停止'};
        await wait(new Promise(resolve=>setTimeout(resolve,100)));
      }
      item=await wait(evaluate(view,VIDEO_METADATA_JS,8000));
      if(!verifiedPage(item,cmd.videoUrl)||!item.playing)
        return {ok:false,error:'未确认目标视频正在播放',diag:{...meta,verify:!!item?.verify}};
    }
    return {ok:true,item,diag:{...meta,pageVerified:true}};
    }catch(error){
      if(error?.code==='COMMAND_STOPPED')return {ok:false,stopped:true,error:error.message};
      throw error;
    }finally{control.close();}
  },cmd.accountId);
}

async function inspectLeadgenSocial(cmd) {
  return withView(1000,async view=>{
    const {createCommandControl}=require('./command-control');
    const control=createCommandControl({check:()=>request('GET','/desktop/command-status/'+encodeURIComponent(cmd.id||'')),
      interrupt:async()=>{liveViews.delete(view);try{view.webContents.stop();}catch(_){}}});
    const wait=p=>control.wait(p);
    const io={
      evaluate:code=>wait(evaluate(view,code,8000)),
      navigate:url=>wait(loadPage(view,url,1800)),
      sleep:ms=>wait(new Promise(resolve=>setTimeout(resolve,ms))),
      stopped:()=>!liveViews.has(view),
      readComments:()=>wait(evaluate(view,commentExtractScript(cmd),90000)),
      hasDraft:()=>view.webContents.getURL()==='about:blank'?false:wait(evaluate(view,
        "Array.from(document.querySelectorAll('[contenteditable=true],textarea')).some(e=>(e.value||e.innerText||'').trim()) || Array.from(document.querySelectorAll('input[type=file]')).some(e=>e.files?.length>0)",8000)),
      click:async(x,y)=>{
        control.check();const sc=await viewScale(view);control.check();
        const point={x:Math.round(x*sc.x),y:Math.round(y*sc.y),button:'left',clickCount:1};
        for(const type of ['mouseMove','mouseDown','mouseUp'])view.webContents.sendInputEvent({type,...point});
      },
      type:text=>{control.check();return view.webContents.insertText(text);},
      key:async(keyCode,modifiers=[])=>{control.check();for(const type of ['keyDown','keyUp'])view.webContents.sendInputEvent({type,keyCode,modifiers});}
    };
    try {
      const social=require('./live-social');
      return cmd.type==='inspect-profile'?await social.inspectProfile(cmd,io):cmd.type==='inspect-comment-extras'?
        await require('./live-comment-extras').inspectExtras(cmd,io):await social.inspectShareTargets(cmd,io);
    }catch(error){if(error?.code==='COMMAND_STOPPED')return {ok:false,stopped:true,error:'已停止准备'};throw error;}
    finally{control.close();}
  },cmd.accountId);
}

/** 作者主页最近作品（监听主播） */
async function readAuthorWorks(cmd) {
  const urls = Array.isArray(cmd.authorUrls) ? cmd.authorUrls
    : (cmd.authorUrl ? [cmd.authorUrl] : []);
  const limit = Math.max(1, Math.min(parseInt(cmd.limit, 10) || 12, 24));
  if (!urls.length) return { ok: false, error: "指令缺少作者主页地址" };
  return withView(3200, async (view) => {
    const out = [];
    let author = "";
    for (const url of urls.slice(0, 6)) {
      if (!liveViews.has(view)) break;
      await loadPage(view, url, 2800);
      const page = await evaluate(view, authorWorksScript(cmd), 45000);
      if (page.loginWall) return { ok: false, error: "抖音账号未登录（检测到登录墙）", loginWall: true };
      author = author || page.author || "";
      for (const it of page.items || []) {
        out.push({ ...it, author, authorUrl: url });
        if (out.length >= limit) return { ok: true, items: out, author };
      }
    }
    return { ok: true, items: out, author, note: out.length ? "" : "主页未解析到作品" };
  }, cmd.accountId);
}

/* ------------------------------------------------- 互动通知（只读） */
/**
 * 抖音网页版没有独立的互动通知页（/notifications 实测 404），互动通知是顶栏
 * 一个弹层。真机实测（2026-11-17，账号 1794886618996）弹层里有三类：
 *   1. 「<昵称> 赞了你的作品」——多人赞会聚合成「等 N 人赞了你的作品」
 *   2. 「<昵称> 关注了你」，行内直接带「回关」按钮
 *   3. 「<昵称> 赞了你的评论」
 * 这三类正好是「作品点赞 / 新增关注」两个自动回应策略的事件源。
 *
 * 这个通道只读：打开自己的主页 → 点顶栏「通知」→ 把弹层里的行结构化返回。
 * 不点「回关」、不点任何写动作 —— 写动作由后端按签名作用域单独下发。
 *
 * 必须真实点击：弹层是 React 组件，脚本 dispatch 的 MouseEvent 不触发
 * （和精选页点赞同一个坑），要走 webContents.sendInputEvent。
 */
const NOTICE_BUTTON_JS = `(() => {
  const n = v => String(v || '').replace(/\\s+/g, ' ').trim();
  for (const el of Array.from(document.querySelectorAll('li,div,span'))) {
    if (n(el.innerText) !== '通知') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.width > 160 || r.top > 100) continue;   // 顶栏那颗，不是正文里出现的字
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  }
  return null;
})()`;

// 通知行文案 → 动作类型。顺序有意义：「等 N 人赞了你的作品」要先于「赞了你的作品」。
const NOTICE_KINDS = `[
  [/等\\\\s*\\\\d+\\\\s*人赞了你的作品/, 'like_work'],
  [/赞了你的作品/, 'like_work'],
  [/赞了你的评论/, 'like_comment'],
  [/回复了你的评论/, 'reply_comment'],
  [/评论了你的作品/, 'comment_work'],
  [/关注了你/, 'follow']
]`;

const NOTICE_LIST_JS = `(() => {
  const n = v => String(v || '').replace(/\\s+/g, ' ').trim();
  const KINDS = ${NOTICE_KINDS};
  const hasKind = t => KINDS.some(([re]) => re.test(t));
  // 时间必须「整段就是一个时间」，否则整行里任何含数字的片段都会被当成时间。
  const TIME_EXACT = /^\\s*(\\d{1,2}:\\d{2}|昨天|前天|\\d+天前|\\d+小时前|\\d+分钟前|星期[一二三四五六日天]|\\d{1,2}-\\d{1,2}|\\d{4}-\\d{1,2}-\\d{1,2})\\s*$/;
  const TIME_G = /(\\d{1,2}:\\d{2}|昨天|前天|\\d+天前|\\d+小时前|\\d+分钟前|星期[一二三四五六日天]|\\d{1,2}-\\d{1,2}|\\d{4}-\\d{1,2}-\\d{1,2})/g;
  // 昵称后面会粘关系标签：「XX 粉丝」「XX 在线」「XX 3小时前在线」，要剥掉。
  // 行尾还会粘按钮文案（「关注」「回关」…）：内容层爬升时按钮会把这几层带进来。
  const REL_TAG = /\\s*(粉丝|朋友|互相关注|回关你|正在直播|在线|刚刚|本周新关注|已关注|已请求|回关|关注|\\d+\\s*(?:小时|分钟)前在线)\\s*$/;
  // 通知行右侧的动作按钮：这些词整段出现在一个叶子上才算按钮，不是文案的一部分。
  const BTN_WORDS = ['回关', '关注', '已关注', '互相关注', '已请求'];
  const BTN_TAIL = /(?:^|\\s)(回关|关注|已关注|互相关注|已请求)$/;
  const body = n(document.body && document.body.innerText);
  if (/扫码登录|登录后查看/.test(body.slice(0, 3000))) return { loginWall: true, items: [] };

  const items = [];
  const seen = new Set();
  for (const el of Array.from(document.querySelectorAll('div'))) {
    const t = n(el.innerText);
    if (!t || t.length > 120 || !hasKind(t)) continue;
    // 只认「最小」容器：子元素里已经有同关键词的是外层壳，跳过
    if (Array.from(el.children || []).some(c => hasKind(n(c.innerText)))) continue;
    const hit = KINDS.find(([re]) => re.test(t));
    const kind = hit[1];

    // 内容层 = 昵称 + 文案 + 时间（不含右侧动作按钮）；整行 = 内容层再往上一层。
    // 实测层级：文案 → 内容层 → 内容层包装 → 整行(带按钮) → 列表项。
    // 只认「回关」会漏掉按钮显示「关注」的行（对方已关注我、我还没回关），
    // 那层里会把按钮文字吃进昵称 —— 实测出现过「XXX 关注」。
    let content = el;
    for (let i = 0; i < 3 && content.parentElement; i++) {
      const pt = n(content.parentElement.innerText);
      if (BTN_TAIL.test(pt) || pt.length > 120) break;
      content = content.parentElement;
    }
    // 整行只再往上一层（实测那层才带动作按钮）。再往上会吃到相邻行，
    // 甚至吃到「今天 / 昨天 / 更早」这类分组标题 —— 之前时间全变成「昨天」就是这个。
    let row = content;
    if (content.parentElement) {
      const pt = n(content.parentElement.innerText);
      if (pt.length <= 170) row = content.parentElement;
    }
    const r = row.getBoundingClientRect();
    if (r.width < 120) continue;                      // 侧栏里的字不算通知行

    // 按钮状态：回关 / 关注 → 还没关注对方，可以回关；已关注 / 互相关注 / 已请求 → 跳过。
    let followState = '';
    for (const node of Array.from(row.querySelectorAll('*'))) {
      if (node.children.length) continue;
      const nt = n(node.innerText);
      if (BTN_WORDS.includes(nt)) { followState = nt; break; }
    }
    const rowText = n(row.innerText);
    if (seen.has(rowText)) continue;
    seen.add(rowText);

    // 时间取「内容层里整段就是时间」的叶子：行内显示 18:18，分组标题才是「昨天」，
    // 在整行文本里 match 会把分组标题误当时间。
    let timeText = '';
    for (const node of Array.from(content.querySelectorAll('*'))) {
      if (node.children.length) continue;
      const nt = n(node.innerText);
      if (TIME_EXACT.test(nt)) { timeText = nt; break; }
    }
    // 昵称 = 内容层文本砍掉「文案 + 时间」
    let nickname = n(content.innerText)
      .replace(new RegExp(hit[0].source), ' ').replace(TIME_G, ' ')
      .replace(/\\s+/g, ' ').trim();
    for (let i = 0; i < 3; i++) {
      const stripped = nickname.replace(REL_TAG, '').trim();
      if (stripped === nickname) break;
      nickname = stripped;
    }
    // 聚合行「等 N 人赞了你的作品」前面挂的是最后一个人，保留但把前缀剥干净
    const aggregated = /等\\s*(\\d+)\\s*人/.exec(rowText);
    const img = row.querySelector('img');
    // 回关要落到「哪个人」，只有昵称不够（重名、改过名都对不上）。
    // 头像/昵称通常是 /user/<sec_uid> 的锚点，先认 href；退化再认 data-* 属性。
    // 取不到就留空 —— 空 uid 的回关一律不做，绝不按昵称猜人。
    const uidOf = (el) => {
      if (!el) return '';
      const link = el.matches && el.matches('a[href*="/user/"]') ? el : el.querySelector('a[href*="/user/"]');
      const href = link ? String(link.getAttribute('href') || link.href || '') : '';
      const hit = /\\/user\\/([A-Za-z0-9_-]{6,160})/.exec(href);
      if (hit) return hit[1];
      const attr = el.querySelector('[data-uid],[data-user-id],[data-sec-uid]');
      if (!attr) return '';
      const v = String(attr.getAttribute('data-uid') || attr.getAttribute('data-user-id')
        || attr.getAttribute('data-sec-uid') || '').trim();
      return /^[A-Za-z0-9_-]{6,160}$/.test(v) ? v : '';
    };
    const userId = uidOf(row) || uidOf(content);
    items.push({
      kind, nickname: nickname.slice(0, 40), text: rowText.slice(0, 160),
      timeText, userId, followState,
      aggregatedFrom: aggregated ? Number(aggregated[1]) : 0,
      // 「回关」「关注」两种按钮都表示「我还没关注他」；这两个字决定回关能不能做。
      canFollowBack: followState === '回关' || followState === '关注',
      hasAvatar: !!(img && img.src),
      y: Math.round(r.top),
      // 取不到 uid 时把结构线索带回去（只给标签名和类名前 18 字，不带正文），
      // 下次改版能一眼看出是「锚点没了」还是「换成 data 属性了」。
      diagUid: userId ? '' : Array.from(row.querySelectorAll('*')).slice(0, 40)
        .map(e => e.tagName.toLowerCase() + (e.getAttribute('href') ? '[href]' : '')
          + (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\\s+/)[0].slice(0, 18) : ''))
        .join(' '),
    });
  }
  items.sort((a, b) => a.y - b.y);
  return { items };
})()`;

// 弹层里的滚动容器：右侧、可纵向滚动、且里面装着通知行。
const NOTICE_SCROLL_JS = `(() => {
  let best = null;
  for (const d of Array.from(document.querySelectorAll('div,ul'))) {
    if (d.scrollHeight - d.clientHeight < 60) continue;
    const r = d.getBoundingClientRect();
    if (r.left < innerWidth * 0.45 || r.width < 200) continue;
    if (!best || d.scrollHeight > best.scrollHeight) best = d;
  }
  if (!best) return { found: false };
  best.scrollTop = best.scrollHeight;
  return { found: true, scrollHeight: best.scrollHeight, scrollTop: best.scrollTop };
})()`;

async function readInteractionNotices(cmd) {
  const limit = Math.max(1, Math.min(parseInt(cmd.limit, 10) || 40, 120));
  const scrolls = Math.max(0, Math.min(parseInt(cmd.scrolls, 10) || 0, 15));
  const url = String(cmd.url || "https://www.douyin.com/user/self");
  return withView(2500, async (view) => {
    // 弹层只在页面可见时渲染：隐藏窗口下连「通知」都点不开（document.hidden）。
    if (typeof view.showQuiet === "function") {
      try { view.showQuiet(); } catch (_) { /* 无窗口管理器时按原样尝试 */ }
      await new Promise((r) => setTimeout(r, 350));
    }
    await loadPage(view, url, 9000);
    const state = await evaluate(view, "document.visibilityState", 6000).catch(() => "");
    const button = await evaluate(view, NOTICE_BUTTON_JS, 8000).catch(() => null);
    if (!button) return { ok: false, error: "没找到顶栏「通知」入口（页面未登录或已改版）",
                          visibility: state };
    await clickPoint(view, button.x, button.y, { wait: 2200 });

    const out = [];
    let diag = {};
    for (let i = 0; i <= scrolls; i++) {
      if (!liveViews.has(view)) break;
      const page = await evaluate(view, NOTICE_LIST_JS, 15000).catch(() => null);
      if (page && page.loginWall) return { ok: false, error: "抖音账号未登录", loginWall: true };
      const items = (page && page.items) || [];
      diag = { ...diag, [i]: items.length };
      for (const it of items) {
        if (out.length >= limit) break;
        if (out.some((x) => x.text === it.text)) continue;
        out.push(it);
      }
      if (out.length >= limit || !scrolls) break;
      const scrolled = await evaluate(view, NOTICE_SCROLL_JS, 8000).catch(() => null);
      if (!scrolled || !scrolled.found) break;
      await new Promise((r) => setTimeout(r, 1500));
    }
    return { ok: true, items: out, diag, visibility: state,
             note: out.length ? "" : "通知弹层里没读到互动行（可能没有新通知）" };
  }, cmd.accountId);
}

/**
 * 只读探测精选页第一张卡片的中心坐标。
 *
 * 为什么点击不写在页面脚本里：抖音精选页的卡片是 div + img（整个页面没有
 * /video/ 锚点，真机实测），点击处理挂在 React 上，脚本合成的 MouseEvent
 * （isTrusted=false）会被直接忽略 —— 真机表现是「点了三下，URL 没变、
 * 也没有新窗口请求」，日志里只能报 no-like-after-click。真人点击必须走
 * Chromium 输入管道（webContents.sendInputEvent），所以这里只探坐标，
 * 点击交给 Node 侧 clickCardToDetail()。
 */
const CARD_PROBE_JS = `(() => {
  const sels = ['[class*="discover-video-card"]', '[class*="jingxuan-top-card"]',
                '[class*="discover"] img', 'div[class*="card"] img'];
  for (const s of sels) {
    for (const el of Array.from(document.querySelectorAll(s))) {
      const r = el.getBoundingClientRect();
      if (r.width < 120 || r.height < 80) continue;      // 缩略图/图标不算卡片
      if (r.bottom < 60 || r.top > window.innerHeight - 20) continue;  // 滚出视口的不点
      const hit = document.elementFromPoint(
        Math.round(r.left + r.width / 2), Math.round(r.top + Math.min(r.height / 2, 160)));
      const video = el.matches && el.matches('video') ? el : el.querySelector('video');
      const link = el.matches && el.matches('a') ? el : el.querySelector('a[href]');
      const clean = (v) => String(v || '').split('?')[0].slice(0, 300);
      // 精选页没有 /video/ 链接时，用 poster/currentSrc/文案组成稳定键。
      // Node 侧据此挡住同一视频的二次点击（二次点击会取消赞）。
      const key = clean(link && link.href) || clean(video && video.poster)
               || clean(video && video.currentSrc)
               || ('text:' + String(el.innerText || '').trim().slice(0, 160));
      return { found: true,
               x: Math.round(r.left + r.width / 2),
               y: Math.round(r.top + Math.min(r.height / 2, 160)),
               w: Math.round(r.width), h: Math.round(r.height), key,
               cls: String(el.className || '').slice(0, 60),
               // 命中测试：点在卡片上时上层元素应该是卡片自己；不是就说明被遮罩盖住了
               hitCls: hit ? String(hit.tagName + '.' + String(hit.className || '').slice(0, 50)) : '' };
    }
  }
  return { found: false };
})()`;

/**
 * CSS 像素 → 窗口 DIP 的换算系数。
 *
 * 为什么需要：Windows 显示缩放 150%/165% 时，页面里的 getBoundingClientRect 报的是
 * CSS px（实测 innerWidth=2115），而 webContents.sendInputEvent 收的是 DIP
 * （窗口只有 1280 宽）。直接把 CSS 坐标传进去 = 整体放大 1.65 倍，鼠标落在
 * 窗口外 —— 真机表现就是「卡片勉强点得开（偏差小）／点赞永远 click-no-effect
 * （按钮在右侧，偏出更多）」。
 */
async function viewScale(view) {
  try {
    const vp = await evaluate(view, "({ w: window.innerWidth || 1, h: window.innerHeight || 1 })",
      8000);
    const box = view.getContentBounds ? view.getContentBounds() : null;
    if (vp && box && vp.w > 1 && box.width > 1) {
      const sx = box.width / vp.w;
      const sy = box.height / vp.h;
      if (sx > 0.2 && sx < 5 && sy > 0.2 && sy < 5) return { x: sx, y: sy, vp, box };
    }
  } catch (e) { /* 拿不到就按 1:1 */ }
  return { x: 1, y: 1, vp: null, box: null };
}

/**
 * 在页面坐标 (x, y) 上做一次真实鼠标点击。
 *
 * 为什么不用脚本里的 element.click()：抖音是 React + 自研事件层，脚本合成的
 * MouseEvent（isTrusted=false）会被直接忽略 —— 精选页点赞踩过（点三下 URL 没变、
 * 也没有新窗口请求），顶栏「通知」弹层同样只认 Chromium 输入管道。
 * 前提是页面已经处于可见渲染状态（showQuiet），否则 Chromium 会丢掉可信事件。
 */
async function clickPoint(view, x, y, opts = {}) {
  const sc = (await viewScale(view)) || { x: 1, y: 1 };
  const px = Math.round(Number(x) * sc.x);
  const py = Math.round(Number(y) * sc.y);
  const fire = (type) => view.webContents.sendInputEvent(
    { type, x: px, y: py, button: "left", clickCount: 1 });
  fire("mouseMove");
  await new Promise((r) => setTimeout(r, 160));
  fire("mouseDown");
  await new Promise((r) => setTimeout(r, 80));
  fire("mouseUp");
  await new Promise((r) => setTimeout(r, Math.max(300, Number(opts.wait) || 1200)));
  return { x: px, y: py };
}

/** 在当前页面点一个赞，结果统一成 {note, liked, found, title, detail} */
/**
 * 点赞：先把采集页「安静地显示」出来，再走真实鼠标点击。
 *
 * 为什么必须这样：页面停在隐藏窗口里时 document.visibilityState=hidden，
 * Chromium 会丢弃 sendInputEvent 的可信鼠标事件（真机实测：按钮找对了、
 * 命中测试也过了，但页面 window/document 两级捕获计数都是 0/0/0）。
 * 历史成功过的点赞都是在采集页挂到可见宿主的时候。这里在点击前后临时激活、
 * 点完立刻收回，用户看不到窗口（移到屏幕外、不抢焦点）。
 */
async function likeOnCurrentPage(view) {
  const quiet = typeof view.showQuiet === "function" ? view.showQuiet() : false;
  try {
    return await likeOnCurrentPageInner(view);
  } finally {
    if (quiet && typeof view.hideQuiet === "function") {
      await new Promise((r) => setTimeout(r, 250));
      view.hideQuiet();
    }
  }
}

/**
 * 视频页写动作的「可点击帧」核验（monitor / 自动回复模式）。
 *
 * live-platform 里那道 ensureLivePreview 等的是审核台面板里渲染出首帧，
 * 直播预告和人工审核流程成立；自动回复没有那块面板，但底层约束一模一样：
 * 页面停在隐藏窗口里时 document.visibilityState=hidden，
 * Chromium 会丢弃 sendInputEvent 的可信鼠标事件（真机实测 0/0/0）。
 * 所以这里给一个等价核验：把采集页安静显示出来（屏幕内 + opacity 0 + 不抢焦点），
 * 再用双 requestAnimationFrame 确认页面真的渲染了一帧。两者都成立才算可点击。
 */
async function visibleForAction(view) {
  if (!view || (view.isDestroyed && view.isDestroyed())) return false;
  const shown = typeof view.showQuiet === "function" ? view.showQuiet() : false;
  if (!shown) { trace("可交互核验：采集页无法显示（showQuiet=false）"); return false; }
  try { view.webContents.focus?.(); } catch (_) { /* 焦点拿不到不算失败，看渲染结果 */ }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitPainted = async (rounds) => {
    let seen = "";
    for (let i = 0; i < rounds; i++) {
      const painted = await evaluate(view, `new Promise(resolve => {
        if (document.visibilityState !== 'visible' || document.readyState === 'loading')
          return resolve(document.visibilityState + '/' + document.readyState);
        let done = false;
        requestAnimationFrame(() => requestAnimationFrame(() => {
          done = true; resolve(document.visibilityState === 'visible' && document.readyState !== 'loading');
        }));
        setTimeout(() => { if (!done) resolve('rAF-timeout/' + document.visibilityState); }, 800);
      })`, 5000).catch((e) => "error:" + String((e && e.message) || e).slice(0, 80));
      if (painted === true) return { ok: true, seen };
      seen = String(painted);
      await sleep(150);
    }
    return { ok: false, seen };
  };
  const quiet = await waitPainted(10);
  if (quiet.ok) return true;
  // 安静显示（opacity 0）在部分环境下会被判成 occluded：窗口 show 着，
  // document.visibilityState 却是 hidden，可信鼠标事件也进不去。
  // 这一档把窗口真正显示出来再核验一次；动作完成后由 hideQuiet 收回。
  if (typeof view.showForCheck === "function" && view.showForCheck()) {
    // 窗口「刚被显示」和「DWM 真把这一帧呈现出来」之间有时间差：冷启动的账号窗
    // 第一条动作常常连 1.5s 都还是 hidden，同账号第二条就过（2026-11-18 真机复现）。
    // 所以这里先给系统一点呈现时间，再核验；还没过就再等一轮，最多 ~2.6s。
    await sleep(450);
    let loud = await waitPainted(10);
    if (!loud.ok) {
      await sleep(650);
      loud = await waitPainted(10);
    }
    if (loud.ok) {
      // 置顶只为过核验：过了就还回去，别让窗口挡在用户眼前。
      // 但**不能 hide** —— 页面重新隐藏会变回 hidden，后面的点击事件会被丢掉。
      if (typeof view.releaseTopmost === "function") {
        try { view.releaseTopmost(); } catch (_) { /* 忽略 */ }
      }
      trace("可交互核验：安静显示仍 hidden，已改用真显示通过");
      return true;
    }
    trace("可交互核验失败（真显示）：", loud.seen);
    if (typeof view.hideQuiet === "function") { try { view.hideQuiet(); } catch (_) {} }
    return false;
  }
  trace("可交互核验失败：", quiet.seen);
  return false;
}

async function likeOnCurrentPageInner(view) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const sc = await viewScale(view);   // CSS px → DIP，缩放屏上不换算就点在窗口外
  // 每轮结束都会把窗口收回隐藏，页面被暂停；重新可见后浏览器不会自动续播。
  // 暂停态的点赞处理器会直接短路（真机实测：事件已进页 1/1/1、按钮也命中，
  // 但平台没有任何回执），所以点之前先把这一屏放起来。
  try {
    const resume = await evaluate(view, `(() => {
      const v = document.querySelector('video');
      if (!v) return 'no-video';
      if (!v.paused) return 'playing';
      try { const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) { /* 忽略 */ }
      return 'resumed';
    })()`, 5000).catch(() => null);
    if (resume === "resumed") await sleep(900);
    // 自动播放策略可能把 play() 拦下来：静音后再试一次（采集页本来就不可见，静音无感）。
    const playing = await evaluate(view, `(() => {
      const v = document.querySelector('video');
      return v ? !v.paused : null;
    })()`, 4000).catch(() => null);
    if (playing === false) {
      await evaluate(view, `(() => {
        const v = document.querySelector('video');
        if (!v) return false;
        try { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) { /* 忽略 */ }
        return true;
      })()`, 4000).catch(() => null);
      await sleep(700);
    }
  } catch (e) { /* 续播失败不影响后续判定 */ }
  // 推荐流的操作栏（点赞/评论/收藏）是鼠标移入才展开的：不先 hover，
  // 探针只能摸到上一条视频残留的按钮（rect 在视口外），点过去什么都不会发生。
  try {
    const vw = (sc.vp && sc.vp.w) || 1280, vh = (sc.vp && sc.vp.h) || 900;
    const hover = (fx) => view.webContents.sendInputEvent({
      type: "mouseMove", x: Math.round(vw * fx * sc.x), y: Math.round(vh * 0.5 * sc.y),
    });
    hover(0.5);
    await sleep(450);
    hover(0.78);
    await sleep(350);
  } catch (e) { /* 悬停失败不影响后续判定 */ }
  let info = null;
  // 详情页/浮层首屏先出骨架，最多等 12s 再判定「没有按钮」
  for (let i = 0; i < 12; i++) {
    try {
      info = await evaluate(view, LIKE_PROBE_JS, 15000);
    } catch (e) {
      info = null;
    }
    if (info && info.found) break;
    await sleep(1000);
  }
  if (!info || !info.found) {
    // 带 count/rects/viewport：能直接看出是「页面上压根没有按钮」还是
    // 「有按钮但全落在视口外」，这两种修法完全不同。
    const brief = info ? { count: info.count, rects: info.rects,
                           viewport: info.viewport, e2e: info.e2e } : null;
    return { note: "no-button", liked: false, found: false, title: "",
             detail: JSON.stringify(brief || {}).slice(0, 300) };
  }
  if (info.already) {
    return { note: "already-liked", liked: false, found: true, title: info.title, detail: "" };
  }
  if (info.hitOk === false) {
    // 命中测试没过：按钮上方压着别的东西（播放器渐变层/暂停遮罩），这样点下去
    // 点在遮罩上，按钮收不到事件 —— 就是「click-no-effect」最常见的原因。
    // 先在画面上轻点一下把遮罩收掉，再重新探一次，别对着遮罩盲点。
    const yy = Math.max(60, info.y - 240);
    const ax = Math.round(info.x * sc.x), ay = Math.round(yy * sc.y);
    try {
      view.webContents.sendInputEvent({ type: "mouseMove", x: ax, y: ay,
                                        button: "left", clickCount: 1 });
      await sleep(140);
      view.webContents.sendInputEvent({ type: "mouseDown", x: ax, y: ay,
                                        button: "left", clickCount: 1 });
      view.webContents.sendInputEvent({ type: "mouseUp", x: ax, y: ay,
                                        button: "left", clickCount: 1 });
    } catch (e) { /* 忽略 */ }
    await sleep(900);
    const again = await evaluate(view, LIKE_PROBE_JS, 10000).catch(() => null);
    if (again && again.found) info = again;
  }
  // 接口监听先挂上（幂等）：DOM 不变的点赞只能靠回执确认
  await evaluate(view, DIGG_HOOK_JS, 8000).catch(() => null);
  const diggSeen = (await evaluate(view, "(window.__diggLog || []).length", 8000)
    .catch(() => 0)) || 0;
  const before = String(info.sig || "");
  const px = Math.round(Number(info.x || 0) * sc.x);
  const py = Math.round(Number(info.y || 0) * sc.y);
  trace("点赞点击:", `css=${info.x},${info.y}`, `dip=${px},${py}`,
        `scale=${sc.x.toFixed(3)}`, `vp=${sc.vp ? sc.vp.w + "x" + sc.vp.h : "-"}`,
        `box=${sc.box ? sc.box.width + "x" + sc.box.height : "-"}`);
  // 真实鼠标事件（isTrusted=true）：脚本合成 click 会被点赞处理器忽略
  const send = (type) => {
    try {
      view.webContents.sendInputEvent({ type, x: px, y: py,
                                        button: "left", clickCount: 1 });
    } catch (e) { /* 忽略 */ }
  };
  // 点之前清空事件探针：点完读它，就能分清「事件没进页面」还是「平台不认」。
  await evaluate(view, INPUT_SPY_JS, 8000).catch(() => null);
  send("mouseMove");
  await sleep(120);
  send("mouseDown");
  await sleep(60);
  send("mouseUp");
  let verdict = { ok: false, failed: false };
  for (let i = 0; i < 8; i++) {
    await sleep(500);
    let now = null;
    try { now = await evaluate(view, LIKE_PROBE_JS, 10000); } catch (e) { now = null; }
    if (now && now.found && now.already === true && info.already !== true
        && String(now.sig || "") !== before) {
      return { note: "liked", liked: true, found: true,
               title: String(now.title || ""), detail: "按钮已切换为已赞状态" };
    }
    const ent = await evaluate(view, "(window.__diggLog || []).slice(" + diggSeen + ")",
      8000).catch(() => null);
    verdict = classifyDiggReceipts(ent);
    if (verdict.ok) {
      return { note: "liked", liked: true, found: true, title: String(info.title || ""),
               detail: "点赞接口回执 status_code=0" };
    }
    if (verdict.failed) break;   // 平台明确拒了，再点也是白点
  }
  const why = verdict.failed
    ? `平台拒绝（status_code=${verdict.code}，HTTP ${verdict.status}）`
    : (verdict.note === "no-status-code" ? "有请求但没解析出结果" : "点击后计数/样式未变化");
  const evt = await evaluate(view, "window.__likeInput || null", 6000).catch(() => null);
  const evtText = evt
    ? `事件 win=${evt.wd}/${evt.wu}/${evt.wc} doc=${evt.dd}/${evt.du}/${evt.dc}` +
      ` vis=${evt.vis || "?"} 播放器=${evt.hasVideo ? (evt.paused ? "暂停" : "播放中") : "无视频"}` +
      `${evt.login ? " 登录框弹出" : ""}${evt.last ? " " + evt.last : ""}`
    : "事件 未读到";
  return { note: "click-no-effect", liked: false, found: true, title: info.title,
           detail: `${why}｜命中${info.hitOk === false
             ? "被遮挡:" + String(info.hitCls || "?") : "按钮"}｜${evtText}${Array.isArray(info.rect)
             ? "｜rect=" + info.rect.join(",") : ""}｜el=${String(info.html || "").slice(0, 90)}`
             .replace(/\s+/g, "") };
}

/**
 * 真实鼠标点开精选页的一张卡片，等详情层出现（判据：点赞控件出现）。
 *
 * 为什么必须 sendInputEvent：脚本合成 click 是 isTrusted=false，抖音的点击
 * 处理器不认（真机实测：URL 不变、无新窗口请求）。这里点一下真鼠标，
 * 再轮询 LIKE_PROBE_JS 直到操作栏出来。
 */
async function clickCardToDetail(view) {
  const cand = await evaluate(view, CARD_PROBE_JS, 15000).catch(() => null);
  if (!cand || !cand.found) return { clicked: false, likeReady: false, note: "no-card" };
  if (cand.key && warmupLiked.has(cand.key)) {
    trace("跳过重复视频:", String(cand.key).slice(0, 120));
    return { clicked: false, likeReady: false, note: "duplicate-card", key: cand.key };
  }
  const before = await evaluate(view, "location.href", 8000).catch(() => "");
  const sc = await viewScale(view);   // CSS px → DIP（缩放屏必换）
  const px = Math.round(cand.x * sc.x);
  const py = Math.round(cand.y * sc.y);
  const send = (type) => {
    try {
      view.webContents.sendInputEvent({ type, x: px, y: py,
                                        button: "left", clickCount: 1 });
    } catch (e) { /* 忽略 */ }
  };
  trace("点卡片进详情:", `css=${cand.x},${cand.y}`, `dip=${px},${py}`,
        `scale=${sc.x.toFixed(3)}`, `size=${cand.w}x${cand.h}`,
        `hit=${cand.hitCls || "-"}`, `cls=${cand.cls || "-"}`);
  send("mouseMove");
  await new Promise((r) => setTimeout(r, 160));
  send("mouseDown");
  await new Promise((r) => setTimeout(r, 90));
  send("mouseUp");
  let url = before;
  for (let i = 0; i < 14; i++) {
    await new Promise((r) => setTimeout(r, 600));
    let probe = null;
    try { probe = await evaluate(view, LIKE_PROBE_JS, 10000); } catch (e) { probe = null; }
    if (probe && probe.found) {
      const now = String(probe.url || url);
      // 把按钮的真实位置/候选数/视口一起打出来：点赞点不中时，凭这一行就能判断
      // 是「取了屏幕外那个」还是「被遮罩挡住」。
      trace("详情层已开:", `rect=${(probe.rect || []).join(",")}`,
            `候选=${probe.count}`, `vp=${(probe.viewport || []).join("x")}`,
            `命中=${probe.hitOk ? "按钮" : String(probe.hitCls || "?")}`);
      return { clicked: true, likeReady: true, url: now, changed: now !== before,
               note: "detail", hitCls: cand.hitCls || "", key: cand.key || "" };
    }
    const href = await evaluate(view, "location.href", 8000).catch(() => url);
    if (href) url = String(href);
  }
  return { clicked: true, likeReady: false, url, changed: url !== before,
           note: "no-like", hitCls: cand.hitCls || "", key: cand.key || "" };
}

/** 养号：真实浏览（滚动/停留），点赞受配额约束 */
async function warmupWatch(cmd) {
  const scrolls = Math.max(1, Math.min(parseInt(cmd.scrolls, 10) || 6, 20));
  const deadlineMs = Math.max(0, Number(cmd.deadlineMs) || 0);
  // 上限放到 30s：页面「停留 8-25 秒」是真实浏览时长，压到 15s 会让设置失真
  const dwell = Math.max(400, Math.min(parseInt(cmd.dwellMs, 10) || 1800, 30000));
  const likeMax = Math.max(0, Math.min(parseInt(cmd.likeMax, 10) || 0, 5));
  const likeProb = Math.max(0, Math.min(parseFloat(cmd.likeProbability) || 0, 100));
  return withView(4000, async (view) => {
    const { createCommandControl } = require("./command-control");
    const commandId = String(cmd.id || "offline-warmup");
    let reportedVideo = "";
    const control = createCommandControl({
      check: async () => {
        const status = await request("GET", "/desktop/command-status/" + encodeURIComponent(commandId));
        if (status?.cancelled) return status;
        if (!/^https:\/\//.test(view.webContents.getURL()) || view.webContents.isLoadingMainFrame()) return status;
        const progress = await evaluate(view, `(() => {
          const current = document.querySelector('[data-e2e="feed-active-video"]');
          const video = current && current.querySelector('video');
          if (window.__warmupCommandId !== ${JSON.stringify(commandId)} || window.__warmupStop ||
              !video || video.paused || video.readyState < 2 || video.currentTime < 0.1) return null;
          return {videoId:current.getAttribute('data-e2e-vid'), firstVideoAt:window.__warmupFirstVideoAt};
        })()`, 1500).catch(() => null);
        if (progress?.videoId && progress.videoId !== reportedVideo) {
          reportedVideo = progress.videoId;
          trace("养号进入视频:", commandId, progress.videoId, "firstVideoAt=" + progress.firstVideoAt);
          await request("POST", "/desktop/warmup-progress", {id:commandId, videoId:progress.videoId});
        }
        return status;
      },
      interrupt: async () => {
        trace("养号收到停止:", commandId);
        try { view.webContents.stop(); } catch (_) {}
        await evaluate(view, WARMUP_STOP_JS.replace('__COMMAND_ID__', JSON.stringify(commandId)), 1500).catch(() => {});
      },
    });
    try {
    const wait = pending => control.wait(pending);
    const url = cmd.url || "https://www.douyin.com/?recommend=1&from_nav=1";
    const ready = /^https:\/\//.test(view.webContents.getURL()) ? await wait(evaluate(view, `({
      url:location.href, feed:!!document.querySelector('[data-e2e="feed-active-video"], [data-e2e="feed-live"]')
    })`, 2500).catch(() => null)) : null;
    control.check();
    // Keep the actual feed between rounds. Reloading resets video playback and
    // used to send the preview back to the grid repeatedly.
    if (cmd.url ? ready?.url !== url : !ready?.feed) await wait(loadPage(view, url, 600));
    control.check();
    // 注入自检：先跑一句最小脚本。整段养号脚本一旦注入失败，Electron 只会回
    // 「Script failed to execute」，分不清是页面挂了还是脚本本身的问题。
    // 窗口刚建/正在加载时执行会超时，所以重试 3 次再下结论（别把「还没准备好」
    // 记成「养号失败」）。
    let pingOk = false;
    let pingErr = "";
    for (let attempt = 0; attempt < 3 && !pingOk; attempt++) {
      try {
        await wait(evaluate(view, "({ ping: 1, href: location.href })", 6000));
        pingOk = true;
      } catch (e) {
        control.check();
        pingErr = String((e && e.message) || e);
        await wait(new Promise((r) => setTimeout(r, 500)));
      }
    }
    if (!pingOk) {
      trace("注入自检失败:", pingErr, "| url=", String(view.webContents.getURL()).slice(0, 80));
      return { ok: false, error: "页面无法执行脚本：" + pingErr };
    }
    // 点赞接口监听：页面每次 loadPage 都会清空，所以每轮都在跑养号脚本前挂一次
    control.check();
    await wait(evaluate(view, `(() => {
      if (window.__warmupReleasePause) window.__warmupReleasePause();
      window.__warmupCommandId = ${JSON.stringify(commandId)};
      window.__warmupStop = false;
      window.__warmupFirstVideoAt = 0;
      window.__warmupProgress = {phase:'opening', startedAt:Date.now()};
      return true;
    })()`, 3000));
    await wait(evaluate(view, DIGG_HOOK_JS, 8000).catch((e) => {
      trace("点赞监听注入失败:", String((e && e.message) || e).slice(0, 100));
    }));
    const js = WARMUP_JS
      .replace("__SCROLLS__", String(scrolls))
      .replace("__DWELL__", String(dwell))
      .replace("__LIKE_MAX__", String(likeMax))
      .replace("__LIKE_PROB__", String(likeProb))
      .replace("__DEADLINE__", String(deadlineMs));
    const page = await wait(evaluate(view, js, Math.min(180000, scrolls * dwell + 30000)));
    control.check();
    if (page.stopped) return {ok:false,stopped:true,error:"已手动停止"};
    if (page.loginWall) return { ok: false, error: "抖音账号未登录（检测到登录墙）", loginWall: true };
    if (page.error) {
      trace("养号脚本报错:", page.error, page.stack || "");
      if (!page.actions?.length) return { ok: false, error: "页面脚本报错：" + page.error, url: page.url };
    }
    const acts = page.actions || [];
    let liked = page.liked || 0;
    let detailNote = "";
    let detailTitle = "";
    let likePath = "";
    // 写确认来自任务本身：用户在养号页设了点赞概率、点了开始，目标就是当前正在播放的这一屏。
    // 这里只拦「连执行账号都没有」的调用。以前用 rejectLiveAction 无条件拦 like-video，
    // 结果是这条路径整条变成死代码：概率 100% 也 0 赞，日志还只写「未尝试」，看不出是被拦了。
    const writesBlocked = !String(cmd.accountId || "").trim();
    if (!writesBlocked && (likeProb > 0 || likeMax > 0) && (page.needDetail || 0) > 0 && liked === 0
        && (!deadlineMs || Date.now() + 25000 < deadlineMs)) {
      let out = null;
      // 首选在当前信息流这一屏直接点：推荐流点赞按钮就在右侧操作栏、已在视口内，
      // 少一次跳转也就少一次被详情浮层/骨架挡住的机会。点不动才走详情页。
      try { out = await likeOnCurrentPage(view); } catch (e) { out = null; }
      if (out && out.found) {
        likePath = "feed";
        trace("信息流点赞:", page.url || "", "→", out.note, out.detail || "");
      } else {
        out = null;
        const candidates = (page.videoLinks || []).filter(u => !warmupLiked.has(u));
        if (candidates[0]) {
          // 有直链：直接打开详情页
          try {
            await loadPage(view, candidates[0], 2600);
            out = await likeOnCurrentPage(view);
            if (out.liked || out.found) warmupLiked.add(candidates[0]);
            likePath = "detail";
            trace("详情页点赞:", candidates[0], "→", out.note, out.detail || "");
          } catch (e) {
            detailNote = "detail-error:" + String((e && e.message) || e).slice(0, 60);
          }
        } else {
          // 精选页是单页应用，卡片没有 /video/ 链接 —— 用真实鼠标点开卡片
          // （浮层/详情页），操作栏出来后在同一页把赞点掉。
          const card = await clickCardToDetail(view);
          if (card && card.likeReady) {
            out = await likeOnCurrentPage(view);
            if (card.key && (out.liked || out.found)) warmupLiked.add(card.key);
            likePath = "detail";
            trace("点卡片进详情点赞:", card.url || "", "→", out.note, out.detail || "");
          } else {
            detailNote = card && card.clicked ? "no-like-after-click" : "no-video-link";
            trace("点卡片未进详情:", JSON.stringify(card || {}).slice(0, 200));
          }
        }
      }
      if (out) {
        detailNote = out.note;
        detailTitle = out.title;
        if (out.liked) liked += 1;
      }
      // 把「掷中了但没点成」的那一屏标成真实结果，后端统计才不是「无点赞按钮」一堆。
      const idx = acts.findIndex(a => a.like === "need-detail");
      if (idx >= 0) {
        acts[idx] = { ...acts[idx],
          like: liked > 0
            ? (likePath === "feed" ? "liked" : "liked-in-detail")
            : (detailNote || "no-button"),
          caption: detailTitle || acts[idx].caption };
      }
    }
    for (const act of acts) {
      if (act.like === "need-detail") act.like = writesBlocked ? "write-blocked" : "not-attempted";
    }
    return { ok: true, actions: acts, liked, likeProbability: likeProb,
             likePath: likePath || null,
             ...(page.error ? {browseError:page.error} : {}),
             detailLike: detailNote || null, probe: page.probe || null, url: page.url };
    } catch (error) {
      if (error?.code === "COMMAND_STOPPED") return {ok:false,stopped:true,error:error.message};
      throw error;
    } finally { control.close(); }
  }, cmd.accountId);
}

/**
 * 跑一条直播间的写动作（点赞 / 关注主播 / 抽奖 / 关注观众 / 私信观众）。
 *
 * 用直播采集那个「屏幕外可见窗口」：隐藏窗口里抖音不渲染聊天区，
 * 也就点不到弹幕昵称、点不动点赞。动作前后的状态由 live-room.js 自己复核。
 */
async function runLiveRoomAction(cmd) {
  const liveRoom = require("./live-room");
  const view = getLiveWindow(cmd.accountId);
  liveViews.add(view);
  try {
    await loadPage(view, cmd.videoUrl, 9000);
    await new Promise((r) => setTimeout(r, 2200));
    const scale = await viewScale(view);
    const io = {
      evaluate: (code) => evaluate(view, code, 15000),
      click: async (x, y) => {
        if (!liveViews.has(view) || view.isDestroyed()) throw new Error("直播动作已停止");
        const point = { x: Math.round(x * scale.x), y: Math.round(y * scale.y),
          button: "left", clickCount: 1 };
        for (const type of ["mouseMove", "mouseDown", "mouseUp"])
          view.webContents.sendInputEvent({ type, ...point });
      },
      dblclick: async (x, y) => {
        if (!liveViews.has(view) || view.isDestroyed()) throw new Error("直播动作已停止");
        const point = { x: Math.round(x * scale.x), y: Math.round(y * scale.y),
          button: "left", clickCount: 2 };
        for (const type of ["mouseMove", "mouseDown", "mouseUp"])
          view.webContents.sendInputEvent({ type, ...point });
      },
      hover: async (x, y) => {
        if (!liveViews.has(view) || view.isDestroyed()) throw new Error("直播动作已停止");
        view.webContents.sendInputEvent({ type: "mouseMove",
          x: Math.round(x * scale.x), y: Math.round(y * scale.y) });
      },
      key: async (keyCode) => {
        for (const type of ["keyDown", "keyUp"])
          view.webContents.sendInputEvent({ type, keyCode });
      },
      type: (text) => view.webContents.insertText(String(text || "")),
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      stopped: () => !liveViews.has(view) || view.isDestroyed(),
    };
    const result = await liveRoom.execute(cmd, io);
    trace("直播动作:", cmd.type, cmd.commentAuthor || cmd.videoUrl, "→", result.code || result.status);
    return require("./leadgen-extensions").describeResult(result);
  } catch (e) {
    return { ok: false, status: "uncertain", code: "live_action_exception",
      error: String((e && e.message) || e).slice(0, 120) };
  } finally {
    liveViews.delete(view);
  }
}

/* ------------------------------------------------------------------- 轮询 */
/** 上报结果；失败重试一次，最终仍失败时把原因写进日志（这是唯一会丢结果的点） */
async function report(id, payload, attempt = 0) {
  trace("回传结果:", id, payload.ok ? "ok" : (payload.error || ""), "attempt=" + attempt);
  const res = await request("POST", "/desktop/result", { id, ...payload });
  // 后端回 {"success":true}；空对象 = 请求失败/超时
  if (res && Object.keys(res).length === 0 && attempt < 1) {
    await new Promise((r) => setTimeout(r, 600));
    return report(id, payload, attempt + 1);
  }
  if (!res || Object.keys(res).length === 0) {
    trace("结果上报最终失败（后端未响应）:", id);
  }
  return res;
}

/**
 * 跑一条指令。
 *
 * 这里最重要的纪律：**任何分支都必须回传一条结果**。
 * 以前 run() 抛错会经 Promise.race 直接 reject，被 catch 吞成一行 warn，
 * 后端于是永远等不到回执（表现为「指令取走了、结果不回来、240s 超时」）。
 * 现在把异常在这一层就转成 {ok:false,error}，保证下游一定能 POST 出去。
 */
async function runCommand(cmd) {
  const started = Date.now();
  // 系统设置类壳侧任务（清缓存 / 清闲置分区）：不属于任何账号，也不碰采集窗口，
  // 所以放在 workerRunIds 记账之前处理，避免把任务级 runId 记到默认账号上。
  if (cmd.type === "shell-task") {
    return await require("./system-settings").handleShellCommand(cmd);
  }
  workerRunIds.set(accountKey(cmd.accountId), String(cmd.runId || ""));
  try {
    // 直播间动作走自己的执行器：目标是 live.douyin.com/<roomId>，没有 videoId，
    // 与「视频页动作」那套（live-platform）不是同一条路径。
    const liveRoom = require("./live-room");
    if (liveRoom.isLiveAction(cmd)) return await runLiveRoomAction(cmd);
    const live = require("./live-platform");
    if (live.scopeFor(cmd)) {
      const view = getWorker(cmd.accountId);
      liveViews.add(view);
      try {
        const { clipboard } = require("electron");
        const result = await live.executeLive(cmd, {
          currentUrl: () => view.webContents.getURL(),
          evaluate: code => evaluate(view, code, 8000),
          readComments: () => evaluate(view, commentExtractScript(cmd), 90000),
          navigate: url => loadPage(view, url, 1800),
          reload: () => loadPage(view, cmd.videoUrl, 1800, true),
          sleep: ms => new Promise(resolve => setTimeout(resolve, ms)),
          stopped: () => !liveViews.has(view),
          ensureLivePreview: () => liveDashboardHooks?.ready(cmd, () => !liveViews.has(view)) ?? false,
          // 自动回复/监控（monitor）没有审核台面板，等的不是首帧而是一帧可见渲染，
          // 否则每个动作都会在最后一步被判 live_preview_not_*。
          ensureActionable: () => visibleForAction(view),
          phase: phase => liveDashboardHooks?.phase(cmd, phase),
          click: async (x,y) => {
            if(!liveViews.has(view)||!live.scopeFor(cmd))throw new Error("Live command stopped or authorization expired");
            const sc=await viewScale(view);
            if(!liveViews.has(view)||!live.scopeFor(cmd))throw new Error("Live command stopped or authorization expired");
            const point={x:Math.round(x*sc.x),y:Math.round(y*sc.y),button:"left",clickCount:1};
            for(const type of ["mouseMove","mouseDown","mouseUp"])view.webContents.sendInputEvent({type,...point});
          },
          type: text => view.webContents.insertText(text),
          key:async(keyCode,modifiers=[])=>{
            if(!liveViews.has(view)||!live.scopeFor(cmd))throw Error('Live command stopped');
            for(const type of ['keyDown','keyUp'])view.webContents.sendInputEvent({type,keyCode,modifiers});
          },
          captureQr:async rect=>require('./profile-qr').captureQr(view.webContents,rect,await viewScale(view),
            ()=>liveViews.has(view)&&!!live.scopeFor(cmd)),
          uploadImage: (command,selector) => require('./live-media').uploadImage(view.webContents,command,selector,
            ()=>liveViews.has(view)&&!!live.scopeFor(cmd)),
          clipboard: {
            read: () => {
              if(clipboard.availableFormats().some(format=>!["text/plain","text/html","text/rtf","image/png","image/bmp"].includes(format))) {
                throw new Error("Unsupported clipboard formats must be preserved");
              }
              return {text:clipboard.readText(),html:clipboard.readHTML(),rtf:clipboard.readRTF(),image:clipboard.readImage()};
            },
            write: text => clipboard.writeText(text),
            readText: () => clipboard.readText(),
            restore: saved => clipboard.write({text:saved.text,html:saved.html,rtf:saved.rtf,
              ...(saved.image.isEmpty()?{}:{image:saved.image})}),
          },
        });
        return require('./leadgen-extensions').describeResult(result);
      } finally { liveViews.delete(view); }
    }
    const gated = rejectLiveAction(cmd);
    if (gated) return gated;
    if (cmd.type === "send-dm-chat") {
      // 自动回复的私信发送：目标不是某个视频里的人，而是私信列表里的某个会话，
      // 所以不走 live-platform / live-room 那两条路由（它们都要求视频或直播间地址）。
      const view = getLiveWindow(cmd.accountId);
      liveViews.add(view);
      try {
        await loadPage(view, DM_CHAT_URL, 9000);
        const io = {
          evaluate: (code, ms) => evaluate(view, code, ms || 20000),
          click: async (x, y) => {
            const sc = await viewScale(view);
            const point = { x: Math.round(x * sc.x), y: Math.round(y * sc.y), button: "left", clickCount: 1 };
            for (const type of ["mouseMove", "mouseDown", "mouseUp"]) {
              view.webContents.sendInputEvent({ type, ...point });
            }
          },
          type: (text) => view.webContents.insertText(text),
          sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
          stopped: () => !liveViews.has(view) || view.isDestroyed(),
        };
        return await require("./autoreply-dm").send(cmd, io);
      } finally {
        liveViews.delete(view);
      }
    }
    if (cmd.type === "sample-dom") return await sampleDom(cmd);
    if (cmd.type === "check-login") return await checkLogin(cmd);
    if (cmd.type === "read-account-stats") return await readAccountStats(cmd);
    if (cmd.type === "collect-comments") return await collectComments(cmd);
    if (cmd.type === "search-videos") return await searchVideos(cmd);
    if (cmd.type === "suggest-keywords") return await suggestKeywords(cmd);
    if (cmd.type === "read-source-videos") return await readSourceVideos(cmd);
    if (cmd.type === "read-comments") return await readComments(cmd);
    if (cmd.type === "open-video") return await openVideo(cmd);
    if (cmd.type === "next-video") return await nextVideo(cmd);
    if (cmd.type === "recommend-scan") return await recommendScan(cmd);
    if (cmd.type === "inspect-video" || cmd.type === "watch-video") return await inspectLeadgenVideo(cmd);
    if (["inspect-profile","inspect-share-targets","inspect-comment-extras"].includes(cmd.type)) return await inspectLeadgenSocial(cmd);
    if (cmd.type === "read-author-works") return await readAuthorWorks(cmd);
    if (cmd.type === "read-interaction-notices") return await readInteractionNotices(cmd);
    if (cmd.type === "read-live-events") return await readLiveEvents(cmd);
    if (cmd.type === "read-group-members") return await readGroupMembers(cmd);
    if (cmd.type === "read-group-list") return await readGroupList(cmd);
    // 手动兜底：验证拦截已经会自动弹窗，这里留给「用户自己想把窗口叫出来」的场景
    // （托盘菜单同一个入口），也用于验收脚本核对弹窗确实生效。
    if (cmd.type === "show-worker") return showWorker(cmd.accountId);
    if (cmd.type === "read-dm-conversations") return await readDmConversations(cmd);
    if (cmd.type === "read-dm-threads") return await readDmThreads(cmd);
    if (cmd.type === "inspect-live-ui") return await inspectLiveUi(cmd);
    if (cmd.type === "warmup-watch") return await warmupWatch(cmd);
    if (cmd.type === "publish-submit" || cmd.type === "open-work-publish-view") {
      // 作品发布：真的把本地素材传给创作者中心，或在可视频道里打开同一页面。
      // 用账号自己的常驻窗口，登录态与「账号池里那个账号」必须是同一个分区。
      return await withView(3000, async (view) => {
        const io = {
          view,
          evaluate: (code, ms) => evaluate(view, code, ms || 15000),
          loadPage: (url, wait) => loadPage(view, url, wait || 6000),
          sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
          stopped: () => !liveViews.has(view),
        };
        const publisher = require("./work-publish");
        if (cmd.type === "open-work-publish-view") {
          await io.loadPage("https://creator.douyin.com/creator-micro/content/manage", 6000);
          const probe = await io.evaluate(publisher.PROBE_JS, 8000).catch(() => null);
          if (probe && probe.loginWall) {
            return { ok: false, code: "login_required",
              error: "该账号未登录创作者中心，发布画面无法创建" };
          }
          return { ok: true, url: (probe && probe.url) || "", note: "已打开发布画面" };
        }
        return await publisher.submit(cmd, io);
      }, cmd.accountId);
    }
    if (cmd.type === "stop") {
      // 停任务时把直播采集窗口也收掉：它是「可见」窗口，放着会一直占着一个渲染进程
      closeLiveWindows();
      return { ok: true, note: "已停止" };
    }
    return { ok: false, error: "未知指令类型: " + cmd.type };
  } catch (e) {
    const message = String((e && e.message) || e);
    trace("指令执行出错:", cmd.type, message);
    return {
      ok: false,
      error: message,
      elapsedMs: Date.now() - started,
      // 让后端/验收脚本能分辨「页面没执行」和「执行了但没数据」
      stage: /超时/.test(message) ? "evaluate-timeout" : "exception",
    };
  }
}

async function tick() {
  if (polling) return;
  polling = true;
  try {
    while (inflightAccounts.size < MAX_PARALLEL) {
      const res = await request("GET", "/desktop/command?agent=" + encodeURIComponent(AGENT_ID));
      const cmd = res && res.command;
      if (!cmd || !cmd.id) {
        // 空响应有两种：队列真空，或者请求本身失败（看 trace 里的 HTTP 失败行）
        if (!res || !("command" in res)) trace("取指令响应异常（无 command 字段）");
        return;
      }
      trace("收到指令:", cmd.type, cmd.keyword || cmd.videoUrls || cmd.id);

      // 两类指令要求「全场只剩它在跑」：
      // - shell-task：清缓存等系统任务，不属于任何账号；
      // - copy-video-link：剪贴板是整机共享的，read/write/restore 必须独占，
      //   两个账号并发读写会把对方的链接串掉。
      if ((cmd.type === "shell-task" || cmd.type === "copy-video-link") && inflightAccounts.size > 0) {
        await request("POST", "/desktop/command-requeue", { id: cmd.id }).catch(() => {});
        return; // 等在跑的结束，下个 tick 再取
      }
      const key = accountKey(cmd.accountId);
      if (inflightAccounts.has(key)) {
        // 同账号串行：后端已按账号过滤，这里是最后一道防线
        await request("POST", "/desktop/command-requeue", { id: cmd.id }).catch(() => {});
        return;
      }
      inflightAccounts.add(key);
      runOne(cmd); // 不等待：槽位空着就继续取下一条（不同账号并行）
    }
  } catch (e) {
    trace("轮询异常:", (e && e.message) || e);
  } finally {
    polling = false;
  }
}

/** 跑一条已占槽的指令并在收尾时释放账号槽位（无论成败）。 */
async function runOne(cmd) {
  const key = accountKey(cmd.accountId);
  try {
    let result, hardTimer;
    const hardTimeoutMs=cmd.type==='watch-video' && Number.isFinite(Number(cmd.seconds))
      ? Math.max(6*60*1000,(Math.max(0,Math.min(3600,Number(cmd.seconds)))+120)*1000) : 6*60*1000;
    try {
      result = await Promise.race([
        runCommand(cmd),
        new Promise((resolve) => {hardTimer=setTimeout(
          () => resolve({
            ok: false,
            stage: "hard-timeout",
            error: "单条指令执行超时，已放弃（避免轮询被占死）",
          }),
          hardTimeoutMs);}),
      ]);
    } catch (e) {
      // runCommand 自己已兜住异常，走到这里只能是 race 机制本身出问题
      result = { ok: false, error: String((e && e.message) || e), stage: "race" };
    } finally {clearTimeout(hardTimer);}

    // 统一兜底：任何分支只要判定「需要人工验证」，就把采集窗口弹到前台。
    // 视频页 / 用户主页 / 直播那些执行器各自返回 needVerify，不经过 verifyBlocked，
    // 只靠各自弹窗会漏；这里按账号再兜一次，重复弹同一个窗口无副作用。
    if (result && result.needVerify) popForVerify(cmd.accountId, result.error || "");

    await report(cmd.id, result);
    trace("指令完成:", cmd.id, result.ok ? "ok" : result.error);
  } catch (e) {
    trace("指令执行异常:", cmd.id, (e && e.message) || e);
  } finally {
    inflightAccounts.delete(key);
  }
}

function start() {
  if (timer) return;
  getDirectSession(); // 预热：让 setProxy({mode:"direct"}) 在第一次轮询前生效
  timer = setInterval(tick, POLL_MS);
  trace("真机采集执行器已启动（轮询 " + POLL_MS + "ms，实例 " + AGENT_ID + "）");
}

function stop() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  liveViews.clear(); // 中断所有账号正在跑的循环
  releaseWorker();   // 常驻采集窗口在这里统一释放
}

/**
 * 把常驻采集窗口显示出来（托盘菜单用）。
 * 只用于「抖音要求人工验证」这类必须用户亲手处理的场合；平时采集全程后台。
 * 复用同一个窗口，登录态与指纹不变，处理完直接关掉（只是隐藏）即可继续采。
 */
function showWorker(accountId) {
  // 不指定账号时显示最近用过的那个：多账号下用户点「显示采集窗口」
  // 想看的多半是刚刚被拦下的那一个。
  const key = accountKey(accountId || lastWorkerKey);
  const w = getWorker(key);
  try {
    w.setSkipTaskbar(false);
    w.show();
    w.focus();
    trace(`采集窗口已临时显示（账号 ${key}，供人工处理）`);
    return { ok: true, account: key, visible: w.isVisible ? w.isVisible() : null };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

/**
 * 只读状态：某个账号的采集窗口此刻在哪一页、是否正在跑任务。
 *
 * 只给桌面壳的实况大屏用（同进程直调），不联网、不写库、不改变采集行为。
 * 页面常驻但可能处于指令间隔；busy 只说明当前指令占用，不代表任务结束。
 */
let liveDashboardHooks = null;
const workerRunIds = new Map();
function setLiveDashboardHooks(hooks) { liveDashboardHooks = hooks; }

function liveState(accountId) {
  const key = accountKey(accountId);
  const w = workers.get(key);
  if (!w || w.isDestroyed()) return null;
  let url = "";
  try { url = w.webContents.getURL() || ""; } catch (e) { url = ""; }
  return {
    account: key,
    url: /^https?:\/\//i.test(url) ? url : "",
    busy: liveViews.has(w),
    runId: workerRunIds.get(key) || "",
  };
}

function currentUrl(accountId) {
  const st = liveState(accountId);
  return st ? st.url : "";
}

async function captureWorkerFrame(accountId) {
  const key = accountKey(accountId), runId = workerRunIds.get(key) || "";
  const worker = workers.get(key);
  if (!worker || worker.isDestroyed()) return null;
  // Capturing an uncommitted/blank window can stall its first navigation on
  // Windows. Let the real page finish loading before requesting pixels.
  if (!/^https:\/\//.test(worker.webContents.getURL()) || worker.webContents.isLoadingMainFrame()) return null;
  const image = await worker.webContents.capturePage();
  // A new run may acquire the same account while the native capture is pending.
  if (workers.get(key) !== worker || (workerRunIds.get(key) || "") !== runId) return null;
  if (image.isEmpty()) throw new Error("执行窗口暂未产生画面");
  return { image: "data:image/jpeg;base64," + image.resize({width:1120}).toJPEG(65).toString("base64"),
           capturedAt: Date.now(), busy: liveViews.has(worker), runId };
}

module.exports = { start, stop, onNotice, showWorker, liveState, currentUrl, captureWorkerFrame, setLiveDashboardHooks, getWorkerSurface:getWorker };
