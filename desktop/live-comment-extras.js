"use strict";
// Only platform-created mention chips and native sticker selections are used.
// These DOM adapters require live calibration; unknown layouts fail closed.
const {videoIdOf}=require('./write-commands');
const HELPERS=`
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;};
 const point=e=>{if(!e||e.disabled||e.getAttribute('aria-disabled')==='true')return null;
   e.scrollIntoView({block:'nearest'});const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);
   return visible(e)&&(hit===e||e.contains(hit))?{x,y}:null;};
 const unique=nodes=>nodes.filter(e=>!nodes.some(other=>other!==e&&e.contains(other)));
 const containers=Array.from(document.querySelectorAll('.comment-input-inner-container')).filter(visible);
 const container=containers.length===1?containers[0]:null;
 const editors=container?Array.from(container.querySelectorAll('[contenteditable="true"]')).filter(visible):[];
 const editor=editors.length===1?editors[0]:null;
`;
function pickerButton(kind) {
  return `(() => {${HELPERS}
    if(!editor)return null;
    const kind=${JSON.stringify(kind)},labels=kind==='sticker'?['表情','表情包','GIF','添加表情']:['@','@账号','提及','提及账号'];
    const selector=kind==='sticker'?'[data-e2e="comment-emoji"],[data-e2e="comment-expression"]':'[data-e2e="comment-mention"]';
    const nodes=unique(Array.from(container.querySelectorAll(selector+',button,[role="button"]')).filter(e=>visible(e)&&
      (e.matches(selector)||labels.includes(e.getAttribute('aria-label'))||labels.includes(e.getAttribute('title'))||labels.includes((e.innerText||'').trim()))));
    return nodes.length===1?point(nodes[0]):null;
  })()`;
}
const STICKERS_JS=`(() => {${HELPERS}
  const panels=unique(Array.from(document.querySelectorAll('[data-e2e="emoji-panel"],[data-e2e="sticker-panel"],[role="dialog"],[role="tabpanel"]')).filter(visible))
    .filter(e=>e.querySelector('[data-sticker-id],[data-emoji-id]')||/表情包|GIF|动图/.test(e.innerText||''));
  if(panels.length!==1)return null;
  const p=panels[0],tabs=unique(Array.from(p.querySelectorAll('[role="tab"],button')).filter(e=>visible(e)&&['GIF','动图','表情包'].includes((e.innerText||'').trim())));
  const selected=tabs.find(e=>e.getAttribute('aria-selected')==='true');
  const nodes=unique(Array.from(p.querySelectorAll('[data-sticker-id],[data-emoji-id]')).filter(visible));
  const items=nodes.map(e=>{const img=e.matches('img')?e:e.querySelector('img');return {
    id:e.getAttribute('data-sticker-id')||e.getAttribute('data-emoji-id'),name:e.getAttribute('data-name')||img?.alt||e.getAttribute('title')||'',
    url:img?.currentSrc||img?.src||'',point:point(e)};}).filter(s=>s.point&&s.id&&s.name&&s.url.startsWith('https://'));
  return {items,tab:!selected&&tabs.length===1?point(tabs[0]):null};
})()`;
const MENTIONS_JS=`(() => {${HELPERS}
  const panels=unique(Array.from(document.querySelectorAll('[data-e2e="mention-panel"],[role="listbox"],[role="dialog"]')).filter(visible))
    .filter(e=>e.querySelector('input[placeholder*="搜索"],input[placeholder*="账号"],input[placeholder*="昵称"]'));
  if(panels.length!==1)return null;
  const p=panels[0],inputs=Array.from(p.querySelectorAll('input')).filter(e=>visible(e)&&e.type!=='checkbox');
  const rows=unique(Array.from(p.querySelectorAll('[data-sec-uid],[data-user-id]')).filter(visible));
  return {search:inputs.length===1?{...point(inputs[0]),value:inputs[0].value}:null,
    items:rows.map(e=>({id:e.getAttribute('data-sec-uid')||e.getAttribute('data-user-id'),
      name:e.getAttribute('data-name')||e.querySelector('[data-e2e="user-name"],[class*="nickname"]')?.textContent?.trim()||'',
      handle:e.getAttribute('data-unique-id')||e.querySelector('[data-e2e="user-unique-id"]')?.textContent?.trim()?.replace(/^抖音号[:：]?\\s*/, '')||'',point:point(e)}))};
})()`;
const DRAFT_JS=`(() => {${HELPERS}
  if(!editor)return null;
  const nodes=unique(Array.from(editor.querySelectorAll('[data-mention-id],[contenteditable="false"][data-user-id],a[data-sec-uid]')));
  const mentions=nodes.map(e=>({id:e.getAttribute('data-mention-id')||e.getAttribute('data-sec-uid')||e.getAttribute('data-user-id'),name:(e.innerText||'').trim().replace(/^@/, '')}));
  const stickers=unique(Array.from(container.querySelectorAll('[data-e2e="comment-sticker-preview"] [data-sticker-id],[data-e2e="comment-sticker-preview"][data-sticker-id],img[data-sticker-id]')))
    .map(e=>({id:e.getAttribute('data-sticker-id'),url:(e.matches('img')?e:e.querySelector('img'))?.src||''}));
  return {text:(editor.innerText||'').split(String.fromCharCode(160)).join(' ').trim(),mentions,stickers,focused:document.activeElement===editor};
})()`;
function targets(command) {
  if(!command.mentionTargets)return [];
  try {const list=JSON.parse(command.mentionTargets);
    if(!Array.isArray(list)||!list.length||list.length>10||new Set(list.map(m=>m.id)).size!==list.length)return null;
    if(list.some(m=>!m||!/^[A-Za-z0-9_-]{6,160}$/.test(m.id)||typeof m.name!=='string'||!m.name||m.name.length>100||typeof m.query!=='string'||!m.query||m.query.length>100))return null;
    return list;
  }catch(_){return null;}
}
function validExtras(command) {
  const list=targets(command);
  if(!list)return false;
  if(command.stickerId&&(!/^[A-Za-z0-9_-]{1,160}$/.test(command.stickerId)||!command.stickerName||typeof command.stickerUrl!=='string'||!/^https:\/\/\S{1,1500}$/.test(command.stickerUrl)||command.attachmentPath))return false;
  if(!command.stickerId&&(command.stickerName||command.stickerUrl))return false;
  if((list.length||command.stickerId)&&!['post-comment','reply-comment'].includes(command.type))return false;
  if(list.length) {
    if(!['before','after'].includes(command.mentionPosition)||typeof command.commentBody!=='string')return false;
    const names=list.map(m=>'@'+m.name).join(' ');
    if(command.text!==(command.mentionPosition==='before'?names+' '+command.commentBody:command.commentBody+' '+names))return false;
  }else if(command.mentionPosition||command.commentBody)return false;
  return true;
}
async function openPicker(kind,io,active) {
  if(!active())return null;
  const button=await io.evaluate(pickerButton(kind));
  if(!button||!active())return null;
  await io.click(button.x,button.y);
  const script=kind==='sticker'?STICKERS_JS:MENTIONS_JS;
  for(let i=0;i<12;i++) {
    if(!active())return null;
    const panel=await io.evaluate(script);
    if(panel) {
      if(kind==='sticker'&&panel.tab) {await io.click(panel.tab.x,panel.tab.y);await io.sleep(300);return await io.evaluate(script);}
      return panel;
    }
    await io.sleep(250);
  }
  return null;
}
async function findMention(query,io,active) {
  let panel=await openPicker('mention',io,active);
  if(!panel?.search||panel.search.value||!Number.isFinite(panel.search.x)||!active())return null;
  await io.click(panel.search.x,panel.search.y);await io.type(query);
  let match=null;
  // Let native search finish; a transient first result is never accepted.
  for(let i=0;i<12;i++) {
    await io.sleep(350);if(!active())return null;
    panel=await io.evaluate(MENTIONS_JS);
    if(!panel||panel.search?.value!==query)return null;
    const matches=panel.items.filter(m=>m.name===query||m.handle===query);
    const unique=matches.filter((m,i,a)=>a.findIndex(v=>v.id===m.id)===i);
    if(unique.length!==1){match=null;continue;}
    if(match&&match.id===unique[0].id&&match.name===unique[0].name)return unique[0];
    match=unique[0];
  }
  return null;
}
async function inspectExtras(command,io) {
  const fail=error=>({ok:false,error});
  if(!videoIdOf(command.videoUrl)||!command.nickname||!Number.isInteger(command.stickerCount)||command.stickerCount<0||command.stickerCount>8||
    !Array.isArray(command.mentionQueries)||command.mentionQueries.length>10||command.mentionQueries.some(q=>typeof q!=='string'||!q||q.length>100))return fail('GIF或@账号参数无效');
  if(io.hasDraft&&await io.hasDraft())return fail('页面存在未发送草稿，请先处理草稿');
  const active=()=>!io.stopped?.();
  const navigate=async()=>{
    if(!active())return false;
    await io.navigate(command.videoUrl);
    const identity=await io.evaluate(require('./live-platform').IDENTITY_JS);
    // 与 live-platform 同口径：点「下一个」进来时地址不变，先认画面里正在播的那条。
    const here=identity?(String(identity.videoId||'')||videoIdOf(identity.url)):'';
    return !!identity&&here===videoIdOf(command.videoUrl)&&!identity.verify&&!identity.loginWall&&identity.self.startsWith(command.nickname+'关注');
  };
  const result={ok:true,stickers:[],mentions:[]};
  try {
    if(!await navigate())return fail('无法核对当前账号或视频，未发送');
    const draft=await io.evaluate(DRAFT_JS);
    if(!draft||draft.text||draft.mentions.length||draft.stickers.length)return fail('原生评论编辑器不可用或存在草稿');
    if(command.stickerCount) {
      const panel=await openPicker('sticker',io,active);
      if(!panel||new Set(panel.items.map(s=>s.id)).size!==panel.items.length||panel.items.length<command.stickerCount)return fail('平台GIF不足配置数量或表情入口不可用');
      result.stickers=panel.items.slice(0,command.stickerCount).map(({id,name,url})=>({id,name,url}));
    }
    for(const query of command.mentionQueries) {
      if(!await navigate())return fail('账号身份变化或准备已停止');
      const m=await findMention(query,io,active);
      if(!m)return fail('@账号不存在、重名或无法定位：'+query+'，请填写准确抖音号');
      result.mentions.push({query,id:m.id,name:m.name});
    }
    // Only search fields were typed. Never select a user/sticker during planning.
    if(!active())return fail('已停止准备');
    await io.key?.('Escape');
    return result;
  }catch(_){return fail('读取原生GIF或@账号失败，未发送');}
}
async function focusEnd(io,active) {
  if(!active()||!io.key)return false;
  const focused=await io.evaluate(`(() => {${HELPERS}if(!editor)return false;editor.focus();return document.activeElement===editor;})()`);
  if(!focused||!active())return false;
  await io.key('End',['control']);return true;
}
function matchesDraft(draft,command) {
  const list=targets(command);
  return !!draft&&!!list&&draft.text===command.text.trim()&&
    draft.mentions.length===list.length&&list.every((m,i)=>draft.mentions[i].id===m.id&&draft.mentions[i].name===m.name)&&
    (command.stickerId?draft.stickers.length===1&&draft.stickers[0].id===command.stickerId&&draft.stickers[0].url===command.stickerUrl:!draft.stickers.length);
}
async function composeExtras(command,io,active) {
  if(!validExtras(command)||!await focusEnd(io,active))return false;
  const list=targets(command);
  if(!list.length||command.mentionPosition==='after')await io.type(list.length?command.commentBody+' ':command.text);
  for(let i=0;i<list.length;i++) {
    const m=list[i],found=await findMention(m.query,io,active);
    if(!found||found.id!==m.id||found.name!==m.name||!found.point||!active())return false;
    await io.click(found.point.x,found.point.y);await io.sleep(250);
    const draft=await io.evaluate(DRAFT_JS);
    if(!draft||draft.mentions.length!==i+1||draft.mentions.some((d,j)=>d.id!==list[j].id||d.name!==list[j].name)||!await focusEnd(io,active))return false;
    await io.type(' ');
  }
  if(list.length&&command.mentionPosition==='before')await io.type(command.commentBody);
  if(command.stickerId) {
    const panel=await openPicker('sticker',io,active);
    const found=panel?.items.filter(s=>s.id===command.stickerId&&s.name===command.stickerName&&s.url===command.stickerUrl)||[];
    if(found.length!==1||!found[0].point||!active())return false;
    await io.click(found[0].point.x,found[0].point.y);await io.sleep(300);
  }
  return await focusEnd(io,active)&&matchesDraft(await io.evaluate(DRAFT_JS),command);
}
module.exports={pickerButton,STICKERS_JS,MENTIONS_JS,DRAFT_JS,targets,validExtras,openPicker,findMention,inspectExtras,composeExtras,matchesDraft};
