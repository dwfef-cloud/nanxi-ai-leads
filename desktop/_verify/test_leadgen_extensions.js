const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto'),vm=require('node:vm');
const social=require('../live-social'),media=require('../live-media'),live=require('../live-platform');
const extras=require('../live-comment-extras'),qr=require('../profile-qr');
const {classifyCommentReceipts,COMMENT_HOOK_JS}=require('../write-commands');
const {EXTRA_FIELDS,signatureFields}=require('../leadgen-extensions');
const video='https://www.douyin.com/video/7400000000000000001';
const base={videoUrl:video,recipientId:'user_123456',commentAuthor:'潜客',commentId:'123456789',sourceText:'想要开发工作流',text:'',
  shareRecipient:'测试 群',shareRecipientId:'group_123456',shareRecipientKind:'group'};
function fixture(type,options={}) {
  const command={...base,type},p={userId:base.recipientId,nickname:base.commentAuthor,gender:'male',age:28,private:false};
  let followed=!!options.already,selected=false,submitted=0,claimed=0,opened=0;
  const point={x:2,y:2},entry={x:1,y:1},send={x:3,y:3};
  const contact={name:base.shareRecipient,id:base.shareRecipientId,kind:'group',point,checkbox:point};
  const receipt={seq:1,http:200,code:0,id:'123456789',receiver:base.shareRecipientId,receiverKind:'group',
    cardType:type==='share-profile'?'user':'comment',userId:base.recipientId,videoId:video.split('/').pop(),commentId:base.commentId};
  const scope={nickname:'执行账号',effectiveConfig:{targetGender:'male'},reviewedActions:[command]};
  const io={navigate:async()=>{},sleep:async()=>{},stopped:()=>false,
    readComments:async()=>({comments:options.changedSource?[]:[{commentId:base.commentId,nickname:base.commentAuthor,text:base.sourceText}]}),
    click:async x=>{if(x===1)opened++;if(x===2)selected=true;if(x===3)submitted++;if(x===4){submitted++;followed=true;}},
    type:async()=>assert.fail('this fixture already has the exact contact'),
    evaluate:async code=>{
      if(code===social.profileProbe(command,scope.nickname))return {...p,gender:options.wrongGender?'female':'male',control:{state:followed?'已关注':'关注',x:4,y:4}};
      if(code===social.shareEntry(command,scope.nickname))return entry;
      if(code===social.SHARE_DIALOG_JS)return {url:type==='share-profile'?'https://www.douyin.com/user/'+base.recipientId:video,self:options.accountChanged&&selected?'别的账号关注':'执行账号关注',
        contacts:options.duplicate?[{...contact,selected},{...contact,selected}]:[{...contact,checkbox:options.noCheckbox?null:point,selected:options.preselected||selected}],
        send,draft:options.draft||''};
      if(code===social.SHARE_HOOK_JS)return;
      if(code==='window.__leadShareSeq || 0')return 0;
      if(code.startsWith('(window.__leadShareReceipts'))return options.receipts===undefined?[receipt]:options.receipts;
      assert.fail('Unknown social probe: '+code.slice(0,70));
    }};
  const guard={scope,active:()=>!options.stopped,claim:()=>{claimed++;return !options.consumed;}};
  return {command,io,guard,receipt,run:()=>social.executeSocial(command,io,guard),state:()=>({submitted,claimed,opened})};
}
test('follow persists after reload and never clicks an already-followed control',async()=>{
  let f=fixture('follow-user');assert.equal((await f.run()).status,'sent_ok');assert.equal(f.state().submitted,1);
  f=fixture('follow-user',{already:true});assert.equal((await f.run()).code,'already_followed');assert.equal(f.state().submitted,0);
  for(const options of [{wrongGender:true},{stopped:true},{consumed:true}]){
    f=fixture('follow-user',options);assert.equal((await f.run()).status,'skipped');assert.equal(f.state().submitted,0);
  }
});
for(const type of ['share-profile','share-comment']) {
  test(type+' uses native selection and exact recipient/card acknowledgement',async()=>{
    const f=fixture(type);assert.equal((await f.run()).status,'sent_ok');assert.equal(f.state().submitted,1);assert.equal(f.state().claimed,1);
  });
  test(type+' preserves drafts and refuses ambiguous, preselected or unverified recipients',async()=>{
    for(const options of [{duplicate:true},{draft:'用户草稿'},{preselected:true},{noCheckbox:true},{stopped:true},{consumed:true},{accountChanged:true}]){
      const f=fixture(type,options);assert.equal((await f.run()).status,'skipped');assert.equal(f.state().submitted,0);
    }
  });
  test(type+' unknown or wrong card receipt never counts as sent',async()=>{
    const initial=fixture(type);
    for(const receipts of [[],[{...initial.receipt,receiver:'other_12345'}],[{...initial.receipt,id:''}],
      [{...initial.receipt,userId:'other_12345',commentId:'999999999'}],[{...initial.receipt,cardType:'text'}]]) {
      const f=fixture(type,{receipts});assert.equal((await f.run()).status,'uncertain');assert.equal(f.state().submitted,1);
    }
  });
}
test('changed original comment is skipped before opening share picker',async()=>{
  const f=fixture('share-comment',{changedSource:true});assert.equal((await f.run()).status,'skipped');assert.deepEqual(f.state(),{submitted:0,claimed:0,opened:0});
});
test('public gender and age filter mirrors the page including explicitly allowed unknown gender',()=>{
  const p={private:false,gender:null,age:25};
  assert.equal(social.profileAllowed({targetGender:'male'},p),false);
  assert.equal(social.profileAllowed({targetGender:'male_unknown'},p),true);
  assert.equal(social.profileAllowed({targetGender:'all',ageFilterEnabled:true,targetAgeMin:20,targetAgeMax:30},p),true);
  assert.equal(social.profileAllowed({targetGender:'all',ageFilterEnabled:true,targetAgeMin:20,targetAgeMax:30},{...p,age:null}),false);
});
test('social probes compile with hostile user-controlled names',()=>{
  const c={...base,type:'share-comment',commentAuthor:'\"\\\n</script>',shareRecipient:'\"\\\n</script>'};
  for(const code of [social.profileProbe(c,'账号'),social.shareEntry(c,'账号'),social.SHARE_DIALOG_JS,social.SHARE_HOOK_JS,media.IMAGE_UPLOAD_HOOK_JS,media.uploadProbe({attachmentName:'\"\\\n</script>',attachmentSize:20,attachmentSha256:'abc'})])
    assert.doesNotThrow(()=>new Function('return '+code));
});
test('native share hook reduces JSON receipts and ignores unrelated and opaque messages',async()=>{
  const c={...base,type:'share-comment'};
  let body={status_code:0,message:{server_message_id:'123456789',conversation_id:base.shareRecipientId,
    content:JSON.stringify({type:'comment',aweme_id:video.split('/').pop(),comment_id:base.commentId}),secret:'do-not-retain'}};
  const window={fetch:async()=>({status:200,clone:()=>({text:async()=>JSON.stringify(body)})})};
  function XHR(){}XHR.prototype.open=function(){};XHR.prototype.send=function(){};
  vm.runInNewContext(social.SHARE_HOOK_JS,{window,XMLHttpRequest:XHR});
  await window.fetch('https://www.douyin.com/im/message/send');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(social.classifyShare(window.__leadShareReceipts,c),'sent_ok');
  assert.ok(!JSON.stringify(window.__leadShareReceipts).includes('do-not-retain'));
  body={status_code:0,message:{content:'opaque'}};
  await window.fetch('https://www.douyin.com/im/message/send');await new Promise(resolve=>setImmediate(resolve));
  await window.fetch('https://www.douyin.com/other/read');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(window.__leadShareReceipts.length,1);
});
function imageFixture(t){
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'lead-photo-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const file=path.join(dir,'测试图.png'),data=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4E8AAAAASUVORK5CYII=','base64');
  fs.writeFileSync(file,data);
  return {dir,data,file,command:{attachmentPath:file,attachmentName:path.basename(file),attachmentSize:data.length,attachmentSha256:crypto.createHash('sha256').update(data).digest('hex')}};
}
test('photo authorization rejects changed, missing, oversized or replaced files',t=>{
  const {file,data,command}=imageFixture(t);assert.equal(media.verifyImage(command),true);
  for(const patch of [{attachmentSize:1},{attachmentSha256:'x'},{attachmentName:'other.png'},{attachmentPath:'../image.png'}])assert.equal(media.verifyImage({...command,...patch}),false);
  fs.writeFileSync(file,Buffer.concat([data,Buffer.from('changed')]));assert.equal(media.verifyImage(command),false);
});
test('picture must be uploaded and returned in the published comment; text-only receipt stays uncertain',()=>{
  const expected={videoId:'7400000000000000001',commentId:'',text:'这是已确认的测试正文',imageUri:'tos-cn/image_verified'};
  const body={status_code:0,comment:{cid:'123456789',aweme_id:expected.videoId,reply_id:'0',text:expected.text}};
  const receipt=()=>[{status:200,targetVideoId:expected.videoId,targetCommentId:'0',body:JSON.stringify(body)}];
  assert.equal(classifyCommentReceipts(receipt(),expected),'uncertain');
  body.comment.image_list=[{uri:'tos-cn/another_image'}];assert.equal(classifyCommentReceipts(receipt(),expected),'uncertain');
  body.comment.image_list=[{origin_image:{uri:expected.imageUri}}];assert.equal(classifyCommentReceipts(receipt(),expected),'sent_ok');
});
test('image upload cannot detach an existing debugger or set files into ambiguous inputs',async t=>{
  const {command}=imageFixture(t);let attached=0,detached=0,uploaded=0;
  const web={debugger:{isAttached:()=>true,attach:()=>attached++,detach:()=>detached++}};
  await assert.rejects(()=>media.uploadImage(web,command,media.INPUT_SELECTOR,()=>true));assert.equal(detached,0);
  web.debugger={isAttached:()=>false,attach:()=>attached++,detach:()=>detached++,sendCommand:async method=>method==='DOM.getDocument'?{root:{nodeId:1}}:method==='DOM.querySelectorAll'?{nodeIds:[2,3]}:uploaded++};
  await assert.rejects(()=>media.uploadImage(web,command,media.INPUT_SELECTOR,()=>true));assert.equal(uploaded,0);assert.equal(detached,1);
});
test('scope binds image bytes and exact recipient even for a correctly signed replacement',t=>{
  const {dir,command:photo}=imageFixture(t),file=path.join(dir,'scope.json');
  const action={...base,type:'share-comment'},scope={secret:'x'.repeat(40),scopeId:'extensions',accountId:'a',leadgenTaskId:'t',nickname:'执行账号',expiresAt:Date.now()/1000+60,
    videoUrls:[video],reviewedActions:[action],actions:{'share-comment':1}};
  fs.writeFileSync(file,JSON.stringify(scope));
  const sign=a=>{const c={...a,accountId:'a',leadgenTaskId:'t',scopeId:'extensions',deadlineAt:Date.now()+60000};
    c.acceptanceSignature=crypto.createHmac('sha256',scope.secret).update(JSON.stringify(signatureFields(c,['type','accountId','leadgenTaskId','videoUrl','commentId','text','scopeId','deadlineAt']).map(k=>String(c[k]||'')))).digest('hex');return c;};
  assert.ok(live.scopeFor(sign(action),file));
  for(const patch of [{shareRecipient:'另一个群'},{shareRecipientId:'other_123456'},{shareRecipientKind:'user'},photo])assert.equal(live.scopeFor(sign({...action,...patch}),file),null);
  assert.equal(require('../action-executor').rejectLiveAction(action).status,'blocked');
});

