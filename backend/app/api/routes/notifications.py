"""通知提醒 · API 路由（P4-A）

5 个端点：
  GET  /api/notifications?unread_only=&limit=   通知列表（附带未读数）
  GET  /api/notifications/unread-count           未读数 {count: N}
  POST /api/notifications/{id}/read              标记单条已读
  POST /api/notifications/read-all               全部已读
  POST /api/notifications/test                   发送测试通知（验证链路）
"""
from fastapi import APIRouter, Depends

from app.core.dependencies import get_notification_service
from app.schemas.notification import (
    NotificationListResponse,
    NotificationOut,
    TestNotificationRequest,
    UnreadCountResponse,
)
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    service: NotificationService = Depends(get_notification_service),
) -> NotificationListResponse:
    """通知列表（进入时顺带巡检跟进到期/逾期）"""
    service.check_followup_due()
    items = service.list(unread_only=unread_only, limit=limit)
    unread = service.unread_count()
    return NotificationListResponse(
        items=[NotificationOut.model_validate(n) for n in items],
        unreadCount=unread,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
def unread_count(
    service: NotificationService = Depends(get_notification_service),
) -> UnreadCountResponse:
    """未读通知数（铃铛角标）"""
    service.check_followup_due()
    return UnreadCountResponse(count=service.unread_count())


@router.post("/read-all")
def read_all(
    service: NotificationService = Depends(get_notification_service),
) -> dict:
    """全部已读"""
    service.mark_all_read()
    return {"ok": True}


@router.post("/test")
def send_test(
    payload: TestNotificationRequest,
    service: NotificationService = Depends(get_notification_service),
) -> dict:
    """发送测试通知（验证链路）"""
    n = service.create(
        type="system",
        title=payload.title,
        content=payload.content,
        level=payload.level,
    )
    return {"ok": True, "id": n.id}


@router.post("/{notification_id}/read")
def read_one(
    notification_id: str,
    service: NotificationService = Depends(get_notification_service),
) -> dict:
    """标记单条已读"""
    service.mark_read(notification_id)
    return {"ok": True}
