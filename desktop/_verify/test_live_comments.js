const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
const live=require('../live-platform'),{COMMENT_HOOK_JS}=require('../write-commands');
const extras=require('../live-comment-extras'),{classifyCommentReceipts}=require('../write-commands');
const {signatureFields}=require('../leadgen-extensions');
const video='https://www.douyin.com/video/7400000000000000001';
function setup(t,type,options={}) {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'live-comment-flow-')),file=path.join(dir,'scope.json');
  const action={type,videoUrl:video,text:'关于工作流的输入输出，可以具体说说吗？',keyword:'工作流',
    ...(type==='reply-comment'?{commentId:'123456789',commentAuthor:'潜客',sourceText:'工作流怎么做'}:{})};
  const scope={secret:'x'.repeat(40),scopeId:dir,accountId:'account',leadgenTaskId:'task',nickname:'执行账号',
    expiresAt:Date.now()/1000+60,actions:{[type]:1},videoUrls:[video],reviewedActions:[action]};
  fs.writeFileSync(file,JSON.stringify(scope));
  const old=process.env.LEADS_LEADGEN_SCOPE;process.env.LEADS_LEADGEN_SCOPE=file;
  t.after(()=>{if(old===undefined)delete process.env.LEADS_LEADGEN_SCOPE;else process.env.LEADS_LEADGEN_SCOPE=old;fs.rmSync(dir,{recursive:true,force:true});});
  const command={...action,accountId:'account',leadgenTaskId:'task',scopeId:scope.scopeId,deadlineAt:Date.now()+60000};
  const fields=['type','accountId','leadgenTaskId','videoUrl','commentId','text','scopeId','deadlineAt',
    ...(type==='reply-comment'?['commentAuthor','recipientId']:[])];
  const sign=c=>({...c,acceptanceSignature:crypto.createHmac('sha256',scope.secret).update(JSON.stringify(fields.map(k=>String(c[k]||'')))).digest('hex')});
  let text='',submits=0,typed=0,probes=0;
  const receipt={seq:1,status:200,targetVideoId:'7400000000000000001',targetCommentId:action.commentId||'0',
    body:JSON.stringify({status_code:0,comment:{cid:'987654321',aweme_id:'7400000000000000001',reply_id:action.commentId||'0',text:action.text}})};
  // ownCommentProbe：提交前拍基线、提交后看增量。用调用次数区分两次采样，
  // 便于模拟「别人发过同样文案」（before=1/mine=0，after 不增长）与
  // 「自己发成功」（before=0，after 多出一条 mine=1）。
  const ownProbe=()=>{probes+=1;return probes===1?(options.ownBefore||{total:0,mine:0}):(options.ownAfter||{total:0,mine:0});};
  const io={currentUrl:()=>'',navigate:async()=>{},sleep:async()=>{},stopped:()=>false,
    ensureLivePreview:async()=>!options.previewHidden,
    readComments:async()=>({comments:[{commentId:action.commentId,nickname:options.changedAuthor?'另一个人':action.commentAuthor,text:action.sourceText}]}),
    type:async value=>{text=value;typed++;},click:async(x)=>{if(x===30)submits++;},evaluate:async code=>{
      if(code===live.IDENTITY_JS)return {url:video,loginWall:false,verify:false,self:'执行账号关注'};
      if(code===live.prepareEditor(action.commentId||'',action)||code.includes('const id =')&&code.includes('comment-input-inner-container'))return {found:true,x:10,y:10};
      if(code===live.EDITOR_JS)return {text,focused:true,label:type==='reply-comment'?'回复 潜客':'留下评论',x:20,y:20};
      if(code===COMMENT_HOOK_JS)return true;
      if(code==='window.__commentReceiptSeq || 0')return 0;
      if(code===live.SUBMIT_JS)return {x:30,y:30};
      if(code.startsWith('(window.__commentReceipts'))return options.missingReceipt?[]:[receipt];
      if(code.includes('comment-username-1'))return ownProbe();
      if(code.includes("[data-e2e=\"comment-item\"]"))return true;
      throw Error('Unexpected probe');
    }};
  return {run:()=>live.executeLive(sign(command),io),state:()=>({typed,submits,probes}),command,sign,file,io};
}
for(const type of ['post-comment','reply-comment']) {
  test(type+' uses live adapter, exact approved text and platform acknowledgement',async t=>{
    const f=setup(t,type);assert.equal((await f.run()).status,'sent_ok');assert.equal(f.state().submits,1);
    assert.equal((await f.run()).code,'existing_draft_or_editor_missing');assert.equal(f.state().submits,1);
  });
}
test('reply target author change stops before typing and before consuming an attempt',async t=>{
  const f=setup(t,'reply-comment',{changedAuthor:true});assert.equal((await f.run()).code,'reply_source_changed_or_missing');
  assert.equal(f.state().typed,0);assert.equal(f.state().submits,0);
});
test('hidden live screen prevents real comment submission',async t=>{
  const f=setup(t,'post-comment',{previewHidden:true});assert.equal((await f.run()).code,'live_preview_not_visible');
  assert.equal(f.state().typed,0);assert.equal(f.state().submits,0);
});

