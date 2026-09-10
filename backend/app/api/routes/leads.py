from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_lead_service
from app.schemas.lead import LeadCreate, LeadRead, LeadUpdate
from app.services.lead_service import LeadService

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("", response_model=list[LeadRead])
def list_leads(
    status: str | None = None,
    source: str | None = None,
    min_score: int | None = None,
    keyword: str | None = None,
    service: LeadService = Depends(get_lead_service),
) -> list[LeadRead]:
    if status is None and source is None and min_score is None and keyword is None:
        return service.list_leads()
    return service.filter_leads(status=status, source=source, min_score=min_score, keyword=keyword)


@router.get("/status-meta")
def get_status_meta() -> dict:
    return {
        "collected": {"label": "已采集", "cls": "collected"},
        "pending_outreach": {"label": "待发送", "cls": "pending"},
        "throttled": {"label": "频控暂缓", "cls": "throttled"},
        "sent": {"label": "已私信", "cls": "sent"},
        "replied": {"label": "已回复", "cls": "replied"},
        "wechat_added": {"label": "已加微 ★", "cls": "wechat"},
        "deal_won": {"label": "已成交", "cls": "deal"},
        "send_failed": {"label": "发送失败", "cls": "failed"},
        "rejected": {"label": "已过滤", "cls": "rejected"},
    }


@router.get("/source-meta")
def get_source_meta() -> dict:
    return {
        "own_comment": {"label": "自有视频评论区", "cls": "sent"},
        "competitor": {"label": "对标账号监控", "cls": "throttled"},
        "inbound_dm": {"label": "用户主动私信", "cls": "replied"},
        "fan_dm": {"label": "粉丝私信", "cls": "pending"},
        "referral": {"label": "老客转介绍", "cls": "wechat"},
        "lead_card": {"label": "留资卡", "cls": "mid"},
    }


@router.get("/{lead_id}", response_model=LeadRead)
def get_lead(lead_id: str, service: LeadService = Depends(get_lead_service)) -> LeadRead:
    try:
        return service.get_lead(lead_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Lead not found") from exc


@router.post("", response_model=LeadRead)
def create_lead(payload: LeadCreate, service: LeadService = Depends(get_lead_service)) -> LeadRead:
    return service.create_lead(payload)


@router.post("/import", response_model=list[LeadRead])
def import_leads(payload: list[LeadCreate], service: LeadService = Depends(get_lead_service)) -> list[LeadRead]:
    return service.import_external_leads(payload)


@router.post("/import/raw", response_model=list[LeadRead])
def import_raw_leads(payload: list[dict], service: LeadService = Depends(get_lead_service)) -> list[LeadRead]:
    return service.import_media_crawler_payloads(payload)


@router.post("/import/text", response_model=list[LeadRead])
def import_text_leads(payload: dict, service: LeadService = Depends(get_lead_service)) -> list[LeadRead]:
    return service.import_media_crawler_text(payload.get("content", ""))


@router.post("/{lead_id}/wechat_added")
def mark_wechat_added(
    lead_id: str,
    payload: dict | None = None,
    service: LeadService = Depends(get_lead_service),
) -> dict:
    manual = (payload or {}).get("manual", True)
    try:
        lead = service.mark_wechat_added(lead_id, manual=manual)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Lead not found") from exc
    return {"ok": True, "lead_id": lead.id, "manual": manual}


@router.patch("/{lead_id}", response_model=LeadRead)
def update_lead(lead_id: str, payload: LeadUpdate, service: LeadService = Depends(get_lead_service)) -> LeadRead:
    try:
        return service.update_lead(lead_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Lead not found") from exc
