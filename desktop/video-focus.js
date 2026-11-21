"use strict";

// Read only: never move/style a platform node or replace the media element.
function playerProbe() {
  const visible = element => {
    if(!element?.isConnected)return false;
    const r=element.getBoundingClientRect();
    if(r.width<40||r.height<40||r.right<=0||r.bottom<=0||r.left>=innerWidth||r.top>=innerHeight)return false;
    for(let node=element;node&&node.nodeType===1;node=node.parentElement){
      const css=getComputedStyle(node);
      if(css.display==='none'||css.visibility==='hidden'||Number(css.opacity)===0)return false;
    }
    return true;
  };
  const viewport={width:innerWidth,height:innerHeight};
  // Authentication must stay visible even when a video exists underneath it.
  const blockers=Array.from(document.querySelectorAll(
    '[id*="captcha"], [class*="captcha"], [data-e2e="login-dialog"], [data-e2e="login-modal"], [data-e2e="verify-dialog"]'));
  if(blockers.some(visible))return {viewport,reason:'verification_or_login'};
  const active=document.querySelector('[data-e2e="feed-active-video"]');
  const detail=/\/video\/\d+/.test(location.pathname)||new URL(location.href).searchParams.has('modal_id');
  // Search-grid autoplay and preload videos must not become the live target.
  if(!active&&!detail)return {viewport,reason:'no_current_video'};
  const candidates=Array.from((active||document).querySelectorAll('video')).filter(video=>{
    if(!visible(video)||video.readyState<2)return false;
    const r=video.getBoundingClientRect();
    const area=Math.max(0,Math.min(r.right,innerWidth)-Math.max(0,r.left))*
      Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(0,r.top));
    return area/(r.width*r.height)>.6;
  });
  const playing=candidates.filter(v=>!v.paused&&!v.ended);
  const pool=playing.length?playing:candidates;
  if(pool.length!==1)return {viewport,reason:pool.length?'ambiguous_video':'video_loading'};
  const video=pool[0],r=video.getBoundingClientRect();
  let rect={x:r.x,y:r.y,width:r.width,height:r.height};
  // A contain-fitted portrait video may live in a landscape <video> box.
  // Focus its actual image, preserving the full image and original aspect.
  const css=getComputedStyle(video);
  if(css.objectFit==='contain'&&video.videoWidth>0&&video.videoHeight>0 &&
     /^(50%|center)( (50%|center))?$/.test(css.objectPosition)){
    const scale=Math.min(r.width/video.videoWidth,r.height/video.videoHeight);
    const width=video.videoWidth*scale,height=video.videoHeight*scale;
    rect={x:r.x+(r.width-width)/2,y:r.y+(r.height-height)/2,width,height};
  }
  return {viewport,rect,reason:'current_video',videoKey:active?.getAttribute('data-e2e-vid')||location.pathname};
}
const PLAYER_PROBE='('+playerProbe.toString()+')()';

function focusGeometry(host,probe,executionViewport=probe?.viewport) {
  const r=probe?.rect,vp=probe?.viewport;
  if(!r||!vp||![host.width,host.height,r.x,r.y,r.width,r.height,vp.width,vp.height].every(Number.isFinite)||
    Math.min(host.width,host.height,r.width,r.height,vp.width,vp.height)<=0||r.x<0||r.y<0||
    r.x+r.width>vp.width+1||r.y+r.height>vp.height+1)return null;
  const zoom=Math.min(host.width/r.width,host.height/r.height);
  // Electron zoom limits. Tiny thumbnail-like targets are not valid players.
  if(zoom<.25||zoom>3)return null;
  const width=Math.max(1,Math.round(r.width*zoom)),height=Math.max(1,Math.round(r.height*zoom));
  if(!executionViewport||![executionViewport.width,executionViewport.height].every(n=>Number.isFinite(n)&&n>0))return null;
  return {zoom,clip:{x:host.x+Math.round((host.width-width)/2),y:host.y+Math.round((host.height-height)/2),width,height},
    page:{x:-Math.round(r.x*zoom),y:-Math.round(r.y*zoom),width:Math.ceil(executionViewport.width*zoom),height:Math.ceil(executionViewport.height*zoom)}};
}

module.exports={PLAYER_PROBE,focusGeometry};
