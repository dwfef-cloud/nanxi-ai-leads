"use strict";
// Live page adapter, deliberately separate from interaction-dom's offline schema.
// A platform DOM/protocol change produces skipped/uncertain, never fake success.
const {trace}=require('./diag-log');
/**
 * 私信入口定位失败的现场诊断。
 *
 * 为什么需要：收件人主页上「私信」按钮定位不到时只回一句
 * recipient_profile_not_verified，分不清是 页面没到 / 昵称对不上 / 按钮有多个 /
 * 按钮点不中（被遮挡）。2026-11-18 真机就是「关注能成、私信必定 not_verified」，
 * 只凭返回码完全没法判断卡在哪一条。
 */
function diag(tag,payload) {
  try{console.error('['+tag+']',JSON.stringify(payload));}catch(_){}
  try{trace(tag,JSON.stringify(payload));}catch(_){}
}
const profileUrl=id=>/^[A-Za-z0-9_-]{6,160}$/.test(String(id||''))?'https://www.douyin.com/user/'+id:'';
function profileProbe(command,nickname) {
  return `(() => {
    const expected=${JSON.stringify(command.commentAuthor)},nickname=${JSON.stringify(nickname)};
    if(location.href.split('?')[0]!==${JSON.stringify(profileUrl(command.recipientId))})return null;
    if(document.querySelector('#login-pannel,#captcha_container,[data-e2e="verify-dialog"]'))return null;
    const self=Array.from(document.querySelectorAll('header [class*="userMenuPanel"]')).map(e=>e.textContent||'').join(' ');
    if(!self.startsWith(nickname+'关注'))return null;
    const titles=Array.from(document.querySelectorAll('h1,[data-e2e="user-title"]'));
    if(!titles.some(e=>(e.innerText||'').trim()===expected))return null;
    // 抖音主页会同时挂一个 0×0 的隐藏「私信」节点（预置菜单/骨架），
    // 按全局唯一判定会让每一次私信都判 not_verified —— 2026-11-18 真机：
    // dmButtons=[{88×33,hit},{0×0}]，dmCount=2，而「关注」那边只有一个按钮所以一直正常。
    // 这里先按可见尺寸过滤，再要求恰好一个；不放宽成「取第一个」——
    // 真要出现两个可见同名按钮，那是有歧义的页面，应该停下来等人看。
    const buttons=Array.from(document.querySelectorAll('button,[role="button"]'))
      .filter(e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;})
      .filter(e=>(e.innerText||'').trim()==='私信');
    if(buttons.length!==1)return null;
    const e=buttons[0],r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);
    return r.width>8&&r.height>8&&!e.disabled&&(hit===e||e.contains(hit))?{x,y}:null;
  })()`;
}
/**
 * 主页上「私信」定位失败时就地取证：把 profileProbe 的每条判据单独打出来。
 * 只读，不改页面。
 */
function dmDiagProbe(command,nickname) {
  return `(() => {
    const expected=${JSON.stringify(command.commentAuthor)},nickname=${JSON.stringify(nickname)};
    const self=Array.from(document.querySelectorAll('header [class*="userMenuPanel"]')).map(e=>e.textContent||'').join(' ');
    const titles=Array.from(document.querySelectorAll('h1,[data-e2e="user-title"]')).map(e=>(e.innerText||'').trim());
    const dm=Array.from(document.querySelectorAll('button,[role="button"]')).filter(e=>(e.innerText||'').trim()==='私信')
      .map(e=>{const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
        const hit=document.elementFromPoint(x,y);
        return {w:Math.round(r.width),h:Math.round(r.height),disabled:!!e.disabled,hit:hit===e||e.contains(hit)};});
    return {
      url:location.href.split('?')[0],
      urlOk:location.href.split('?')[0]===${JSON.stringify(profileUrl(command.recipientId))},
      expectTitle:expected, titles, titleOk:titles.some(t=>t===expected),
      expectSelfPrefix:nickname+'关注', selfOk:self.startsWith(nickname+'关注'),
      self:String(self).slice(0,140),
      dmButtons:dm, dmCount:dm.length,
      ready:document.readyState,
      loginWall:!!document.querySelector('#login-pannel'),
      verify:!!document.querySelector('#captcha_container,[data-e2e="verify-dialog"]')
    };
  })()`;
}
/**
 * 聊天面板核验失败时的取证：逐条回答「编辑器/面板/头部/收件人链接/发送」哪一条没过。
 * 只读，不发任何内容。
 */
