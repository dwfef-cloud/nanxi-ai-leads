"use strict";
// Read-only probes. Missing metadata stays null so enabled filters fail closed.

// 抖音视频详情（点封面 / 点「下一个」进来的浮层）是「上一条 / 当前 / 下一条」
// 三段竖排的滑动容器：上一条已经不播、下一条还没播，但它们的标题、点赞数全在
// 同一个文档里。全页 querySelector 会把邻条的数据读串（真机 2026-11-19：
// 屏幕上播的是 TaroBird，document 里第一个 video-desc 却是上一条 ZCode 的）。
// 所以统一先定位「屏幕中间那条播放器」，再从它所在的 panel（类名 video_<id>）
// 里读标题与计数；panel 认不出来时才退回全页 + 地址。
const STAGE_JS = `
  const vh = window.innerHeight || 900;
  let stage = null;
  for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect();
    if (r.height > 60 && r.top < vh * 0.7 && r.top + r.height > vh * 0.3) { stage = v; break; }
  }
  let panel = null, panelId = '', feedId = '';
  if (stage) {
    let el = stage;
    for (let d = 0; el && d < 12; d++, el = el.parentElement) {
      const cls = String(el.className && (el.className.baseVal || el.className) || '');
      const m = /video[_-]([0-9]{6,})/.exec(cls);
      const attrs = ['data-e2e-vid','data-video-id','data-aweme-id','aweme-id','data-id'];
      const attrId = attrs.map(name => String(el.getAttribute?.(name) || ''))
        .find(value => /^[0-9]{6,}$/.test(value));
      if (m || attrId) {
        panelId = (m && m[1]) || attrId;
        panel = el;
        break;
      }
      if (!feedId && el.querySelectorAll) {
        const links = Array.from(el.querySelectorAll('a[href*="/video/"]'));
        const link = links.map(a => /\\/video\\/([0-9]{6,})/.exec(String(a.href || a.getAttribute('href') || '')))
          .find(Boolean);
        if (link) {
          feedId = link[1];
          panel = el;
          break;
        }
      }
    }
  }
  const pathStr = String(location.pathname || '');
  const at = pathStr.indexOf('/video/');
  const urlId = at >= 0 ? String(pathStr.slice(at + 7).split('/')[0] || '') : '';
`;

const VIDEO_METADATA_JS = `(() => {
${STAGE_JS}
  const video = stage || document.querySelector('video');
  const scope = panel || document;
  const text=s=>scope.querySelector(s)?.textContent?.trim()||'';
  // 标题不能只认一个选择器：真机实测有的视频页没有 data-e2e="video-desc"
  // （详情层是另一种布局），页面明明打开了、地址也核对上了，标题却是空字符串，
  // 后端只能记「未读到视频标题」并跳过 —— 用户看到的却是「筛选/AI 把它判掉了」。
  // 这里按「越贴近视频正文越优先」的顺序退让，并回报标题来自哪里。
  const TITLE_SELECTORS=['[data-e2e="video-desc"]','[data-e2e="detail-video-desc"]',
    '[data-e2e="video-title"]','h1'];
  let title='',titleSource='';
  for(const sel of TITLE_SELECTORS){
    const t=text(sel);
    if(t){title=t;titleSource=sel;break;}
  }
  if(!title){
    const meta=document.querySelector('meta[property="og:title"]')?.getAttribute('content')
      ||document.querySelector('meta[name="description"]')?.getAttribute('content')||'';
    const t=String(meta).trim();
    if(t){title=t.slice(0,200);titleSource='meta';}
  }
  const time=scope.querySelector('time[datetime]');
  // 推荐流 / 关注流的详情浮层没有 [data-e2e="video-publish-time"]、也没有 time[datetime]
  // （真机 2026-11-20：设置了「只处理 3 天内发布」后每条视频都被判「发布时间未知」
  //  全部跳过，0 互动）。这里从视频信息区文案里兜底抠发布时间：相对时间（3天前 /
  //  2小时前 / 刚刚）与绝对日期（2024-11-18）都认，后端 age_seconds 两边都能解析。
  let publishedText=text('[data-e2e="video-publish-time"]');
  if(!publishedText && !time?.getAttribute('datetime')){
    const probe=[scope.querySelector('[data-e2e="video-info"]')?.innerText,
      scope.querySelector('[data-e2e="video-desc"]')?.innerText,scope.innerText]
      .filter(Boolean).join('\\n').slice(0,1200);
    const m=probe.match(/(\\d{4}年\\d{1,2}月\\d{1,2}日|\\d{1,2}月\\d{1,2}日|\\d+\\s*(?:分钟|小时|天|周|个月|年)前|刚刚)/)
      ||probe.match(/(\\d{4}-\\d{1,2}-\\d{1,2})/)
      ||probe.match(/(\\d{1,2}-\\d{1,2})/);
    publishedText=m?m[1]:'';
  }
  const count=s=>{const el=scope.querySelector(s);return el?.getAttribute('data-count')||el?.textContent?.trim()||null;};
  // 视频页改版后 data-e2e 计数经常整块消失（只剩图标+数字），此时按 aria-label
  // 上的「点赞 1.2万」兜底。拿不到就返回 null —— 阈值判定会如实写「平台未提供指标」，
  // 不会拿 0 当成绩把视频全跳过。
  const ariaStats=()=>{
    const out={};
    const num=/[0-9]+(?:\.[0-9]+)?[万亿wW]?/;
    for(const el of scope.querySelectorAll('[aria-label]')){
      const label=el.getAttribute('aria-label')||'';
      const m=label.match(num);
      if(!m) continue;
      if(!out.likeCount&&/点赞|喜欢|赞/.test(label)) out.likeCount=m[0];
      else if(!out.commentCount&&/评论/.test(label)) out.commentCount=m[0];
      else if(!out.collectCount&&/收藏/.test(label)) out.collectCount=m[0];
      else if(!out.shareCount&&/分享|转发/.test(label)) out.shareCount=m[0];
    }
    return out;
  };
  const stats=ariaStats();
  return {
    url:location.href,videoId:panelId||feedId||urlId,title,titleSource,
    author:text('[data-e2e="user-name"]'),
    publishedAt:time?.getAttribute('datetime')||null,
    publishedText:publishedText,
    likeCount:count('[data-e2e="like-count"]')||stats.likeCount||null,
    commentCount:count('[data-e2e="comment-count"]')||stats.commentCount||null,
    collectCount:count('[data-e2e="collect-count"]')||stats.collectCount||null,
    shareCount:count('[data-e2e="share-count"]')||stats.shareCount||null,
    duration:video&&Number.isFinite(video.duration)?video.duration:null,
    playing:!!video&&!video.paused&&video.readyState>=2,currentTime:video?.currentTime||0,
    loginWall:!!document.querySelector('#login-pannel,[data-e2e="login-guide"],.login-mask'),
    verify:!!document.querySelector('#captcha_container,[data-e2e="verify-dialog"]')||/验证|captcha|安全验证/i.test(document.title)
  };
})()`;

