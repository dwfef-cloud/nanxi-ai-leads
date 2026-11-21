"use strict";
const {PLAYER_PROBE,focusGeometry}=require('./video-focus');

// One account owns one live page. Move its native container between dashboard
// and a hidden parking window; never reload/copy the page to change its host.
function createWorkerSurface({BrowserWindow, WebContentsView, View, webPreferences}) {
  const parking = new BrowserWindow({show:false, width:1280, height:900,
    skipTaskbar:true, paintWhenInitiallyHidden:true,
    webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,backgroundThrottling:false}});
  const page = new WebContentsView({webPreferences});
  const clip = new View();
  clip.setBackgroundColor('#000000');
  clip.addChildView(page);
  const wc=page.webContents;
  // Keep a fixed CSS viewport: multiplying a rounded innerWidth on each probe
  // would accumulate native-pixel rounding and slowly grow the execution page.
  const executionViewport={width:1280,height:900};
  let owner = null, destroyed = false;
  // showForCheck 临时置顶的记账：核验过了要立刻还回去，不能一直挡着用户。
  let topmostForCheck = false;
  let hostBounds={x:0,y:0,width:1280,height:900},mode='page',preferredZoom=1,probing=false,revision=0;
  let presentation={mode:'page',reason:'full_page'};
  let refreshTimer;
  const same=(a,b)=>a&&['x','y','width','height'].every(key=>a[key]===b[key]);
  function applyGeometry(geometry,details) {
    if(destroyed||wc.isDestroyed())return;
    if(wc.getZoomFactor()!==geometry.zoom)wc.setZoomFactor(geometry.zoom);
    if(!same(clip.getBounds(),geometry.clip))clip.setBounds(geometry.clip);
    if(!same(page.getBounds(),geometry.page))page.setBounds(geometry.page);
    presentation={...details,clip:geometry.clip,page:geometry.page,zoom:geometry.zoom};
  }
  function wholePage() {
    if(mode==='video'){
      const zoom=Math.max(.25,Math.min(hostBounds.width/1280,hostBounds.height/900));
      const width=Math.ceil(1280*zoom),height=Math.ceil(900*zoom);
      applyGeometry({zoom,clip:{x:hostBounds.x,y:hostBounds.y,width:hostBounds.width,height:hostBounds.height},
        page:{x:Math.round((hostBounds.width-width)/2),y:Math.round((hostBounds.height-height)/2),width,height}},
        {mode:'page',reason:'waiting_for_current_video'});
    }else applyGeometry({zoom:preferredZoom,clip:hostBounds,page:{x:0,y:0,width:hostBounds.width,height:hostBounds.height}},
      {mode:'page',reason:'full_page'});
  }
  async function refreshVideoFocus() {
    if(mode!=='video'||probing||destroyed||wc.isDestroyed()||wc.isLoadingMainFrame()||!/^https:\/\//.test(wc.getURL()))return false;
    probing=true;const token=revision,url=wc.getURL(),probeOwner=owner;let timeout;
    try{
      const probe=await Promise.race([wc.executeJavaScript(PLAYER_PROBE),
        new Promise(resolve=>{timeout=setTimeout(()=>resolve(null),1000);})]);
      if(!probe||token!==revision||owner!==probeOwner||mode!=='video'||destroyed||wc.isDestroyed()||wc.getURL()!==url)return false;
      const geometry=focusGeometry(hostBounds,probe,executionViewport);
      if(geometry){lastProbe=probe;applyGeometry(geometry,{mode:'video',reason:probe.reason,videoKey:probe.videoKey});return true;}
      lastProbe=null;
      wholePage();presentation.reason=probe.reason;return false;
    }catch(_){return false;}finally{clearTimeout(timeout);probing=false;}
  }
  const view={webContents:wc,getBounds:()=>({...hostBounds}),setBounds:bounds=>{
    hostBounds={...bounds};revision++;
    if(mode==='page'||presentation.mode!=='video')wholePage();
    // Reuse the last CSS target immediately while a fresh probe is pending.
    else if(lastProbe){const geometry=focusGeometry(hostBounds,lastProbe,executionViewport);if(geometry)applyGeometry(geometry,presentation);}
    void refreshVideoFocus();
  }};
  let lastProbe=null;
  function moveTo(target, bounds) {
    if (destroyed || target.isDestroyed()) throw Error('执行页面或宿主已关闭');
    if (owner !== target) {
      revision++; // Ignore a probe started under the previous native host.
      if (owner && !owner.isDestroyed()) owner.contentView.removeChildView(clip);
      target.contentView.addChildView(clip);
      owner = target;
    }
    if (bounds) {revision++;hostBounds={...bounds};clip.setBounds(bounds);}
  }
  function park() {
    if (destroyed) return;
    // Preserve viewport and zoom during background work. Resizing it here could
    // move a target between a DOM probe and its native input event.
    const bounds = clip.getBounds();
    parking.setContentSize(Math.max(1,bounds.width),Math.max(1,bounds.height));
    moveTo(parking,{x:0,y:0,width:bounds.width,height:bounds.height});
  }
  moveTo(parking,{x:0,y:0,width:1280,height:900});
  wholePage();
  wc.on('did-finish-load',()=>{lastProbe=null;wholePage();void refreshVideoFocus();});
  wc.on('did-start-navigation',(_event,_url,_inPlace,isMainFrame)=>{
    if(isMainFrame!==false){revision++;lastProbe=null;wholePage();}
  });
  refreshTimer=setInterval(()=>{
    if(owner!==parking)void refreshVideoFocus();
  },350);
  refreshTimer.unref?.();
  parking.on('close',event=>{
    if (destroyed) return;
    event.preventDefault();parking.hide();parking.setSkipTaskbar(true);
  });
  parking.on('resize',()=>{
    if (owner !== parking || destroyed) return;
    const [width,height] = parking.getContentSize();
    if(mode==='page')view.setBounds({x:0,y:0,width,height});
  });
  return {
    view, webContents:wc, nativeView:clip,
    attachTo:target=>moveTo(target), park,
    bringToFront:()=>{if(owner&&!owner.isDestroyed())owner.contentView.addChildView(clip);},
    setPresentation:value=>{
      const next=value==='video'?'video':'page';if(next===mode)return;
      mode=next;revision++;lastProbe=null;wholePage();void refreshVideoFocus();
    },
    setPageZoom:value=>{preferredZoom=value;if(mode==='page')wholePage();},
    refreshVideoFocus,getPresentation:()=>({...presentation}),
    isAttachedTo:target=>owner===target && !target.isDestroyed() && !wc.isDestroyed(),
    // 宿主窗口此刻是不是真看得见：验证弹窗要求「弹出来」，不能只看 show() 没报错。
    isVisible:()=>{if(!owner||owner.isDestroyed())return false;try{return owner.isVisible();}catch(_){return false;}},
    // 用户此刻能不能**真看见**这个页面：visible 且 opacity > 0。
    // 与 isVisible 的区别：showQuiet 会把停靠窗压到 opacity 0，此时 isVisible 仍是 true。
    // 用户在「查看采集画面」里盯着的时候不能再调 showQuiet，否则画面直接变透明的空壳
    // （2026-11-19 踩坑：读评论区要 showQuiet 才会懒加载，但会把用户正在看的窗口弄没）。
    isShown:()=>{if(!owner||owner.isDestroyed())return false;
      try{return owner.isVisible() && owner.getOpacity() > 0.01;}catch(_){return false;}},
    isDestroyed:()=>destroyed || wc.isDestroyed(),
    // Input coordinates are relative to the full webContents, not the crop.
    getContentBounds:()=>page.getBounds(),
    loadURL:url=>wc.loadURL(url),
    setSkipTaskbar:value=>{if(owner===parking)parking.setSkipTaskbar(value);},
    show:()=>{
      if(!owner||owner.isDestroyed())return;
      try{
        // 安静显示会把停靠窗压到 opacity 0 / 挪到 (0,0)。真要让用户看见时
        // 必须先把这两项还原，否则 show() 出来的是一个"看得见窗口、看不见页面"的壳。
        if(owner===parking){
          parking.setOpacity(1);
          if(parking.__homeBounds){parking.setBounds(parking.__homeBounds);parking.__homeBounds=null;}
        }
        if(owner.isMinimized())owner.restore();
        owner.show();
      }catch(_){/* 忽略 */}
    },
    // 安静显示：窗口摆回屏幕内、透明度归零、不抢焦点。
    // 为什么需要：隐藏窗口里的页面 document.visibilityState=hidden，
    // Chromium 会丢弃 sendInputEvent 的可信鼠标事件（真机实测 事件计数 0/0/0）；
    // 只把窗口挪到屏幕外（-4000,-4000）也不够——窗口虽已 show，
    // 但整块不可见区域仍被判为 hidden（实测 事件进页 1/1/1 而 vis=hidden）。
    // 屏幕内 + setOpacity(0) 是目前唯一既让页面 visible、又不打扰用户的方式。
    showQuiet:()=>{
      if(!owner||owner.isDestroyed())return false;
      try{
        if(owner===parking){
          if(!parking.__homeBounds)parking.__homeBounds=parking.getBounds();
          parking.setOpacity(0);
          parking.setPosition(0,0);
        }
        if(owner.isMinimized())owner.restore();
        owner.showInactive();
        return true;
      }catch(_){return false;}
    },
    // 兜底：真把采集页显示出来再核验。
    // 为什么还要这一档：opacity:0 的窗口在部分环境（远程桌面 / 息屏唤醒 / 某些显卡驱动）
    // 会被 DWM 判成 occluded，document.visibilityState 恒为 hidden，sendInputEvent 的事件
    // 也进不了页面（2026-11-18 真机：opacity 0 → hidden/complete，同一页 show() 后 visible）。
    // 只在「安静显示仍然 hidden」时调用，用完由 hideQuiet 收回。
    showForCheck:()=>{
      if(!owner||owner.isDestroyed())return false;
      try{
        if(owner===parking){
          if(!parking.__homeBounds)parking.__homeBounds=parking.getBounds();
          parking.setPosition(0,0);
          parking.setOpacity(1);
        }
        if(owner.isMinimized())owner.restore();
        owner.showInactive();
        // 只「显示」还不够：屏幕上被别的窗口盖住时，DWM 仍会把这个页面判成
        // occluded → document.visibilityState=hidden（2026-11-18 真机：opacity 1 +
        // 屏幕内 + showInactive 之后依旧 hidden/complete，而同一页点一下置顶就 visible）。
        // 短暂置顶让系统承认这一帧真的被呈现；核验一过立刻 releaseTopmost 让开。
        try{owner.setAlwaysOnTop(true,'screen-saver');topmostForCheck=true;}catch(_){topmostForCheck=false;}
        try{owner.moveTop();}catch(_){/* 不影响判定 */}
        return true;
      }catch(_){return false;}
    },
    // 置顶只是为了让可见性核验通过，不是要挡住用户：核验一过就还回去。
    // 注意不 hide —— 页面一旦重新隐藏又变回 hidden，后面的点击事件会被丢掉。
    releaseTopmost:()=>{
      if(!topmostForCheck)return;
      topmostForCheck=false;
      try{if(owner&&!owner.isDestroyed())owner.setAlwaysOnTop(false);}catch(_){/* 忽略 */}
    },
    hideQuiet:()=>{
      try{
        if(topmostForCheck){topmostForCheck=false;try{if(owner&&!owner.isDestroyed())owner.setAlwaysOnTop(false);}catch(_){}}
        if(owner&&!owner.isDestroyed()&&owner===parking){
          parking.hide();
          parking.setOpacity(1);
          if(parking.__homeBounds)parking.setBounds(parking.__homeBounds);
        }
      }catch(_){/* 忽略 */}
    },
    focus:()=>{if(owner&&!owner.isDestroyed()){owner.focus();wc.focus();}},
    destroy:()=>{
      if (destroyed) return;
      destroyed=true;
      clearInterval(refreshTimer);
      if(owner&&!owner.isDestroyed())owner.contentView.removeChildView(clip);
      if(!wc.isDestroyed())wc.close();
      if(!parking.isDestroyed())parking.destroy();
      owner=null;
    },
  };
}

