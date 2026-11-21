const test=require('node:test'),assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {createWorkerSurface,initializeLoginPage,observeEmbeddedPage}=require('../worker-surface');

class Host extends EventEmitter {
  constructor(options={}){super();this.options=options;this.children=[];this.size=[1280,900];this.dead=false;this.visible=!!options.show;this.contentView=new NativeView();}
  isDestroyed(){return this.dead;}isVisible(){return this.visible;}isMinimized(){return false;}
  setContentSize(w,h){this.size=[w,h];this.emit('resize');}getContentSize(){return this.size;}
  setSkipTaskbar(){}hide(){this.visible=false;}show(){this.visible=true;}focus(){}destroy(){this.dead=true;}
}
class NativeView {
  constructor(){this.children=[];this.bounds={x:0,y:0,width:0,height:0};}
  addChildView(view){this.children=this.children.filter(v=>v!==view);this.children.push(view);}
  removeChildView(view){this.children=this.children.filter(v=>v!==view);}
  setBackgroundColor(){}
  setBounds(bounds){this.bounds={...bounds};}getBounds(){return {...this.bounds};}
}
class Page extends NativeView {
  constructor(options){super();this.webContents={options,dead:false,url:'https://fixture.invalid/video',zoom:1,on:()=>{},
    getZoomFactor(){return this.zoom;},setZoomFactor(z){this.zoom=z;},
    isDestroyed(){return this.dead;},close(){this.dead=true;},loadURL(url){this.url=url;return Promise.resolve();},
    getURL(){return this.url;},isLoadingMainFrame:()=>false,executeJavaScript:async()=>true};}
  setBounds(bounds){this.bounds={...bounds};}getBounds(){return {...this.bounds};}
}
const make=()=>createWorkerSurface({BrowserWindow:Host,WebContentsView:Page,View:NativeView,webPreferences:{partition:'persist:a'}});
test('initializing the dashboard never replaces an idle or busy execution page with homepage',async()=>{
  for(const busy of [false,true]){
    const surface=make();
    assert.equal(await initializeLoginPage(surface,{busy}),false);
    assert.equal(surface.webContents.url,'https://fixture.invalid/video');surface.destroy();
  }
});
test('login opens a blank page once, and explicit navigation cannot interrupt a running command',async()=>{
  const surface=make();surface.webContents.url='about:blank';
  assert.equal(await initializeLoginPage(surface,{busy:false}),true);
  assert.equal(surface.webContents.url,'https://www.douyin.com/');
  assert.equal(await initializeLoginPage(surface,{busy:false}),false);
  assert.equal(await initializeLoginPage(surface,{busy:true},'https://fixture.invalid/login'),false);
  assert.equal(surface.webContents.url,'https://www.douyin.com/');surface.destroy();
});
test('dashboard, background and login share one account page without a navigation',()=>{
  const surface=make(),page=surface.webContents,host=new Host({show:true});
  assert.equal(page.options.webPreferences.partition,'persist:a');
  for(let i=0;i<4;i++){
    surface.attachTo(host);assert.equal(surface.webContents,page);assert.equal(host.contentView.children.length,1);
    surface.view.setBounds({x:260,y:370,width:820,height:477});surface.park();
    assert.equal(host.contentView.children.length,0);assert.deepEqual(surface.getContentBounds(),{x:0,y:0,width:820,height:477});
    assert.equal(page.url,'https://fixture.invalid/video');assert.equal(page.dead,false);
  }
  surface.destroy();assert.equal(page.dead,true);
});
test('different accounts keep separate page instances and detach independently',()=>{
  const a=make(),b=make(),host=new Host({show:true});a.attachTo(host);b.attachTo(host);
  assert.notEqual(a.webContents,b.webContents);a.park();assert.equal(b.isAttachedTo(host),true);
  a.destroy();assert.equal(b.isDestroyed(),false);b.destroy();
});

test('a late player probe cannot resize the parked execution viewport',async()=>{
  const surface=make(),host=new Host({show:true});surface.attachTo(host);
  surface.view.setBounds({x:260,y:300,width:820,height:477});
  let resolveProbe;
  surface.webContents.executeJavaScript=()=>new Promise(resolve=>{resolveProbe=resolve;});
  surface.setPresentation('video');
  const before=surface.getContentBounds(),zoom=surface.webContents.getZoomFactor();
  surface.park();
  resolveProbe({viewport:{width:1280,height:900},rect:{x:240,y:140,width:640,height:360},reason:'current_video'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(surface.getContentBounds(),before);
  assert.equal(surface.webContents.getZoomFactor(),zoom);
  surface.destroy();
});
test('closing a temporary platform window hides it without destroying the page',()=>{
  const hosts=[];
  class SavedHost extends Host{constructor(options){super(options);hosts.push(this);}}
  const surface=createWorkerSurface({BrowserWindow:SavedHost,WebContentsView:Page,View:NativeView,webPreferences:{}}),parking=hosts[0];let prevented=false;
  surface.show();parking.emit('close',{preventDefault:()=>{prevented=true;}});
  assert.equal(prevented,true);assert.equal(parking.visible,false);assert.equal(surface.isDestroyed(),false);
  surface.destroy();assert.equal(parking.dead,true);
});
test('destroy also releases a host after the page has already closed',()=>{
  const hosts=[];
  class SavedHost extends Host{constructor(options){super(options);hosts.push(this);}}
  const surface=createWorkerSurface({BrowserWindow:SavedHost,WebContentsView:Page,View:NativeView,webPreferences:{}}),parking=hosts[0];surface.webContents.close();surface.destroy();
  assert.equal(parking.dead,true);surface.destroy();
});
test('native readiness rejects hidden, stale, detached, loading and changed pages',async()=>{
  for(const mode of ['ready','hidden','stale','detached','loading','navigated','unpainted']){
    const surface=make(),host=new Host({show:true});surface.attachTo(host);
    const entry={surface,view:surface.view,attached:true};
    const state={runId:'r'},latest={runId:'r'};
    surface.webContents.executeJavaScript=async()=>{
      if(mode==='stale')latest.runId='next';
      if(mode==='detached')surface.park();
      if(mode==='navigated')surface.webContents.url='https://fixture.invalid/other';
      return mode!=='unpainted';
    };
    if(mode==='loading')surface.webContents.isLoadingMainFrame=()=>true;
    if(mode==='hidden')host.visible=false;
    assert.equal(await observeEmbeddedPage(entry,state,host,()=>latest),mode==='ready',mode);
    assert.equal(entry.__displayedRunId,mode==='ready'?'r':undefined,mode);
    surface.destroy();
  }
});
