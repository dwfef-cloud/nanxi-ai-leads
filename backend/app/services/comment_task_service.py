"""评论回复任务 Service · 评论引流策略核心

追踪每一条高意向评论的回复状态，以及回复后对方是否回访/主动私信。
状态机：pending → replied → user_replied / user_dm / ignored
"""
from datetime import datetime, timezone

from app.models.domain import CommentReplyTask
from app.repositories.base import Repository
from app.schemas.comment_task import CommentTaskCreate, CommentTaskRead, CommentTaskUpdate


class CommentTaskService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def list_tasks(self, status: str | None = None) -> list[CommentTaskRead]:
        tasks = self._repo.list_comment_tasks(status=status)
        return [self._to_read(t) for t in tasks]

    def get_task(self, task_id: str) -> CommentTaskRead:
        task = self._repo.get_comment_task(task_id)
        return self._to_read(task)

    def create_task(self, payload: CommentTaskCreate) -> CommentTaskRead:
        """创建评论回复任务（从高意向线索生成）"""
        task = CommentReplyTask(
            lead_id=payload.lead_id,
            comment_content=payload.comment_content,
            video_title=payload.video_title,
            reply_content=payload.reply_content,
            account=payload.account,
            reply_script_id=payload.reply_script_id,
            status="pending",
        )
        saved = self._repo.save_comment_task(task)
        return self._to_read(saved)

    def update_task(self, task_id: str, payload: CommentTaskUpdate) -> CommentTaskRead:
        """更新任务状态（标记已回复/对方私信/对方追评）。

        状态联动：
        - status=replied → 自动设置 replied_at
        - status=user_dm → 自动设置 user_dm=True（转化成功 ★）
        - status=user_replied → 自动设置 user_replied_comment=True
        """
        task = self._repo.get_comment_task(task_id)

        if payload.status is not None:
            task.status = payload.status  # type: ignore[assignment]
            # 状态联动
            if payload.status == "replied" and task.replied_at is None:
                task.replied_at = datetime.now(timezone.utc)
            if payload.status == "user_dm":
                task.user_dm = True
            if payload.status == "user_replied":
                task.user_replied_comment = True

        if payload.reply_content is not None:
            task.reply_content = payload.reply_content
        if payload.replied_at is not None:
            task.replied_at = payload.replied_at
        if payload.user_visited is not None:
            task.user_visited = payload.user_visited
        if payload.user_dm is not None:
            task.user_dm = payload.user_dm
        if payload.user_replied_comment is not None:
            task.user_replied_comment = payload.user_replied_comment

        task.updated_at = datetime.now(timezone.utc)
        saved = self._repo.save_comment_task(task)
        return self._to_read(saved)

    # ═══════════════════════════════════════════════════════
    # 内部转换
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _to_read(task: CommentReplyTask) -> CommentTaskRead:
        return CommentTaskRead(
            id=task.id,
            lead_id=task.lead_id,
            comment_content=task.comment_content,
            video_title=task.video_title,
            reply_content=task.reply_content,
            status=task.status,
            account=task.account,
            replied_at=task.replied_at,
            user_visited=task.user_visited,
            user_dm=task.user_dm,
            user_replied_comment=task.user_replied_comment,
        )
