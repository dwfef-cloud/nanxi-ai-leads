"use strict";
// Native follow and share adapters. Selectors and JSON acknowledgements must be
// calibrated on a logged-in platform; missing/ambiguous evidence never succeeds.
const {profileUrl}=require('./live-dm');
const {trace}=require('./diag-log');
/**
 * 写动作的现场诊断：同时进控制台和 _verify/agent.log。
 *
 * 为什么不再只 console.error：真机上「关注点不出去」只回了一个
 * follow_control_not_verified，而壳的 stdout 在启动器里是丢的 —— 事后完全
 * 无法区分是页面没加载、昵称对不上还是按钮文案改了。日志落文件才能复盘。
 */
function diag(tag,payload) {
  try{console.error('['+tag+']',JSON.stringify(payload));}catch(_){}
  try{trace(tag,JSON.stringify(payload));}catch(_){}
}

function profileProbe(command,nickname) {
  return `(() => {
    if(location.href.split('?')[0]!==${JSON.stringify(profileUrl(command.recipientId))})return null;
    if(document.querySelector('#captcha_container,[data-e2e="verify-dialog"]'))return {needVerify:true};
    if(document.querySelector('#login-pannel,[data-e2e="login-guide"],.login-mask'))return {loginWall:true};
    const self=Array.from(document.querySelectorAll('header [class*="userMenuPanel"]')).map(e=>e.textContent||'').join(' ');
    if(!self.startsWith(${JSON.stringify(nickname)}+'关注'))return null;
    const titles=Array.from(document.querySelectorAll('h1,[data-e2e="user-title"]'));
    if(!titles.some(e=>(e.innerText||'').trim()===${JSON.stringify(command.commentAuthor)}))return null;
    const info=Array.from(document.querySelectorAll('[data-e2e="user-info"],[data-e2e="user-info-detail"],[class*="user-info-detail"]')).map(e=>e.innerText||'').join(' ');
    const age=/(?:^|\\s)(\\d{1,3})岁(?:\\s|$)/.exec(info);
    const gender=/(?:^|\\s)男(?:\\s|$)/.test(info)?'male':/(?:^|\\s)女(?:\\s|$)/.test(info)?'female':null;
    const point=e=>{e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);
      return r.width>8&&r.height>8&&!e.disabled&&(hit===e||e.contains(hit))?{x,y}:null;};
    const buttons=Array.from(document.querySelectorAll('main button,main [role="button"],[data-e2e="user-detail"] button,[data-e2e="user-detail"] [role="button"]'));
    // 「回关」也是可执行状态：对方关注了我、我还没关注他时，主页按钮写的是「回关」，
    // 只认「关注」会让通知场景的回关一条都点不出去（2026-11-18 真机实测）。
    const FOLLOWABLE=['关注','回关'], DONE=['已关注','互相关注','已请求'];
    const hits=buttons.filter(e=>FOLLOWABLE.concat(DONE).includes((e.innerText||'').trim()));
    // 同一按钮的内层 span 会重复命中同一文案：先按包含关系去掉内层，
    // 再按屏幕位置去重（真机上主页同时渲染了外层按钮与内层文字两个节点）。
    const outer=hits.filter(e=>!hits.some(o=>o!==e&&e.contains(o)));
    const seenAt=new Map();
    for(const e of outer){
      const r=e.getBoundingClientRect();
      const key=Math.round(r.left)+':'+Math.round(r.top);
      if(!seenAt.has(key))seenAt.set(key,e);
    }
    const unique=Array.from(seenAt.values());
    // PC 主页会同时渲染顶部常驻栏和页面内的按钮组，同一功能出现两个「回关」。
    // 只认「恰好一个」会让回关在真机上一条都点不出去；改成取第一个真能点中的
    // （尺寸够、未被遮挡、未禁用），并把它当时的文案作为状态。
    const usable=unique.map(e=>({e,pt:point(e)})).filter(o=>o.pt);
    const pick=usable.length?usable[0].e:null;
    const control=pick?{...point(pick),state:(pick.innerText||'').trim()}:null;
    // 用户类型/粉丝数筛选的数据源：粉丝数在资料区文本里（关注 X 粉丝 Y 获赞 Z），
    // 认证徽章是昵称附近的徽章元素（企业认证/个人认证），读不到就如实返回空。
    const fansM=/(?:^|\\s)粉丝\\s*([\\d.]+\\s*[万亿wW]?)/.exec(info);
    const badges=Array.from(document.querySelectorAll(
      '[data-e2e*="badge"],[data-e2e*="verify"],[class*="verify" i],[class*="badge" i]'))
      .map(e=>(e.innerText||'').trim()).filter(Boolean).slice(0,4);
    const badgeText=badges.join('|');
    const enterprise=/官方账号|企业/.test(badgeText) ? 1 : 0;
    const personal=/认证/.test(badgeText)&&!enterprise ? 1 : 0;
    // 诊断用：按钮的文案 + 位置 + e2e 标记（不含用户内容）。
    // 定位不到时能看出是「文案改版」「多个同类按钮」还是「按钮在折叠区里」。
    const candidates=buttons.map(e=>{const r=e.getBoundingClientRect();return {
      t:(e.innerText||'').trim().slice(0,8),x:Math.round(r.left),y:Math.round(r.top),
      w:Math.round(r.width),h:Math.round(r.height),
      e2e:e.getAttribute('data-e2e')||'',tag:e.tagName.toLowerCase()};})
      .filter(o=>o.t&&o.w>8).slice(0,12);
    return {userId:${JSON.stringify(command.recipientId)},nickname:${JSON.stringify(command.commentAuthor)},gender,
      age:age?Number(age[1]):null,private:/私密账号|仅朋友可见/.test(document.body.innerText||''),control,candidates,
      fansText:fansM?fansM[1]:'', enterprise, personal};
  })()`;
}
function profileDiagJs(command,nickname) {
  // 「按钮没定位到」时用来自证卡在哪一步：profileProbe 的一切不满足都返回
  // null，光看回执分不清是页面还没到、页头身份不对、昵称对不上，还是按钮文案变了。
  return `(()=>{
    const pick=s=>Array.from(document.querySelectorAll(s));
    const titles=pick('h1,[data-e2e="user-title"]').map(e=>(e.innerText||'').trim()).filter(Boolean).slice(0,3);
    const self=pick('header [class*="userMenuPanel"]').map(e=>e.textContent||'').join(' ').slice(0,40);
    const btns=pick('main button,main [role="button"],[data-e2e="user-detail"] button,[data-e2e="user-detail"] [role="button"]')
      .map(e=>{const r=e.getBoundingClientRect();
        // 逐项复现 point() 的判定，才知道「点不中」是尺寸、禁用还是被遮挡。
        const x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);
        return {t:(e.innerText||'').trim().slice(0,10),
          w:Math.round(r.width),h:Math.round(r.height),dis:!!e.disabled,
          hit:hit?(hit.tagName.toLowerCase()+(hit.getAttribute('data-e2e')?'['+hit.getAttribute('data-e2e')+']':'')):'null',
          hitIn:!!(hit&&(hit===e||e.contains(hit))),
          vis:getComputedStyle(e).visibility,pe:getComputedStyle(e).pointerEvents,
          op:getComputedStyle(e).opacity};})
      .filter(o=>o.t&&o.w>8).slice(0,12);
    return {url:location.href.split('?')[0],expectUrl:${JSON.stringify(profileUrl(command.recipientId))},
      expectTitle:${JSON.stringify(command.commentAuthor)},expectSelf:${JSON.stringify(nickname)},
      self,titles,btns,ready:document.readyState,
      vw:innerWidth,vh:innerHeight,vis:document.visibilityState,
      loginWall:!!document.querySelector('#login-pannel,[data-e2e="login-guide"],.login-mask'),
      verify:!!document.querySelector('#captcha_container,[data-e2e="verify-dialog"]')};
  })()`;
}
function profileAllowed(config,p) {
  if(!p||p.private!==false)return false;
  const gender=config.targetGender||'all';
  if(gender!=='all'&&p.gender!==gender.replace(/_unknown$/,'')&&!(gender.endsWith('_unknown')&&p.gender==null))return false;
  return !config.ageFilterEnabled||Number.isInteger(p.age)&&p.age>=config.targetAgeMin&&p.age<=config.targetAgeMax;
}
async function inspectProfile(command,io) {
  if(!profileUrl(command.recipientId)||!command.commentAuthor||!command.nickname)return {ok:false,error:'用户主页标识或账号身份不完整'};
  if(io.hasDraft&&await io.hasDraft())return {ok:false,error:'页面存在未发送草稿，请先处理草稿'};
  await io.navigate(profileUrl(command.recipientId));
  for(let i=0;i<20;i++) {
    if(io.stopped?.())return {ok:false,error:'已停止'};
    const item=await io.evaluate(profileProbe(command,command.nickname));
    if(item?.needVerify)return {ok:false,needVerify:true,error:'用户主页需要人工验证'};
    if(item?.loginWall)return {ok:false,loginWall:true,error:'账号需要重新登录'};
    if(item)return {ok:true,item};
    await io.sleep(500);
  }
  return {ok:true,item:{},reason:'无法核对用户主页或公开资料，此用户跳过'};
}
function shareEntry(command,nickname) {
  return `(() => {
    if(document.querySelector('#login-pannel,#captcha_container,[data-e2e="verify-dialog"]'))return null;
    const self=Array.from(document.querySelectorAll('header [class*="userMenuPanel"]')).map(e=>e.textContent||'').join(' ');
    if(!self.startsWith(${JSON.stringify(nickname)}+'关注'))return null;
    const point=e=>{e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);
      return r.width>8&&r.height>8&&!e.disabled&&(hit===e||e.contains(hit))?{x,y}:null;};
    let container;
    if(${JSON.stringify(command.type)}==='share-profile') {
      if(location.href.split('?')[0]!==${JSON.stringify(profileUrl(command.recipientId))})return null;
      if(!Array.from(document.querySelectorAll('h1,[data-e2e="user-title"]')).some(e=>(e.innerText||'').trim()===${JSON.stringify(command.commentAuthor)}))return null;
      const headers=Array.from(document.querySelectorAll('[data-e2e="user-detail"],main')).filter(e=>e.querySelector('h1,[data-e2e="user-title"]'));
      container=headers.find(e=>!headers.some(other=>other!==e&&e.contains(other)));
    }else{
      if(location.href.split('?')[0]!==${JSON.stringify(command.videoUrl)})return null;
      const row=document.getElementById('tooltip_'+${JSON.stringify(command.commentId)})?.closest('[data-e2e="comment-item"]');
      if(!row)return null;
      const own=s=>Array.from(row.querySelectorAll(s)).find(e=>e.closest('[data-e2e="comment-item"]')===row);
      const author=own('[data-e2e="comment-username-1"],a[href*="/user/"]'),info=own('.comment-item-info-wrap');
      const content=info?.nextElementSibling||own('[data-e2e="comment-level-1"],[class*="comment-content"]');
      if((author?.innerText||'').trim()!==${JSON.stringify(command.commentAuthor)}||(content?.innerText||'').trim()!==${JSON.stringify(command.sourceText)})return null;
      container=row;
    }
    if(!container)return null;
    const candidates=Array.from(container.querySelectorAll('[data-e2e="user-share"],[data-e2e="profile-share"],[data-e2e="comment-share"],button,[role="button"],[aria-label="分享"]'))
      .filter(e=>e.getAttribute('aria-label')==='分享'||(e.innerText||'').trim()==='分享');
    const unique=candidates.filter(e=>!candidates.some(other=>other!==e&&e.contains(other)));
    return unique.length===1?point(unique[0]):null;
  })()`;
}
const SHARE_DIALOG_JS=`(() => {
  const visible=e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;};
  const dialogs=Array.from(document.querySelectorAll('[role="dialog"],[data-e2e="share-dialog"],[class*="share-dialog"],[class*="shareDialog"]')).filter(visible);
  const nodes=dialogs.filter(e=>!dialogs.some(other=>other!==e&&e.contains(other))).filter(e=>/分享给|发送给|分享至/.test(e.innerText||''));
  if(nodes.length!==1)return null;
  const panel=nodes[0];
  const point=e=>{if(!e||e.disabled||e.getAttribute('aria-disabled')==='true')return null;const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);
    return visible(e)&&hit&&(hit===e||e.contains(hit))?{x,y}:null;};
  const rows=Array.from(panel.querySelectorAll('[data-user-id],[data-sec-uid],[data-conversation-id]')).filter(e=>!e.parentElement?.closest('[data-user-id],[data-sec-uid],[data-conversation-id]'));
  const contacts=rows.map(e=>{
    const name=e.querySelector('[data-e2e="contact-name"],[class*="nickname"],[class*="contact-name"]')?.textContent?.trim()||e.getAttribute('data-name')||'';
    const group=e.getAttribute('data-conversation-id'),id=group||e.getAttribute('data-sec-uid')||e.getAttribute('data-user-id');
    return {name,id,kind:group?'group':'user',selected:e.getAttribute('aria-selected')==='true'||e.querySelector('input[type="checkbox"]')?.checked===true,
      point:point(e),checkbox:point(e.querySelector('input[type="checkbox"],[role="checkbox"]'))};
  });
  const search=Array.from(panel.querySelectorAll('input')).filter(e=>/搜索|好友|群聊/.test(e.placeholder||'')&&visible(e));
  const sends=Array.from(panel.querySelectorAll('button,[role="button"]')).filter(e=>['发送','分享'].includes((e.innerText||'').trim()));
  const drafts=Array.from(panel.querySelectorAll('textarea,[contenteditable="true"]')).some(e=>(e.value||e.innerText||'').trim());
  return {contacts,search:search.length===1?{...point(search[0]),text:search[0].value}:null,send:sends.length===1?point(sends[0]):null,draft:drafts,
    url:location.href.split('?')[0],self:Array.from(document.querySelectorAll('header [class*="userMenuPanel"]')).map(e=>e.textContent||'').join(' '),
    blocked:!!document.querySelector('#login-pannel,#captcha_container,[data-e2e="verify-dialog"]')};
})()`;
async function openShare(command,io,nickname) {
  if(command.type==='share-profile')await io.navigate(profileUrl(command.recipientId));
  else {
    await io.navigate(command.videoUrl);
    if(!io.readComments)return false;
    const data=await io.readComments();
    if((data?.comments||[]).filter(c=>c.commentId===command.commentId&&c.nickname===command.commentAuthor&&c.text===command.sourceText).length!==1)return false;
  }
  for(let i=0;i<12;i++) {
    if(io.stopped?.())return false;
    const entry=await io.evaluate(shareEntry(command,nickname));
    if(entry){await io.click(entry.x,entry.y);break;}
    await io.sleep(500);
  }
  for(let i=0;i<10;i++) {
    if(io.stopped?.())return false;
    if(await io.evaluate(SHARE_DIALOG_JS))return true;
    await io.sleep(400);
  }
  return false;
}
function exactContact(panel,name,id,kind) {
  if(!panel||panel.draft)return null;
  const names=panel.contacts.filter(c=>c.name===name);
  return names.length===1&&(!id||names[0].id===id&&names[0].kind===kind)?names[0]:null;
}
async function findContact(io,name,id,kind) {
  let panel=await io.evaluate(SHARE_DIALOG_JS),match=exactContact(panel,name,id,kind);
  if(match)return {panel,match};
  // Do not replace a user's typed search or existing selected recipients.
  if(!panel||panel.draft||panel.contacts.some(c=>c.selected)||!panel.search||panel.search.text)return null;
  await io.click(panel.search.x,panel.search.y);await io.type(name);
  await io.sleep(700);
  panel=await io.evaluate(SHARE_DIALOG_JS);match=exactContact(panel,name,id,kind);
  return match?{panel,match}:null;
}
async function inspectShareTargets(command,io) {
  if(!['share-profile','share-comment'].includes(command.shareType)||!Array.isArray(command.receiverNames)||!command.nickname)return {ok:false,error:'分享目标参数无效'};
  const receivers=[];
  for(const name of command.receiverNames.slice(0,50)) {
    if(io.stopped?.())return {ok:false,error:'已停止'};
    if(io.hasDraft&&await io.hasDraft())return {ok:false,error:'请先处理未发送的草稿'};
    if(!await openShare({...command,type:command.shareType},io,command.nickname))return {ok:true,receivers,reason:'平台原生分享入口不可定位'};
    const found=await findContact(io,name);
    if(found&&/^[A-Za-z0-9_-]{6,160}$/.test(found.match.id)){
      receivers.push({name,id:found.match.id,kind:found.match.kind});return {ok:true,receivers};
    }
  }
  return {ok:true,receivers};
}
// Only exact server acknowledgement of a native card counts. Protocols that
// return binary/opaque payloads remain uncertain and require manual readback.
const SHARE_HOOK_JS=`(() => {
  if(window.__leadShareHook)return;window.__leadShareHook=true;window.__leadShareReceipts=[];window.__leadShareSeq=0;
  const relevant=url=>/\\/(?:im|message)[^?]*\\/(?:send|send_message)(?:[/?]|$)/.test(String(url));
  const add=(url,http,body)=>{if(!relevant(url))return;try{
    const b=JSON.parse(body),m=b.message||b.data?.message;let c=m?.content;
    if(typeof c==='string')c=JSON.parse(c);
    if(!c||typeof c!=='object')return;
    const card=c.card||c;
    window.__leadShareReceipts.push({seq:++window.__leadShareSeq,http,code:b.status_code,
      id:String(m.server_message_id||''),receiver:String(m.recipient_sec_uid||m.recipient_uid||m.conversation_id||''),
      receiverKind:m.conversation_id&&!m.recipient_sec_uid&&!m.recipient_uid?'group':'user',
      cardType:String(card.type||''),userId:String(card.sec_uid||card.user_id||''),videoId:String(card.aweme_id||''),commentId:String(card.comment_id||'')});
    if(window.__leadShareReceipts.length>10)window.__leadShareReceipts.shift();}catch(_){} };
  const fetch=window.fetch;window.fetch=function(...args){const url=String(args[0]?.url||args[0]||'');
    return fetch.apply(this,args).then(res=>{if(relevant(url))res.clone().text().then(body=>add(url,res.status,body)).catch(()=>{});return res;});};
  const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,url,...rest){this.__leadShareUrl=String(url||'');return open.call(this,method,url,...rest);};
  XMLHttpRequest.prototype.send=function(...args){if(relevant(this.__leadShareUrl))this.addEventListener('loadend',()=>{
    try{add(this.__leadShareUrl,this.status,this.responseText);}catch(_){}},{once:true});return send.apply(this,args);};
})()`;
function classifyShare(entries,command) {
  for(const e of Array.isArray(entries)?entries:[]) {
    if(e.receiver!==command.shareRecipientId||e.receiverKind!==command.shareRecipientKind)continue;
    const target=command.type==='share-profile'?e.cardType==='user'&&e.userId===command.recipientId:
      e.cardType==='comment'&&e.commentId===command.commentId&&e.videoId===command.videoUrl.split('/').pop();
    if(!target)continue;
    if(e.http===200&&e.code===0&&/^[0-9]{6,}$/.test(e.id))return 'sent_ok';
    if(e.http>=200&&e.http<500&&Number.isInteger(e.code)&&e.code!==0)return 'sent_fail';
  }
  return 'uncertain';
}
async function executeSocial(command,io,guard) {
  let submitted=false;
  let qr={};
  const skip=code=>({ok:false,status:'skipped',code,...qr});
  try {
    if(!guard.active())return skip('stopped');
    if(['follow-user','share-profile'].includes(command.type)) {
      const page=await inspectProfile({...command,nickname:guard.scope.nickname},io);
      if(!page.ok||!profileAllowed(guard.scope.effectiveConfig||{},page.item))return skip('profile_filter_or_identity_changed');
      if(command.type==='follow-user') {
        const probe=profileProbe(command,guard.scope.nickname);
        let before=page.item.control;
        const FOLLOWABLE=['关注','回关'];
        if(['已关注','互相关注','已请求'].includes(before?.state))return skip('already_followed');
        // 「按钮文案认得，但没有可点位置」= elementFromPoint 打不中，通常是后台窗口
        // 被判定为不可见/被遮挡（2026-11-18 真机踩到：同一个人第一次执行 hasPoint=false，
        // 重开壳后同样的代码一次点中）。这类是环境态，先重新确保窗口可交互再读一次，
        // 不该直接判成「找不到按钮」。
        let stuck=!FOLLOWABLE.includes(before?.state)&&Array.isArray(page.item.candidates)&&
          page.item.candidates.some(c=>FOLLOWABLE.includes(String(c.t||'').trim()));
        if(stuck&&typeof guard.reactable==='function'&&guard.active()) {
          await guard.reactable();
          const retry=await io.evaluate(probe);
          if(FOLLOWABLE.includes(retry?.control?.state)&&Number.isFinite(retry?.control?.x)) {
            before=retry.control;stuck=false;
            diag('follow-retry-ok',{state:retry.control.state});
          }
        }
        if(!FOLLOWABLE.includes(before?.state)||!Number.isFinite(before.x)||!guard.active()) {
          // 现场回传给 node 侧日志：主页上「关注/回关」按钮没定位到时，
          // 只报一句 follow_control_not_verified 无法判断是改版还是多个同类按钮。
          // probe 的静默失败（页面还没到、昵称对不上、页头身份变了）也会落到
          // control 为空，所以再就地探一次「到底卡在哪一步」。
          let why=null;
          try{why=await io.evaluate(profileDiagJs(command,guard.scope.nickname));}catch(_){why=null;}
          diag('follow-diag',{state:before?.state||'',hasPoint:Number.isFinite(before?.x),
            reason:page.reason||'',candidates:page.item.candidates||[],why:why||{}});
          return skip(stuck?'follow_control_occluded':'follow_control_not_verified');
        }
        const again=await io.evaluate(probe);
        if(!profileAllowed(guard.scope.effectiveConfig||{},again)||
           !FOLLOWABLE.includes(again.control?.state)||!guard.active())return skip('follow_target_changed');
        if(!guard.claim())return skip('acceptance_attempt_consumed');
        submitted=true;await io.click(again.control.x,again.control.y);
        let lastState=null;
        for(let i=0;i<15;i++) {
          await io.sleep(500);
          const after=await io.evaluate(probe);
          const state=after?.control?.state||'';
          // 点了却没读到翻转时，至少能看出「按钮还在原样」还是「页面读不到了」。
          if(i===0||state!==lastState) {
            diag('follow-after',{i,state,probed:!!after});
            lastState=state;
          }
          if(['已关注','互相关注'].includes(after?.control?.state)) {
            await io.navigate(profileUrl(command.recipientId));
            const refreshed=await io.evaluate(probe);
            if(['已关注','互相关注'].includes(refreshed?.control?.state))return {ok:true,status:'sent_ok',code:'follow_persisted_after_reload'};
          }
          if(!guard.active())break;
        }
        // 轮询读不到翻转不等于没关注上：真机上点完按钮会重排，probe 有一段时间
        // 拿不到 control（返回 null），而关注其实已经生效（2026-11-18 实测：
        // 记成 uncertain 之后，同一条通知的按钮已经变成「已关注/互相关注」）。
        // 所以最后重载主页读一次真实状态 —— 已关注就记成功，仍可关注才记不确定。
        if(guard.active()) {
          await io.navigate(profileUrl(command.recipientId));
          const final=await io.evaluate(probe);
          const state=final?.control?.state||'';
          diag('follow-reload',{state,probed:!!final});
          if(['已关注','互相关注'].includes(state))
            return {ok:true,status:'sent_ok',code:'follow_persisted_after_reload'};
        }
        return {ok:false,status:'uncertain',code:'follow_state_unknown_no_retry'};
      }
    }
    // 分享评论要凭「已确认的原文」定位那条评论；分享主页的目标是用户本身，
    // 不需要原文，批量跟进（mode=batch）因此可以不写 reviewedActions。
    const approved=(guard.scope.reviewedActions||[]).find(a=>a.type===command.type&&a.videoUrl===command.videoUrl&&(a.commentId||'')===(command.commentId||'')&&(a.recipientId||'')===(command.recipientId||''));
    const sourceText=approved?approved.sourceText:(command.type==='share-profile'?'':null);
    if(sourceText===null||!await openShare({...command,sourceText},io,guard.scope.nickname))return skip('native_share_source_missing');
    if(command.saveProfileQr) {
      const saved=await require('./profile-qr').saveProfileQr(command,io,guard);
      if(!saved.ok) {
        if(saved.code==='profile_qr_saved_share_not_sent')qr={qrFile:saved.qrFile||'',qrUserId:saved.qrUserId||command.recipientId,qrSha256:saved.qrSha256||''};
        return skip(saved.code||'profile_qr_not_saved');
      }
      const {ok,...details}=saved;qr=details;
      if(!saved.shareDialog||!guard.active())return skip('profile_qr_saved_share_not_sent');
    }
    if(!guard.active())return skip('stopped');
    const found=await findContact(io,command.shareRecipient,command.shareRecipientId,command.shareRecipientKind);
    if(!found||found.panel.contacts.some(c=>c.selected))return skip('share_recipient_missing_ambiguous_or_preselected');
    // Select only an explicit checkbox. A generic row may send immediately.
    if(!found.match.checkbox)return skip('share_checkbox_not_verified');
    await io.click(found.match.checkbox.x,found.match.checkbox.y);
    const panel=await io.evaluate(SHARE_DIALOG_JS),selected=panel?.contacts?.filter(c=>c.selected)||[];
    const sourceUrl=command.type==='share-profile'?profileUrl(command.recipientId):command.videoUrl;
    if(panel?.blocked||panel?.url!==sourceUrl||!panel?.self?.startsWith(guard.scope.nickname+'关注')||panel?.draft||selected.length!==1||selected[0].name!==command.shareRecipient||selected[0].id!==command.shareRecipientId||selected[0].kind!==command.shareRecipientKind||!panel.send||!guard.active())return skip('share_selection_changed');
    await io.evaluate(SHARE_HOOK_JS);
    const seq=await io.evaluate('window.__leadShareSeq || 0');
    if(!guard.active())return skip('stopped');
    if(!guard.claim())return skip('acceptance_attempt_consumed');
    submitted=true;await io.click(panel.send.x,panel.send.y);
    for(let i=0;i<20;i++) {
      await io.sleep(500);
      const receipts=await io.evaluate('(window.__leadShareReceipts||[]).filter(e=>e.seq>'+Number(seq)+')');
      const status=classifyShare(receipts,command);
      if(status!=='uncertain')return {ok:status==='sent_ok',status,code:status==='sent_ok'?'native_card_receiver_and_receipt_verified':'share_rejected',...qr};
    }
    return {ok:false,status:'uncertain',code:'share_receipt_missing_no_retry',...qr};
  }catch(_){return {ok:false,status:submitted?'uncertain':'skipped',
    code:submitted?'social_outcome_unknown_no_retry':command.saveProfileQr?'profile_qr_not_saved':'social_page_not_verified',...qr};}
}
module.exports={profileProbe,profileAllowed,inspectProfile,shareEntry,SHARE_DIALOG_JS,openShare,exactContact,findContact,inspectShareTargets,
  SHARE_HOOK_JS,classifyShare,executeSocial};
