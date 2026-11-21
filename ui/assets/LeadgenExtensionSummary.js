// Uses the same task config object as the original settings controls and IPC.
// This is a preview of selected options, never an execution result.
export function extensionSummary(config) {
  const rows = [];
  for (const [prefix, label] of [['videoComment', '视频主评'], ['comment', '回复评论']]) {
    const cap = prefix[0].toUpperCase() + prefix.slice(1);
    if (!config['enable' + cap]) {
      rows.push({key: prefix, label, text: '未开启；附件和@账号不参与执行'});
      continue;
    }
    const parts = [];
    const image = config['enable' + cap + 'Image'];
    const gif = config['enable' + cap + 'Expression'];
    if (image && gif) parts.push('图片与GIF同时开启，请选择一种');
    else if (gif) parts.push(`GIF概率 ${config[prefix + 'AttachmentPercent'] ?? 20}%，前 ${config[prefix + 'ExpressionCount'] ?? 3} 张按顺序轮询`);
    else if (image) parts.push(`附图概率 ${config[prefix + 'AttachmentPercent'] ?? 20}%`);
    else parts.push('不附带图片或GIF');
    if (config['enable' + cap + 'Mention']) {
      const names = [...new Set(String(config[prefix + 'MentionNicknames'] || '').split(/[,，\r\n]+/)
        .map(value => value.trim().replace(/^@/, '').trim()).filter(Boolean))];
      const position = {before: '正文前', after: '正文后', random: '随机前后（准备清单时确定）'}[config[prefix + 'MentionPosition'] ?? 'before'] || '位置无效，请重新选择';
      parts.push(names.length ? `@${names.length}个账号，概率 ${config[prefix + 'MentionPercent'] ?? 100}%，${position}` : '@账号已开启，请填写昵称或抖音号');
    } else parts.push('@账号关闭');
    rows.push({key: prefix, label, text: parts.join('；')});
  }
  rows.push({key: 'profileQr', label: '主页二维码', text: !config.enableProfileShare
    ? '主页分享未开启；不保存二维码'
    : config.profileShareSaveQr ? '分享时保存到「用户主页二维码」，每次运行单独建文件夹' : '分享时不保存二维码'});
  return rows;
}
