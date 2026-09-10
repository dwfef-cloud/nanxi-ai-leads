"""系统监控路由 · /api/monitor/*

GET /health       综合健康状态
GET /database     数据库状态
GET /api-stats    API 统计
GET /accounts     账号健康度总览
GET /tasks        任务运行态
GET /logs         最近系统日志
"""
from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_monitor_service
from app.schemas.monitor import (
    AccountHealth,
    ApiStats,
    DatabaseInfo,
    HealthResponse,
    LogEntry,
    TaskStatus,
)
from app.services.monitor_service import MonitorService

router = APIRouter(prefix="/monitor", tags=["monitor"])


@router.get("/health", response_model=HealthResponse, response_model_by_alias=True)
def health(service: MonitorService = Depends(get_monitor_service)) -> dict:
    """综合健康状态：后端 / 数据库 / AI配置 / 采集引擎 / 桌面端 + overall"""
    return service.get_health()


@router.get("/database", response_model=DatabaseInfo, response_model_by_alias=True)
def database(service: MonitorService = Depends(get_monitor_service)) -> dict:
    """数据库状态：文件大小 / 表数量 / 各表记录数 / 最近备份"""
    return service.get_database_info()


@router.get("/api-stats", response_model=ApiStats, response_model_by_alias=True)
def api_stats(service: MonitorService = Depends(get_monitor_service)) -> dict:
    """API 统计：总请求 / 错误率 / 平均响应时间 / Top5 端点"""
    return service.get_api_stats()


@router.get("/accounts", response_model=list[AccountHealth], response_model_by_alias=True)
def accounts(service: MonitorService = Depends(get_monitor_service)) -> list[dict]:
    """账号健康度总览"""
    return service.get_accounts_health()


@router.get("/tasks", response_model=TaskStatus, response_model_by_alias=True)
def tasks(service: MonitorService = Depends(get_monitor_service)) -> dict:
    """任务运行态：采集任务 / 私信队列 / 调度器"""
    return service.get_tasks_status()


@router.get("/logs", response_model=list[LogEntry], response_model_by_alias=True)
def logs(
    lines: int = Query(default=100, ge=1, le=500),
    service: MonitorService = Depends(get_monitor_service),
) -> list[dict]:
    """最近系统日志（审计事件，时间倒序）"""
    return service.get_recent_logs(lines=lines)
