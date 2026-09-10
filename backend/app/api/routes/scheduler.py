"""多账号调度路由 · /api/scheduler/*

GET  /config    获取调度配置
PUT  /config    更新调度配置
GET  /accounts  获取所有账号调度状态
POST /assign    分配发送任务
GET  /stats     调度统计
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_scheduler_service
from app.schemas.scheduler import (
    AccountScheduleStatus,
    AssignRequest,
    AssignResponse,
    SchedulerConfig,
    SchedulerConfigUpdate,
    SchedulerStats,
)
from app.services.scheduler_service import SchedulerService

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


@router.get("/config", response_model=SchedulerConfig, response_model_by_alias=True)
def get_config(
    service: SchedulerService = Depends(get_scheduler_service),
) -> SchedulerConfig:
    """获取调度器配置"""
    return service.get_config()


@router.put("/config", response_model=SchedulerConfig, response_model_by_alias=True)
def update_config(
    payload: SchedulerConfigUpdate,
    service: SchedulerService = Depends(get_scheduler_service),
) -> SchedulerConfig:
    """更新调度器配置（部分字段）"""
    data = payload.model_dump(exclude_none=True)
    return service.update_config(data)


@router.get("/accounts", response_model=list[AccountScheduleStatus], response_model_by_alias=True)
def get_accounts_status(
    service: SchedulerService = Depends(get_scheduler_service),
) -> list[AccountScheduleStatus]:
    """获取所有账号的调度状态（今日发送/剩余额度/健康分/状态）"""
    return service.get_accounts_schedule_status()


@router.post("/assign", response_model=AssignResponse, response_model_by_alias=True)
def assign_task(
    payload: AssignRequest,
    service: SchedulerService = Depends(get_scheduler_service),
) -> AssignResponse:
    """为发送任务分配一个可用账号"""
    result = service.assign_account(
        task_type=payload.task_type,
        priority=payload.priority,
    )
    if not result.account_id:
        raise HTTPException(status_code=503, detail=result.reason)
    return result


@router.get("/stats", response_model=SchedulerStats, response_model_by_alias=True)
def get_stats(
    service: SchedulerService = Depends(get_scheduler_service),
) -> SchedulerStats:
    """调度统计：总发送量/成功率/活跃账号数/各账号明细"""
    return service.get_stats()
