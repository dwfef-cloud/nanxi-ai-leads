"""客户与商机 · Schema 定义

严格对照 api-contract.md 第三节。
响应字段使用 camelCase（与契约示例一致），内部领域模型用 snake_case。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CustomerCreate(BaseModel):
    """新建客户 · 可关联 lead_id 实现线索→客户转化"""
    name: str
    lead_id: str | None = None
    source: Literal[
        "own_comment", "competitor", "inbound_dm",
        "fan_dm", "referral", "lead_card",
    ] = "own_comment"
    est_value: float = 0
    wechat_added_at: datetime | None = None
    manual: bool = False
    referrer: str | None = None
    hue: int = 200


class CustomerStageUpdate(BaseModel):
    """推进商机阶段 · 一键点选"""
    stage: Literal[
        "added", "measured", "proposal", "quoted",
        "negotiating", "won", "lost",
    ]


class DealInput(BaseModel):
    """成交录入 · ≤3次点击，只收金额"""
    amount: float


class LostInput(BaseModel):
    """标记流失 · 传入原因"""
    reason: str


class CustomerLogRead(BaseModel):
    """客户跟进日志 · timeline 展示"""
    model_config = ConfigDict(from_attributes=True)

    time: str
    text: str
    by: str = "系统"


class CustomerRead(BaseModel):
    """客户详情 · 完整字段（camelCase 输出）"""
    model_config = ConfigDict(from_attributes=True)

    id: str
    leadId: str | None = Field(validation_alias="lead_id", default=None)
    name: str
    source: str
    stage: str
    estValue: float = Field(validation_alias="est_value")
    dealAmount: float | None = Field(validation_alias="deal_amount", default=None)
    dealAt: datetime | None = Field(validation_alias="deal_at", default=None)
    wechatAddedAt: datetime | None = Field(validation_alias="wechat_added_at", default=None)
    manual: bool
    referrer: str | None
    nextAction: str | None = Field(validation_alias="next_action", default=None)
    nextAt: str | None = Field(validation_alias="next_at", default=None)
    hue: int
