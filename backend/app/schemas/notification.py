"""通知提醒 · Schema 定义（P4-A）

响应字段使用 camelCase（relatedType / relatedId / createdAt），与前端对齐。
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NotificationOut(BaseModel):
    """单条通知 · camelCase 输出"""
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: str
    title: str
    content: str
    level: str
    read: bool
    relatedType: str | None = Field(default=None, validation_alias="related_type")
    relatedId: str | None = Field(default=None, validation_alias="related_id")
    createdAt: datetime = Field(validation_alias="created_at")


class NotificationListResponse(BaseModel):
    """通知列表响应：items + 当前未读数（一次拿到，铃铛/列表共用）"""
    items: list[NotificationOut]
    unreadCount: int = 0


class UnreadCountResponse(BaseModel):
    """未读计数响应"""
    count: int


class TestNotificationRequest(BaseModel):
    """发送测试通知（验证用）· 可选标题/内容，缺省用默认"""
    title: str = "测试通知"
    content: str = "这是一条来自通知中心的测试消息，用于验证链路是否通畅。"
    level: str = "info"
