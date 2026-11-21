const assert=require("node:assert/strict");
const test=require("node:test");
const fs=require("node:fs");
const os=require("node:os");
const path=require("node:path");
const crypto=require("node:crypto");
const live=require("../live-platform");
const {classifyCommentReceipts}=require("../write-commands");
const vm=require("node:vm");

test("reviewed main comments reject a signed but unapproved text",t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),"leads-reviewed-"));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const file=path.join(dir,"scope.json"),video="https://www.douyin.com/video/7400000000000000001";
  const text="定制开发时，固定工作流和自主规划应该如何选择？";
  const scope={secret:"x".repeat(40),scopeId:"reviewed",accountId:"a",leadgenTaskId:"t",nickname:"测试账号",
    expiresAt:Date.now()/1000+60,videoUrls:[video],actions:{"post-comment":1},reviewedComments:[{videoUrl:video,text}]};
  const signed=value=>{
    const command={type:"post-comment",accountId:"a",leadgenTaskId:"t",videoUrl:video,
      text:value,commentId:"",scopeId:"reviewed",deadlineAt:Date.now()+60000};
    command.acceptanceSignature=crypto.createHmac("sha256",scope.secret)
      .update(JSON.stringify([command.type,"a","t",video,"",value,"reviewed",String(command.deadlineAt)])).digest("hex");
    return command;
  };
  fs.writeFileSync(file,JSON.stringify(scope));
  assert.ok(live.scopeFor(signed(text),file));
  assert.equal(live.scopeFor(signed("换一条评论也不能发送"),file),null);
  for(const malformed of [null,{},[],[{videoUrl:video,text:"另一条评论"}]]) {
    fs.writeFileSync(file,JSON.stringify({...scope,reviewedComments:malformed}));
    assert.equal(live.scopeFor(signed(text),file),null);
  }
});

test("landscape toolbar uses authoritative state and rejects ambiguous layouts",()=>{
  let state="video-player-no-collect", ambiguous=false;
  const hidden={getBoundingClientRect:()=>({width:0,height:0}),getAttribute:()=>state};
  const items=["0 0 90 94","0 0 99 99","0 0 100 100",""].map(box=>({
    querySelector:s=>s==='svg'?{getAttribute:()=>box}: {},
    scrollIntoView(){},getBoundingClientRect:()=>({left:10,top:20,width:40,height:30}),
    contains:hit=>hit===items[2]
  }));
  items[3].parentElement={children:items};
  const document={querySelectorAll:s=>s.includes('share-icon')?(ambiguous?[items[3],items[3]]:[items[3]]):[hidden],
    elementFromPoint:()=>items[2]};
  const probe=()=>vm.runInNewContext(live.stateProbe("favorite-video"),{document});
  assert.equal(probe().already,false);assert.equal(probe().hit,true);
  state="video-player-collected";assert.equal(probe().already,true);
  state="video-player-is-collected";assert.equal(probe().already,true);
  ambiguous=true;assert.equal(probe(),null);
});

test("scope signatures bind all action fields; single attempt survives reloading",()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),"leads-scope-"));
  const file=path.join(dir,"scope.json");
  const video="https://www.douyin.com/video/7400000000000000001";
  const scope={secret:"x".repeat(40),scopeId:"one",accountId:"a",leadgenTaskId:"t",nickname:"测试账号",
    expiresAt:Date.now()/1000+60,videoUrls:[video],actions:{"post-comment":1}};
  fs.writeFileSync(file,JSON.stringify(scope));
  const command={type:"post-comment",accountId:"a",leadgenTaskId:"t",videoUrl:video,
    text:"先明确工作流的输入和输出",commentId:"",scopeId:"one",deadlineAt:Date.now()+60000};
  command.acceptanceSignature=crypto.createHmac("sha256",scope.secret)
    .update(JSON.stringify([command.type,"a","t",video,"",command.text,"one",String(command.deadlineAt)])).digest("hex");
  assert.ok(live.scopeFor(command,file));
  for(const key of ["type","accountId","leadgenTaskId","videoUrl","commentId","text","scopeId","deadlineAt"])
    assert.equal(live.scopeFor({...command,[key]:"other"},file),null);
  assert.equal(live.claimAttempt({...scope,file},"post-comment"),true);
  assert.equal(live.claimAttempt(live.scopeFor(command,file),"post-comment"),false);
  scope.expiresAt=0;fs.writeFileSync(file,JSON.stringify(scope));
  assert.equal(live.scopeFor(command,file),null);
});

test("observed live page probes compile and root parent zero is a main comment",()=>{
  for(const script of [live.IDENTITY_JS,live.prepareEditor(""),live.prepareEditor("123456"),
    live.EDITOR_JS,live.SUBMIT_JS,live.stateProbe("like-video"),live.stateProbe("favorite-video")])
    assert.doesNotThrow(()=>new Function("return "+script));
  const receipt={status:200,targetVideoId:"7400000000000000001",targetCommentId:"0",
    body:JSON.stringify({status_code:0,comment:{cid:"c1",aweme_id:"7400000000000000001",reply_id:"0",text:"正文一致"}})};
  assert.equal(classifyCommentReceipts([receipt],{videoId:"7400000000000000001",commentId:"",text:"正文一致"}),"sent_ok");
  assert.equal(classifyCommentReceipts([receipt],{videoId:"7400000000000000001",commentId:"123456",text:"正文一致"}),"uncertain");
});

test("fresh blank worker navigates before probing and restores clipboard after copy",async t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),"leads-blank-worker-"));
  const file=path.join(dir,"scope.json"),video="https://www.douyin.com/video/7400000000000000001";
  const scope={secret:"x".repeat(40),scopeId:"blank",accountId:"a",leadgenTaskId:"t",nickname:"测试账号",
    expiresAt:Date.now()/1000+60,videoUrls:[video],actions:{"copy-video-link":1}};
  fs.writeFileSync(file,JSON.stringify(scope));
  const previous=process.env.LEADS_LIVE_ACCEPTANCE_SCOPE;process.env.LEADS_LIVE_ACCEPTANCE_SCOPE=file;
  t.after(()=>{if(previous===undefined)delete process.env.LEADS_LIVE_ACCEPTANCE_SCOPE;else process.env.LEADS_LIVE_ACCEPTANCE_SCOPE=previous;});
  const command={type:"copy-video-link",accountId:"a",leadgenTaskId:"t",videoUrl:video,scopeId:"blank",deadlineAt:Date.now()+60000};
  command.acceptanceSignature=crypto.createHmac("sha256",scope.secret)
    .update(JSON.stringify([command.type,"a","t",video,"","","blank",String(command.deadlineAt)])).digest("hex");
  let navigated=false,clipboard="original user text",restored=false;
  const io={currentUrl:()=>"",navigate:async url=>{assert.equal(url,video);navigated=true;},
    evaluate:async()=>{assert.ok(navigated,"cannot execute a draft probe on a blank worker");
      return {url:video,loginWall:false,verify:false,self:"测试账号关注"};},sleep:async()=>{},
    clipboard:{read:()=>clipboard,write:value=>{clipboard=value;},readText:()=>clipboard,
      restore:value=>{clipboard=value;restored=true;}}};
  const result=await live.executeLive(command,io);
  assert.equal(result.status,"sent_ok");assert.ok(restored);assert.equal(clipboard,"original user text");
  assert.equal((await live.executeLive(command,io)).code,"acceptance_attempt_consumed");
});
