"""
分析域路由 · 转化漏斗 / 触点归因 / 合规仪表盘 / 运行态
=============================================================
GET /api/dashboard/funnel      六级转化漏斗 + 成本 + 14天加微趋势
GET /api/dashboard/attribution  6触点归因 + 内容榜
GET /api/dashboard/compliance   合规风险总览 + R1/R2/R3 + 审计日志
GET /api/runtime                 全局运行态（安全模式 + 任务运行）
"""
from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_dashboard_service
from app.schemas.analytics import (
    AttributionResponse, DashboardComplianceResponse,
    FunnelResponse, RuntimeResponse,
)
from app.services.dashboard_service import DashboardService

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/funnel", response_model=FunnelResponse)
def get_funnel(
    days: int = Query(30, ge=7, le=90, description="统计窗口天数"),
    service: DashboardService = Depends(get_dashboard_service),
) -> dict:
    """六级转化漏斗 + 成本计算 + 14天加微趋势"""
    return service.get_funnel(window_days=days)


@router.get("/dashboard/attribution", response_model=AttributionResponse)
def get_attribution(
    days: int = Query(30, ge=7, le=90, description="统计窗口天数"),
    service: DashboardService = Depends(get_dashboard_service),
) -> dict:
    """6 触点归因 + 内容榜 topVideos"""
    return service.get_attribution(window_days=days)


@router.get("/dashboard/compliance", response_model=DashboardComplianceResponse)
def get_compliance_dashboard(
    service: DashboardService = Depends(get_dashboard_service),
) -> dict:
    """合规风险总览 + R1/R2/R3 规则状态 + 最近审计事件"""
    return service.get_compliance()


@router.get("/runtime", response_model=RuntimeResponse)
def get_runtime(
    service: DashboardService = Depends(get_dashboard_service),
) -> dict:
    """全局运行态：安全模式状态 + 任务运行状态"""
    return service.get_runtime()
