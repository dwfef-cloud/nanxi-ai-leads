const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto'),vm=require('node:vm');
const live=require('../live-platform'),{createLeadgenLauncher}=require('../leadgen-launcher');
const video='https://www.douyin.com/video/7400000000000000001';
const other='https://www.douyin.com/video/7400000000000000002';
function sign(scope,action){
  const cmd={...action,accountId:scope.accountId,leadgenTaskId:scope.leadgenTaskId,scopeId:scope.scopeId,deadlineAt:Date.now()+60000};
  const fields=['type','accountId','leadgenTaskId','videoUrl','commentId','text','scopeId','deadlineAt'];
  if(cmd.type==='like-comment')fields.push('commentAuthor');
  if(['reply-comment','send-dm'].includes(cmd.type))fields.push('commentAuthor','recipientId');
  cmd.acceptanceSignature=crypto.createHmac('sha256',scope.secret).update(JSON.stringify(fields.map(k=>String(cmd[k]||'')))).digest('hex');
  return cmd;
}
test('each approved target has its own durable attempt; changing exact text is rejected',t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'leadgen-targets-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const actions=[video,other].map((videoUrl,i)=>({type:'post-comment',videoUrl,text:'已核对的测试评论正文'+i}));
  const scope={secret:'x'.repeat(40),scopeId:'plan',accountId:'a',leadgenTaskId:'t',nickname:'账号',
    expiresAt:Date.now()/1000+60,actions:{'post-comment':2},videoUrls:[video,other],reviewedActions:actions};
  const file=path.join(dir,'approved.json');fs.writeFileSync(file,JSON.stringify(scope));
  for(const action of actions){
    const cmd=sign(scope,action),approved=live.scopeFor(cmd,file);
    assert.ok(approved);
    assert.ok(live.claimAttempt(approved,cmd.type,cmd));
    assert.equal(live.claimAttempt(live.scopeFor(cmd,file),cmd.type,cmd),false);
    assert.equal(live.scopeFor(sign(scope,{...action,text:'没有确认的替代正文'}),file),null);
  }
});
test('stream scope signs runtime-discovered targets and consumes each target once',t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'leadgen-stream-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const scope={mode:'stream',secret:'x'.repeat(40),scopeId:'stream',accountId:'a',leadgenTaskId:'t',nickname:'账号',
    expiresAt:Date.now()/1000+60,actions:{'post-comment':2},interactionLimit:2,videoUrls:['*']};
  const file=path.join(dir,'stream.json');fs.writeFileSync(file,JSON.stringify(scope));
  const action={type:'post-comment',videoUrl:video,text:'按视频和讨论区生成的测试评论'};
  const cmd=sign(scope,action),authorized=live.scopeFor(cmd,file);
  assert.ok(authorized);assert.ok(live.claimAttempt(authorized,cmd.type,cmd));
  assert.equal(live.claimAttempt(live.scopeFor(cmd,file),cmd.type,cmd),false);
  const next=sign(scope,{...action,videoUrl:other,text:'另一条评论'});
  assert.ok(live.claimAttempt(live.scopeFor(next,file),next.type,next));
  const over=sign(scope,{...action,videoUrl:'https://www.douyin.com/video/7400000000000000003',text:'超出上限'});
  assert.equal(live.claimAttempt(live.scopeFor(over,file),over.type,over),false);
});
test('per-account scope routing keeps both accounts and dedicated comment-like scope isolated',t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'leadgen-accounts-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const previous=process.env.LEADS_LEADGEN_SCOPE_DIR;process.env.LEADS_LEADGEN_SCOPE_DIR=dir;
  t.after(()=>{if(previous===undefined)delete process.env.LEADS_LEADGEN_SCOPE_DIR;else process.env.LEADS_LEADGEN_SCOPE_DIR=previous;});
  const commands=[];
  for(const account of ['a','b']){
    const action={type:'post-comment',videoUrl:video,text:'已确认的单条测试正文'};
    const scope={secret:'x'.repeat(40),scopeId:account,accountId:account,leadgenTaskId:'t',nickname:'测试账号',
      expiresAt:Date.now()/1000+60,actions:{'post-comment':1},videoUrls:[video],reviewedActions:[action]};
    fs.writeFileSync(path.join(dir,crypto.createHash('sha256').update(account).digest('hex')+'.json'),JSON.stringify(scope));
    commands.push(sign(scope,action));
  }
  assert.ok(commands.every(cmd=>live.scopeFor(cmd)));
  assert.equal(live.scopeFor({...commands[0],accountId:'b'}),null);
});
test('an unrelated running account does not block preparing another account',async()=>{
  const payload={taskId:'run',accountId:'b'};
  const start=createLeadgenLauncher({runtime:async()=>({busy:true,active:[{accountId:'a'}]}),
    prepare:async()=>({success:true,payload,scopeFile:'file'}),
    activate:async()=>{},start:async()=>({success:true})});
  assert.equal((await start(payload)).success,true);
});
test('invalid configuration and old desktop never reach native preview or start',async()=>{
  for(const version of [undefined,2,3,4]){
    const calls=[];
    const window={ipcRenderer:{invoke(){}},location:{search:''},localStorage:{getItem:()=>null},
      __DESKTOP_SHELL__:true,__desktopShell:{leadgenReal:true,leadgenPlanVersion:version,
        viewInvoke:()=>{throw Error('must not prepare or send');}}};
    const fetch=async url=>{calls.push(url);return {ok:true,json:async()=>({success:true,
      data:{success:false,errors:['互动上限无效']}})};};
    vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../../ui/live-bridge.js'),'utf8'),
      {window,fetch,URLSearchParams,Map,Set,console:{info(){},warn(){}}});
    const result=await window.ipcRenderer.invoke('start-task',{taskMode:'interaction',taskId:'t',accountId:'a'});
    assert.equal(result.success,false);
    assert.equal(calls.length,version===4?1:0);
  }
});
test('actual frontend keyword parser and serializer retain separators and quoted phrases',()=>{
  const source=fs.readFileSync(path.resolve(__dirname,'../../ui/assets/index-1pPBwPwk.js'),'utf8');
  const context=vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('function Rc('),source.indexOf('function CW('))+
    source.slice(source.indexOf('function Bx('),source.indexOf('function OW(')),context);
  assert.equal(JSON.stringify(context.Rc('智能体、工作流；开发;咨询，智能体')),JSON.stringify(['智能体','工作流','开发','咨询']));
  const serialized=context.Fx({keywordSyntax:'quoted',accountKeywords:{a:'"AI agent",工作流'}},'a');
  assert.equal(serialized,'"AI agent"，工作流');
  assert.equal(JSON.stringify(context.Bx(serialized,'quoted')),JSON.stringify(['AI agent','工作流']));
  assert.equal(JSON.stringify(context.Bx('"AI agent"、工作流；开发;咨询','quoted')),JSON.stringify(['AI agent','工作流','开发','咨询']));
});
test('read-only video probes reject a different video, login wall and verification',()=>{
  const {VIDEO_METADATA_JS,verifiedPage}=require('../leadgen-read');
  assert.doesNotThrow(()=>new Function('return '+VIDEO_METADATA_JS));
  assert.ok(verifiedPage({url:video},video));
  assert.equal(verifiedPage({url:other},video),false);
  assert.equal(verifiedPage({url:video,verify:true},video),false);
  assert.equal(verifiedPage({url:video,loginWall:true},video),false);
  // 详情浮层点「下一个」不改地址：只认画面里正在播的那条（videoId），地址只是兜底
  assert.ok(verifiedPage({url:'https://www.douyin.com/search/AI?type=video',videoId:'7400000000000000001'},video));
  assert.equal(verifiedPage({url:video,videoId:'7400000000000000002'},video),false);
});

