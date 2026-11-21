"use strict";
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const {profileUrl}=require('./live-dm');
function rootDirectory(){return process.env.LEADS_PROFILE_QR_ROOT||path.resolve(__dirname,'..','用户主页二维码');}
function safeName(value) {
  let name=String(value||'用户').normalize('NFC').replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').replace(/[. ]+$/g,'').slice(0,70);
  if(!name)name='用户';
  if(/^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)/i.test(name))name='_'+name;
  return name;
}
function runDirectory(key,root=rootDirectory()) {
  if(typeof key!=='string'||!key.trim()||key.length>160||/[\x00-\x1f/\\]/.test(key))throw Error('Invalid QR run key');
  const hash=crypto.createHash('sha256').update(key).digest('hex');
  const index=path.join(root,'.runs');fs.mkdirSync(index,{recursive:true});
  const marker=path.join(index,hash+'.json');
  const folder=new Date().toISOString().replace(/[:.]/g,'-')+'_'+hash.slice(0,12);
  try{fs.writeFileSync(marker,JSON.stringify({folder}),{flag:'wx'});}catch(error){if(error.code!=='EEXIST')throw error;}
  const saved=JSON.parse(fs.readFileSync(marker,'utf8'));
  if(typeof saved.folder!=='string'||!/^\d{4}-[A-Za-z0-9_-]{10,80}$/.test(saved.folder))throw Error('Invalid QR folder');
  const dir=path.join(root,saved.folder);fs.mkdirSync(dir,{recursive:true});return dir;
}
function qrProbe(command,nickname) {
  return `(() => {
    const url=${JSON.stringify(profileUrl(command.recipientId))},id=${JSON.stringify(command.recipientId)};
    if(location.href.split('?')[0]!==url||document.querySelector('#login-pannel,#captcha_container,[data-e2e="verify-dialog"],[data-e2e="login-guide"]'))return null;
    const self=Array.from(document.querySelectorAll('header [class*="userMenuPanel"]')).map(e=>e.textContent||'').join(' ');
    if(!self.startsWith(${JSON.stringify(nickname)}+'关注')||!Array.from(document.querySelectorAll('h1,[data-e2e="user-title"]')).some(e=>(e.innerText||'').trim()===${JSON.stringify(command.commentAuthor)}))return null;
    const visible=e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;};
    const panels=Array.from(document.querySelectorAll('[data-e2e="profile-qr"],[data-e2e="user-qrcode"],[role="dialog"]')).filter(visible)
      .filter(e=>e.matches('[data-e2e="profile-qr"],[data-e2e="user-qrcode"]')||/二维码/.test(e.innerText||''));
    const nodes=panels.filter(e=>!panels.some(other=>other!==e&&e.contains(other)));
    if(nodes.length!==1)return null;
    const p=nodes[0];
    const images=Array.from(p.querySelectorAll('canvas,img')).filter(visible).filter(e=>e.tagName==='CANVAS'||e.complete&&e.naturalWidth>=80);
    const squares=images.filter(e=>{const r=e.getBoundingClientRect();return r.width>=80&&r.height>=80&&Math.abs(r.width-r.height)<8;});
    if(squares.length!==1)return null;
    const e=squares[0];e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();
    if(r.left<0||r.top<0||r.right>innerWidth||r.bottom>innerHeight||r.width>1024||r.height>1024)return null;
    const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    if(hit!==e&&!e.contains(hit))return null;
    return {userId:id,x:r.left,y:r.top,width:r.width,height:r.height};
  })()`;
}
const QR_BUTTON_JS=`(() => {
  const visible=e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;};
  const nodes=Array.from(document.querySelectorAll('[role="dialog"] button,[data-e2e="share-dialog"] button,[data-e2e="profile-qr-button"],[role="dialog"] [role="button"]')).filter(visible)
    .filter(e=>['二维码','分享二维码','保存二维码'].includes((e.innerText||e.getAttribute('aria-label')||'').trim()));
  const unique=nodes.filter(e=>!nodes.some(other=>other!==e&&e.contains(other)));
  if(unique.length!==1)return null;const e=unique[0],r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);
  return !e.disabled&&(hit===e||e.contains(hit))?{x,y}:null;
})()`;
async function saveProfileQr(command,io,guard) {
  const fail=()=>({ok:false,code:'profile_qr_not_saved'});
  if(command.type!=='share-profile'||command.saveProfileQr!=='1'||!io.captureQr||!guard.active())return fail();
  let p=await io.evaluate(qrProbe(command,guard.scope.nickname));
  if(!p) {
    const button=await io.evaluate(QR_BUTTON_JS);
    if(!button||!guard.active())return fail();
    await io.click(button.x,button.y);
    for(let i=0;i<15;i++) {if(!guard.active())return fail();await io.sleep(300);p=await io.evaluate(qrProbe(command,guard.scope.nickname));if(p)break;}
  }
  if(!p||!guard.active())return fail();
  const png=await io.captureQr(p);
  const after=await io.evaluate(qrProbe(command,guard.scope.nickname));
  if(!guard.active()||!after||JSON.stringify(after)!==JSON.stringify(p)||!Buffer.isBuffer(png)||!png.subarray(0,8).equals(Buffer.from('89504e470d0a1a0a','hex')))return fail();
  const dir=runDirectory(command.profileQrRunKey),name=safeName(command.commentAuthor);
  for(let i=0;i<10000;i++) {
    if(!guard.active())return fail();
    const file=path.join(dir,name+(i?'_'+(i+1):'')+'.png');
    try {
      fs.writeFileSync(file,png,{flag:'wx'});
      const result={qrFile:file,qrUserId:command.recipientId,qrSha256:crypto.createHash('sha256').update(png).digest('hex')};
      try {
        if(!io.key||!guard.active())return {ok:false,code:'profile_qr_saved_share_not_sent',...result};
        await io.key('Escape');
        for(let wait=0;wait<10;wait++) {
          if(!guard.active())return {ok:false,code:'profile_qr_saved_share_not_sent',...result};
          if(await io.evaluate(require('./live-social').SHARE_DIALOG_JS))return {ok:true,shareDialog:true,...result};
          await io.sleep(250);
        }
      } catch (_) {
        return {ok:false,code:'profile_qr_saved_share_not_sent',...result};
      }
      return {ok:false,code:'profile_qr_saved_share_not_sent',...result};
    }
    catch(error){if(error.code!=='EEXIST')throw error;}
  }
  return fail();
}
async function captureQr(web,rect,scale,active) {
  if(!active()||!rect||['x','y','width','height'].some(k=>!Number.isFinite(rect[k]))||rect.x<0||rect.y<0||rect.width<80||rect.height<80||rect.width>1024||rect.height>1024)throw Error('QR bounds invalid');
  const image=await web.capturePage({x:Math.floor(rect.x*scale.x),y:Math.floor(rect.y*scale.y),
    width:Math.ceil(rect.width*scale.x),height:Math.ceil(rect.height*scale.y)});
  if(!active()||image.isEmpty())throw Error('QR capture unavailable');
  return image.toPNG();
}
module.exports={rootDirectory,safeName,runDirectory,qrProbe,QR_BUTTON_JS,saveProfileQr,captureQr};
