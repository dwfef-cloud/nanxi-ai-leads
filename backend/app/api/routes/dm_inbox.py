"""私信收件箱路由 · 评论引流策略转化闭环

5 个端点：
  GET    /api/dm/inbox                      收件箱列表（支持 ?status= 筛选）
  GET    /api/dm/inbox/{conversation_id}    会话详情（标记入站已读）
  POST   /api/dm/inbox/{conversation_id}/reply   发送回复
  POST   /api/dm/inbox/{conversation_id}/mark-wechat  标记加微转客户
  POST   /api/dm/inbox/simulate             模拟接收私信（开发测试用）
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_dm_inbox_service
from app.schemas.dm_inbox import (
    InboxConversationDetail,
    InboxConversationItem,
    InboxMessageItem,
    MarkWechatRequest,
    MarkWechatResponse,
    ReplyRequest,
    SimulateRequest,
    SimulateResponse,
)
from app.services.dm_inbox_service import DmInboxService

router = APIRouter(prefix="/dm/inbox", tags=["dm-inbox"])


@router.get("", response_model=list[InboxConversationItem])
def list_inbox(
    status: str | None = None,
    service: DmInboxService = Depends(get_dm_inbox_service),
) -> list[InboxConversationItem]:
    """收件箱会话列表，按最后消息时间倒序。

    status 筛选：new / replied / wechat_added / closed / all（默认 all）
    """
    return service.list_inbox(status=status)


@router.get("/{conversation_id}", response_model=InboxConversationDetail)
def get_conversation(
    conversation_id: str,
    service: DmInboxService = Depends(get_dm_inbox_service),
) -> InboxConversationDetail:
    """会话详情 + 全部消息（按时间正序），同时标记所有入站消息为已读"""
    try:
        return service.get_conversation_detail(conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc


@router.post("/{conversation_id}/reply", response_model=InboxMessageItem)
def send_reply(
    conversation_id: str,
    payload: ReplyRequest,
    service: DmInboxService = Depends(get_dm_inbox_service),
) -> InboxMessageItem:
    """发送回复（创建一条 outbound 消息，更新会话最后消息时间）"""
    try:
        return service.send_reply(conversation_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc


@router.post("/{conversation_id}/mark-wechat", response_model=MarkWechatResponse)
def mark_wechat(
    conversation_id: str,
    payload: MarkWechatRequest,
    service: DmInboxService = Depends(get_dm_inbox_service),
) -> MarkWechatResponse:
    """标记加微转客户：关闭会话、线索标记加微、自动创建客户"""
    try:
        return service.mark_wechat(conversation_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc


@router.post("/simulate", response_model=SimulateResponse)
def simulate_inbound(
    payload: SimulateRequest,
    service: DmInboxService = Depends(get_dm_inbox_service),
) -> SimulateResponse:
    """模拟接收私信（开发测试用）：创建/获取会话 + 创建入站消息"""
    return service.simulate_inbound(payload)