// Dashboard also sends init-automation-view without a URL. That must preserve
// an existing execution page, even when the command is between busy periods.
async function initializeLoginPage(surface, state, requestedUrl) {
  if(state?.busy)return false;
  const current=surface.webContents.getURL();
  const target=requestedUrl || (!current || current==='about:blank' ? 'https://www.douyin.com/' : '');
  if(!target || current.startsWith(String(target).split('?')[0]))return false;
  await surface.webContents.loadURL(String(target));
  return true;
}

// A readiness acknowledgement belongs to the actual attached page and run.
// requestAnimationFrame observes its render lifecycle; no JPEG/Canvas mirror.
async function observeEmbeddedPage(entry, state, host, currentState) {
  if (!entry?.surface || !entry.attached || !state?.runId || entry.__observing ||
      !host || host.isDestroyed() || !host.isVisible() || host.isMinimized() ||
      !entry.surface.isAttachedTo(host)) return false;
  const wc=entry.view.webContents, bounds=entry.view.getBounds();
  const pageUrl=wc.isDestroyed() ? '' : wc.getURL();
  if(wc.isDestroyed() || wc.isLoadingMainFrame() || bounds.width<=2 || bounds.height<=2 ||
     !/^https:\/\//.test(wc.getURL())) return false;
  entry.__observing=true;
  try {
    // Bound the wait when Chromium stops producing animation frames. A late
    // result is ignored and can never acknowledge another run or hidden view.
    let timeout;
    const painted=await Promise.race([
      wc.executeJavaScript(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(
        document.readyState!=='loading' && document.visibilityState==='visible'))))`),
      new Promise(resolve=>{timeout=setTimeout(()=>resolve(false),1000);}),
    ]).finally(()=>clearTimeout(timeout));
    const latest=currentState();
    if(!painted || !latest || latest.runId!==state.runId || wc.isDestroyed() || wc.isLoadingMainFrame() ||
       wc.getURL()!==pageUrl || !entry.attached || !entry.surface.isAttachedTo(host) || !host.isVisible() || host.isMinimized())return false;
    entry.__displayedRunId=state.runId;
    entry.__displayedFrameAt=Date.now();
    return true;
  } catch (_) { return false; }
  finally { entry.__observing=false; }
}

module.exports={createWorkerSurface,initializeLoginPage,observeEmbeddedPage};
