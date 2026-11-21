const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const path = require('node:path');

test('cold worker capture waits for navigation before requesting a frame', async()=>{
  const filename=path.join(__dirname,'../douyin_agent.js');
  const actualRequire=createRequire(filename);
  const workerMap=new Map();
  const source=fs.readFileSync(filename,'utf8')+'\nmodule.exports.__workerMap=workers;';
  const context={module:{exports:{}},require:name=>name==='electron'?{}:actualRequire(name),
    __dirname:path.dirname(filename),process,setInterval,clearInterval,setTimeout,clearTimeout,Buffer};
  vm.runInNewContext(source,context,{filename});
  let url='',loading=true,captures=0;
  const image={isEmpty:()=>false,resize(){return this;},toJPEG:()=>Buffer.from('fixture')};
  context.module.exports.__workerMap.set('a',{isDestroyed:()=>false,webContents:{
    getURL:()=>url,isLoadingMainFrame:()=>loading,capturePage:async()=>{captures++;return image;},
  }});
  const capture=context.module.exports.captureWorkerFrame;
  assert.equal(await capture('a'),null);
  url='https://www.douyin.com/?recommend=1';
  assert.equal(await capture('a'),null);
  assert.equal(captures,0);
  loading=false;
  assert.match((await capture('a')).image,/^data:image\/jpeg;base64,/);
  assert.equal(captures,1);
  assert.equal(await capture('other-account'),null);
});
