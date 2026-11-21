"use strict";
// Desktop confirmation authorizes one exact cached comment. Renderer payloads
// never choose account/content/scope or turn on other platform writes.
function createCommentLikeLauncher(io) {
  let busy = false;
  return async taskId => {
    if (typeof taskId !== 'string' || !/^comment_like_[a-zA-Z0-9_-]{1,100}$/.test(taskId))
      return {success:false,error:'请选择已保存的评论点赞任务'};
    if (busy) return {success:false,error:'正在核对另一个真机任务，请先完成或取消'};
    busy = true;
    try {
      if ((await io.runtime()).taskIds?.length) throw Error('请先停止正在执行的评论点赞任务');
      const selected = await io.preview(taskId);
      if (selected.taskId !== taskId || selected.attempts !== 1 || !selected.binding)
        throw Error('无法核对真实评论，请重新读取并选择');
      if (!await io.confirm(selected)) return {success:false,cancelled:true};
      if ((await io.runtime()).taskIds?.length) throw Error('任务状态已改变，请先停止正在执行的任务');
      const file = await io.prepare(taskId, selected.binding);
      await io.activate(file);
      return await io.start(taskId, selected);
    } catch (error) {
      return {success:false,error:error.message || '真机启动失败'};
    } finally { busy = false; }
  };
}
module.exports = {createCommentLikeLauncher};
