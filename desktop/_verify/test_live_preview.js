const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {updateWorkerPreview,applyViewBounds,cssToViewBounds} = require('../live-preview');

test('CSS host bounds track main-window zoom without cumulative scaling',()=>{
  const applied=[];
  const entry={attached:true,__workerPreview:true,
    bounds:{x:265.265,y:378.412,width:1775.673,height:1044.695},
    view:{setBounds:b=>applied.push(b)}};
  applyViewBounds(entry,0.60375);applyViewBounds(entry,0.60375);
  assert.deepEqual(applied,[{x:160,y:228,width:1072,height:631}]);
  applyViewBounds(entry,1.25);
  assert.deepEqual(applied[1],{x:332,y:473,width:2220,height:1306});
  applyViewBounds(entry,0.60375);
  assert.deepEqual(applied[2],applied[0]);
  assert.equal(entry.bounds.width,1775.673);
  assert.equal(cssToViewBounds({x:NaN,y:0,width:100,height:100}),null);
});

test('same-origin canvas preview never changes shared main-UI zoom, even during login navigation',()=>{
  const zooms=[];
  const entry={attached:true,bounds:{x:0,y:0,width:800,height:500},zoomFactor:0.6,
    view:{setBounds:()=>{},webContents:{
      getURL:()=> 'http://127.0.0.1:8899/ui/live-worker-preview.html',
      setZoomFactor:z=>zooms.push(z)}}};
  applyViewBounds(entry);assert.deepEqual(zooms,[]);
  entry.__workerPreview=true;
  entry.view.webContents.getURL=()=> 'about:blank';
  applyViewBounds(entry);assert.deepEqual(zooms,[]);
  entry.__workerPreview=false;
  entry.view.webContents.getURL=()=> 'https://www.douyin.com/';
  applyViewBounds(entry);assert.deepEqual(zooms,[0.6]);
});

test('live preview renders the selected worker frame, without navigating a duplicate platform feed', async()=>{
  const navigated=[], displayed=[], context={window:{showWorkerFrame:f=>displayed.push(f)}};
  let current='https://www.douyin.com/jingxuan', captures=0;
  const url='http://127.0.0.1:8899/ui/live-worker-preview.html';
  const entry={attached:true,accountId:'a',view:{webContents:{
    getURL:()=>current,loadURL:async u=>{navigated.push(u);current=u;},
    executeJavaScript:async js=>vm.runInNewContext(js,context),
  }}};
  const agent={captureWorkerFrame:async account=>{
    assert.equal(account,'a');captures++;
    return {image:'data:image/jpeg;base64,ZmFrZQ==',capturedAt:captures,busy:true};
  }};
  await updateWorkerPreview(entry,{busy:true,url:'https://www.douyin.com/?recommend=1'},agent,url);
  await updateWorkerPreview(entry,{busy:true,url:'https://www.douyin.com/?recommend=1'},agent,url);
  assert.deepEqual(navigated,[url]);
  assert.deepEqual(displayed.map(f=>f.capturedAt),[1,2]);
  entry.attached=false;
  await updateWorkerPreview(entry,{busy:true},agent,url);
  assert.equal(captures,2);
});

test('idle preview preserves the last frame and never claims a new capture', async()=>{
  let ended=0;
  const url='http://127.0.0.1:8899/ui/live-worker-preview.html';
  const entry={attached:true,__workerPreview:true,view:{webContents:{getURL:()=>url,
    executeJavaScript:async js=>vm.runInNewContext(js,{window:{endWorkerPreview:()=>ended++}}),
  }}};
  await updateWorkerPreview(entry,{busy:false},{captureWorkerFrame:()=>assert.fail('idle')},url);
  await updateWorkerPreview(entry,{busy:false},{captureWorkerFrame:()=>assert.fail('idle')},url);
  assert.equal(ended,1);
});

test('repeated layout notifications do not resize or zoom the native view again',()=>{
  const bounds=[],zooms=[];
  const entry={attached:true,bounds:{x:10,y:20,width:900,height:600},zoomFactor:0.75,
    view:{setBounds:b=>bounds.push({...b}),webContents:{setZoomFactor:z=>zooms.push(z)}}};
  applyViewBounds(entry);applyViewBounds(entry);
  assert.equal(bounds.length,1);assert.deepEqual(zooms,[0.75]);
  entry.bounds={...entry.bounds,width:901};applyViewBounds(entry);
  assert.equal(bounds.length,2);assert.equal(zooms.length,1);
  entry.zoomFactor=0.8;applyViewBounds(entry);
  assert.equal(bounds.length,2);assert.deepEqual(zooms,[0.75,0.8]);
  entry.attached=false;entry.bounds.width=1000;applyViewBounds(entry);
  assert.equal(bounds.length,2);
});

test('a slow capture never creates a backlog of preview frames',async()=>{
  const url='http://127.0.0.1:8899/ui/live-worker-preview.html';
  let finish,captures=0;
  const entry={attached:true,accountId:'a',view:{webContents:{getURL:()=>url,executeJavaScript:async()=>{}}}};
  const agent={captureWorkerFrame:()=>{captures++;return new Promise(resolve=>{finish=resolve;});}};
  const pending=updateWorkerPreview(entry,{busy:true},agent,url);
  for(let i=0;i<5;i++)await updateWorkerPreview(entry,{busy:true},agent,url);
  assert.equal(captures,1);
  finish({image:'data:image/jpeg;base64,a',capturedAt:1,busy:true});await pending;
  assert.equal(entry.__capturing,false);
});

test('first-frame acknowledgement binds only decoded pixels still attached to the run',async()=>{
  const url='http://127.0.0.1:8899/ui/live-worker-preview.html';
  for(const scenario of ['drawn','undecoded','detached']){
    const entry={attached:true,accountId:'a',__workerPreview:true,view:{webContents:{getURL:()=>url,
      executeJavaScript:async()=>{
        if(scenario==='detached')entry.attached=false;
        return scenario==='undecoded'?undefined:{displayed:true,capturedAt:1234};
      }}}};
    await updateWorkerPreview(entry,{busy:true},{captureWorkerFrame:async()=>({runId:'new-run',capturedAt:1234})},url);
    assert.equal(entry.__displayedRunId,scenario==='drawn'?'new-run':undefined,scenario);
    assert.equal(entry.__displayedFrameAt,scenario==='drawn'?1234:undefined,scenario);
  }
});