test('empty visible comment panel is a valid opened state, not a failed toggle',()=>{
  const {COMMENT_OPEN_JS}=require('../leadgen-read');
  const list={innerText:'',getBoundingClientRect:()=>({left:900,top:50,width:300,height:500,right:1200,bottom:550})};
  const document={querySelector:selector=>selector==='[data-e2e="comment-list"]'?list:null,
    querySelectorAll:()=>[],title:'抖音'};
  const result=vm.runInNewContext(COMMENT_OPEN_JS,{document,window:{innerHeight:800,innerWidth:1280},
    location:{pathname:'/video/7400000000000000001',hostname:'www.douyin.com'},String,Array,Math});
  assert.equal(result.open,true);
  assert.equal(result.empty,true);
  assert.equal(result.items,0);
  const textOnlyDocument={innerText:'评论\n全部评论(0)\n暂无评论',querySelector:()=>null,querySelectorAll:()=>[]};
  const textOnly=vm.runInNewContext(COMMENT_OPEN_JS,{document:textOnlyDocument,
    window:{innerHeight:800,innerWidth:1280},location:{pathname:'/video/7400000000000000001',hostname:'www.douyin.com'},
    String,Array,Math});
  assert.equal(textOnly.open,true,'explicit zero-comment empty state is recognized without comment items');
  assert.equal(textOnly.empty,true);
});

