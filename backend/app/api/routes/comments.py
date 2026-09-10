"""评论回复任务路由 · 评论引流策略核心

GET/POST/PATCH /api/comments/tasks
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_comment_task_service
from app.schemas.comment_task import CommentTaskCreate, CommentTaskRead, CommentTaskUpdate
from app.services.comment_task_service import CommentTaskService

router = APIRouter(prefix="/comments", tags=["comments"])


@router.get("/tasks", response_model=list[CommentTaskRead], response_model_by_alias=True)
def list_tasks(
    status: str | None = None,
    service: CommentTaskService = Depends(get_comment_task_service),
) -> list[CommentTaskRead]:
    """评论回复任务列表。

    状态值：pending / replied / user_replied / user_dm / ignored
    """
    return service.list_tasks(status=status)


@router.post("/tasks", response_model=CommentTaskRead, response_model_by_alias=True)
def create_task(
    payload: CommentTaskCreate,
    service: CommentTaskService = Depends(get_comment_task_service),
) -> CommentTaskRead:
    """创建评论回复任务（从高意向线索生成）"""
    return service.create_task(payload)


@router.patch("/tasks/{task_id}", response_model=CommentTaskRead, response_model_by_alias=True)
def update_task(
    task_id: str,
    payload: CommentTaskUpdate,
    service: CommentTaskService = Depends(get_comment_task_service),
) -> CommentTaskRead:
    """更新任务状态（标记已回复/对方私信/对方追评）"""
    try:
        return service.update_task(task_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Comment task not found") from exc
