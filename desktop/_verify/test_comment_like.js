const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
const {commentProbe,RECEIPT_HOOK,receiptStatus,executeCommentLike}=require('../comment-like');
const live=require('../live-platform');
const VIDEO='https://www.douyin.com/video/7400000000000000001';
const command={type:'like-comment',accountId:'a',videoUrl:VIDEO,commentId:'123456789',commentAuthor:'作者',text:'想了解工作流'};

function fixture(options={}) {
  let liked=!!options.already,clicks=0,stopped=false,claimed=0,probes=0,reloads=0,seeked=0;
  const row={querySelectorAll(selector){
    if(selector.startsWith('[id^='))return [marker];
    if(selector.startsWith('[data-e2e="comment-username'))return [name];
    if(selector==='.comment-item-info-wrap')return [{...own,nextElementSibling:body}];
    if(selector.startsWith('[data-e2e="comment-digg')){const other={...control,contains:e=>e===other};return options.ambiguousControl?[control,other]:[control];}
    return [];
  }};
  const own={closest:()=>row};
  const marker={...own,id:'tooltip_'+(options.wrongComment?'999999999':command.commentId)};
  const name={...own,innerText:options.wrongAuthor?'别人':'作者'};
  const body={...own,innerText:options.wrongText?'不同正文':options.badge?command.text+' 作者赞过':command.text};
  const control={...own,contains:e=>e===control,scrollIntoView(){},
    matches:s=>options.platformDom && s==='p.VpA2NKl1',
    classList:{contains:s=>s==='tOgCrAK_' && liked},
    closest:s=>s==='.comment-item-stats-container'?{closest:()=>row}:row,
    querySelector:s=>options.platformDom&&!options.missingIcon&&s==='svg.Z4qg7NEM'?{}:null,
    querySelectorAll:s=>s==='span'?[{}]:[],
    getAttribute:key=>key==='aria-pressed'?(options.unknownState||options.platformDom?null:String(liked)):key==='aria-disabled'?String(!!options.disabled):null,
    getBoundingClientRect:()=>({left:100,top:200,width:40,height:30,right:140,bottom:230})};
  const document={querySelectorAll:()=>options.ambiguousRow?[row,row]:options.missing?[]:[row],elementFromPoint:()=>options.obscured?null:control};
  const window={fetch:async()=>({status:options.http||200,clone:()=>({text:async()=>JSON.stringify({status_code:options.declined?1:0})})})};
  class XMLHttpRequest {
    open(){}
    addEventListener(_name,callback){this.callback=callback;}
    send(){this.status=options.http||200;this.responseText=JSON.stringify({status_code:options.declined?1:0});this.callback?.();}
  }
  const context=vm.createContext({document,window,innerWidth:1280,innerHeight:900,URL,URLSearchParams,XMLHttpRequest,location:{href:VIDEO}});
  const evaluate=async code=>{
    if(code===RECEIPT_HOOK&&options.noHook)return false;
    if(code.includes('const expected=')) {
      probes++;
      // 刷新后评论区收起、目标评论不在首屏：探测先拿不到，滚动若干屏后才出现。
      if(options.hiddenAfterRefresh && reloads>0 && probes<=options.hiddenAfterRefresh)
        return {found:false,reason:'comment_not_found'};
      if(options.changeBeforeClick&&probes===2)liked=true;
      if(options.stopBeforeClick&&probes===2)stopped=true;
    }
    if(code.startsWith('(dir =>')) {seeked++;return {opened:false,scrollTop:0};}
    return vm.runInContext(code,context);
  };
  const io={evaluate,sleep:async()=>{},reload:async()=>{reloads++;if(options.lostAfterRefresh)liked=false;if(options.stopDuringRefresh)stopped=true;if(options.throwDuringRefresh)throw Error('reload failed');},click:async()=>{
    clicks++;
    if(options.throwAfterClick)throw new Error('lost transport');
    if(!options.noState&&!options.declined)liked=true;
    if(options.stopAfterClick)stopped=true;
    if(!options.noReceipt){
      const url='https://'+(options.receiptHost||'www.douyin.com')+'/aweme/v1/web/comment/digg/';
      const body=new URLSearchParams({cid:options.receiptComment||command.commentId,
        digg_type:options.unlike?'0':'1',aweme_id:options.receiptVideo||VIDEO.split('/').pop()});
      if(options.xhr){const request=new XMLHttpRequest();request.open('POST',url);request.send(body);}
      else await window.fetch(url,{body});
      await Promise.resolve();await Promise.resolve();
    }
  }};
  io.ensureLivePreview=async()=>true;
  return {io,context,clicks:()=>clicks,claimed:()=>claimed,reloads:()=>reloads,seeked:()=>seeked,options:{active:()=>!stopped,
    identityMatches:async()=>!options.wrongAccount,claim:()=>{claimed++;return !options.consumed;}}};
}

