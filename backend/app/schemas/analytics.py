from datetime import datetime
from pydantic import BaseModel


class BehaviorEventCreate(BaseModel):
    lead_id: str
    event_type: str
    content: str = ""
    value: float = 0
    created_at: datetime | None = None


# ═══════════════════════════════════════════════════════════
# 分析域 · 漏斗 / 归因 / 合规仪表盘 / 运行态 / 工作台
# ═══════════════════════════════════════════════════════════

class FunnelStage(BaseModel):
    key: str
    label: str
    value: int


class FunnelCost(BaseModel):
    totalSpend: float
    perLead: float
    perWechatAdd: float
    perDeal: float
    avgDealAmount: float


class WechatTrendPoint(BaseModel):
    date: str
    v: int


class FunnelResponse(BaseModel):
    window: str
    stages: list[FunnelStage]
    cost: FunnelCost
    wechatTrend: list[WechatTrendPoint]


class AttributionSource(BaseModel):
    source: str
    label: str
    leads: int
    wechat: int
    deal: int
    pilot: bool = False


class TopVideo(BaseModel):
    video: str
    leads: int
    wechat: int
    dealAmount: float


class AttributionResponse(BaseModel):
    window: str
    bySource: list[AttributionSource]
    topVideos: list[TopVideo]


class ComplianceSummary(BaseModel):
    unsubscribe7d: int
    blacklistTotal: int
    blacklistDelta7d: int
    complaints7d: int
    auditEvents: int


class ComplianceRuleItem(BaseModel):
    id: str
    desc: str
    status: str
    hitAt: str | None = None
    target: str | None = None


class ComplianceAuditItem(BaseModel):
    time: str
    type: str
    level: str
    text: str


class DashboardComplianceResponse(BaseModel):
    summary: ComplianceSummary
    rules: list[ComplianceRuleItem]
    audit: list[ComplianceAuditItem]


class SafeModeInfo(BaseModel):
    active: bool
    reason: str
    triggeredAt: str | None
    triggerSource: str | None


class RuntimeResponse(BaseModel):
    safeMode: SafeModeInfo
    taskRunning: bool


class WorkbenchDeal(BaseModel):
    count: int
    amount: float


class WorkbenchToday(BaseModel):
    newLeads: int
    pendingSend: int
    sentToday: int
    wechatToday: int
    followupsPending: int
    followupsOverdue: int
    deal: WorkbenchDeal


class WorkbenchAlert(BaseModel):
    level: str
    text: str
    action: str = ""
    href: str = ""


class WorkbenchActivity(BaseModel):
    time: str
    type: str
    level: str
    text: str


class WorkbenchResponse(BaseModel):
    date: str
    today: WorkbenchToday
    alerts: list[WorkbenchAlert]
    activity: list[WorkbenchActivity]
