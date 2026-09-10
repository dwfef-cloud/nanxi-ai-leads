"""
获客系统 v1.0 · 领域模型定义
================================
数据底座 · 所有子智能体共享的统一模型。

设计原则：
1. 字段与前端 mock-data.js 一一对齐，前后端对接零摩擦
2. 保留 v0.1 旧字段以兼容现有代码，新增字段有默认值
3. 枚举用 Literal 类型约束，状态机清晰可追溯
4. 时间统一用 datetime（UTC），序列化层负责格式化

模型分层：
  获客域  Lead / Account / Script / ScriptVariant / CommentReplyTask
  客户域  Customer / CustomerLog / FollowUp
  消息域  Conversation / Message
  分析域  ComplianceEvent / ComplianceRule / RuntimeState
  配置域  BusinessProfile / ProductKnowledge / AudienceProfile / ScriptStrategy / WeChatSettings
  任务域  Task / LeadBehaviorEvent
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4


# ═══════════════════════════════════════════════════════════
# 枚举定义
# ═══════════════════════════════════════════════════════════

# 线索来源（6 种触点，IMP-020）
LeadSource = Literal[
    "own_comment",   # 自有视频评论区
    "competitor",    # 对标账号监控
    "inbound_dm",    # 用户主动私信
    "fan_dm",        # 粉丝私信
    "referral",      # 老客转介绍
    "lead_card",     # 留资卡
]

# 线索状态（9 种状态机，前端 LEAD_STATUS）
LeadStatus = Literal[
    "collected",        # 已采集
    "pending_outreach", # 待发送
    "throttled",        # 频控暂缓
    "sent",             # 已私信
    "replied",          # 已回复
    "wechat_added",     # 已加微 ★
    "deal_won",         # 已成交
    "send_failed",      # 发送失败
    "rejected",         # 已过滤
]

# v0.1 旧状态（保留兼容，新代码请用 LeadStatus）
LegacyLeadStatus = Literal["new", "qualified", "contacted", "replied", "follow_up", "won", "lost"]

# 意向等级（两套并存：A/B/C/D 用于评分，high/mid/low 用于前端展示）
IntentLevel = Literal["A", "B", "C", "D"]
IntentTag = Literal["high", "mid", "low"]

# 商机阶段（7 阶段，前端 STAGE_META）
CustomerStage = Literal[
    "added",       # 已加微
    "measured",    # 已量房
    "proposal",    # 方案中
    "quoted",      # 已报价
    "negotiating", # 谈判中
    "won",         # 已成交
    "lost",        # 已流失
]

# 账号限流状态
AccountLimitStatus = Literal[
    "healthy",     # 健康
    "warn",        # 预警
    "throttled40", # R1 降速至 40
    "safe_mode",   # 安全模式（全量暂停）
    "banned",      # 已封禁
]

# v0.1 旧账号状态（保留兼容）
AccountStatus = Literal["active", "idle", "needs_login", "paused", "risk"]

# 健康因子等级
HealthFactor = Literal["good", "warn", "bad"]

# 话术分类（评论引流策略下分四类）
ScriptCategory = Literal[
    "comment",        # 评论区回复话术
    "private_message",# 私信话术
    "wechat_guide",   # 微信引导话术
    "objection",      # 异议处理话术
    "nurture",        # 加微后培育 SOP
]

# 话术变体状态
VariantStatus = Literal["active", "switched_off", "draft"]

# 评论回复任务状态（评论引流策略核心）
CommentReplyStatus = Literal[
    "pending",      # 待回复
    "replied",      # 已回复，等待对方反应
    "user_replied", # 对方追评/回复评论
    "user_dm",      # 对方主动私信（转化成功）
    "ignored",      # 对方无反应
]

# 任务状态
TaskStatus = Literal["pending", "running", "done", "failed", "paused"]

# 对话状态
ConversationStatus = Literal["open", "waiting", "closed"]

# 消息发送方
MessageSender = Literal["ai", "human", "user", "system"]

# 私信方向（前端 messages 用 dir）
MessageDirection = Literal["out", "in"]

# 合规事件类型
ComplianceEventType = Literal[
    "safe_mode",    # 安全模式触发/退出
    "r1_hit",       # R1 命中
    "r2_switch",    # R2 话术切换
    "unsubscribe",  # 用户退订
    "dm_reply",     # 私信回复
    "ban",          # 账号封禁/限流
    "wechat_added", # 加微成功
    "pause",        # 手动暂停
    "resume",       # 手动恢复
]

# 合规事件等级
ComplianceLevel = Literal["danger", "warn", "info", "ok"]

# 合规规则状态
RuleStatus = Literal["hit", "standby"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


# ═══════════════════════════════════════════════════════════
# 获客域
# ═══════════════════════════════════════════════════════════

@dataclass(slots=True)
class Lead:
    """线索 · 获客域核心实体

    与前端 leads 数组对齐。v0.1 旧字段全部保留，新增字段有默认值。
    """
    # ── 基础标识 ──
    nickname: str
    source_url: str = ""
    source_keyword: str = ""
    platform: str = "douyin"
    external_id: str = ""
    source_task_id: str = ""
    id: str = field(default_factory=_new_id)

    # ── 来源与内容（IMP-020 触点归因） ──
    source: LeadSource = "own_comment"       # 6 种触点来源
    comment: str = ""                         # 评论原文（前端 comment 字段）
    video: str = ""                           # 来源视频标题（前端 video 字段）
    referral_note: str | None = None          # 转介绍备注

    # ── 意向与评分 ──
    score: int = 0
    intent_level: IntentLevel = "C"           # A/B/C/D 评分等级
    intent: IntentTag = "mid"                 # high/mid/low 前端展示标签
    tags: list[str] = field(default_factory=list)
    note: str = ""
    customer_need: str = ""

    # ── 状态机（9 种） ──
    status: LeadStatus = "collected"
    account: str | None = None                # 负责账号昵称
    throttled_note: str | None = None         # 频控暂缓说明
    fail_note: str | None = None              # 发送失败说明
    reject_reason: str | None = None          # 过滤原因

    # ── 转化里程碑 ──
    wechat_added_at: datetime | None = None   # 加微时间
    manual: bool = False                      # 是否手动标记加微
    deal_amount: float | None = None          # 成交金额
    deal_at: datetime | None = None           # 成交时间

    # ── 展示 ──
    hue: int = 200                            # 头像色相（0-360）
    region: str = ""

    # ── 跟进 ──
    next_followup_at: datetime | None = None
    lost_reason: str = ""                     # v0.1 兼容字段
    conversion_amount: float = 0              # v0.1 兼容字段（= deal_amount）

    # ── 时间戳 ──
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class Account:
    """抖音账号 · 含健康分与限流状态机

    与前端 accounts 数组对齐。
    """
    # ── 基础 ──
    name: str = ""                            # v0.1 兼容字段
    nickname: str = ""                        # 前端展示名
    platform: str = "douyin"
    avatar_hue: int = 150                     # 头像色相
    id: str = field(default_factory=_new_id)

    # ── 健康分（IMP-006 五维因子） ──
    health_score: int = 80                    # 0-100，≥60绿 / <60黄 / <40红
    factors: dict[str, HealthFactor] = field(default_factory=lambda: {
        "dailyFreq": "good",
        "banHistory": "good",
        "login": "good",
        "unsubscribe": "good",
        "complaint": "good",
    })

    # ── 限流状态机 ──
    limit_status: AccountLimitStatus = "healthy"
    status: AccountStatus = "idle"            # v0.1 兼容字段
    daily_outreach: int = 0                   # 今日已发送
    daily_limit: int = 80                     # 日频上限（R1 命中后降为 40）
    daily_send_count: int = 0                 # v0.1 兼容字段
    risk_level: int = 0                       # v0.1 兼容字段
    weight: int = 1                           # 调度权重（weighted 策略用）
    today_sent: int = 0                       # 今日调度发送数
    today_success: int = 0                    # 今日发送成功数

    # ── 风控备注 ──
    last_ban_reason: str | None = None
    r1_note: str | None = None                # R1 命中说明
    r3_note: str | None = None                # R3 安全模式说明

    notes: str = ""
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class Script:
    """话术 · 含多个 AB 变体

    与前端 scripts 数组对齐。按 category 区分评论/私信/微信引导。
    """
    name: str
    industry: str = "通用"
    category: ScriptCategory = "private_message"
    is_main: bool = False
    active: bool = True
    intro: str = ""
    welcome_msg: str = ""                     # 加微后欢迎语
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class ScriptVariant:
    """话术变体 · AB 测试与 R2 自动切换

    与前端 scripts[].variants 对齐。
    """
    script_id: str
    variant_id: str                           # A / B / C
    text: str
    weight: int = 50                          # 流量权重 0-100
    status: VariantStatus = "active"
    # ── 效果统计 ──
    sent: int = 0
    replied: int = 0
    wechat_added: int | None = None
    conv_rate: float | None = None            # 转化率 %
    sample_enough: bool = False               # 样本量是否 ≥30
    r2_note: str | None = None                # R2 命中说明
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class ScriptTemplate:
    """行业话术模板包（内置模板，非用户创建）"""
    industry: str
    count: int = 0
    desc: str = ""
    installed: bool = False


@dataclass(slots=True)
class CommentReplyTask:
    """评论回复任务 · 评论引流策略核心实体

    追踪每一条高意向评论的回复状态，以及回复后对方是否回访/主动私信。
    这是「评论区回复引导主动私信」策略的数据底座。
    """
    lead_id: str
    comment_content: str = ""                 # 对方评论原文
    video_title: str = ""                     # 所在视频
    reply_script_id: str | None = None        # 选用的话术 ID
    reply_content: str = ""                   # 实际回复内容
    status: CommentReplyStatus = "pending"
    account: str | None = None                # 用哪个账号回复
    scheduled_at: datetime | None = None
    replied_at: datetime | None = None
    # ── 回复后行为追踪 ──
    user_visited: bool = False                # 对方是否回访主页
    user_dm: bool = False                     # 对方是否主动私信（转化成功）
    user_replied_comment: bool = False        # 对方是否追评
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


# ═══════════════════════════════════════════════════════════
# 客户域
# ═══════════════════════════════════════════════════════════

@dataclass(slots=True)
class Customer:
    """客户与商机 · 已加微的线索进入客户域

    与前端 customers 数组对齐。stage 管成交流程，与 lead.status 正交。
    """
    name: str
    source: LeadSource = "own_comment"
    stage: CustomerStage = "added"
    lead_id: str | None = None                # 关联的线索 ID（可为空：手动录入/转介绍）
    # ── 价值 ──
    est_value: float = 0                      # 预估金额
    deal_amount: float | None = None          # 成交金额
    deal_at: datetime | None = None
    # ── 加微 ──
    wechat_added_at: datetime | None = None
    manual: bool = False                      # 手动标记加微
    referrer: str | None = None               # 转介绍推荐人
    # ── 下一步 ──
    next_action: str | None = None
    next_at: str | None = None                # 前端展示用，如"今天 16:00"
    # ── 流失 ──
    lost_reason: str | None = None
    lost_at: datetime | None = None
    # ── 展示 ──
    hue: int = 200
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class CustomerLog:
    """客户跟进日志 · timeline 展示

    与前端 customers[].logs 对齐。
    """
    customer_id: str
    text: str
    time: str = ""                            # 展示用时间字符串，如"08-25 10:30"
    by: str = "系统"                           # 系统 / 老板
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class FollowUp:
    """今日跟进 · 三段式待办（逾期/今天/以后）

    扩展自 v0.1 FollowUpEvent，关联客户而非线索。
    """
    customer_id: str | None = None
    customer_name: str = ""
    type: str = ""                            # 报价跟进 / 方案推进 / 谈判 / SOP·D1
    text: str = ""
    due: str = ""                             # 截止时间，如"今天 15:00"
    overdue: bool = False
    done: bool = False
    # ── v0.1 兼容字段 ──
    lead_id: str = ""
    event_type: str = ""
    content: str = ""
    scheduled_at: datetime | None = None
    result: str = ""
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)


# v0.1 旧名别名（保留兼容）
FollowUpEvent = FollowUp


# ═══════════════════════════════════════════════════════════
# 消息域
# ═══════════════════════════════════════════════════════════

@dataclass(slots=True)
class Conversation:
    """私信会话 · 关联线索和账号"""
    lead_id: str
    account_name: str = "default"
    status: ConversationStatus = "open"
    last_message_at: datetime = field(default_factory=now_utc)
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class Message:
    """私信消息"""
    conversation_id: str
    sender: MessageSender
    content: str
    is_ai_generated: bool = False
    direction: MessageDirection = "out"       # out=我们发 / in=用户发
    variant: str | None = None                # 使用的话术变体 A/B/C
    is_read: bool = False                     # 入站消息是否已读（收件箱未读数依据）
    script_id: str | None = None              # 回复时使用的话术 ID（可选）
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)


# ═══════════════════════════════════════════════════════════
# 分析域 / 合规域
# ═══════════════════════════════════════════════════════════

@dataclass(slots=True)
class ComplianceEvent:
    """合规审计事件 · 不可变日志

    与前端 compliance.audit 对齐。所有高风险动作必须写审计。
    """
    type: ComplianceEventType
    level: ComplianceLevel
    text: str
    time: str = ""                            # 展示用时间
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class ComplianceRule:
    """合规规则 · R1/R2/R3

    与前端 compliance.rules 对齐。
    """
    rule_id: str                              # R1 / R2 / R3
    desc: str
    status: RuleStatus = "standby"
    hit_at: str | None = None
    target: str | None = None


@dataclass(slots=True)
class RuntimeState:
    """全局运行态 · 安全模式 / 全量暂停

    单例实体，id 固定为 "default"。
    """
    safe_mode_active: bool = False
    safe_mode_reason: str = ""
    safe_mode_triggered_at: str | None = None
    safe_mode_trigger_source: str | None = None  # manual / r3 / health_score
    task_running: bool = True
    id: str = "default"
    updated_at: datetime = field(default_factory=now_utc)


# ═══════════════════════════════════════════════════════════
# 任务域 / 行为事件
# ═══════════════════════════════════════════════════════════

@dataclass(slots=True)
class Task:
    """采集/导入任务"""
    task_type: str
    payload: dict
    status: TaskStatus = "pending"
    error_message: str = ""
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class LeadBehaviorEvent:
    """线索行为事件 · 用于漏斗归因和触点分析

    事件类型示例：collected / outreach_sent / dm_replied / wechat_added / deal_won / comment_replied
    """
    lead_id: str
    event_type: str
    content: str = ""
    value: float = 0
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)


# ═══════════════════════════════════════════════════════════
# 配置域（工作台配置 · 单例）
# ═══════════════════════════════════════════════════════════

@dataclass(slots=True)
class BusinessProfile:
    industry: str = ""
    product: str = ""
    service_area: str = ""
    target_customer: str = ""
    price_range: str = ""
    conversion_goal: str = "添加微信"
    tone: str = "专业、真诚"
    id: str = "default"
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class ProductKnowledge:
    product_name: str = ""
    description: str = ""
    selling_points: str = ""
    target_customers: str = ""
    price_range: str = ""
    faq: str = ""
    forbidden_claims: str = ""
    id: str = "default"
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class AudienceProfile:
    name: str = ""
    industry: str = ""
    region: str = ""
    needs: str = ""
    pain_points: str = ""
    intent_keywords: str = ""
    excluded_keywords: str = ""
    id: str = "default"
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class ScriptStrategy:
    comment_script: str = ""
    private_message_script: str = ""
    wechat_script: str = ""
    objection_script: str = ""
    id: str = "default"
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class WeChatSettings:
    wechat_id: str = ""
    guide_timing: str = "客户明确表达兴趣后"
    guide_reason: str = "发送详细方案和案例"
    compliance_note: str = ""
    id: str = "default"
    updated_at: datetime = field(default_factory=now_utc)


# ═══════════════════════════════════════════════════════════
# 采集域 · CrawlTask（MediaCrawler 对接）
# ═══════════════════════════════════════════════════════════

# 采集任务状态
CrawlTaskStatus = Literal["pending", "running", "completed", "failed"]

# 采集类型
CrawlType = Literal["comment", "search", "profile"]


@dataclass(slots=True)
class CrawlTask:
    """采集任务 · MediaCrawler 对接核心实体

    追踪从抖音评论区自动采集高意向用户的任务配置与执行状态。
    采集到的评论通过 /api/crawl/import 入库为 Lead。
    """
    name: str = ""                              # 任务名称
    crawl_type: CrawlType = "comment"           # 采集类型
    keyword: str = ""                            # 搜索关键词
    competitor_account: str = ""                 # 对标账号（抖音昵称/主页URL）
    video_url: str = ""                          # 指定视频URL
    source: LeadSource = "own_comment"           # 采集来源归因（own_comment / competitor）
    intent_keywords: str = ""                    # 高意向关键词（逗号分隔，加权评分）
    excluded_keywords: str = ""                  # 排除关键词
    max_comments: int = 100                      # 最大采集评论数
    status: CrawlTaskStatus = "pending"          # pending/running/completed/failed
    collected_count: int = 0                     # 已采集评论数
    imported_count: int = 0                      # 已入库线索数
    error_message: str = ""                      # 失败原因
    started_at: datetime | None = None            # 启动时间
    finished_at: datetime | None = None           # 完成时间
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


# ═══════════════════════════════════════════════════════════
# 私信发送执行域 · DmSendResult（P4-D）
# ═══════════════════════════════════════════════════════════

# 发送结果状态（与 integrations/dm_sender.py 的 SendStatus 对齐）
DmSendStatus = Literal["success", "failed", "throttled", "need_captcha", "account_banned"]

# 发送器模式
DmSenderMode = Literal["mock", "cdp"]


@dataclass(slots=True)
class DmSendResult:
    """私信发送结果 · 每次发送的不可变留痕

    由 DmSenderService 在每次发送后写入，用于执行审计与前端结果列表。
    """
    lead_id: str                                 # 关联线索（测试发送为 "__test__"）
    account_id: str | None = None                # 实际使用的账号
    content: str = ""                            # 实际发送的内容
    status: DmSendStatus = "success"             # success/failed/throttled/need_captcha/account_banned
    message: str = ""                            # 发送器返回的说明
    sender_mode: DmSenderMode = "mock"           # mock / cdp
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=now_utc)


# ═══════════════════════════════════════════════════════════
# 通知域 · Notification（P4-A 通知提醒系统）
# ═══════════════════════════════════════════════════════════

# 通知类型（7 种业务事件源）
NotificationType = Literal[
    "new_dm",            # 新私信
    "followup_due",      # 跟进到期
    "followup_overdue",  # 跟进逾期
    "deal_won",          # 成交
    "wechat_added",      # 加微
    "account_warning",   # 账号预警
    "system",            # 系统通知
]

# 通知等级（决定前端配色）
NotificationLevel = Literal["info", "warning", "error", "success"]


@dataclass(slots=True)
class Notification:
    """通知提醒 · 通知中心核心实体

    顶部铃铛未读数 + 通知中心页面，按类型/等级聚合全系统事件。
    所有时间用 UTC datetime，ID 用 uuid4().hex。
    """
    id: str = field(default_factory=_new_id)
    type: str = "system"                  # NotificationType
    title: str = ""
    content: str = ""
    level: str = "info"                   # NotificationLevel
    read: bool = False
    related_type: str | None = None       # lead / customer / account ...
    related_id: str | None = None
    created_at: datetime = field(default_factory=now_utc)