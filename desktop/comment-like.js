"use strict";
// Platform class/state mapping observed in the logged-in desktop and its loaded
// comment component on 2026-11-15. Unknown/ambiguous DOM never triggers a click.
function commentProbe(target) {
  return `(() => {
    const expected=${JSON.stringify(target)};
    const norm=v=>String(v||'').normalize('NFKC').replace(/[\u200b\u200e\u200f\u2060\ufeff]/g,'').replace(/\\s+/g,' ').trim();
    const rows=Array.from(document.querySelectorAll('[data-e2e="comment-item"]'));
    // 评论正文所在元素会随互动变化：作者点了赞，平台会往同一行里追加「作者赞过」角标，
    // 置顶的还会带「置顶」。整段精确比对会在动作做完之后就再也匹配不上（真机踩过：
    // 第一次点赞成功，之后每条重试都报 comment_not_found）。这里先把角标剥掉再比。
    const stripBadges=v=>norm(v).replace(/(?:\s*作者赞过)+$/,'').replace(/\s*置顶$/,'').trim();
    const sameText=(pageText,expected)=>{
      const a=stripBadges(pageText),b=stripBadges(expected);
      if(a===b)return true;
      // 角标/后缀没枚举全时，仍允许「一个是另一个的前缀」——
      // 身份已经由 tooltip_<commentId>（唯一）和作者名双重钉住，正文只用来确认没串行。
      return a.length>=4 && b.length>=4 && (a.startsWith(b) || b.startsWith(a));
    };
    const matches=rows.filter(row=>{
      const own=s=>Array.from(row.querySelectorAll(s)).filter(e=>e.closest('[data-e2e="comment-item"]')===row);
      const markers=own('[id^="tooltip_"]').filter(e=>e.id==='tooltip_'+expected.commentId);
      const names=own('[data-e2e="comment-username-1"], a[href*="/user/"]');
      const info=own('.comment-item-info-wrap')[0];
      const body=info?.nextElementSibling || own('[data-e2e="comment-level-1"], [class*="comment-content"]')[0];
      return markers.length===1 && names.some(e=>norm(e.innerText).replace(/^@+/,'')===expected.authorName) &&
        body && sameText(body.innerText,expected.content);
    });
    if(matches.length!==1)return {found:false,reason:matches.length?'ambiguous_comment':'comment_not_found'};
    const row=matches[0];
    const controls=Array.from(row.querySelectorAll('[data-e2e="comment-digg"], [data-e2e="comment-like"], [aria-label="点赞"], [aria-label="取消点赞"], .comment-item-stats-container p.VpA2NKl1'))
      .filter(e=>e.closest('[data-e2e="comment-item"]')===row);
    // Prefer one semantic outer control when an icon repeats its parent's label.
    const unique=controls.filter(e=>!controls.some(other=>other!==e && other.contains(e)));
    if(unique.length!==1)return {found:false,reason:'like_control_ambiguous_or_missing'};
    const el=unique[0];
    const read=()=>{
      const pressed=el.getAttribute('aria-pressed'), state=el.getAttribute('data-e2e-state'),label=el.getAttribute('aria-label');
      if(pressed==='true')return true;if(pressed==='false')return false;
      if(['comment-digged','comment-is-digged','liked'].includes(state)||label==='取消点赞')return true;
      if(['comment-no-digged','unliked'].includes(state)||label==='点赞')return false;
      if(el.matches?.('p.VpA2NKl1') &&
         el.closest('.comment-item-stats-container')?.closest('[data-e2e="comment-item"]')===row &&
         el.querySelector('svg.Z4qg7NEM') && el.querySelectorAll('span').length===1)
        return el.classList.contains('tOgCrAK_');
      return null;
    };
    const already=read();
    if(already===null)return {found:false,reason:'like_state_unknown'};
    el.scrollIntoView({block:'center'});
    const r=el.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
    const hit=document.elementFromPoint(x,y);
    if(r.width<8||r.height<8||r.left<0||r.top<0||r.right>innerWidth||r.bottom>innerHeight||
       !hit||!(hit===el||el.contains(hit))||el.getAttribute('aria-disabled')==='true'||el.disabled)
      return {found:false,reason:'like_control_obscured_or_disabled'};
    return {found:true,already,x,y};
  })()`;
}

