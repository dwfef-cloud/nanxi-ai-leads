"""
报表路由 · 周报 / 月报 / 自定义区间汇总 / 趋势
=================================================
GET /api/reports/weekly?date=YYYY-MM-DD     所在周（周一到周日）聚合
GET /api/reports/monthly?year=2026&month=9  自然月聚合
GET /api/reports/summary?start_date=&end_date=  自定义区间汇总
GET /api/reports/trend?days=14              近 N 天每日线索/加微趋势
"""
from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_report_service
from app.schemas.report import (
    MonthlyReport, SummaryReport, TrendResponse, WeeklyReport,
)
from app.services.report_service import ReportService

router = APIRouter(tags=["reports"])


@router.get("/reports/weekly", response_model=WeeklyReport)
def get_weekly_report(
    date: str = Query(..., description="参考日期 YYYY-MM-DD，取该日期所在周"),
    service: ReportService = Depends(get_report_service),
) -> dict:
    """周报：以 date 所在周（周一到周日）聚合"""
    return service.get_weekly_report(date)


@router.get("/reports/monthly", response_model=MonthlyReport)
def get_monthly_report(
    year: int = Query(..., ge=2020, le=2100, description="年份"),
    month: int = Query(..., ge=1, le=12, description="月份"),
    service: ReportService = Depends(get_report_service),
) -> dict:
    """月报：自然月聚合"""
    return service.get_monthly_report(year, month)


@router.get("/reports/summary", response_model=SummaryReport)
def get_summary(
    start_date: str = Query(..., description="开始日期 YYYY-MM-DD"),
    end_date: str = Query(..., description="结束日期 YYYY-MM-DD"),
    service: ReportService = Depends(get_report_service),
) -> dict:
    """自定义日期区间汇总报表"""
    return service.get_summary(start_date, end_date)


@router.get("/reports/trend", response_model=TrendResponse)
def get_trend(
    days: int = Query(14, ge=1, le=90, description="趋势天数"),
    service: ReportService = Depends(get_report_service),
) -> dict:
    """近 N 天每日新增线索 / 加微数趋势（前端趋势图用）"""
    return service.get_daily_trend(days)
