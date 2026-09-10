from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_lead_service, get_repository
from app.models.domain import LeadBehaviorEvent, now_utc
from app.repositories.memory import MemoryRepository
from app.schemas.outreach import OutreachExecuteRequest, OutreachPlanRead
from app.services.lead_service import LeadService

router = APIRouter(prefix="/leads", tags=["outreach"])


@router.get("/{lead_id}/outreach-plan", response_model=OutreachPlanRead)
def outreach_plan(lead_id: str, service: LeadService = Depends(get_lead_service)) -> OutreachPlanRead:
    try:
        lead = service.get_lead(lead_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Lead not found") from exc
    return OutreachPlanRead(
        lead_id=lead.id,
        platform=lead.platform,
        stage=lead.status,
        requires_confirmation=True,
        steps=[
            {"id": "comment_reply", "title": "公开评论回复", "status": "ready", "meaning": "在原评论下公开回应，确认需求并邀请继续沟通。", "available": lead.platform == "douyin"},
            {"id": "private_message", "title": "私信承接", "status": "ready", "meaning": "用户产生互动后，再发送针对需求的私信，不做无差别群发。", "available": lead.platform == "douyin"},
            {"id": "wechat_guide", "title": "引导进入私域", "status": "waiting", "meaning": "客户明确表达兴趣或需要详细方案后，引导添加微信并记录结果。", "available": True},
        ],
    )


@router.post("/{lead_id}/outreach-execute")
def execute_outreach(
    lead_id: str, payload: OutreachExecuteRequest,
    service: LeadService = Depends(get_lead_service),
    repo: MemoryRepository = Depends(get_repository),
) -> dict:
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="平台互动必须经过人工确认")
    try:
        lead = service.get_lead(lead_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Lead not found") from exc
    if payload.step not in {"comment_reply", "private_message", "wechat_guide"}:
        raise HTTPException(status_code=400, detail="不支持的获客动作")
    event_type = {"comment_reply": "comment_reply", "private_message": "private_message", "wechat_guide": "wechat_guide"}[payload.step]
    repo.save_behavior_event(LeadBehaviorEvent(lead_id=lead.id, event_type=event_type, content=payload.content))
    if payload.step == "comment_reply":
        lead.status = "contacted"
    elif payload.step == "private_message":
        lead.status = "replied"
    else:
        lead.status = "follow_up"
    lead.updated_at = now_utc()
    repo.save_lead(lead)
    return {"lead_id": lead.id, "step": payload.step, "status": "recorded", "message": "已记录人工确认动作；当前 MediaCrawler 仅负责采集，平台发送需接入对应账号执行器。"}