const RECEIPT_HOOK = `(() => {
  if(window.__commentLikeHook)return true;
  window.__commentLikeHook=true;window.__commentLikeReceipts=[];window.__commentLikeSeq=0;
  const relevant=url=>{try{const u=new URL(String(url),location.href);return u.hostname==='www.douyin.com' &&
    /^\\/aweme\\/v[0-9]+\\/web\\/comment\\/(digg|like)\\/?$/.test(u.pathname);}catch(_){return false;}};
  const binding=(url,body)=>{
    const query=new URL(String(url),location.href).searchParams;let data=query;
    if(body&&typeof body.get==='function')data=body;
    if(typeof body==='string'){try{data=new URLSearchParams(JSON.parse(body));}catch(_){data=new URLSearchParams(body);}}
    return {comment:String(data.get('cid')||data.get('comment_id')||query.get('cid')||query.get('comment_id')||''),
      action:String(data.get('digg_type')||data.get('action')||query.get('digg_type')||query.get('action')||''),
      video:String(data.get('aweme_id')||query.get('aweme_id')||(/\\/video\\/([0-9]+)/.exec(location.href)||[])[1]||'')};
  };
  const add=(target,status,text)=>{
    let code=null;try{const parsed=JSON.parse(String(text));if(Number.isInteger(parsed.status_code))code=parsed.status_code;}catch(_){}
    window.__commentLikeReceipts.push({seq:++window.__commentLikeSeq,...target,http:status,code});
    if(window.__commentLikeReceipts.length>20)window.__commentLikeReceipts.shift();
  };
  const fetchBefore=window.fetch;
  window.fetch=function(...args){const url=String(args[0]?.url||args[0]||'');const ok=relevant(url);
    const target=ok?binding(url,args[1]?.body):null;
    return fetchBefore.apply(this,args).then(res=>{if(ok)res.clone().text().then(body=>add(target,res.status,body)).catch(()=>{});return res;});};
  const openBefore=XMLHttpRequest.prototype.open,sendBefore=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,url,...rest){this.__commentLikeUrl=String(url);return openBefore.call(this,method,url,...rest);};
  XMLHttpRequest.prototype.send=function(...args){if(relevant(this.__commentLikeUrl)){
    const target=binding(this.__commentLikeUrl,args[0]);this.addEventListener('loadend',()=>{
      try{add(target,this.status,this.responseText);}catch(_){add(target,this.status,'');}},{once:true});}
    return sendBefore.apply(this,args);};
  return true;
})()`;

function receiptStatus(entries, expected) {
  const matched=Array.isArray(entries)?entries.filter(e=>e && e.comment===expected.commentId &&
    e.video===expected.videoId && e.action==='1'):[];
  if(!matched.length)return "uncertain";
  const statuses=matched.map(e=>{
    if(!Number.isInteger(e.http)||!Number.isInteger(e.code)||e.http<200||e.http>=500||e.http>=300&&e.http<400)return "uncertain";
    return e.http<300&&e.code===0?"sent_ok":"sent_fail";
  });
  return statuses.every(s=>s===statuses[0])?statuses[0]:"uncertain";
}

/**
 * 刷新后把目标评论重新找出来。
 *
 * 视频页刷新后评论区默认收起，且评论列表是懒加载的 —— 目标评论常常不在首屏，
 * 光靠探测只会一直得到 comment_not_found，被误判成「点赞没留存」。
 * 这里做两件事：第一屏先点开评论图标；之后每次调用把列表往下推一屏。
 */
const READBACK_SEEK_JS = `(dir => {
  const list = document.querySelector('[data-e2e="comment-list"],[data-e2e="comment-list-container"],div[class*="comment-list"],div[class*="CommentList"]');
  if (!document.querySelector('[data-e2e="comment-item"]')) {
    const icon = document.querySelector('[data-e2e="comment-icon"],[data-e2e="feed-comment-icon"]');
    if (icon) { try { icon.click(); } catch (_) {} return { opened: true }; }
  }
  const scroller = list || document.scrollingElement;
  if (!scroller) return { opened: false, scrollTop: null };
  const step = Math.max(240, Math.round((scroller.clientHeight || 600) * 0.9));
  if (dir === 'top') scroller.scrollTop = 0;
  else scroller.scrollTop = Math.min(scroller.scrollHeight || 0, (scroller.scrollTop || 0) + step);
  return { opened: false, scrollTop: scroller.scrollTop, scrollHeight: scroller.scrollHeight };
})`;