test('video metadata probe extracts Chinese publish dates from the info panel',()=>{
  const {VIDEO_METADATA_JS}=require('../leadgen-read');
  const document={title:'抖音',querySelectorAll:()=>[],querySelector:selector=>
    selector==='[data-e2e="video-info"]'?{innerText:'作品信息 10月24日'}:null};
  const result=vm.runInNewContext(VIDEO_METADATA_JS,{document,window:{innerHeight:900},
    location:{href:video,pathname:'/video/7400000000000000001'},URL,Number,Array,Math,String});
  assert.equal(result.publishedText,'10月24日');
});

test('video state recognizes a visible ad badge outside the player panel text',()=>{
  const {VIDEO_STATE_JS}=require('../leadgen-read');
  const info={innerText:'@火山引擎 · 4天前 广告',getBoundingClientRect:()=>({left:850,top:100,width:300,height:24,bottom:124})};
  const panel={className:'video_7400000000000000001',parentElement:null,
    getAttribute:()=>null,querySelector:selector=>selector==='[data-e2e="video-info"]'?info:null,
    querySelectorAll:()=>[]};
  const video={parentElement:panel,getBoundingClientRect:()=>({left:200,top:60,width:500,height:700})};
  const document={querySelectorAll:selector=>selector==='video'?[video]:
      selector==='[data-e2e="video-info"]'?[info]:[],
    querySelector:()=>null};
  const result=vm.runInNewContext(VIDEO_STATE_JS,{document,window:{innerHeight:900,innerWidth:1280},
    location:{href:'https://www.douyin.com/video/7400000000000000001',pathname:'/video/7400000000000000001',hostname:'www.douyin.com'},
    String,Array,Math});
  assert.equal(result.isAd,true);
});

test('video state does not mistake a live feed card for the stale video URL',()=>{
  const {VIDEO_STATE_JS}=require('../leadgen-read');
  const activeFeed={getAttribute:name=>name==='data-e2e-vid'?'':null};
  const liveFeed={};
  const document={title:'抖音',innerText:'直播中',
    querySelector:selector=>selector==='[data-e2e="feed-active-video"]'?activeFeed:
      selector==='[data-e2e="feed-live"]'?liveFeed:null,
    querySelectorAll:()=>[]};
  const result=vm.runInNewContext(VIDEO_STATE_JS,{document,window:{innerHeight:900,innerWidth:1280},
    location:{href:'https://www.douyin.com/video/7400000000000000001',
      pathname:'/video/7400000000000000001',hostname:'www.douyin.com'},String,Array});
  assert.equal(result.id,'');
  assert.equal(result.liveFeed,true);
  assert.equal(result.immersiveFeed,true);
});

test('non-video recovery escalates inputs and search recovery cannot open a detail URL',()=>{
  const source=fs.readFileSync(path.resolve(__dirname,'../douyin_agent.js'),'utf8');
  const nextStart=source.indexOf('async function nextVideo(cmd)');
  const nextEnd=source.indexOf('async function inspectLeadgenVideo(cmd)',nextStart);
  const next=source.slice(nextStart,nextEnd);
  assert.match(next,/const strategies = \["wheel", "wheel", "wheel", "down", "page-down", "wheel"\]/);
  assert.match(next,/keyCode = strategy === "page-down" \? "PAGEDOWN" : "DOWN"/);
  const openStart=source.indexOf('async function openVideo(cmd)');
  const openEnd=source.indexOf('async function inspectLeadgenVideo(cmd)',openStart);
  const open=source.slice(openStart,openEnd);
  assert.match(open,/if \(cmd\.preserveSearch\)[\s\S]*未打开视频详情地址[\s\S]*按地址打开/);
});