function chatDiagProbe(command,nickname) {
  return `(() => {
    const target=${JSON.stringify(profileUrl(command.recipientId))},name=${JSON.stringify(command.commentAuthor)};
    const visible=e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8&&r.top>=0&&r.bottom<=innerHeight;};
    const self=Array.from(document.querySelectorAll('header [class*="userMenuPanel"]')).map(e=>e.textContent||'').join(' ');
    const editorSel='textarea[placeholder*="消息"],[contenteditable="true"][role="textbox"],[contenteditable="true"][data-slate-editor="true"]';
    const all=Array.from(document.querySelectorAll(editorSel));
    const vis=all.filter(visible);
    const editor=vis[0]||null;
${PANEL_FIND_JS}
    const links=panel?Array.from(panel.querySelectorAll('a[href*="/user/"]')).map(a=>({href:a.href.split('?')[0],text:(a.innerText||'').trim(),hrefOk:a.href.split('?')[0]===target,textOk:(a.innerText||'').trim()===name})):[];
    const sends=panel?Array.from(panel.querySelectorAll('button,[role="button"]')).filter(e=>visible(e)&&(e.innerText||e.getAttribute('aria-label')||'').trim()==='发送').length:0;
    // 编辑器祖先链：定位失败时最想知道的是「真正的面板长什么样、有多宽」。
    const chain=[];
    if(editor)for(let n=editor.parentElement;n&&n!==document.body&&chain.length<6;n=n.parentElement){
      const r=n.getBoundingClientRect();
      chain.push(Math.round(r.width)+'x'+Math.round(r.height)+'|'+String(n.className||'').slice(0,40));
    }
    const panelBtns=panel?Array.from(panel.querySelectorAll('button,[role="button"]')).slice(0,10)
      .map(b=>(b.innerText||b.getAttribute('aria-label')||'').trim()).filter(Boolean):[];
    return {url:location.href.slice(0,120), expectTarget:target,
      selfOk:self.startsWith(${JSON.stringify(nickname)}+'关注'),
      editorsTotal:all.length, editorsVisible:vis.length,
      editorTag:editor?editor.tagName.toLowerCase():'', editorPlaceholder:editor?(editor.getAttribute('placeholder')||''):'',
      panelFound:!!panel, panelSize:panel?(Math.round(panel.getBoundingClientRect().width)+'x'+Math.round(panel.getBoundingClientRect().height)):'',
      recipientLinks:links.slice(0,3), sendButtons:sends,
      panelButtons:panelBtns, ancestorChain:chain,
      panelSize2:panel?(Math.round(panel.getBoundingClientRect().width)+'x'+Math.round(panel.getBoundingClientRect().height)):'',
      panelLinkCount:panel?panel.querySelectorAll('a[href*="/user/"]').length:0,
      panelText:panel?String(panel.innerText||'').replace(/\s+/g,' ').slice(0,180):'',
      loginWall:!!document.querySelector('#login-pannel'),
      verify:!!document.querySelector('#captcha_container,[data-e2e="verify-dialog"]')};
  })()`;
}
// 从编辑器向上找私信面板。不依赖 class 名：2026-11-18 真机实测抖音用的是
// CSS Modules 生成的 componentsRightPanel* 系列，conversation-panel / chat-panel
// 这些传统类名一个都不存在。真机拿到的祖先链：
//   186x44 messageEditor imChatEditorContainer → 310x44 messageMsgInput* →
//   342x44 messageMsgInput container → 342x76 componentsRightPanel inputArea →
//   342x782 componentsRightPanel notHeaderArea（更外层还有抽屉根）
// 所以按「右侧抽屉的量级」锁定：宽度 240–720、高度 ≥200，并取**最外层**满足者 ——
// 收件人头部在 inputArea 的兄弟节点里，只取最内层拿不到。
// 不要求「含用户链接或发送按钮」：网页版私信是回车发送，面板里没有「发送」按钮。
const PANEL_FIND_JS = `
    let panel=null;
    if(editor)for(let n=editor.parentElement;n&&n!==document.body;n=n.parentElement){
      const pr=n.getBoundingClientRect();
      if(pr.width<240||pr.width>720||pr.height<200)continue;
      panel=n;
    }
`;

