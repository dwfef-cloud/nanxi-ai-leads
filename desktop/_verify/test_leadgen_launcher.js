const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {createLeadgenLauncher}=require('../leadgen-launcher');
const payload={taskId:'run',leadgenTaskId:'task',accountId:'account',keywords:'工作流'};
function fixture(overrides={}) {
  const calls=[];
  const prepared={success:true,scopeFile:'exact-local-file',payload};
  return {calls,run:createLeadgenLauncher({runtime:async()=>({active:[]}),prepare:async p=>{
      assert.equal(p.taskId,'run');calls.push('prepare');return prepared;},
    activate:async (file,p)=>{assert.equal(file,'exact-local-file');assert.equal(p,payload);calls.push('activate');},
    start:async p=>{assert.equal(p,payload);calls.push('start');return {success:true};},...overrides})};
}
test('saved settings authorize bounded streaming without a separate batch review',async()=>{
  const {run,calls}=fixture();assert.equal((await run({...payload,scopeFile:'untrusted'})).success,true);
  assert.deepEqual(calls,['prepare','activate','start']);
});
test('busy task and authorization failures do not start',async()=>{
  for(const overrides of [{runtime:async()=>({active:[{taskId:'other'}]})},{runtime:async()=>({busy:true,active:[]})},
    {prepare:async()=>{throw Error('invalid settings');}},
    {activate:async()=>{throw Error('scope registration failed');}},
    {prepare:async()=>({success:true,scopeFile:'file',payload:{...payload,taskId:'other'}})}]) {
    const {run,calls}=fixture(overrides);assert.equal((await run(payload)).success,false);
    assert.deepEqual(calls,overrides.activate?['prepare']:overrides.prepare?[]:[]);
  }
});
test('double click cannot replace a pending streaming authorization',async()=>{
  let resolve;const pendingPrepare=new Promise(r=>resolve=r);const second=fixture({prepare:()=>pendingPrepare});
  const pending=second.run(payload);assert.equal((await second.run(payload)).success,false);
  resolve({success:true,scopeFile:'exact-local-file',payload});await pending;
  assert.deepEqual(second.calls,['activate','start']);
});
test('shipped bridge routes leadgen to streaming desktop start while nurture stays on existing HTTP path',async()=>{
  const calls=[];const shell={leadgenReal:true,leadgenPlanVersion:4,viewInvoke:async(...args)=>{calls.push(['native',...args]);return {success:true};}};
  const window={ipcRenderer:{invoke:()=>{}},location:{search:''},localStorage:{getItem:()=>null},
    __DESKTOP_SHELL__:true,__desktopShell:shell};
  const fetch=async(url,opts)=>{calls.push(['http',url,JSON.parse(opts.body)]);return {ok:true,json:async()=>({success:true,data:{success:true}})};};
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../../ui/live-bridge.js'),'utf8'),
    {window,fetch,URLSearchParams,console:{info(){},warn(){}},Map,Set});
  await window.ipcRenderer.invoke('start-task',{...payload,taskMode:'interaction'});
  await window.ipcRenderer.invoke('start-task',{...payload,taskMode:'nurture'});
  assert.equal(calls[0][1],'/ipc/validate-leadgen-config');
  assert.equal(calls[1][1],'start-leadgen-real');assert.equal(calls[2][1],'/ipc/start-task');
});
