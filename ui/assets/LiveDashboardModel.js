// Shared by the desktop UI and isolated tests. Offline runs never become cards.
export function projectLiveCards(items = [], occupied = new Set(), dismissed = new Set()) {
  const cards = new Map();
  for (const item of [...items].sort((a, b) => b.startedAt - a.startedAt)) {
    if (item.mode !== "live" || item.taskKind !== "comment_like" || !item.runId || !item.accountId) continue;
    if (dismissed.has(item.runId) || occupied.has(item.accountId) || cards.has(item.accountId)) continue;
    const stats = item.stats || {};
    cards.set(item.accountId, {...item, id: item.accountId, taskKind: "entity_leadgen",
      dashboardKind: "comment_like", entityLeadgenTaskId: item.taskId, sourceTypes: ["comment_like"],
      finishReason: stats.commentLikeUncertain ? "uncertain" : item.status,
      displayStats: {status: item.isSessionFinished ? "finished" : "running", action: item.phase,
        actionLevel: stats.commentLikeUncertain ? "warning" : "",
        keyword: item.target?.authorName ? "评论作者：" + item.target.authorName : "评论点赞",
        commentLikeStats: {processed: stats.commentLikeProcessed || 0, planned: stats.commentLikePlanned || 0,
          success: stats.commentLikeSuccess || 0, already: stats.commentLikeAlready || 0,
          uncertain: stats.commentLikeUncertain || 0, failed: stats.commentLikeFailed || 0}}});
  }
  return [...cards.values()];
}

export function liveStatusText(card) {
  if (card.stats?.commentLikeUncertain) return "结果待核验";
  if (card.status === "completed" && card.stats?.commentLikeNotFound) return "未找到评论";
  if (card.status === "completed" && card.stats?.commentLikeSkipped) return "未执行";
  return {running: "执行中", stopping: "停止中", stopped: "已停止", completed: "已完成",
    failed: "执行失败", interrupted: "运行中断"}[card.status] || "准备执行";
}