function chatProbe(command,nickname) {
  return `(() => {
    const target=${JSON.stringify(profileUrl(command.recipientId))},name=${JSON.stringify(command.commentAuthor)};
    if(document.querySelector('#login-pannel,#captcha_container,[data-e2e="verify-dialog"]'))return null;
    const self=Array.from(document.querySelectorAll('header [class*="userMenuPanel"]')).map(e=>e.textContent||'').join(' ');
    if(!self.startsWith(${JSON.stringify(nickname)}+'关注'))return null;
    const visible=e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8&&r.top>=0&&r.bottom<=innerHeight;};
    const editors=Array.from(document.querySelectorAll('textarea[placeholder*="消息"],[contenteditable="true"][role="textbox"],[contenteditable="true"][data-slate-editor="true"]')).filter(visible);
    if(editors.length!==1)return null;
    const editor=editors[0];
${PANEL_FIND_JS}
    if(!panel)return null;
    // 收件人身份仍然严格核对：面板内必须**恰好一个**指向该用户主页、且文案等于昵称的链接。
    // 只放宽「怎么找到面板」，不放宽「发给谁」——侧边栏里同名联系人不能当成收件人凭证，
    // 所以链接必须在 panel 内、且唯一。
    const links=Array.from(panel.querySelectorAll('a[href*="/user/"]')).filter(e=>visible(e)&&e.href.split('?')[0]===target&&(e.innerText||'').trim()===name);
    if(links.length!==1)return null;
    const buttons=Array.from(panel.querySelectorAll('button,[role="button"]')).filter(e=>visible(e)&&(e.innerText||e.getAttribute('aria-label')||'').trim()==='发送');
    const point=e=>{if(!e||e.disabled||e.getAttribute('aria-disabled')==='true')return null;
      const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);
      return hit&&(hit===e||e.contains(hit))?{x,y}:null;};
    return {draft:String(editor.value==null?editor.innerText||'':editor.value).trim(),
      editor:point(editor),submit:buttons.length===1?point(buttons[0]):null,focused:document.activeElement===editor};
  })()`;
}