test('live dashboard countdown stays visible below scrolling logs and updates seconds',()=>{
  let listener,tick,status,clock=0,stylesheet;
  const element=()=>({dataset:{},children:[],isConnected:true,className:'',
    classList:{contains:()=>false},setAttribute(){},
    appendChild(child){this.children.push(child);if(child.className==='task-countdown-status')status=child;},
    insertAdjacentElement(_where,child){status=child;},
    get textContent(){return this._textContent!==undefined?this._textContent:this.children.map(child=>child.textContent||'').join('');},
    set textContent(value){this._textContent=String(value);}});
  const sidebar=element();
  const container=element();
  container.parentElement=sidebar;
  container.classList.contains=name=>name==='sidebar-body';
  const document={head:{appendChild(node){stylesheet=node.textContent;}},documentElement:{},
    createElement:element,querySelectorAll:()=>[container]};
  const window={addEventListener:(_name,callback)=>{listener=callback;},
    setInterval:callback=>{tick=callback;return 1;},clearInterval(){}};
  class MutationObserver{observe(){}}
  class FakeDate{static now(){return clock;}}
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../../ui/task-countdown.js'),'utf8'),
    {document,window,MutationObserver,Map,WeakMap,Date:FakeDate,String,Math});
  listener({detail:{channel:'automation-data',payload:{type:'countdown',taskId:'run',payload:{
    active:true,action:'当前视频停留结束后浏览评论区',remainingSeconds:15,paused:false}}}});
  assert.equal(status.dataset.active,'true');
  assert.ok(sidebar.children.includes(status),'countdown is a sibling below the scrolling log');
  assert.match(stylesheet,/background:var\(--card-30/);
  assert.match(stylesheet,/color:var\(--text-secondary/);
  assert.doesNotMatch(stylesheet,/\.task-countdown-status\{display:none/);
  assert.match(status.textContent,/正在浏览视频 · 下一步：读取评论15 秒/);
  clock=1000;
  tick();
  assert.match(status.textContent,/读取评论14 秒/);
  listener({detail:{channel:'automation-data',payload:{type:'countdown',taskId:'run',payload:{
    active:false}}}});
  assert.equal(status.dataset.active,'false');
  assert.match(status.textContent,/当前无待执行动作—/,'idle countdown bar remains visible with a stable empty state');
});

test('feed reader recognizes sponsored marker, visible player and ignores grid thumbnails',async()=>{
  const source=fs.readFileSync(path.resolve(__dirname,'../douyin_agent.js'),'utf8');
  const start=source.indexOf('const FEED_LIST_JS = `')+'const FEED_LIST_JS = `'.length;
  const end=source.indexOf('`;',start);
  assert.ok(start>0&&end>start,'feed page script is present');
  const template=vm.runInNewContext('`'+source.slice(start,end)+'`');
  const run=async({videoHeight,videoWidth,videoId,ad=false}={})=>{
    let arrowClicks=0;
    const attrs=videoId?{'data-aweme-id':videoId}:{};
    const box={className:'feed-video-panel',innerText:'测试视频标题 3天前',parentElement:null,
      getAttribute:name=>attrs[name]||null,
      getBoundingClientRect:()=>({left:260,top:80,width:400,height:650,right:660,bottom:730}),
      querySelector:selector=>selector==='[data-e2e="video-info"]'&&ad?{innerText:'@火山引擎 · 4天前 广告'}:null,
      querySelectorAll:()=>[]};
    const player={paused:false,readyState:4,className:'',parentElement:box,
      getAttribute:()=>null,
      getBoundingClientRect:()=>({left:280,top:90,width:videoWidth,height:videoHeight,
        right:280+videoWidth,bottom:90+videoHeight})};
    const arrow={click(){arrowClicks++;}};
    const document={body:{innerText:'推荐视频页面正文 '.repeat(400)},
      querySelector:selector=>selector.includes('video-switch-next-arrow')?arrow:null,
      querySelectorAll:selector=>selector==='video'?[player]:[]};
    const script=template.replace('__LIMIT__','1').replace('__DWELL__','1').replace('__BUDGET__','1000');
    const result=await vm.runInNewContext(script,{document,location:{href:'https://www.douyin.com/jingxuan',pathname:'/jingxuan'},
      window:{innerWidth:1280,innerHeight:900,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})},
      Date,URL,Array,Number,String,Math,Promise,setTimeout:fn=>{fn();return 0;}});
    return {result,arrowClicks};
  };
  const active=await run({videoHeight:600,videoWidth:360,videoId:'7400000000000000012'});
  assert.equal(active.result.items.length,1);
  assert.equal(active.result.items[0].videoId,'7400000000000000012');
  const sponsored=await run({videoHeight:600,videoWidth:360,videoId:'7400000000000000014',ad:true});
  assert.equal(sponsored.result.items[0].isAd,true);
  assert.equal(active.arrowClicks,0,'limit=1 must not advance before returning the current video');
  const grid=await run({videoHeight:120,videoWidth:240,videoId:'7400000000000000013'});
  assert.equal(grid.result.items.length,0);
  assert.equal(grid.result.diag.visiblePlayers,0);
  assert.match(grid.result.error,/未进入视频信息流/);
  const readerStart=source.indexOf('async function readSourceVideos(cmd)');
  const reader=source.slice(readerStart,source.indexOf('const RECOMMEND_SCAN_JS',readerStart));
  assert.match(reader,/const blankSkeleton[\s\S]*diag\.bodyLen[^;]*< 1200/);
  assert.match(reader,/!diag\.loginWall && !diag\.activeMarker && !diag\.visiblePlayers/);
});

