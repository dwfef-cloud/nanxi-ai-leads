from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class LeadCreate(BaseModel):
    """新建线索 · 与 v1.0 领域模型对齐"""
    nickname: str
    # ── 来源与内容 ──
    source: Literal["own_comment", "competitor", "inbound_dm", "fan_dm", "referral", "lead_card"] = "own_comment"
    comment: str = ""
    video: str = ""
    source_url: str = ""
    source_keyword: str = ""
    referral_note: str | None = None
    # ── 平台与标识 ──
    platform: str = "douyin"
    external_id: str = ""
    # ── 意向 ──
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    customer_need: str = ""
    # ── 展示 ──
    region: str = ""
    hue: int = 200
    account: str | None = None


class LeadUpdate(BaseModel):
    """更新线索 · 所有字段可选"""
    # ── 基础 ──
    nickname: str | None = None
    region: str | None = None
    note: str | None = None
    customer_need: str | None = None
    tags: list[str] | None = None
    # ── 意向与评分 ──
    score: int | None = None
    intent_level: Literal["A", "B", "C", "D"] | None = None
    intent: Literal["high", "mid", "low"] | None = None
    # ── 状态机（9 种） ──
    status: Literal[
        "collected", "pending_outreach", "throttled", "sent", "replied",
        "wechat_added", "deal_won", "send_failed", "rejected",
    ] | None = None
    account: str | None = None
    throttled_note: str | None = None
    fail_note: str | None = None
    reject_reason: str | None = None
    # ── 转化里程碑 ──
    wechat_added_at: datetime | None = None
    manual: bool | None = None
    deal_amount: float | None = None
    deal_at: datetime | None = None
    # ── 跟进 ──
    next_followup_at: datetime | None = None
    lost_reason: str | None = None
    conversion_amount: float | None = None  # v0.1 兼容


class LeadRead(BaseModel):
    """线索详情 · 完整字段"""
    id: str
    nickname: str
    # ── 来源与内容 ──
    source: str
    comment: str
    video: str
    source_url: str
    source_keyword: str
    referral_note: str | None
    # ── 平台与标识 ──
    platform: str
    external_id: str
    source_task_id: str
    # ── 意向与评分 ──
    score: int
    intent_level: str
    intent: str
    tags: list[str]
    note: str
    customer_need: str
    # ── 状态机 ──
    status: str
    account: str | None
    throttled_note: str | None
    fail_note: str | None
    reject_reason: str | None
    # ── 转化里程碑 ──
    wechat_added_at: datetime | None
    manual: bool
    deal_amount: float | None
    deal_at: datetime | None
    # ── 展示 ──
    region: str
    hue: int
    # ── 跟进 ──
    next_followup_at: datetime | None
    lost_reason: str
    conversion_amount: float
    # ── 时间戳 ──
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