/**
 * 点开折叠的楼中楼。
 *
 * 目标是一条「回复」时，它常被收在「展开 N 条回复」后面 —— 这种情况下
 * commentProbe 只会一直回 comment_not_found，看着像「评论被删了」。
 * 这里只点文案确实是展开的按钮，不碰任何写入口。
 */
const EXPAND_REPLIES_JS = `(() => {
  const expanders = Array.from(document.querySelectorAll('button,[role="button"],span,div'))
    .filter(el => el.offsetParent && /展开\\s*(?:\\d+\\s*条)?\\s*(?:回复|评论)/.test(
      String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()))
    .slice(0, 40);
  expanders.forEach(el => { try { el.click(); } catch (_) {} });
  return expanders.length;
})`;

function commentDiag(target) {
  return `(JSON.stringify((() => {
    const expected = ${JSON.stringify(target)};
    const rows = Array.from(document.querySelectorAll('[data-e2e="comment-item"]'));
    const norm = v => String(v || '').normalize('NFKC').replace(/[\\u200b\\u200e\\u200f\\u2060\\ufeff]/g, '').replace(/\\s+/g, ' ').trim();
    const one = row => {
      const own = s => Array.from(row.querySelectorAll(s)).filter(e => e.closest('[data-e2e="comment-item"]') === row);
      const marker = own('[id^="tooltip_"]')[0];
      const info = own('.comment-item-info-wrap')[0];
      const body = info && info.nextElementSibling;
      const authorEl = own('[data-e2e="comment-username-1"], a[href*="/user/"]')[0];
      const bodyText = body ? norm(body.innerText) : '';
      return {
        id: marker ? marker.id : '',
        idMatch: !!marker && marker.id === 'tooltip_' + expected.commentId,
        author: authorEl ? norm(authorEl.innerText).replace(/^@+/, '') : '',
        bodyLen: bodyText.length,
        bodyHead: bodyText.slice(0, 60),
        expectLen: expected.content.length,
        bodyMatch: bodyText === expected.content,
      };
    };
    return { url: location.href, visibility: document.visibilityState, items: rows.length,
      rows: rows.slice(0, 8).map(one) };
  })()))`;
}