test('video-like also waits for the verified visible live screen',async t=>{
  const f=setup(t,'like-video',{previewHidden:true});
  assert.equal((await f.run()).code,'live_preview_not_visible');
  assert.equal(f.state().typed,0);assert.equal(f.state().submits,0);
});
test('missing live receipt stays uncertain and cannot be submitted again',async t=>{
  const f=setup(t,'post-comment',{missingReceipt:true});assert.equal((await f.run()).status,'uncertain');
  assert.equal(f.state().submits,1);assert.notEqual((await f.run()).status,'sent_ok');assert.equal(f.state().submits,1);
});
test('someone else posted the same template text: not a success without a receipt',async t=>{
  // 页面上本来就有一条同文案、署别人的名；提交后条数没增长（或增长也仍是别人发的）
  const f=setup(t,'post-comment',{missingReceipt:true,
    ownBefore:{total:1,mine:0},ownAfter:{total:1,mine:0}});
  const result=await f.run();
  assert.equal(result.status,'uncertain');assert.notEqual(result.code,'visible_own_comment_verified');
});
test('missing receipt but my own new comment shows up: verified by text plus account',async t=>{
  const f=setup(t,'post-comment',{missingReceipt:true,
    ownBefore:{total:0,mine:0},ownAfter:{total:1,mine:1}});
  const result=await f.run();
  assert.equal(result.status,'sent_ok');assert.equal(result.code,'visible_own_comment_verified');assert.equal(result.visible,true);
});
test('new matching comment from another account still refuses to claim success',async t=>{
  // 提交后确实多了一条，但署名不是执行账号 —— 不能算我发出的
  const f=setup(t,'post-comment',{missingReceipt:true,
    ownBefore:{total:0,mine:0},ownAfter:{total:1,mine:0}});
  const result=await f.run();
  assert.equal(result.status,'uncertain');assert.equal(result.code,'comment_visible_but_not_own');
});
test('even correctly signed replacement text is rejected by worker review contract',t=>{
  const f=setup(t,'reply-comment');assert.equal(live.scopeFor(f.sign({...f.command,text:'换掉已确认正文'}),f.file),null);
  assert.equal(live.scopeFor(f.sign({...f.command,commentAuthor:'另一个作者'}),f.file),null);
});

test('unverified native GIF or mention never degrades into and sends plain text',async t=>{
  const f=setup(t,'post-comment');
  const targets=[{query:'AI助手',id:'user_123456',name:'AI助手'}];
  const command={...f.command,text:'@AI助手 关于工作流的输入输出，可以具体说说吗？',commentBody:'关于工作流的输入输出，可以具体说说吗？',
    mentionPosition:'before',mentionTargets:JSON.stringify(targets),stickerId:'gif_123456',stickerName:'点头',stickerUrl:'https://cdn.example/gif.webp'};
  const scope=JSON.parse(fs.readFileSync(f.file,'utf8'));scope.reviewedActions=[command];fs.writeFileSync(f.file,JSON.stringify(scope));
  const signed={...command,accountId:'account',leadgenTaskId:'task',scopeId:scope.scopeId,deadlineAt:Date.now()+60000};
  signed.acceptanceSignature=crypto.createHmac('sha256',scope.secret).update(JSON.stringify(signatureFields(signed,
    ['type','accountId','leadgenTaskId','videoUrl','commentId','text','scopeId','deadlineAt']).map(k=>String(signed[k]||'')))).digest('hex');
  const compose=extras.composeExtras;
  extras.composeExtras=async()=>false;
  const io={...f.io,ensureLivePreview:async()=>true,evaluate:async code=>code.includes('contenteditable')&&code.includes('textarea')&&code.includes('data-e2e=comment-sticker-preview')?false:f.io.evaluate(code)};
  try{assert.equal((await live.executeLive(signed,io)).code,'native_extras_not_verified');
    assert.equal(f.state().submits,0);assert.equal(f.state().typed,0);}
  finally{extras.composeExtras=compose;}
});

test('GIF receipt must be a platform-selected sticker ID; HTTP text-only success stays uncertain',()=>{
  const target={videoId:'7400000000000000001',commentId:'',text:'评论正文',stickerId:'gif_123456'};
  const make=sticker=>[{status:200,targetVideoId:target.videoId,targetCommentId:'0',body:JSON.stringify({status_code:0,
    comment:{cid:'987654321',aweme_id:target.videoId,reply_id:'0',text:target.text,...(sticker?{sticker_id:sticker}:{})}})}];
  assert.equal(classifyCommentReceipts(make(''),target),'uncertain');
  assert.equal(classifyCommentReceipts(make('gif_other'),target),'uncertain');
  assert.equal(classifyCommentReceipts(make('gif_123456'),target),'sent_ok');
});
