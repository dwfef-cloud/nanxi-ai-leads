"""客户与商机 · API 路由

严格对照 api-contract.md 第三节 3.1。
响应字段使用 camelCase，与契约示例和前端 mock-data.js 对齐。
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_customer_service
from app.schemas.customer import (
    CustomerCreate,
    CustomerStageUpdate,
    DealInput,
    LostInput,
)
from app.services.customer_service import CustomerService

router = APIRouter(prefix="/customers", tags=["customers"])


def _customer_to_dict(customer, logs: list) -> dict:
    """将领域模型 + 日志转为契约格式的 dict（camelCase）"""
    return {
        "id": customer.id,
        "leadId": customer.lead_id,
        "name": customer.name,
        "source": customer.source,
        "stage": customer.stage,
        "estValue": customer.est_value,
        "dealAmount": customer.deal_amount,
        "dealAt": customer.deal_at,
        "wechatAddedAt": customer.wechat_added_at,
        "manual": customer.manual,
        "referrer": customer.referrer,
        "nextAction": customer.next_action,
        "nextAt": customer.next_at,
        "hue": customer.hue,
        "logs": [{"time": l.time, "text": l.text, "by": l.by} for l in logs],
    }


@router.get("")
def list_customers(
    stage: str | None = None,
    keyword: str | None = None,
    service: CustomerService = Depends(get_customer_service),
) -> list[dict]:
    """客户列表（含商机阶段），支持 stage 过滤和 keyword 搜索"""
    customers = service.list_customers(stage=stage, keyword=keyword)
    result = []
    for c in customers:
        logs = service.list_logs(c.id)
        result.append(_customer_to_dict(c, logs))
    return result


@router.get("/{customer_id}")
def get_customer(
    customer_id: str,
    service: CustomerService = Depends(get_customer_service),
) -> dict:
    """单个客户详情（含商机阶段和跟进日志）"""
    try:
        customer = service.get_customer(customer_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Customer not found") from exc
    logs = service.list_logs(customer_id)
    return _customer_to_dict(customer, logs)


@router.get("/stage-meta")
def get_stage_meta() -> dict:
    """商机阶段元数据（7个阶段的标签、颜色、顺序）"""
    return {
        "added": {"label": "已加微", "cls": "wechat", "order": 1},
        "measured": {"label": "已量房", "cls": "sent", "order": 2},
        "proposal": {"label": "方案中", "cls": "pending", "order": 3},
        "quoted": {"label": "已报价", "cls": "throttled", "order": 4},
        "negotiating": {"label": "谈判中", "cls": "mid", "order": 5},
        "won": {"label": "已成交", "cls": "deal", "order": 6},
        "lost": {"label": "已流失", "cls": "rejected", "order": 7},
    }


@router.post("")
def create_customer(
    payload: CustomerCreate,
    service: CustomerService = Depends(get_customer_service),
) -> dict:
    """手动新建客户（可关联 lead_id 实现线索→客户转化）"""
    customer = service.create_customer(payload)
    logs = service.list_logs(customer.id)
    return _customer_to_dict(customer, logs)


@router.post("/{customer_id}/stage")
def advance_stage(
    customer_id: str,
    payload: CustomerStageUpdate,
    service: CustomerService = Depends(get_customer_service),
) -> dict:
    """推进商机阶段（一键点选，记录变更日志）"""
    try:
        service.advance_stage(customer_id, payload.stage)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Customer not found") from exc
    return {"ok": True}


@router.post("/{customer_id}/deal")
def record_deal(
    customer_id: str,
    payload: DealInput,
    service: CustomerService = Depends(get_customer_service),
) -> dict:
    """成交录入（≤3次点击：只收金额）"""
    try:
        customer = service.record_deal(customer_id, payload.amount)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Customer not found") from exc
    return {"ok": True, "customerId": customer.id, "amount": payload.amount}


@router.post("/{customer_id}/lost")
def mark_lost(
    customer_id: str,
    payload: LostInput,
    service: CustomerService = Depends(get_customer_service),
) -> dict:
    """标记流失（传入流失原因）"""
    try:
        service.mark_lost(customer_id, payload.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Customer not found") from exc
    return {"ok": True}


@router.get("/{customer_id}/logs")
def get_customer_logs(
    customer_id: str,
    service: CustomerService = Depends(get_customer_service),
) -> list[dict]:
    """客户跟进日志（timeline）"""
    try:
        service.get_customer(customer_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Customer not found") from exc
    logs = service.list_logs(customer_id)
    return [{"time": l.time, "text": l.text, "by": l.by} for l in logs]
