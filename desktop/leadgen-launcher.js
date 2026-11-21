"use strict";
// The trusted main process authorizes a bounded sequential run from saved settings.
function createLeadgenLauncher(io) {
  let busy = false;
  const accountBusy = (state, payload) => {
    const active=Array.isArray(state.active)?state.active:[];
    return active.some(r=>!r.accountId||String(r.accountId)===String(payload.accountId)) ||
      (state.busy && !active.length);
  };
  return async payload => {
    if (!payload || typeof payload !== 'object' || !payload.taskId || !payload.accountId)
      return {success:false,error:'缺少任务或账号'};
    if(busy)return {success:false,error:'正在准备另一个真机任务，请等待或停止当前准备'};
    busy=true;
    let prepared;
    try {
      const before=await io.runtime();
      if(accountBusy(before,payload))throw Error('当前账号正在执行任务或收尾，请先停止');
      prepared=await io.prepare(payload);
      if(prepared?.success!==true || prepared.payload?.taskId!==payload.taskId ||
        prepared.payload?.accountId!==payload.accountId || !prepared.scopeFile)
        throw Error(prepared?.error || '任务授权范围与设置不一致');
      await io.activate(prepared.scopeFile, prepared.payload);
      return await io.start(prepared.payload,prepared);
    } catch(error) {
      if(prepared && io.cleanup) {
        try {await io.cleanup(prepared.payload);} catch(_) {}
      }
      return {success:false,error:error.message || '真机任务启动失败'};
    } finally {busy=false;}
  };
}
module.exports={createLeadgenLauncher};
