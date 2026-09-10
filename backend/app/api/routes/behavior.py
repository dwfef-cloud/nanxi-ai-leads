from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_repository
from app.models.domain import LeadBehaviorEvent, now_utc
from app.repositories.memory import MemoryRepository
from app.schemas.analytics import BehaviorEventCreate

router = APIRouter(prefix="/behavior-events", tags=["analytics"])


@router.get("")
def list_events(repo: MemoryRepository = Depends(get_repository)) -> list[LeadBehaviorEvent]:
    return repo.list_behavior_events()


@router.post("")
def create_event(payload: BehaviorEventCreate, repo: MemoryRepository = Depends(get_repository)) -> LeadBehaviorEvent:
    if not any(lead.id == payload.lead_id for lead in repo.list_leads()):
        raise HTTPException(status_code=404, detail="客户不存在")
    event = LeadBehaviorEvent(lead_id=payload.lead_id, event_type=payload.event_type, content=payload.content, value=payload.value, created_at=payload.created_at or now_utc())
    return repo.save_behavior_event(event)
