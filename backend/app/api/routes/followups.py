"""今日跟进 · API 路由

严格对照 api-contract.md 第三节 3.2。
三段式：overdue（逾期红色）/ today（今天待跟进）/ upcoming（明天及以后）。
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_followup_service
from app.schemas.followup import FollowUpCreate, FollowUpRead
from app.services.followup_service import FollowUpService

router = APIRouter(prefix="/followups", tags=["followups"])


@router.get("", response_model=list[FollowUpRead])
def list_followups(
    date: str | None = None,
    service: FollowUpService = Depends(get_followup_service),
) -> list[FollowUpRead]:
    """跟进待办列表

    date: today（今天待跟进）/ overdue（逾期未完成，红色标记）/ upcoming（明天及以后）
    """
    return service.list_followups(date=date)


@router.post("", response_model=FollowUpRead)
def create_followup(
    payload: FollowUpCreate,
    service: FollowUpService = Depends(get_followup_service),
) -> FollowUpRead:
    """新建跟进待办"""
    return service.create_followup(payload)


@router.post("/{followup_id}/complete")
def complete_followup(
    followup_id: str,
    payload: dict | None = None,
    service: FollowUpService = Depends(get_followup_service),
) -> dict:
    """标记完成（记录完成时间和备注）"""
    note = (payload or {}).get("note")
    try:
        service.complete_followup(followup_id, note=note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Followup not found") from exc
    return {"ok": True}
