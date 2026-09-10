"""私信队列路由 · GET /api/dm/queue"""
from fastapi import APIRouter, Depends

from app.core.dependencies import get_dm_service
from app.schemas.dm import DmQueueItem
from app.services.dm_service import DmService

router = APIRouter(prefix="/dm", tags=["dm"])


@router.get("/queue", response_model=list[DmQueueItem], response_model_by_alias=True)
def list_queue(
    status: str | None = None,
    service: DmService = Depends(get_dm_service),
) -> list[DmQueueItem]:
    """私信发送队列（执行器视角）。

    状态值：pending_outreach / throttled / send_failed / sent / replied
    """
    return service.list_queue(status=status)
