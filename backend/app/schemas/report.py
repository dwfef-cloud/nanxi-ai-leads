"""
报表域 Schema · 周报 / 月报 / 区间汇总 / 导出响应
===================================================
"""
from pydantic import BaseModel


class _ReportMetrics(BaseModel):
    """报表通用指标字段"""
    new_leads: int = 0
    contacted: int = 0
    replied: int = 0
    added_wechat: int = 0
    deals: int = 0
    deal_amount: float = 0.0
    conversion_rate: float = 0.0


class WeeklyReport(_ReportMetrics):
    """周报：以周一到周日为统计区间"""
    week_start: str
    week_end: str


class MonthlyReport(_ReportMetrics):
    """月报：以自然月为统计区间"""
    year: int
    month: int


class SummaryReport(_ReportMetrics):
    """自定义区间汇总报表"""
    start_date: str
    end_date: str


class DailyTrendPoint(BaseModel):
    """单日趋势点"""
    date: str
    new_leads: int = 0
    added_wechat: int = 0


class TrendResponse(BaseModel):
    """近 N 天趋势响应"""
    days: int
    points: list[DailyTrendPoint]


class ExportResponse(BaseModel):
    """导出文件响应"""
    file_url: str
    file_name: str
    row_count: int
    format: str