// 当前画面里正在播的那条视频（同 STAGE_JS 口径）：切视频时抖音不改地址，
// 只认 location.href 会把「已经切到下一条」误判成「还停在原来那条」，
// 于是又整页加载一遍 —— 用户看到的就是同一个视频反复加载。
const VIDEO_STATE_JS = `(() => {
${STAGE_JS}
  const activeFeed = document.querySelector('[data-e2e="feed-active-video"]');
  const liveFeed = document.querySelector('[data-e2e="feed-live"]');
  const activeFeedId = String(activeFeed && activeFeed.getAttribute('data-e2e-vid') || '');
  const arrow = document.querySelector('[data-e2e="video-switch-next-arrow"]');
  const panelScope = panel || document.body || document;
  const desc = panelScope.querySelector('[data-e2e="video-desc"]')
    || Array.from(document.querySelectorAll('[data-e2e="video-desc"]')).find(e => {
         const r = e.getBoundingClientRect();
         return r.width > 0 && r.height > 0 && r.top < vh && r.bottom > 0;
       });
  const author = panelScope.querySelector('[data-e2e="video-author-name"], [data-e2e="user-name"]');
  // 浮层关闭后 video 节点还留在文档里（宽高 0 / 被推到视口外）：
  // stageShown=false 用来区分「真的停在这条视频上」和「只剩残留 DOM」。
  const stageShown = (() => {
    if (!stage) return false;
    const r = stage.getBoundingClientRect();
    const vw = window.innerWidth || 1280;
    return r.width > 40 && r.height > 40 && r.right > 0 && r.left < vw - 4;
  })();
  return {
    // 信息流直播卡片可能沿用上一条 /video/ 地址；可见信息流/直播页上没有 feed 编号时，
    // 不回退到旧 URL，否则会把直播卡片误认成仍在旧视频页。
    id: panelId || (activeFeed ? activeFeedId : (liveFeed
      || /(^|\\.)live\\.douyin\\.com$/i.test(location.hostname)
      ? '' : feedId || urlId)),
    domId: panelId || feedId, urlId,
    url: location.href,
    immersiveFeed: !!activeFeed,
    liveFeed: !!liveFeed,
    liveRoom: /(^|\\.)live\\.douyin\\.com$/i.test(location.hostname)
      || /\\/(?:live|webcast)(?:\\/|$)/i.test(location.pathname),
    hasArrow: !!arrow,
    stageShown,
    title: String((desc && desc.innerText) || '').slice(0, 120),
    author: String((author && author.innerText) || '').slice(0, 60),
    isAd: /(^|[\\s·])广告($|[\\s·])/.test(String(panelScope.innerText || '')) ||
      Array.from(document.querySelectorAll('[data-e2e="video-info"]')).some(el => {
        const r = el.getBoundingClientRect();
        return r.width > 8 && r.height > 8 && r.top < vh && r.bottom > 0 &&
          /(^|[\\s·])广告($|[\\s·])/.test(String(el.innerText || ''));
      }),
    // 发布时间（相对文案）：浮层的 video-info 行常带「3天前」；单遍式靠它做
    // 开屏快判（时间窗筛选），丢了它每条视频都要等 AI 才能筛。
    publishedText: (() => {
      const text = String(panelScope.innerText || '');
      const m = text.match(/\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d+\s*(?:分钟|小时|天|周|个月|年)前|刚刚/)
        || text.match(/\d{4}-\d{1,2}-\d{1,2}/)
        || text.match(/\d{1,2}-\d{1,2}/);
      return m ? m[0] : '';
    })(),
    paused: stage ? !!stage.paused : null,
    verify: !!document.querySelector('#captcha_container,[data-e2e="verify-dialog"]'),
  };
})()`;