test('comment DOM probe validates exact row, author, full text, state and hit target',async()=>{
  const f=fixture();assert.equal((await f.io.evaluate(commentProbe({commentId:command.commentId,authorName:command.commentAuthor,content:command.text}))).already,false);
  for(const options of [{wrongComment:true},{wrongAuthor:true},{wrongText:true},{ambiguousRow:true},{ambiguousControl:true},{unknownState:true},{obscured:true},{disabled:true}]){
    const sample=fixture(options);const result=await executeCommentLike(command,sample.io,sample.options);
    assert.equal(result.status,'skipped',JSON.stringify(options));assert.equal(sample.clicks(),0);
  }
});
test('comment-like requires both stable state and bound successful receipt',async()=>{
  const f=fixture();const result=await executeCommentLike(command,f.io,f.options);
  assert.equal(result.status,'sent_ok');assert.equal(f.clicks(),1);assert.equal(f.claimed(),1);
  assert.equal(result.code,'comment_like_receipt_state_and_reload_verified');assert.equal(f.reloads(),1);
  assert.equal(f.context.window.__commentLikeReceipts[0].comment,command.commentId);
});

test('missing or failed first visible frame never claims or clicks',async()=>{
  for(const ready of [undefined,async()=>false,async()=>{throw Error('preview disconnected');}]){
    const f=fixture();f.io.ensureLivePreview=ready;
    const result=await executeCommentLike(command,f.io,f.options);
    assert.equal(result.status,'skipped');assert.equal(f.clicks(),0);assert.equal(f.claimed(),0);
  }
});
test('successful transient state must survive refresh; failed readback never clicks twice',async()=>{
  for(const options of [{lostAfterRefresh:true},{stopDuringRefresh:true},{throwDuringRefresh:true}]){
    const f=fixture(options),result=await executeCommentLike(command,f.io,f.options);
    assert.equal(result.status,'uncertain');assert.equal(f.clicks(),1);assert.equal(f.claimed(),1);assert.equal(f.reloads(),1);
    if(options.lostAfterRefresh)assert.equal(result.code,'comment_like_not_retained_after_refresh');
  }
  const f=fixture();delete f.io.reload;
  assert.equal((await executeCommentLike(command,f.io,f.options)).code,'refresh_verification_unavailable');
  assert.equal(f.clicks(),0);
});
test('observed platform DOM uses the verified selected class and preserves existing likes',async()=>{
  const f=fixture({platformDom:true});
  assert.equal((await executeCommentLike(command,f.io,f.options)).status,'sent_ok');
  assert.equal(f.clicks(),1);
  for(const options of [{platformDom:true,already:true},{platformDom:true,missingIcon:true}]){
    const sample=fixture(options);
    assert.equal((await executeCommentLike(command,sample.io,sample.options)).status,'skipped');
    assert.equal(sample.clicks(),0);
  }
});
test('XHR receipts follow the same exact-target contract as fetch',async()=>{
  const f=fixture({xhr:true});assert.equal((await executeCommentLike(command,f.io,f.options)).status,'sent_ok');
  for(const options of [{xhr:true,receiptComment:'77777777'},{xhr:true,unlike:true},{receiptHost:'evil.invalid'}]){
    const f=fixture(options);assert.equal((await executeCommentLike(command,f.io,f.options)).status,'uncertain');
  }
});
test('existing likes, changed rows, wrong account and failed hook never submit',async()=>{
  for(const options of [{already:true},{changeBeforeClick:true},{wrongAccount:true},{noHook:true},{consumed:true},{stopBeforeClick:true}]){
    const f=fixture(options);assert.equal((await executeCommentLike(command,f.io,f.options)).status,'skipped');assert.equal(f.clicks(),0);
  }
});
test('missing evidence, wrong targets, unlike receipts and stops are uncertain',async()=>{
  for(const options of [{noReceipt:true},{noState:true},{receiptComment:'888888888'},{receiptVideo:'888888888'},
    {unlike:true},{http:503},{throwAfterClick:true},{stopAfterClick:true}]){
    const f=fixture(options);const result=await executeCommentLike(command,f.io,f.options);
    assert.equal(result.status,'uncertain',JSON.stringify(options));assert.equal(f.clicks(),1);
  }
});
test('explicit decline and conflicting receipts cannot count success',async()=>{
  const f=fixture({declined:true});assert.equal((await executeCommentLike(command,f.io,f.options)).status,'sent_fail');
  const receipt={comment:command.commentId,video:VIDEO.split('/').pop(),action:'1',http:200,code:0};
  assert.equal(receiptStatus([receipt,{...receipt,code:1}],{commentId:command.commentId,videoId:receipt.video}),'uncertain');
  assert.equal(receiptStatus([{...receipt,code:true}],{commentId:command.commentId,videoId:receipt.video}),'uncertain');
});
test('monitor scope waits on the visible render frame instead of the review panel',async()=>{
  const monitor={mode:'monitor',nickname:'测试账号',secret:'x'.repeat(40),scopeId:'s',expiresAt:Date.now()/1000+60,
    accountId:'a',leadgenTaskId:'t',videoUrls:['*'],actions:{'like-comment':100}};
  // 审核台面板不存在：ensureLivePreview 永远 false，但采集页可见帧核验通过 → 必须真的点下去。
  const ok=fixture();ok.io.ensureLivePreview=async()=>false;ok.io.ensureActionable=async()=>true;
  const result=await executeCommentLike(command,ok.io,{...ok.options,scope:monitor});
  assert.equal(result.status,'sent_ok');assert.equal(ok.clicks(),1);
  // 可见帧核验不通过 → 不点、不消耗尝试。
  for(const actionable of [async()=>false,async()=>{throw Error('page hidden');},undefined]){
    const f=fixture();f.io.ensureLivePreview=async()=>false;f.io.ensureActionable=actionable;
    const skipped=await executeCommentLike(command,f.io,{...f.options,scope:monitor});
    assert.equal(skipped.code,'comment_page_not_visible');assert.equal(f.clicks(),0);assert.equal(f.claimed(),0);
  }
  // 人工确认路径（非 monitor）仍然只认审核台首帧，不会被可见帧绕过。
  const reviewed=fixture();reviewed.io.ensureLivePreview=async()=>false;reviewed.io.ensureActionable=async()=>true;
  const blocked=await executeCommentLike(command,reviewed.io,{...reviewed.options,scope:{...monitor,mode:'review'}});
  assert.equal(blocked.code,'comment_page_not_visible');assert.equal(reviewed.clicks(),0);
});
test('refresh readback seeks through the lazy comment list before judging retention',async()=>{
  // 刷新后评论收起、目标评论在列表深处：必须滚动找回，不能直接判「点赞没留存」。
  const f=fixture({hiddenAfterRefresh:6});
  const result=await executeCommentLike(command,f.io,f.options);
  assert.equal(result.status,'sent_ok');assert.equal(result.code,'comment_like_receipt_state_and_reload_verified');
  assert.equal(f.clicks(),1);assert.ok(f.seeked()>0);
});
test('like-added badge on the same row never hides an already matched comment',async()=>{
  // 作者点赞后平台会往这一行追加「作者赞过」，正文因此变长；
  // 整段精确比对会让后续每条重试都报 comment_not_found（真机踩过）。
  const f=fixture({badge:true});
  const result=await executeCommentLike(command,f.io,f.options);
  assert.equal(result.status,'sent_ok');assert.equal(f.clicks(),1);
  // 正文确实不同时仍然要拒绝。
  const wrong=fixture({wrongText:true});
  assert.equal((await executeCommentLike(command,wrong.io,wrong.options)).status,'skipped');
});
test('live scope binds exact comment and author and a single durable attempt',()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'comment-like-scope-')),file=path.join(dir,'scope.json');
  const scope={secret:'x'.repeat(40),scopeId:'one-comment',expiresAt:Date.now()/1000+60,
    accountId:'a',nickname:'测试账号',leadgenTaskId:'t',videoUrls:[VIDEO],actions:{'like-comment':1},
    commentTargets:[{videoUrl:VIDEO,commentId:command.commentId,authorName:command.commentAuthor,content:command.text}]};
  fs.writeFileSync(file,JSON.stringify(scope));
  const cmd={...command,leadgenTaskId:'t',scopeId:scope.scopeId,deadlineAt:Date.now()+60000};
  const fields=['type','accountId','leadgenTaskId','videoUrl','commentId','text','scopeId','deadlineAt','commentAuthor'];
  cmd.acceptanceSignature=crypto.createHmac('sha256',scope.secret).update(JSON.stringify(fields.map(k=>String(cmd[k]||'')))).digest('hex');
  assert.ok(live.scopeFor(cmd,file));
  for(const field of ['commentId','commentAuthor','text','videoUrl'])assert.equal(live.scopeFor({...cmd,[field]:'changed'},file),null);
  const accepted=live.scopeFor(cmd,file);assert.equal(live.claimAttempt(accepted,'like-comment'),true);assert.equal(live.claimAttempt(accepted,'like-comment'),false);
  delete scope.commentTargets;fs.writeFileSync(file,JSON.stringify(scope));assert.equal(live.scopeFor(cmd,file),null);
});
