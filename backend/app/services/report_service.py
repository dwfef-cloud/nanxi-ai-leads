"""
ReportService · 报表聚合服务
==============================
周报 / 月报 / 自定义区间汇总 / 近 N 天趋势。

聚合数据源：
  - leads（新增线索）：leads.created_at
  - messages（触达/回复）：direction='out' 为触达，direction='in' 为回复
  - leads.wechat_added_at（加微）
  - leads.status='deal_won' + deal_at（成交/金额）

转化率 = 成交数 / 新增线索数（保留2位小数，分母为0时返回0）
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from app.repositories.base import Repository


def _date_part(dt_str) -> str | None:
    """从 ISO  datetime 字符串提取日期部分 YYYY-MM-DD"""
    if not dt_str:
        return None
    s = str(dt_str)
    return s[:10] if len(s) >= 10 else None


def _in_range(dt_str, start_date: str, end_date: str) -> bool:
    """判断日期时间字符串是否在 [start_date, end_date] 区间内"""
    d = _date_part(dt_str)
    if d is None:
        return False
    return start_date <= d <= end_date


class ReportService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    # ═══════════════════════════════════════════════════════
    # 周报
    # ═══════════════════════════════════════════════════════

    def get_weekly_report(self, date_str: str) -> dict:
        """以 date_str 所在周（周一到周日）聚合"""
        ref = date.fromisoformat(date_str)
        monday = ref - timedelta(days=ref.weekday())
        sunday = monday + timedelta(days=6)
        metrics = self._aggregate(monday.isoformat(), sunday.isoformat())
        return {
            "week_start": monday.isoformat(),
            "week_end": sunday.isoformat(),
            **metrics,
        }

    # ═══════════════════════════════════════════════════════
    # 月报
    # ═══════════════════════════════════════════════════════

    def get_monthly_report(self, year: int, month: int) -> dict:
        """以自然月聚合"""
        first_day = date(year, month, 1)
        last_day_num = calendar.monthrange(year, month)[1]
        last_day = date(year, month, last_day_num)
        metrics = self._aggregate(first_day.isoformat(), last_day.isoformat())
        return {
            "year": year,
            "month": month,
            **metrics,
        }

    # ═══════════════════════════════════════════════════════
    # 自定义区间汇总
    # ═══════════════════════════════════════════════════════

    def get_summary(self, start_date: str, end_date: str) -> dict:
        """自定义日期区间汇总"""
        metrics = self._aggregate(start_date, end_date)
        return {
            "start_date": start_date,
            "end_date": end_date,
            **metrics,
        }

    # ═══════════════════════════════════════════════════════
    # 近 N 天趋势（前端趋势图用）
    # ═══════════════════════════════════════════════════════

    def get_daily_trend(self, days: int = 14) -> dict:
        """近 N 天每日新增线索 / 加微数趋势"""
        leads = self._repo.list_leads()
        today = date.today()
        # 初始化每天为 0
        trend_map: dict[str, dict[str, int]] = {}
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            trend_map[d.isoformat()] = {"new_leads": 0, "added_wechat": 0}
        # 统计
        for lead in leads:
            created = _date_part(lead.created_at.isoformat() if lead.created_at else None)
            if created and created in trend_map:
                trend_map[created]["new_leads"] += 1
            wechat = _date_part(lead.wechat_added_at.isoformat() if lead.wechat_added_at else None)
            if wechat and wechat in trend_map:
                trend_map[wechat]["added_wechat"] += 1
        points = [
            {"date": k, "new_leads": v["new_leads"], "added_wechat": v["added_wechat"]}
            for k, v in trend_map.items()
        ]
        return {"days": days, "points": points}

    # ═══════════════════════════════════════════════════════
    # 核心聚合逻辑
    # ═══════════════════════════════════════════════════════

    def _aggregate(self, start_date: str, end_date: str) -> dict:
        """在指定日期区间内聚合所有指标"""
        leads = self._repo.list_leads()
        messages = self._repo.list_messages()

        new_leads = 0
        added_wechat = 0
        deals = 0
        deal_amount = 0.0

        for lead in leads:
            # 新增线索：按 created_at
            created = _date_part(lead.created_at.isoformat() if lead.created_at else None)
            if created and _in_range(created, start_date, end_date):
                new_leads += 1
            # 加微：按 wechat_added_at
            wechat = _date_part(lead.wechat_added_at.isoformat() if lead.wechat_added_at else None)
            if wechat and _in_range(wechat, start_date, end_date):
                added_wechat += 1
            # 成交：按 deal_at
            if lead.status == "deal_won":
                deal_dt = _date_part(lead.deal_at.isoformat() if lead.deal_at else None)
                if deal_dt and _in_range(deal_dt, start_date, end_date):
                    deals += 1
                    deal_amount += float(lead.deal_amount or 0)

        # 触达 / 回复：按 messages.direction
        contacted = 0
        replied = 0
        for msg in messages:
            msg_date = _date_part(msg.created_at.isoformat() if msg.created_at else None)
            if not msg_date or not _in_range(msg_date, start_date, end_date):
                continue
            if msg.direction == "out":
                contacted += 1
            elif msg.direction == "in":
                replied += 1

        # 转化率 = 成交数 / 新增线索数
        conversion_rate = round(deals / new_leads, 2) if new_leads > 0 else 0.0

        return {
            "new_leads": new_leads,
            "contacted": contacted,
            "replied": replied,
            "added_wechat": added_wechat,
            "deals": deals,
            "deal_amount": round(deal_amount, 2),
            "conversion_rate": conversion_rate,
        }
