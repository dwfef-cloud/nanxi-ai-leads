const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../../ui/live-worker-preview.js'),'utf8');

function fixture() {
  const images=[],draws=[],resizes=[];
  let width=300,height=150;
  const frame={style:{},get width(){return width;},set width(v){width=v;resizes.push('width');},
    get height(){return height;},set height(v){height=v;resizes.push('height');},
    getContext:()=>({drawImage:image=>draws.push(image.src)})};
  const elements={frame,waiting:{style:{}},status:{},at:{}};
  class Image {
    constructor(){images.push(this);}
    decode(){return new Promise((resolve,reject)=>{
      this.finish=(w=1120,h=630)=>{this.naturalWidth=w;this.naturalHeight=h;resolve();};
      this.fail=reject;
    });}
  }
  const context={window:{},document:{getElementById:id=>elements[id]},Image,Date,setInterval:()=>{}};
  vm.runInNewContext(source,context);
  return {context,images,draws,resizes,elements,show:(at,label='a')=>context.window.showWorkerFrame({
    image:'data:image/jpeg;base64,'+label,capturedAt:at,busy:true})};
}

test('preview keeps old pixels until decoding finishes; same-size frames never clear the canvas',async()=>{
  const f=fixture();
  const first=f.show(1);assert.equal(f.draws.length,0);
  f.images[0].finish();await first;
  const next=f.show(2,'b');
  assert.equal(f.draws.length,1);assert.equal(f.elements.frame.style.display,'block');
  assert.equal(f.resizes.length,2);
  f.images[1].finish();await next;
  assert.equal(f.draws.length,2);assert.equal(f.resizes.length,2);
  const broken=f.show(3);f.images[2].fail(Error('bad frame'));await broken;
  assert.equal(f.draws.length,2);assert.equal(f.elements.frame.style.display,'block');
});

test('out-of-order decoding and a pending frame after stopping cannot replace the last frame',async()=>{
  const f=fixture();
  const old=f.show(1,'old'),fresh=f.show(2,'fresh');
  f.images[1].finish();await fresh;f.images[0].finish();await old;
  assert.deepEqual(f.draws,['data:image/jpeg;base64,fresh']);
  const pending=f.show(3);f.context.window.endWorkerPreview();
  f.images[2].finish();await pending;
  assert.equal(f.draws.length,1);
  assert.equal(f.elements.status.textContent,'执行窗口空闲 · 最后画面');
});

test('invalid and stale captures do not decode or replace the retained canvas',async()=>{
  const f=fixture();const first=f.show(5);f.images[0].finish();await first;
  await f.show(4);await f.context.window.showWorkerFrame({image:'https://example.invalid/image',capturedAt:6});
  assert.equal(f.images.length,1);assert.equal(f.draws.length,1);
});
