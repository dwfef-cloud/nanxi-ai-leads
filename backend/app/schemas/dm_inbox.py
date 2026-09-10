"""私信收件箱 Schema · 评论引流策略转化闭环

收件箱用于承接用户主动私信，支持查看会话、回复、标记加微转客户。
状态值：new（新会话未回复）/ replied（已回复）/ wechat_added（已加微）/ closed（已关闭）
"""
from datetime import datetime

from pydantic import BaseModel


# ═══════════════════════════════════════════════════════════
# 请求体
# ═══════════════════════════════════════════════════════════

class ReplyRequest(BaseModel):
    """发送回复请求体"""
    content: str
    script_id: str | None = None


class MarkWechatRequest(BaseModel):
    """标记加微转客户请求体"""
    wechat_id: str
    customer_name: str | None = None
    phone: str | None = None


class SimulateRequest(BaseModel):
    """模拟接收私信（开发测试用）"""
    nickname: str
    content: str
    lead_id: str | None = None


# ═══════════════════════════════════════════════════════════
# 响应体（snake_case，前端 _normalize 自动转 camelCase）
# ═══════════════════════════════════════════════════════════

class InboxMessageItem(BaseModel):
    """收件箱消息项"""
    id: str
    direction: str          # in=用户发 / out=我们发
    sender: str             # ai / human / user / system
    content: str
    is_read: bool = False
    script_id: str | None = None
    created_at: datetime


class InboxConversationItem(BaseModel):
    """收件箱会话列表项"""
    id: str
    lead_id: str | None = None
    customer_id: str | None = None
    nickname: str
    avatar_hue: int = 200
    last_message: str = ""
    last_message_at: datetime
    unread_count: int = 0
    status: str             # new / replied / wechat_added / closed


class InboxConversationDetail(BaseModel):
    """收件箱会话详情"""
    id: str
    lead_id: str | None = None
    customer_id: str | None = None
    nickname: str
    avatar_hue: int = 200
    status: str
    messages: list[InboxMessageItem]


class MarkWechatResponse(BaseModel):
    """标记加微响应"""
    ok: bool
    conversation_id: str
    customer_id: str
    lead_id: str | None = None


class SimulateResponse(BaseModel):
    """模拟接收私信响应"""
    ok: bool
    conversation_id: str
    message_id: str
    lead_id: str