test('native mention and GIF metadata is exact, ordered, and cannot fall back to plain text',()=>{
  const targets=[{query:'AI助手',id:'user_123456',name:'AI助手'}];
  const command={type:'reply-comment',text:'@AI助手 谢谢分享工作流。',commentBody:'谢谢分享工作流。',mentionPosition:'before',
    mentionTargets:JSON.stringify(targets),stickerId:'gif_123456',stickerName:'点头',stickerUrl:'https://cdn.example/gif.webp'};
  assert.equal(extras.validExtras(command),true);
  assert.equal(extras.validExtras({...command,text:'AI助手 谢谢分享工作流。'}),false);
  assert.equal(extras.validExtras({...command,type:'send-dm'}),false);
  assert.equal(extras.validExtras({...command,attachmentPath:'other.png'}),false);
  assert.equal(extras.matchesDraft({text:command.text,mentions:[{id:'user_123456',name:'AI助手'}],
    stickers:[{id:'gif_123456',url:'https://cdn.example/gif.webp'}]},command),true);
  assert.equal(extras.matchesDraft({text:command.text,mentions:[],stickers:[]},command),false);
  assert.doesNotThrow(()=>new Function('return '+extras.MENTIONS_JS));
  assert.doesNotThrow(()=>new Function('return '+extras.STICKERS_JS));
});