// 详情浮层（点封面 / 点「下一个」进来的那种）评论面板默认是收起的：不点评论图标，
// 页面上既没有 comment-list 也没有 comment-item，读到 0 条。视频页则默认展开。
// 这里只回报「开没开 + 该点哪儿」——真实点击必须由主进程 sendInputEvent 完成，
// 脚本里的 element.click() 会被抖音的 React 事件层直接忽略（真机实测）。
const COMMENT_OPEN_JS = `(() => {
${STAGE_JS}
  const vw = window.innerWidth || 1280;
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    // 必须整块落在视口里：只判 top < vh 会选中「在视口上方」的图标
    // （真机实测取到 y=-305，点出去什么也没点到，面板一直不开）。
    return r.width > 8 && r.height > 8 && r.right > 0 && r.left < vw - 4 &&
           r.top < vh - 4 && r.bottom > 0;
  };
  const scope = panel || document.body || document;
  const list = document.querySelector('[data-e2e="comment-list"]');
  const items = Array.from(scope.querySelectorAll('[data-e2e="comment-item"]'));
  const shownItems = items.filter(visible).length;
  const scopeText = String(scope.innerText || '');
  const icons = Array.from(document.querySelectorAll('[data-e2e="feed-comment-icon"]')).filter(visible);
  const icon = icons[0];
  const toggle = icon ? (() => {
    const r = icon.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  })() : null;
  // 两种布局要分开判：
  //  - 详情浮层：评论在右侧窄栏（~336px），收起时 item 可见数为 0，必须点图标；
  //  - 独立视频页：评论区常驻在视频下方（真机实测 top=920 > 视口 900），
  //    列表宽 880、内容在视口外 —— 这时「可见数为 0」不代表没打开。
  const listWide = !!(list && list.getBoundingClientRect().width > 500);
  const emptyLabel = /暂无评论|没有评论|还没有评论|快来.*评论/.test(scopeText);
  const zeroCommentCount = /(?:全部评论|评论)\\s*[（(]\\s*0\\s*[）)]/.test(scopeText);
  const emptyState = !!(list && visible(list) && items.length === 0) ||
    (emptyLabel && zeroCommentCount) ||
    Array.from(scope.querySelectorAll('[data-e2e*="comment-empty"], [class*="comment-empty"]'))
      .some(el => visible(el) && /暂无评论|没有评论|还没有评论|快来.*评论/.test(String(el.innerText || el.textContent || '')));
  // 浮层关掉之后评论区 DOM 还留在文档里（宽度 0 / 被推到视口外，真机实测
  // comment-list 宽 23px、left 正好等于视口宽度）。只数 DOM 条数会误判
  // 「已经打开」→ 跳过点击 → 后面读到 0 条，用户看到的就是「评论一条没读到」。
  if (shownItems > 0 || (listWide && items.length > 0) || emptyState) {
    return { open: true, empty: emptyState, items: items.length, visibleItems: shownItems, listWide, toggle };
  }
  if (!icon) {
    const anyIcons = document.querySelectorAll('[data-e2e="feed-comment-icon"]').length;
    return { open: false, items: items.length, visibleItems: shownItems, click: null,
      note: items.length ? 'stale-comment-dom' : (anyIcons ? 'icon-offscreen' : 'comment-icon-missing'),
      listShown: visible(list) };
  }
  return { open: false, items: items.length, visibleItems: shownItems,
           click: toggle };
})()`;

function verifiedPage(page,url) {
  const id=value=>/\/video\/(\d{6,})/.exec(String(value||''))?.[1];
  if(!page||page.loginWall||page.verify)return false;
  const target=id(url);
  if(!target)return false;
  // 页面身份：画面里正在播的那条优先（详情浮层不改地址），没有再退回地址。
  return (String(page.videoId||'')||String(id(page.url)||''))===target;
}
module.exports={VIDEO_METADATA_JS,VIDEO_STATE_JS,COMMENT_OPEN_JS,verifiedPage};
