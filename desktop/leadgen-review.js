"use strict";
const LABELS={'post-comment':'发表评论','reply-comment':'回复评论','send-dm':'私信','like-comment':'评论点赞','like-video':'视频点赞',
  'favorite-video':'收藏作品','follow-user':'关注用户','share-profile':'分享用户主页名片','share-comment':'分享原评论'};

function reviewPages(selected) {
  const pages=[];
  for(const [index,a] of selected.actions.entries()) {
    const text=`${index+1}. ${LABELS[a.type]}\n来源关键词：${a.keyword}\n视频：${a.title}\n${a.videoUrl}\n`+
      (a.commentAuthor?`对象：${a.commentAuthor}\n原评论：${a.sourceText}\n`:'')+
      (a.recipientId?`对象主页：https://www.douyin.com/user/${a.recipientId}\n`:'')+
      (a.shareRecipient?`分享至：${a.shareRecipient}（${a.shareRecipientKind==='group'?'群聊':'好友'}，${a.shareRecipientId}）\n`:'')+
      (a.attachmentPath?`附图：${a.attachmentName}\n路径：${a.attachmentPath}\n大小：${a.attachmentSize} 字节\nSHA256：${a.attachmentSha256}\n`:'')+`正文：${a.text||'（无文本动作）'}`;
    const extras=(a.stickerId?`\nGIF：${a.stickerName}（${a.stickerId}）\n预览：${a.stickerUrl}`:'')+
      (a.mentionTargets?`\n@账号（${a.mentionPosition==='before'?'正文前':'正文后'}）：`+JSON.parse(a.mentionTargets).map(m=>`${m.name}（${m.id}）`).join('、'):'')+
      (a.saveProfileQr?`\n保存该用户的原生主页二维码；运行：${a.profileQrRunKey}`:'');
    // Keep every approved text visible, below native Windows dialog limits.
    if(text.length+extras.length>12000)throw Error('目标内容过长，无法完整核对，请缩小范围后重新准备');
    if(!pages.length||pages[pages.length-1].length+text.length+extras.length>12000)pages.push(text+extras);
    else pages[pages.length-1]+='\n\n'+text+extras;
  }
  return pages;
}

async function confirmPlan(selected,show) {
  const pages=reviewPages(selected);
  if(!pages.length)return false;
  for(let i=0;i<pages.length;i++) {
    const last=i===pages.length-1;
    const result=await show({type:'question',title:'确认评论获客真实执行',
      message:`使用“${selected.nickname}”核对 ${selected.actions.length} 项动作（第 ${i+1}/${pages.length} 页）`,
      detail:(selected.configSummary||[]).join('\n')+'\n\n'+pages[i]+
        '\n\n'+(last?'确认后仅执行以上各页核对的目标和原文；结果不明确立即停止，不自动重试。':'本页尚未授权执行，请继续核对下一页。'),
      buttons:['取消',last?'确认并真实执行':'下一页（尚不执行）'],defaultId:0,cancelId:0,noLink:true});
    if(result.response!==1)return false;
  }
  return true;
}
module.exports={reviewPages,confirmPlan};
