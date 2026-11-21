const test=require('node:test'),assert=require('node:assert/strict');
const dm=require('../live-dm');
const command={type:'send-dm',recipientId:'MS4wLjAB_target',commentAuthor:'潜客',text:'你好，关于工作流的问题可以交流。'};
function fixture(options={}) {
  let draft='',seq=0,claimed=0,clicks=0;
  const receipt={seq:1,http:200,code:0,recipientId:command.recipientId,text:command.text,messageId:'123456789'};
  const io={navigate:async()=>{},sleep:async()=>{},click:async()=>{clicks++;if(clicks===3){draft='';seq=1;}},
    type:async text=>{draft=text;},evaluate:async script=>{
      if(script===dm.profileProbe(command,'测试账号'))return options.profileMissing?null:{x:1,y:2};
      if(script===dm.chatProbe(command,'测试账号'))return options.wrongRecipient?null:
        {editor:{x:2,y:3},submit:{x:3,y:4},draft:options.draft||draft,focused:!options.unfocused};
      if(script==='window.__leadDmSeq || 0')return 0;
      if(script.startsWith('(window.__leadDmReceipts'))return options.receipts||[receipt];
    }};
  const guard={scope:{nickname:'测试账号'},active:()=>!options.stopped,claim:()=>{claimed++;return !options.consumed;}};
  return {run:()=>dm.executeDm(command,io,guard),state:()=>({claimed,clicks,seq})};
}
test('DM requires exact fresh server acknowledgement and cleared draft in same recipient panel',async()=>{
  const f=fixture();const result=await f.run();assert.equal(result.status,'sent_ok');assert.equal(f.state().claimed,1);
});
test('unknown protocol or different recipient, text or missing server id stays uncertain without retry',async()=>{
  for(const receipts of [[],[{http:200,code:0,recipientId:'other',text:command.text,messageId:'123456789'}],
    [{http:200,code:0,recipientId:command.recipientId,text:'different',messageId:'123456789'}],
    [{http:200,code:0,recipientId:command.recipientId,text:command.text,messageId:''}]]) {
    const f=fixture({receipts});assert.equal((await f.run()).status,'uncertain');assert.equal(f.state().clicks,3);
    assert.equal(f.state().claimed,1);
  }
});
test('DM rejects missing identity, wrong recipient, preserved drafts, stop and consumed attempts',async()=>{
  for(const options of [{profileMissing:true},{wrongRecipient:true},{draft:'用户未发送的草稿'},
    {stopped:true},{unfocused:true},{consumed:true}]) {
    const f=fixture(options);assert.equal((await f.run()).status,'skipped');assert.ok(f.state().clicks<3);
  }
});
test('DM explicit rejection is recorded as failure, not success',async()=>{
  const f=fixture({receipts:[{http:200,code:7,recipientId:command.recipientId,text:command.text}]});
  assert.equal((await f.run()).status,'sent_fail');
});
test('live probes are valid with hostile names, and no offline fake conversation attributes are imported',()=>{
  const hostile={...command,commentAuthor:'<script>"\\\n</script>'};
  for(const code of [dm.profileProbe(hostile,'账号'),dm.chatProbe(hostile,'账号'),dm.DM_HOOK_JS])
    assert.doesNotThrow(()=>new Function('return '+code));
  assert.equal(dm.profileUrl('../wrong'),'');
  assert.ok(!dm.chatProbe(command,'账号').includes('data-conversation-id'));
});

test('recipient identity must be in the active chat header, never just a contacts sidebar',()=>{
  const vm=require('node:vm');
  const rect=()=>({left:0,top:0,width:100,height:40,bottom:40});
  let headerTarget=true,headerCount=1,panelAvailable=true;
  const link={getBoundingClientRect:rect,href:dm.profileUrl(command.recipientId),innerText:command.commentAuthor};
  const header={getBoundingClientRect:rect,closest:()=>panel,querySelectorAll:()=>headerTarget?[link]:[]};
  const submit={getBoundingClientRect:rect,innerText:'发送',disabled:false,getAttribute:()=>null,contains:()=>true};
  const editor={getBoundingClientRect:rect,innerText:'',getAttribute:()=>null,closest:()=>panelAvailable?panel:null,contains:()=>true};
  const panel={querySelectorAll:selector=>selector==='button,[role="button"]'?[submit]:Array(headerCount).fill(header)};
  const document={body:{},activeElement:editor,querySelector:()=>null,elementFromPoint:()=>editor,
    querySelectorAll:selector=>selector.startsWith('header ')?[{textContent:'测试账号关注'}]:[editor]};
  const probe=()=>vm.runInNewContext(dm.chatProbe(command,'测试账号'),{document,innerHeight:1000});
  assert.ok(probe());
  headerTarget=false;assert.equal(probe(),null);
  headerTarget=true;headerCount=2;assert.equal(probe(),null);
  headerCount=1;panelAvailable=false;assert.equal(probe(),null);
});

test('live DM hook records only reduced confirmation fields from fresh relevant responses',async()=>{
  const vm=require('node:vm');
  const body={status_code:0,message:{recipient_sec_uid:command.recipientId,text:command.text,server_message_id:'123456789',secret:'must-not-retain'}};
  const window={fetch:async()=>({status:200,clone:()=>({text:async()=>JSON.stringify(body)})})};
  function XHR(){}XHR.prototype.open=function(){};XHR.prototype.send=function(){};
  vm.runInNewContext(dm.DM_HOOK_JS,{window,XMLHttpRequest:XHR});
  await window.fetch('https://www.douyin.com/im/message/send');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(window.__leadDmReceipts.length,1);
  assert.equal(dm.classifyDmReceipts(window.__leadDmReceipts,command),'sent_ok');
  assert.ok(!JSON.stringify(window.__leadDmReceipts).includes('must-not-retain'));
  await window.fetch('https://www.douyin.com/other/read');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(window.__leadDmReceipts.length,1);
});