test('mention receipt must contain exact platform user IDs and UTF-16 text offsets',()=>{
  const write=require('../write-commands');
  const targets=[{id:'user_123456',name:'AI助手'}],text='谢谢 @AI助手 的分享';
  const start=text.indexOf('@AI助手');
  assert.equal(write.mentionsConfirmed([{type:0,user_id:targets[0].id,start,end:start+5}],targets,text),true);
  assert.equal(write.mentionsConfirmed([{type:0,user_id:'another_123456',start,end:start+5}],targets,text),false);
  assert.equal(write.mentionsConfirmed([{type:0,user_id:targets[0].id,start:start+1,end:start+5}],targets,text),false);
  const astral='AI😀 @AI助手';const utf16Start=astral.indexOf('@AI助手');
  assert.equal(write.mentionsConfirmed([{type:0,sec_uid:targets[0].id,start:utf16Start,end:utf16Start+5}],targets,astral),true);
});

test('QR output is account-safe, run-scoped and opened with recoverable unique names',t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'lead-qr-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const first=qr.runDirectory('task_run_key',root),again=qr.runDirectory('task_run_key',root),next=qr.runDirectory('another_run',root);
  assert.equal(first,again);assert.notEqual(first,next);
  assert.equal(qr.safeName('CON: /用户?'), 'CON_ _用户_');
  assert.throws(()=>qr.runDirectory('../escape',root));
  assert.throws(()=>qr.runDirectory('',root));
});
