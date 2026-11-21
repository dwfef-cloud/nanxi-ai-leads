import {i as h,A as ref,y as watch,z as onUnmounted} from './index-1pPBwPwk.js';
import {extensionSummary} from './LeadgenExtensionSummary.js';

export default {
  name:'LeadgenSettingsAudit',
  props:{config:{type:Object,required:true}},
  setup(props){
    const report=ref(null),busy=ref(false);
    let timer,revision=0,disposed=false;
    async function check(){
      const version=++revision;
      busy.value=true;
      try{
        const snapshot=JSON.parse(JSON.stringify(props.config));
        const result=await window.ipcRenderer.invoke('validate-leadgen-config',snapshot);
        if(!disposed&&version===revision)report.value=result;
      }catch(_){if(!disposed&&version===revision)report.value={success:false,errors:['配置检查服务暂不可用，请重试']};}
      finally{if(!disposed&&version===revision)busy.value=false;}
    }
    watch(()=>JSON.stringify(props.config),()=>{
      clearTimeout(timer);++revision;
      busy.value=true;report.value=null;
      timer=setTimeout(check,500);
    },{immediate:true});
    onUnmounted(()=>{disposed=true;clearTimeout(timer);++revision;});
    return ()=>h('section',{class:'leadgen-config-audit',style:{border:'1px solid var(--border-color, #dce3ef)',
      borderRadius:'10px',padding:'12px 16px',marginBottom:'12px',background:'var(--bg-card, #f6f8fc)'},
      'aria-label':'获客任务执行检查','aria-live':'polite'},[
      h('div',{style:{display:'flex',alignItems:'center',justifyContent:'space-between',gap:'12px'}},[
        h('strong',null,'执行设置检查'),
        h('button',{type:'button',disabled:busy.value,onClick:check,
          style:{cursor:'pointer',padding:'4px 10px',borderRadius:'6px',border:'1px solid #9bb8ef',background:'transparent',color:'inherit'}},
          busy.value?'检查中…':'重新检查（不执行）')]),
      h('p',{style:{fontSize:'12px',margin:'6px 0'}},'启动时逐账号核对关键词、智能体和目标清单；关注对象、分享好友／群聊及附图文件会一并列出确认。'),
      h('details',{class:'leadgen-extension-summary'},[
        h('summary',{style:{cursor:'pointer',fontSize:'12px'}},'查看本任务的 GIF、@账号与二维码设置'),
        ...extensionSummary(props.config).map(row=>h('p',{'data-extension':row.key,
          style:{fontSize:'12px',margin:'6px 0',overflowWrap:'anywhere'}},`${row.label}：${row.text}`)),
        h('p',{style:{fontSize:'12px',margin:'6px 0'}},'在下方「视频主评」「回复评论」设置附件和@账号；在「分享用户主页」勾选保存二维码。这里只预览当前配置，实际目标仍以启动确认清单为准。')
      ]),
      report.value?h('div',null,[
        h('p',{style:{fontSize:'13px',margin:'6px 0',color:report.value.success?'#25824e':'#b56a16'}},
          report.value.success?'配置检查通过，真实动作仍需核对目标清单':'以下设置需要处理后才能启动'),
        ...(report.value.errors||(report.value.success?[]:[report.value.error||report.value.msg||'检查服务未返回有效结果'])).map(text=>h('p',{style:{fontSize:'12px',margin:'4px 0'}},'• '+text)),
        h('details',null,[h('summary',{style:{cursor:'pointer',fontSize:'12px'}},'查看执行范围与规则'),
          ...(report.value.summary||[]).concat(report.value.warnings||[]).map(text=>h('p',{style:{fontSize:'12px',margin:'5px 0'}},text))])
      ]):null
    ]);
  }
};
