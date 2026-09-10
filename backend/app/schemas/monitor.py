"""系统监控 Schema · 健康检查 / 数据库 / API 统计 / 账号健康 / 任务 / 日志

响应字段使用 camelCase（与前端 api-monitor.js 一致）。
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
# 综合健康状态
# ═══════════════════════════════════════════════════════════

class ComponentHealth(_Camel):
    """单个组件健康状态"""
    status: str = Field(description="ok | warning | error | stopped | unknown")
    message: str = ""


class BackendHealth(ComponentHealth):
    version: str = ""
    uptime: float = 0.0


class DatabaseHealth(ComponentHealth):
    db_path: str = ""
    file_size_mb: float = 0.0


class AiConfigHealth(ComponentHealth):
    api_key_configured: bool = False
    model: str = ""
    provider: str = ""


class CrawlerHealth(ComponentHealth):
    running: bool = False
    tasks_count: int = 0


class HealthResponse(_Camel):
    backend: BackendHealth
    database: DatabaseHealth
    ai_config: AiConfigHealth
    crawler: CrawlerHealth
    desktop: ComponentHealth
    overall: str = Field(description="healthy | degraded | critical")


# ═══════════════════════════════════════════════════════════
# 数据库信息
# ═══════════════════════════════════════════════════════════

class TableRecordCount(_Camel):
    """单表记录数"""
    table: str
    count: int


class DatabaseInfo(_Camel):
    file_path: str
    file_size_mb: float
    table_count: int
    total_records: int
    last_backup_at: str | None = None
    tables: list[TableRecordCount] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# API 统计
# ═══════════════════════════════════════════════════════════

class EndpointStat(_Camel):
    endpoint: str
    total: int
    errors: int
    avg_time: float
    error_rate: float


class ApiStats(_Camel):
    total_requests: int
    total_errors: int
    error_rate: float
    avg_response_time: float
    uptime: float
    top_5_endpoints: list[EndpointStat] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# 账号健康度总览
# ═══════════════════════════════════════════════════════════

class AccountHealth(_Camel):
    account_id: str
    nickname: str
    health_score: int
    limit_status: str
    today_sent: int
    warning_count: int


# ═══════════════════════════════════════════════════════════
# 任务运行态
# ═══════════════════════════════════════════════════════════

class CrawlTaskStatus(_Camel):
    total: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0


class DmQueueStatus(_Camel):
    pending: int = 0
    sent: int = 0
    failed: int = 0
    throttled: int = 0


class SchedulerStatus(_Camel):
    status: str = "stopped"
    next_run: str | None = None


class TaskStatus(_Camel):
    crawl_tasks: CrawlTaskStatus = Field(default_factory=CrawlTaskStatus)
    dm_queue: DmQueueStatus = Field(default_factory=DmQueueStatus)
    scheduler: SchedulerStatus = Field(default_factory=SchedulerStatus)


# ═══════════════════════════════════════════════════════════
# 最近系统日志（审计事件）
# ═══════════════════════════════════════════════════════════

class LogEntry(_Camel):
    id: str
    event_type: str
    severity: str
    message: str
    created_at: str
