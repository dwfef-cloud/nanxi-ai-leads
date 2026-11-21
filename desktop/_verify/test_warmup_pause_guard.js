const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {render} = require('./check_warmup_js');

function fixture(commandId = 'current') {
  const listeners = new Set();
  class Media {
    tagName='VIDEO';paused=false;
    pause(){this.paused=true;}
    play(){this.paused=false;return Promise.resolve();}
  }
  const player = new Media();
  const context = {window:{__warmupCommandId:commandId,HTMLMediaElement:Media},document:{
    querySelectorAll:()=>[player],
    addEventListener(name,handler,capture){assert.equal(name,'play');assert.equal(capture,true);listeners.add(handler);},
    removeEventListener(name,handler,capture){assert.equal(name,'play');assert.equal(capture,true);listeners.delete(handler);},
  }};
  return {context,player,listeners,stop(id){return vm.runInNewContext(render('WARMUP_STOP_JS').replace('__COMMAND_ID__',JSON.stringify(id)),context);},
    play(){player.paused=false;for(const handler of [...listeners])handler({target:player});}};
}

test('manual stop keeps platform autoplay paused, including replacement videos',()=>{
  const f=fixture();
  assert.equal(f.stop('current'),true);
  assert.equal(f.player.paused,true);
  f.play();assert.equal(f.player.paused,true);
  const replacement={tagName:'VIDEO',paused:false,pause(){this.paused=true;}};
  for(const handler of f.listeners)handler({target:replacement});
  assert.equal(replacement.paused,true);
  f.stop('current');assert.equal(f.listeners.size,1);
});

test('late cancellation cannot pause a new command; old guard releases itself',()=>{
  const f=fixture();f.stop('current');
  f.context.window.__warmupCommandId='next';
  f.context.window.__warmupStop=false;
  f.play();assert.equal(f.player.paused,false);assert.equal(f.listeners.size,0);
  assert.equal(f.stop('current'),false);assert.equal(f.player.paused,false);
});

test('stopped page blocks play before the asynchronous play event, and release restores playback',async()=>{
  const f=fixture();
  const play=f.context.window.HTMLMediaElement.prototype.play;
  f.stop('current');
  await f.player.play();assert.equal(f.player.paused,true);
  const detached=new f.context.window.HTMLMediaElement();
  await detached.play();assert.equal(detached.paused,true);
  f.context.window.__warmupReleasePause();
  assert.equal(f.context.window.HTMLMediaElement.prototype.play,play);
  assert.equal(f.listeners.size,0);
  await f.player.play();assert.equal(f.player.paused,false);
});
