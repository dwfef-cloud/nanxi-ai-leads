const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm');
const {PLAYER_PROBE,focusGeometry}=require('../video-focus');

test('landscape and portrait images fit entirely without distortion or page chrome',()=>{
  const host={x:264,y:374,width:818,height:477};
  const landscape=focusGeometry(host,{viewport:{width:1280,height:900},rect:{x:240,y:140,width:640,height:360}});
  assert.deepEqual(landscape.clip,{x:264,y:383,width:818,height:460});
  assert.equal(landscape.page.x,-307);assert.equal(landscape.page.y,-179);
  assert.equal(landscape.zoom,818/640);
  const portrait=focusGeometry(host,{viewport:{width:1280,height:900},rect:{x:500,y:80,width:360,height:640}});
  assert.deepEqual(portrait.clip,{x:539,y:374,width:268,height:477});
  assert.equal(portrait.zoom,477/640);
  assert.equal(focusGeometry(host,{viewport:{width:1280,height:900},rect:{x:240,y:-100,width:640,height:360}}),null);
});

function probe({active=true,detail=false,paused=false,duplicate=false,blocker=false,hidden=false,portrait=false}={}){
  const rect={x:240,y:140,width:640,height:360,left:240,top:140,right:880,bottom:500};
  const video={isConnected:true,nodeType:1,readyState:2,paused,ended:false,videoWidth:portrait?360:640,videoHeight:portrait?640:360,
    getBoundingClientRect:()=>rect,parentElement:null};
  const root={querySelectorAll:()=>duplicate?[video,{...video}]:[video],getAttribute:()=> 'active-id'};
  const doc={querySelector:()=>active?root:null,querySelectorAll:s=>s==='video'?[video]:blocker?[video]:[]};
  return vm.runInNewContext(PLAYER_PROBE,{document:doc,innerWidth:1280,innerHeight:900,URL,
    location:{href:detail?'https://www.douyin.com/video/123456':'https://www.douyin.com/jingxuan',pathname:detail?'/video/123456':'/jingxuan'},
    getComputedStyle:()=>({display:hidden?'none':'block',visibility:'visible',opacity:'1',objectFit:'contain',objectPosition:'50% 50%'})});
}

test('repeated rounded DOM viewports cannot grow the fixed execution viewport',()=>{
  const host={x:264,y:300,width:818,height:477},fixed={width:1280,height:900};
  for(const width of [1280,1281,1282,1283]){
    const geometry=focusGeometry(host,{viewport:{width,height:901},rect:{x:240,y:140,width:640,height:360}},fixed);
    assert.equal(geometry.page.width,Math.ceil(1280*geometry.zoom));
    assert.equal(geometry.page.height,Math.ceil(900*geometry.zoom));
  }
});
test('only current feed/detail video is selected; grid, hidden, ambiguous and blocked pages stay whole',()=>{
  assert.equal(probe().reason,'current_video');
  assert.equal(probe({paused:true}).reason,'current_video');
  assert.equal(probe({active:false,detail:true}).reason,'current_video');
  assert.equal(probe({active:false}).reason,'no_current_video');
  assert.equal(probe({duplicate:true}).reason,'ambiguous_video');
  assert.equal(probe({hidden:true}).reason,'video_loading');
  assert.equal(probe({blocker:true}).reason,'verification_or_login');
});
test('portrait contain letterboxes are excluded while preserving the complete media image',()=>{
  const r=probe({portrait:true}).rect;
  assert.equal(r.height,360);assert.equal(r.width,202.5);
  assert.equal(r.x,458.75);assert.equal(r.y,140);
});
