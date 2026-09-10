"""多账号调度 Schema · 策略配置 / 账号调度状态 / 任务分配 / 统计

响应字段使用 camelCase（与 api-contract.md 一致）。
"""
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Camel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


# ═══════════════════════════════════════════════════════════
# 调度配置
# ═══════════════════════════════════════════════════════════

class SchedulerConfig(_Camel):
    """调度器配置"""
    strategy: str = Field(default="round_robin", description="round_robin | weighted | health_based")
    health_threshold_warn: int = Field(default=60, description="健康分警告阈值")
    health_threshold_critical: int = Field(default=30, description="健康分严重阈值（低于此值不分配）")
    max_concurrent: int = Field(default=3, description="最大并发账号数")


class SchedulerConfigUpdate(_Camel):
    """更新调度配置（部分字段）"""
    strategy: str | None = None
    health_threshold_warn: int | None = None
    health_threshold_critical: int | None = None
    max_concurrent: int | None = None


# ═══════════════════════════════════════════════════════════
# 账号调度状态
# ═══════════════════════════════════════════════════════════

class AccountScheduleStatus(_Camel):
    """单个账号的调度状态"""
    account_id: str
    account_name: str
    health_score: int
    today_sent: int
    remaining_quota: int
    status: str = Field(description="active | paused | throttled | safe_mode | banned")
    weight: int


# ═══════════════════════════════════════════════════════════
# 任务分配
# ═══════════════════════════════════════════════════════════

class AssignRequest(_Camel):
    """分配发送任务请求"""
    task_type: str = Field(default="dm", description="任务类型：dm | comment | crawl")
    priority: int = Field(default=0, description="优先级，越大越优先")


class AssignResponse(_Camel):
    """分配结果"""
    account_id: str
    account_name: str
    strategy_used: str
    reason: str


# ═══════════════════════════════════════════════════════════
# 调度统计
# ═══════════════════════════════════════════════════════════

class PerAccountStat(_Camel):
    """单账号统计"""
    account_id: str
    account_name: str
    sent: int
    success_rate: float
    health_score: int


class SchedulerStats(_Camel):
    """调度器全局统计"""
    total_sent: int
    success_rate: float
    active_accounts: int
    per_account: list[PerAccountStat]
