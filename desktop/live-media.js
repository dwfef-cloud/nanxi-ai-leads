"use strict";
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const INPUT_SELECTOR='.comment-input-inner-container input[type="file"][data-lead-attachment="reviewed"]';
const IMAGE_UPLOAD_HOOK_JS=`(() => {
  if(window.__leadImageHook)return;window.__leadImageHook=true;window.__leadImageReceipts=[];
  const fileOf=async body=>{try{
    const files=body instanceof FormData?Array.from(body.values()).filter(v=>v instanceof Blob):body instanceof Blob?[body]:[];
    if(files.length!==1)return null;const file=files[0];
    if(!/^image\\/(png|jpeg|webp)$/.test(file.type)||file.size>10485760)return null;
    const hash=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
    return {size:file.size,hash:Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('')};
  }catch(_){return null;}};
  const add=async(url,http,body,filePromise)=>{try{
    if(!/upload/i.test(String(url))||http!==200)return;
    const file=await filePromise;if(!file)return;const b=JSON.parse(body);
    const uri=b.data?.image_uri||b.data?.uri||b.image_uri||b.uri;
    if((b.status_code??b.code)!==0||typeof uri!=='string')return;
    window.__leadImageReceipts.push({...file,uri});if(window.__leadImageReceipts.length>5)window.__leadImageReceipts.shift();
  }catch(_){}};
  const fetch=window.fetch;window.fetch=function(...args){const url=String(args[0]?.url||args[0]||''),file=fileOf(args[1]?.body);
    return fetch.apply(this,args).then(res=>{if(/upload/i.test(url))res.clone().text().then(body=>add(url,res.status,body,file)).catch(()=>{});return res;});};
  const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,url,...rest){this.__leadImageUrl=String(url||'');return open.call(this,method,url,...rest);};
  XMLHttpRequest.prototype.send=function(...args){if(/upload/i.test(this.__leadImageUrl)){
    const file=fileOf(args[0]);this.addEventListener('loadend',()=>{try{add(this.__leadImageUrl,this.status,this.responseText,file);}catch(_){}},{once:true});
  }return send.apply(this,args);};
})()`;
function verifyImage(command) {
  try {
  const p=command.attachmentPath;
  if(typeof p!=='string'||!path.isAbsolute(p)||path.basename(p)!==command.attachmentName)return false;
  const stat=fs.statSync(p);
  if(!stat.isFile()||stat.size!==command.attachmentSize||stat.size<16||stat.size>10*1024*1024)return false;
  const data=fs.readFileSync(p),ext=path.extname(p).toLowerCase();
  const format=ext==='.png'&&data.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])) ||
    ['.jpg','.jpeg'].includes(ext)&&data.subarray(0,3).equals(Buffer.from([255,216,255])) ||
    ext==='.webp'&&data.subarray(0,4).toString()==='RIFF'&&data.subarray(8,12).toString()==='WEBP';
  return !!format&&crypto.createHash('sha256').update(data).digest('hex')===command.attachmentSha256;
  }catch(_){return false;}
}
const IMAGE_INPUT_JS=`(() => {
  const editor=document.activeElement,container=editor?.closest('.comment-input-inner-container');
  if(!container||!editor.matches('[contenteditable="true"]')||(editor.innerText||'').trim())return null;
  if(container.querySelector('[data-e2e="comment-image-preview"] img,[class*="upload-preview"] img'))return null;
  const inputs=Array.from(container.querySelectorAll('input[type="file"]')).filter(e=>/image|png|jpeg|jpg|webp/i.test(e.accept));
  if(inputs.length!==1||inputs[0].files?.length||inputs[0].disabled)return null;
  inputs[0].setAttribute('data-lead-attachment','reviewed');return true;
})()`;
function uploadProbe(command) {
  return `(() => {
    const file=document.querySelector(${JSON.stringify(INPUT_SELECTOR)})?.files?.[0];
    if(!file||file.name!==${JSON.stringify(command.attachmentName)}||file.size!==${Number(command.attachmentSize)})return null;
    const nodes=Array.from(document.querySelectorAll('.comment-input-inner-container [data-e2e="comment-image-preview"],.comment-input-inner-container [class*="upload-preview"]'));
    if(nodes.length!==1)return null;
    const n=nodes[0],img=n.querySelector('img');
    if(!img||!img.complete||!img.naturalWidth||n.querySelector('[role="progressbar"],[class*="loading"],[class*="error"]'))return null;
    const receipt=(window.__leadImageReceipts||[]).filter(e=>e.hash===${JSON.stringify(command.attachmentSha256)}&&e.size===${Number(command.attachmentSize)});
    const uri=n.getAttribute('data-image-uri')||n.getAttribute('data-uri')||img.getAttribute('data-image-uri')||receipt.at(-1)?.uri;
    // A blob URL only proves local preview, never a completed platform upload.
    return uri&&/^[A-Za-z0-9_./-]{6,500}$/.test(uri)&&!uri.startsWith('blob:')?uri:null;
  })()`;
}
async function attachImage(command,io,active) {
  if(!verifyImage(command)||!active()||!await io.evaluate(IMAGE_INPUT_JS))return '';
  await io.evaluate(IMAGE_UPLOAD_HOOK_JS);
  await io.evaluate('window.__leadImageReceipts = []');
  await io.uploadImage(command,INPUT_SELECTOR);
  for(let i=0;i<30;i++) {
    if(!active())return '';
    const uri=await io.evaluate(uploadProbe(command));
    if(uri&&verifyImage(command))return uri;
    await io.sleep(500);
  }
  return '';
}
// Only the reviewed local image can enter this account's unique comment input.
async function uploadImage(webContents,command,selector,active) {
  if(selector!==INPUT_SELECTOR||!verifyImage(command)||!active())throw Error('Image authorization changed');
  const debug=webContents.debugger;
  if(debug.isAttached())throw Error('Existing debugger must be preserved');
  debug.attach('1.3');
  try {
    const {root}=await debug.sendCommand('DOM.getDocument');
    const {nodeIds}=await debug.sendCommand('DOM.querySelectorAll',{nodeId:root.nodeId,selector});
    if(nodeIds.length!==1||!active()||!verifyImage(command))throw Error('Comment input or image changed');
    const {nativeImage}=require('electron');
    if(nativeImage.createFromPath(command.attachmentPath).isEmpty())throw Error('Invalid image data');
    await debug.sendCommand('DOM.setFileInputFiles',{nodeId:nodeIds[0],files:[command.attachmentPath]});
  } finally {debug.detach();}
}
module.exports={verifyImage,attachImage,uploadImage,uploadProbe,IMAGE_INPUT_JS,IMAGE_UPLOAD_HOOK_JS,INPUT_SELECTOR};
