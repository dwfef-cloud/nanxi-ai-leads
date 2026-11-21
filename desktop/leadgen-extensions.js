"use strict";
// Extension fields are covered by both the exact reviewed list and the HMAC.
const EXTRA_FIELDS=['shareRecipient','shareRecipientId','shareRecipientKind','attachmentPath','attachmentSha256','attachmentSize','attachmentName',
  'stickerId','stickerName','stickerUrl','mentionTargets','mentionPosition','commentBody','saveProfileQr','profileQrRunKey'];
const USER_ACTIONS=new Set(['reply-comment','send-dm','follow-user','share-profile','share-comment']);
function signatureFields(command,base) {
  const fields=command.type==='like-comment'?[...base,'commentAuthor']:USER_ACTIONS.has(command.type)?[...base,'commentAuthor','recipientId']:[...base];
  return EXTRA_FIELDS.some(k=>command[k])?[...fields,...EXTRA_FIELDS]:fields;
}
const RESULT_TEXT={
  already_followed:'该用户已关注，本次跳过',profile_filter_or_identity_changed:'用户主页或性别、年龄条件未核对通过',
  follow_control_not_verified:'未找到可确认的关注按钮',follow_target_changed:'关注对象或登录身份发生变化',
  follow_state_unknown_no_retry:'已尝试关注，但刷新后未确认保留，停止并等待人工核对',
  native_share_source_missing:'未能定位原生分享入口或原评论已发生变化',
  share_recipient_missing_ambiguous_or_preselected:'接收人不存在、重名或页面已有选择，未发送',
  share_checkbox_not_verified:'未找到明确的接收人选择框，未发送',share_selection_changed:'分享接收人、来源或账号发生变化，未发送',
  share_receipt_missing_no_retry:'分享已提交，尚未取得目标与接收人一致的平台回执，停止且不重发',
  share_rejected:'平台拒绝本次分享',social_outcome_unknown_no_retry:'互动提交结果不明，停止且不重试',
  social_page_not_verified:'未能核对平台页面，未发送',image_upload_unavailable:'当前客户端没有加载图片上传组件，未发送',
  image_upload_not_verified:'附图未确认上传成功，已保留草稿，未发送纯文字',
  reviewed_image_changed:'已确认的图片发生变化，请重新准备',
  native_extras_not_verified:'GIF或@账号与确认清单不一致，已保留草稿，未发送',
  profile_qr_not_saved:'目标主页二维码未能保存，未发送主页分享',
  profile_qr_saved_share_not_sent:'二维码已保存，主页分享未提交',
  native_extras_not_verified:'平台GIF或@账号没有按确认清单留在编辑器中，未提交评论',
};
function describeResult(result){return result&&RESULT_TEXT[result.code]?{...result,error:RESULT_TEXT[result.code]}:result;}
module.exports={EXTRA_FIELDS,signatureFields,describeResult};
