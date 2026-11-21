/* Inline controls for real leadgen runs. Never starts or approves a write. */
(function(){
  let timer=null,pending=false,root=null,notice='';
  async function refresh(){
    const host=document.querySelector('.leadgen-tasks-view');
    if(!host){root?.remove();root=null;return;}
    if(pending||!window.ipcRenderer)return;
    pending=true;
    try{
      const state=await window.ipcRenderer.invoke('get-runtime-status',{});
      if(!host.isConnected)return;
      const runs=(state.active||[]).filter(run=>run.kind==='leadgen'||run.kind==='leadgen-preview');
      if(!runs.length){root?.remove();root=null;return;}
      if(!root||!root.isConnected){
        root=document.createElement('section');root.className='leadgen-runtime-controls';
        root.setAttribute('aria-label','获客任务运行控制');
        root.style.cssText='padding:10px 14px;margin:10px 0;border:1px solid #9bb8ef;border-radius:8px;';
        const toolbar=host.querySelector('.tasks-toolbar');
        if(toolbar)toolbar.after(root);else host.prepend(root);
      }
      root.replaceChildren();
      for(const run of runs){
        const row=document.createElement('div');
        row.style.cssText='display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:4px 0;';
        const label=document.createElement('span');
        const preparing=run.kind==='leadgen-preview';
        label.textContent=(run.name||run.accountId||'获客账号')+'：'+(preparing?'正在搜索和准备清单（尚未发送）':run.stopping?'正在停止':run.paused?'暂停请求中 / 等待继续':'执行中')+
          (Number.isFinite(run.stats?.remaining)?'，剩余 '+run.stats.remaining+' 项':'');
        row.append(label);
        if(!preparing){
          const counts=document.createElement('span');
          counts.textContent='关注 '+(run.stats?.follows||0)+' · 主页分享 '+(run.stats?.profileShares||0)+' · 评论分享 '+(run.stats?.commentShares||0);
          row.append(counts);
        }
        const controls=preparing?[['停止准备','stop-task']]:[[run.paused?'继续':'暂停',run.paused?'resume-leadgen-task':'pause-leadgen-task'],['停止','stop-task']];
        for(const [text,channel] of controls){
          const button=document.createElement('button');button.type='button';button.textContent=text;
          button.disabled=!!run.stopping;
          button.style.cssText='padding:3px 12px;border:1px solid #9bb8ef;border-radius:6px;background:transparent;color:inherit;cursor:pointer;';
          button.onclick=async()=>{
            button.disabled=true;
            try{const result=await window.ipcRenderer.invoke(channel,{taskId:run.taskId});
              notice=result.success?(channel==='pause-leadgen-task'?'暂停请求已收到，当前已提交动作核验后停止后续动作':channel==='stop-task'?'停止请求已收到，正在收尾':'继续请求已收到'):
                result.error||result.msg||'操作失败，请重试';
            }catch(_){notice='控制服务暂不可用，请重试';}
            await refresh();
          };
          row.append(button);
        }
        root.append(row);
      }
      if(notice){const p=document.createElement('p');p.textContent=notice;p.setAttribute('role','status');root.append(p);}
    }catch(_){
      if(root?.isConnected){
        let error=root.querySelector('[data-control-error]');
        if(!error){error=document.createElement('p');error.dataset.controlError='true';root.append(error);}
        error.textContent='运行状态暂时读取失败，请稍后重试；不能据此判断任务已停止';
      }
    }finally{pending=false;}
  }
  window.addEventListener('DOMContentLoaded',()=>{timer=setInterval(()=>refresh().catch(()=>{}),1500);refresh().catch(()=>{});});
  window.addEventListener('beforeunload',()=>clearInterval(timer));
})();
