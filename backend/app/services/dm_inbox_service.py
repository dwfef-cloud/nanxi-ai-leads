"""私信收件箱 Service · 评论引流策略转化闭环

承接用户主动私信的全流程：
  收件箱列表 → 会话详情（标记已读）→ 发送回复 → 标记加微转客户

依赖 Repository 接口，不感知底层实现。
收件箱状态（new/replied/wechat_added/closed）为派生状态，不存储在 Conversation 上：
  - closed:       Conversation.status == "closed"
  - wechat_added: 关联线索状态为 wechat_added/deal_won，或已有关联客户
  - replied:      会话中存在至少一条 outbound 消息
  - new:          其余（有入站消息但尚未回复）
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.domain import Conversation, Customer, CustomerLog, Lead, Message, now_utc
from app.repositories.base import Repository
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


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M")


class DmInboxService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._notification_service = None  # P4-A 注入

    def set_notification_service(self, svc) -> None:
        """注入通知服务（P4-A），用于新私信自动提醒"""
        self._notification_service = svc

    # ═══════════════════════════════════════════════════════
    # 1. 收件箱列表
    # ═══════════════════════════════════════════════════════

    def list_inbox(self, status: str | None = None) -> list[InboxConversationItem]:
        """收件箱会话列表，按最后消息时间倒序，支持按状态筛选"""
        conversations = self._repo.list_conversations()
        items: list[InboxConversationItem] = []
        for conv in conversations:
            item = self._build_inbox_item(conv)
            items.append(item)

        # 按最后消息时间倒序
        items.sort(key=lambda x: x.last_message_at, reverse=True)

        # 状态筛选
        if status and status != "all":
            items = [i for i in items if i.status == status]

        return items

    # ═══════════════════════════════════════════════════════
    # 2. 会话详情（同时标记入站消息已读）
    # ═══════════════════════════════════════════════════════

    def get_conversation_detail(self, conversation_id: str) -> InboxConversationDetail:
        conv = self._repo.get_conversation(conversation_id)
        messages = self._repo.list_messages(conversation_id)
        # 按时间正序
        messages.sort(key=lambda m: m.created_at)

        # 标记所有入站消息为已读
        for msg in messages:
            if msg.direction == "in" and not msg.is_read:
                msg.is_read = True
                self._repo.save_message(msg)

        lead = self._safe_get_lead(conv.lead_id)
        customer = self._find_customer_by_lead(conv.lead_id)
        nickname = lead.nickname if lead else "未知用户"
        avatar_hue = lead.hue if lead else 200
        status = self._derive_status(conv, messages, lead, customer)

        return InboxConversationDetail(
            id=conv.id,
            lead_id=conv.lead_id,
            customer_id=customer.id if customer else None,
            nickname=nickname,
            avatar_hue=avatar_hue,
            status=status,
            messages=[self._to_message_item(m) for m in messages],
        )

    # ═══════════════════════════════════════════════════════
    # 3. 发送回复
    # ═══════════════════════════════════════════════════════

    def send_reply(self, conversation_id: str, payload: ReplyRequest) -> InboxMessageItem:
        conv = self._repo.get_conversation(conversation_id)
        now = now_utc()

        # 创建出站消息
        message = Message(
            conversation_id=conversation_id,
            sender="human",
            content=payload.content,
            is_ai_generated=False,
            direction="out",
            script_id=payload.script_id,
            is_read=True,  # 出站消息默认已读
            created_at=now,
        )
        message = self._repo.save_message(message)

        # 更新会话最后消息时间
        conv.last_message_at = now
        conv.updated_at = now
        self._repo.save_conversation(conv)

        # 如果关联线索存在且状态为 collected/pending_outreach/sent，自动推进到 replied
        lead = self._safe_get_lead(conv.lead_id)
        if lead and lead.status in ("collected", "pending_outreach", "sent"):
            lead.status = "replied"  # type: ignore[assignment]
            lead.updated_at = now
            self._repo.save_lead(lead)

        return self._to_message_item(message)

    # ═══════════════════════════════════════════════════════
    # 4. 标记加微转客户
    # ═══════════════════════════════════════════════════════

    def mark_wechat(self, conversation_id: str, payload: MarkWechatRequest) -> MarkWechatResponse:
        conv = self._repo.get_conversation(conversation_id)
        now = now_utc()

        # 关闭会话
        conv.status = "closed"  # type: ignore[assignment]
        conv.updated_at = now
        self._repo.save_conversation(conv)

        lead = self._safe_get_lead(conv.lead_id)
        lead_id = lead.id if lead else None

        # 1. 关联线索标记加微
        if lead:
            lead.status = "wechat_added"  # type: ignore[assignment]
            lead.wechat_added_at = now
            lead.manual = True
            lead.updated_at = now
            self._repo.save_lead(lead)

        # 2. 自动创建客户（如果还没有）
        customer = self._find_customer_by_lead(conv.lead_id)
        if customer is None:
            customer_name = payload.customer_name or (lead.nickname if lead else "未命名客户")
            customer = Customer(
                name=customer_name,
                source=lead.source if lead else "inbound_dm",
                stage="added",  # type: ignore[assignment]
                lead_id=lead_id,
                wechat_added_at=now,
                manual=True,
                hue=lead.hue if lead else 200,
            )
            customer = self._repo.save_customer(customer)
            # 记录创建日志
            self._repo.save_customer_log(CustomerLog(
                customer_id=customer.id,
                text=f"客户创建（来自私信收件箱 · 微信号: {payload.wechat_id}）",
                time=_now_str(),
                by="系统",
            ))

        return MarkWechatResponse(
            ok=True,
            conversation_id=conversation_id,
            customer_id=customer.id,
            lead_id=lead_id,
        )

    # ═══════════════════════════════════════════════════════
    # 5. 模拟接收私信（开发测试用）
    # ═══════════════════════════════════════════════════════

    def simulate_inbound(self, payload: SimulateRequest) -> SimulateResponse:
        now = now_utc()

        # 获取或创建线索（lead_id 不存在时自动创建，不报错）
        lead: Lead
        if payload.lead_id:
            try:
                lead = self._repo.get_lead(payload.lead_id)
            except KeyError:
                lead = None
            if lead is None:
                lead = Lead(
                    nickname=payload.nickname,
                    source="inbound_dm",  # type: ignore[assignment]
                    comment=payload.content,
                    platform="douyin",
                    hue=hash(payload.nickname) % 360,
                    status="replied",  # type: ignore[assignment]  # 主动私信直接算已回复
                    score=60,
                    intent_level="B",  # type: ignore[assignment]
                    intent="high",  # type: ignore[assignment]
                )
                lead = self._repo.save_lead(lead)
        else:
            lead = Lead(
                nickname=payload.nickname,
                source="inbound_dm",  # type: ignore[assignment]
                comment=payload.content,
                platform="douyin",
                hue=hash(payload.nickname) % 360,
                status="replied",  # type: ignore[assignment]  # 主动私信直接算已回复
                score=60,
                intent_level="B",  # type: ignore[assignment]
                intent="high",  # type: ignore[assignment]
            )
            lead = self._repo.save_lead(lead)

        # 查找已有会话（按 lead_id），没有则创建
        conv = self._find_conversation_by_lead(lead.id)
        if conv is None:
            conv = Conversation(
                lead_id=lead.id,
                account_name="default",
                status="open",  # type: ignore[assignment]
                last_message_at=now,
            )
            conv = self._repo.save_conversation(conv)

        # 创建入站消息
        message = Message(
            conversation_id=conv.id,
            sender="user",
            content=payload.content,
            is_ai_generated=False,
            direction="in",
            is_read=False,
            created_at=now,
        )
        message = self._repo.save_message(message)

        # 更新会话最后消息时间
        conv.last_message_at = now
        conv.updated_at = now
        self._repo.save_conversation(conv)

        # P4-A：新私信通知
        if self._notification_service:
            self._notification_service.create(
                type="new_dm",
                title=f"新私信 · {lead.nickname}",
                content=(payload.content or "")[:80],
                level="info",
                related_type="lead",
                related_id=lead.id,
            )

        return SimulateResponse(
            ok=True,
            conversation_id=conv.id,
            message_id=message.id,
            lead_id=lead.id,
        )

    # ═══════════════════════════════════════════════════════
    # 内部工具
    # ═══════════════════════════════════════════════════════

    def _build_inbox_item(self, conv: Conversation) -> InboxConversationItem:
        messages = self._repo.list_messages(conv.id)
        messages.sort(key=lambda m: m.created_at)

        lead = self._safe_get_lead(conv.lead_id)
        customer = self._find_customer_by_lead(conv.lead_id)
        nickname = lead.nickname if lead else "未知用户"
        avatar_hue = lead.hue if lead else 200

        # 未读数 = direction=in 且 is_read=false
        unread_count = sum(1 for m in messages if m.direction == "in" and not m.is_read)

        # 最后一条消息
        last_message = messages[-1].content if messages else ""

        status = self._derive_status(conv, messages, lead, customer)

        return InboxConversationItem(
            id=conv.id,
            lead_id=conv.lead_id,
            customer_id=customer.id if customer else None,
            nickname=nickname,
            avatar_hue=avatar_hue,
            last_message=last_message,
            last_message_at=conv.last_message_at,
            unread_count=unread_count,
            status=status,
        )

    @staticmethod
    def _derive_status(
        conv: Conversation,
        messages: list[Message],
        lead: Lead | None,
        customer: Customer | None,
    ) -> str:
        """派生收件箱状态：closed > wechat_added > replied > new"""
        if conv.status == "closed":
            # 已关闭的会话，如果线索已加微则显示 wechat_added，否则 closed
            if lead and lead.status in ("wechat_added", "deal_won"):
                return "wechat_added"
            if customer is not None:
                return "wechat_added"
            return "closed"
        if customer is not None:
            return "wechat_added"
        if lead and lead.status in ("wechat_added", "deal_won"):
            return "wechat_added"
        # 有出站消息 = 已回复
        if any(m.direction == "out" for m in messages):
            return "replied"
        return "new"

    @staticmethod
    def _to_message_item(msg: Message) -> InboxMessageItem:
        return InboxMessageItem(
            id=msg.id,
            direction=msg.direction,
            sender=msg.sender,
            content=msg.content,
            is_read=msg.is_read,
            script_id=msg.script_id,
            created_at=msg.created_at,
        )

    def _safe_get_lead(self, lead_id: str | None) -> Lead | None:
        if not lead_id:
            return None
        try:
            return self._repo.get_lead(lead_id)
        except KeyError:
            return None

    def _find_conversation_by_lead(self, lead_id: str) -> Conversation | None:
        for conv in self._repo.list_conversations():
            if conv.lead_id == lead_id:
                return conv
        return None

    def _find_customer_by_lead(self, lead_id: str | None) -> Customer | None:
        if not lead_id:
            return None
        for c in self._repo.list_customers():
            if c.lead_id == lead_id:
                return c
        return None