test('activation failure cleans prepared state without starting a task',async()=>{
  const calls=[],payload={taskId:'run',accountId:'a'};
  const launcher=createLeadgenLauncher({runtime:async()=>({active:[]}),
    preview:async()=>({success:true,...payload,proposalId:'p',binding:'b',actions:[{type:'post-comment'}]}),
    confirm:async()=>true,prepare:async()=>({payload,scopeFile:'file'}),
    activate:async()=>{throw Error('failed to activate');},
    cleanup:async p=>calls.push(p),start:async()=>assert.fail('must not start')});
  assert.equal((await launcher(payload)).success,false);
  assert.deepEqual(calls,[payload]);
});

test('large approval lists remain fully reviewable and cancelling any page blocks execution',async()=>{
  const {reviewPages,confirmPlan}=require('../leadgen-review');
  const actions=Array.from({length:100},(_,i)=>({type:'post-comment',keyword:'工作流',title:'离线标题',videoUrl:video,
    text:'测试正文'+i+'文'.repeat(195)}));
  const selected={nickname:'离线账号',actions,configSummary:['上限100']};
  const pages=reviewPages(selected);
  assert.ok(pages.length>1&&pages.every(page=>page.length<=12000));
  assert.ok(actions.every(a=>pages.some(page=>page.includes(a.text))));
  let calls=0;
  assert.equal(await confirmPlan(selected,async options=>{
    assert.equal(options.defaultId,0);
    calls++;return {response:calls===2?0:1};
  }),false);
  assert.equal(calls,2);
  const buttons=[];
  assert.equal(await confirmPlan(selected,async options=>{buttons.push(options.buttons[1]);return {response:1};}),true);
  assert.equal(buttons.at(-1),'确认并真实执行');
  assert.ok(buttons.slice(0,-1).every(text=>text.includes('尚不执行')));
});

test('the actual long video watch receives cancellation outside the serial command queue',async()=>{
  const source=fs.readFileSync(path.resolve(__dirname,'../douyin_agent.js'),'utf8');
  const start=source.indexOf('async function inspectLeadgenVideo(cmd)');
  const end=source.indexOf('/** 作者主页',start);
  const view={webContents:{stop(){stopped=true;}}};
  let playing=false,stopped=false,paused=false;
  const liveViews=new Set([view]);
  const context=vm.createContext({liveViews,setTimeout,Date,Number,
    withView:async(_,fn)=>fn(view),loadPage:async()=>{},
    evaluate:async(_,code)=>{
      if(code.includes('?.play()')){playing=true;return;}
      if(code.includes('?.pause()')){paused=true;return;}
      return {url:video,title:'离线视频',playing};
    },
    request:async()=>({cancelled:playing}),
    require:name=>name==='./command-control'?{createCommandControl:options=>
      require('../command-control').createCommandControl({...options,intervalMs:5})}:require('../leadgen-read')});
  vm.runInContext(source.slice(start,end),context);
  const began=Date.now();
  const result=await context.inspectLeadgenVideo({id:'offline',type:'watch-video',videoUrl:video,seconds:3600,accountId:'a'});
  assert.equal(result.stopped,true);
  assert.ok(stopped&&paused&&!liveViews.has(view));
  assert.ok(Date.now()-began<500);
});