// Accept only a JSON send acknowledgement containing the exact recipient,
// content, a server message id and status 0. Binary/unknown protocols stay unknown.
const DM_HOOK_JS=`(() => {
  if(window.__leadDmHook)return;window.__leadDmHook=true;window.__leadDmReceipts=[];window.__leadDmSeq=0;
  const relevant=url=>/\\/(?:im|message)[^?]*\\/(?:send|send_message)(?:[/?]|$)/.test(String(url));
  const add=(url,status,body)=>{if(!relevant(url))return;let value;
    try {const b=JSON.parse(body),m=b.message||b.data?.message;
      value={seq:++window.__leadDmSeq,http:status,code:b.status_code,
        recipientId:String(m?.recipient_sec_uid||''),text:typeof m?.text==='string'?m.text.slice(0,500):'',
        messageId:String(m?.server_message_id||'')};}catch(_){return;}
    window.__leadDmReceipts.push(value);if(window.__leadDmReceipts.length>10)window.__leadDmReceipts.shift();};
  const fetch=window.fetch;window.fetch=function(...args){const url=String(args[0]?.url||args[0]||'');
    return fetch.apply(this,args).then(res=>{if(relevant(url))res.clone().text().then(body=>add(url,res.status,body)).catch(()=>{});return res;});};
  const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,url,...rest){this.__leadDmUrl=String(url||'');return open.call(this,method,url,...rest);};
  XMLHttpRequest.prototype.send=function(...args){if(relevant(this.__leadDmUrl))this.addEventListener('loadend',()=>{
    try{add(this.__leadDmUrl,this.status,this.responseText);}catch(_){}},{once:true});return send.apply(this,args);};
})()`;
function classifyDmReceipts(entries,command) {
  for(const e of Array.isArray(entries)?entries:[]) {
    if(e.recipientId!==command.recipientId || e.text!==command.text)continue;
    if(e.http===200 && e.code===0 && /^[0-9]{6,}$/.test(e.messageId))return 'sent_ok';
    if(Number.isInteger(e.code)&&e.code!==0&&e.http>=200&&e.http<500)return 'sent_fail';
  }
  return 'uncertain';
}
async function executeDm(command,io,guard) {
  let submitted=false;
  const skipped=code=>({ok:false,status:'skipped',code});
  try {
    const url=profileUrl(command.recipientId);
    if(!url||!command.commentAuthor||typeof command.text!=='string'||command.text!==command.text.trim()||command.text.length<5||command.text.length>200)
      return skipped('invalid_dm_target_or_text');
    if(!guard.active())return skipped('stopped');
    await io.navigate(url);
    let open;
    for(let i=0;i<15;i++) {
      if(!guard.active())return skipped('stopped');
      open=await io.evaluate(profileProbe(command,guard.scope.nickname));
      if(open)break;await io.sleep(500);
    }
    if(!open){
      // 定位不到「私信」就把判据逐条摊开：下次真机不用再猜是哪一条没过。
      try{diag('dm-diag',await io.evaluate(dmDiagProbe(command,guard.scope.nickname)));}catch(_){}
      return skipped('recipient_profile_not_verified');
    }
    await io.click(open.x,open.y);
    // 点完「私信」后先留一张现场快照：panelFound=false 时最需要知道的是
    // 「到底没弹出来」还是「弹了别的东西（隐私提示/下载App/权限引导）」。
    try{
      await io.sleep(1200);
      const seen=await io.evaluate(`(() => {
        const out=[];
        for(const e of document.querySelectorAll('body *')){
          const r=e.getBoundingClientRect();if(r.width<80||r.height<40)continue;
          const cs=getComputedStyle(e);
          if(!(cs.position==='fixed'||Number(cs.zIndex)>50))continue;
          if(cs.visibility==='hidden'||cs.display==='none'||Number(cs.opacity)===0)continue;
          const t=(e.innerText||'').replace(/\\s+/g,' ').trim();
          if(!t||t.length>200)continue;
          out.push({tag:e.tagName.toLowerCase(),pos:cs.position,z:cs.zIndex,txt:t.slice(0,120)});
          if(out.length>=6)break;
        }
        return out;
      })()`);
      trace('dm-post-click',JSON.stringify(seen));
    }catch(e){trace('dm-post-click-failed',String((e&&e.message)||e).slice(0,160));}
    const probe=chatProbe(command,guard.scope.nickname);
    // 编辑器计数：用来区分「面板根本没开」和「开了但定位不合格」。
    // 前者可以再点一次；后者绝不能重点 —— 私信面板是 toggle，再点会把面板关掉。
    const countEditors=()=>io.evaluate(`(() => {
      const s='textarea[placeholder*="消息"],[contenteditable="true"][role="textbox"],[contenteditable="true"][data-slate-editor="true"]';
      const vis=e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8&&r.top>=0&&r.bottom<=innerHeight;};
      return Array.from(document.querySelectorAll(s)).filter(vis).length;
    })()`).catch(()=>-1);

    let chat=null, openedEditors=0;
    for(let attempt=0;attempt<3;attempt++) {
      if(!guard.active())return skipped('stopped');
      if(attempt>0) {
        // 重新取一次按钮位置：面板/浮层可能已经改变了布局。
        const again=await io.evaluate(profileProbe(command,guard.scope.nickname));
        if(again)open=again;
        await io.sleep(600);
        await io.click(open.x,open.y);
      }
      for(let i=0;i<14;i++) {
        if(!guard.active())return skipped('stopped');
        chat=await io.evaluate(probe);if(chat)break;
        await io.sleep(500);
      }
      if(chat)break;
      openedEditors=Number(await countEditors())||0;
      if(openedEditors>0)break;   // 面板开着只是定位不合格：停止，交给诊断
      trace('dm-click-retry',`attempt=${attempt+1} editors=0`);
    }
    if(!chat?.editor||!chat.submit){
      try{diag('dm-chat-diag',await io.evaluate(chatDiagProbe(command,guard.scope.nickname)));}
      catch(e){trace('dm-chat-diag-failed',String((e&&e.message)||e).slice(0,200));}
      trace('dm-chat-giveup',`editors=${openedEditors}`);
      return skipped('recipient_chat_not_verified');
    }
    if(chat.draft)return skipped('existing_dm_draft_preserved');
    await io.evaluate(DM_HOOK_JS);
    const seq=await io.evaluate('window.__leadDmSeq || 0');
    if(!guard.active())return skipped('stopped');
    await io.click(chat.editor.x,chat.editor.y);await io.type(command.text);
    chat=await io.evaluate(probe);
    if(!chat?.focused||chat.draft!==command.text||!chat.submit||!guard.active())return skipped('dm_draft_or_recipient_changed');
    if(!guard.claim())return skipped('acceptance_attempt_consumed');
    submitted=true;
    await io.click(chat.submit.x,chat.submit.y);
    for(let i=0;i<20;i++) {
      await io.sleep(500);
      const receipts=await io.evaluate('(window.__leadDmReceipts||[]).filter(e=>e.seq>'+Number(seq)+')');
      const status=classifyDmReceipts(receipts,command);
      if(status!=='uncertain') {
        const after=await io.evaluate(probe);
        if(status==='sent_ok'&&(!after||after.draft))continue;
        return {ok:status==='sent_ok',status,code:status==='sent_ok'?'dm_server_receipt_and_recipient_verified':'dm_send_rejected',recipientId:command.recipientId};
      }
    }
    return {ok:false,status:'uncertain',code:'dm_receipt_missing_no_retry'};
  } catch(_) {return {ok:false,status:submitted?'uncertain':'skipped',code:submitted?'dm_outcome_unknown':'dm_page_not_verified'};}
}
module.exports={profileUrl,profileProbe,chatProbe,DM_HOOK_JS,classifyDmReceipts,executeDm};