async function executeCommentLike(command, io, {active, identityMatches, claim, scope}) {
  const {videoIdOf}=require('./write-commands');
  const target={commentId:command.commentId,authorName:command.commentAuthor,content:command.text};
  if(!/^\d{6,}$/.test(target.commentId)||!target.authorName||!target.content)
    return {ok:false,status:'skipped',code:'invalid_comment_target'};
  let submitted=false;
  const skip=code=>({ok:false,status:'skipped',code});
  try {
    if(typeof io.reload!=='function')return skip('refresh_verification_unavailable');
    if(!active()||!await identityMatches())return skip('stopped_or_identity_changed');
    // 必须在「页面可见」之后才找评论：隐藏窗口里抖音的评论列表不渲染
    // （真机实测：同一页 hidden 时探针恒 comment_not_found，可见后立刻找到）。
    io.phase?.('正在确认评论页可交互');
    if(!await require('./live-platform').ensureActable(io,scope))
      return skip('comment_page_not_visible');
    const probe=commentProbe(target);
    // 评论区默认收起、且列表懒加载：先打开并回到顶部，再一屏一屏往下找。
    // 只等不滚会在「目标评论在列表深处」时一直得到 comment_not_found。
    await io.evaluate(READBACK_SEEK_JS+"('top')").catch(()=>null);
    await io.sleep(600);
    let before;
    let expanded = 0;
    for(let i=0;i<20;i++) {
      if(!active()||!await identityMatches())return skip('stopped_or_identity_changed');
      before=await io.evaluate(probe);
      if(before?.found||before?.reason!=='comment_not_found')break;
      if(i===2 || (i>2 && i%4===2)) {
        // 只找了 3 屏还找不到，多半是回复被折叠在「展开 N 条回复」里：
        // 点开一轮再继续找（每次返回点了几个，便于区分「没有折叠」和「点了也没用」）。
        const n = await io.evaluate(EXPAND_REPLIES_JS).catch(()=>0);
        if(Number(n)>0) {
          expanded += Number(n);
          await io.sleep(500);
          continue;
        }
      }
      if(i%2===1)await io.evaluate(READBACK_SEEK_JS+"('down')").catch(()=>null);
      await io.sleep(400);
    }
    if(!before?.found) {
      // 只报「没找到」看不出是面板没开、列表没渲染，还是正文不匹配 —— 把现场打回壳日志。
      const diag = await io.evaluate(commentDiag(target)).catch(() => null);
      if(diag) await io.evaluate(`console.error('[comment-diag] ' + ${JSON.stringify(String(diag).slice(0, 1200))})`).catch(()=>null);
      if(expanded) await io.evaluate(`console.error(${JSON.stringify('[comment-expand] ' + expanded + ' 个展开按钮点过，仍未找到目标评论')})`).catch(()=>null);
      return skip(before?.reason||'comment_not_found');
    }
    if(before.already)return skip('already_liked');
    io.phase?.('已找到准确评论，正在核对页面可点击状态');
    if(!await require('./live-platform').ensureActable(io,scope))
      return skip('live_preview_not_ready');
    if(!active()||!await identityMatches())return skip('stopped_or_identity_changed');
    if(await io.evaluate(RECEIPT_HOOK)!==true)return skip('receipt_hook_unavailable');
    const seq=await io.evaluate('window.__commentLikeSeq || 0');
    const ready=await io.evaluate(probe);
    if(!ready?.found||ready.already||!active()||!await identityMatches())return skip('comment_changed_before_click');
    if(!claim())return skip('acceptance_attempt_consumed');
    submitted=true;
    io.phase?.('正在提交一次点赞并核对平台回执');
    await io.click(ready.x,ready.y);
    let stable=0;
    for(let i=0;i<12;i++) {
      if(!active()||!await identityMatches())return {ok:false,status:'uncertain',code:'stopped_or_identity_changed_after_click'};
      const after=await io.evaluate(probe);
      const receipts=await io.evaluate('(window.__commentLikeReceipts||[]).filter(e=>e.seq>'+Number(seq)+')');
      const status=receiptStatus(receipts,{commentId:command.commentId,videoId:videoIdOf(command.videoUrl)});
      stable=after?.found&&after.already?stable+1:0;
      if(status==='sent_fail'&&stable===0)return {ok:false,status:'sent_fail',code:'comment_like_declined'};
      if(status==='sent_ok'&&stable>=2) {
        io.phase?.('已收到平台回执，正在刷新核对点赞留存');
        await io.reload();
        let refreshedStable=0;
        // 刷新后评论区收起、目标评论可能不在首屏：先确认页面可交互，回到顶部，再一屏一屏往下找。
        if(!await require('./live-platform').ensureActable(io,scope))
          return {ok:false,status:'uncertain',code:'comment_like_readback_page_not_ready'};
        await io.evaluate(READBACK_SEEK_JS+"('top')").catch(()=>null);
        await io.sleep(1200);
        let seeked=0;
        for(let j=0;j<40;j++) {
          if(!active())return {ok:false,status:'uncertain',code:'stopped_during_comment_like_readback'};
          if(await identityMatches()) {
            const refreshed=await io.evaluate(probe);
            if(refreshed?.found && !refreshed.already)
              return {ok:false,status:'uncertain',code:'comment_like_not_retained_after_refresh'};
            refreshedStable=refreshed?.found && refreshed.already?refreshedStable+1:0;
            if(refreshedStable>=2)return {ok:true,status:'sent_ok',code:'comment_like_receipt_state_and_reload_verified'};
            // 还没定位到那一条：往下推一屏再探（每两次探测推一次，别把懒加载甩出去）。
            if(!refreshed?.found && j%2===1 && seeked<25) {
              seeked++;
              await io.evaluate(READBACK_SEEK_JS+"('down')").catch(()=>null);
            }
          } else refreshedStable=0;
          await io.sleep(700);
        }
        return {ok:false,status:'uncertain',code:'comment_like_refresh_not_verified'};
      }
      await io.sleep(400);
    }
    return {ok:false,status:'uncertain',code:'comment_like_not_confirmed'};
  } catch(_) {
    return {ok:false,status:submitted?'uncertain':'skipped',code:submitted?'comment_like_outcome_unknown':'comment_like_preparation_failed'};
  }
}

module.exports={commentProbe,RECEIPT_HOOK,receiptStatus,executeCommentLike};
