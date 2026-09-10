"""
WorkbenchService · 工作台聚合业务逻辑
========================================
今日运营总览 + 预警 + 最近活动

聚合 Lead / Customer / FollowUp / Account / ComplianceRule / ComplianceEvent
多源数据，返回前端契约格式。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.domain import Account, ComplianceEvent, ComplianceRule, Customer, FollowUp, Lead
from app.repositories.base import Repository

_CST = timezone(timedelta(hours=8))

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _fmt_display_time(dt: datetime) -> str:
    return dt.astimezone(_CST).strftime("%m-%d %H:%M")


def _is_today(dt: datetime | None) -> bool:
    if dt is None:
        return False
    return dt.astimezone(_CST).date() == _now_cst().date()


class WorkbenchService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def get_workbench(self) -> dict:
        """工作台聚合：今日总览 + 预警 + 最近活动"""
        now = _now_cst()
        date_str = f"{now.strftime('%Y-%m-%d')} · {_WEEKDAY_CN[now.weekday()]}"

        return {
            "date": date_str,
            "today": self._build_today(),
            "alerts": self._build_alerts(),
            "activity": self._build_activity(),
        }

    # ═══════════════════════════════════════════════════════
    # 今日总览
    # ═══════════════════════════════════════════════════════

    def _build_today(self) -> dict:
        leads = self._repo.list_leads()
        customers = self._repo.list_customers()
        followups = self._repo.list_followups()

        # 今日新增线索
        new_leads = sum(1 for l in leads if _is_today(l.created_at))

        # 待发送（pending_outreach + throttled）
        pending_send = sum(1 for l in leads if l.status in ("pending_outreach", "throttled"))

        # 今日已发送（状态已推进到 sent 及以后，且 updated_at 为今天）
        sent_statuses = ("sent", "replied", "wechat_added", "deal_won")
        sent_today = sum(1 for l in leads if l.status in sent_statuses and _is_today(l.updated_at))

        # 今日加微
        wechat_today = sum(1 for l in leads if _is_today(l.wechat_added_at))

        # 跟进待办（未完成且未逾期）
        followups_pending = sum(1 for f in followups if not f.done and not f.overdue)

        # 跟进逾期
        followups_overdue = sum(1 for f in followups if f.overdue and not f.done)

        # 成交
        deal_customers = [c for c in customers if c.stage == "won"]
        deal_count = len(deal_customers)
        deal_amount = sum(c.deal_amount or 0 for c in deal_customers)

        return {
            "newLeads": new_leads,
            "pendingSend": pending_send,
            "sentToday": sent_today,
            "wechatToday": wechat_today,
            "followupsPending": followups_pending,
            "followupsOverdue": followups_overdue,
            "deal": {"count": deal_count, "amount": deal_amount},
        }

    # ═══════════════════════════════════════════════════════
    # 预警
    # ═══════════════════════════════════════════════════════

    def _build_alerts(self) -> list[dict]:
        alerts: list[dict] = []

        # 1. 合规规则命中预警
        rules = self._repo.list_compliance_rules()
        for rule in rules:
            if rule.status == "hit":
                level = "danger" if rule.rule_id == "R3" else "warn"
                target = f"（{rule.target}）" if rule.target else ""
                alerts.append({
                    "level": level,
                    "text": f"{rule.rule_id} 规则命中{target}：{rule.desc}",
                    "action": "查看合规",
                    "href": "#/compliance",
                })

        # 2. 账号健康分预警
        accounts = self._repo.list_accounts()
        for acc in accounts:
            if acc.health_score < 40 or acc.limit_status in ("safe_mode", "banned"):
                alerts.append({
                    "level": "danger",
                    "text": f"账号「{acc.nickname or acc.name}」健康分 {acc.health_score}，已进入危险状态",
                    "action": "查看账号",
                    "href": "#/health",
                })
            elif acc.health_score < 60 or acc.limit_status == "warn":
                alerts.append({
                    "level": "warn",
                    "text": f"账号「{acc.nickname or acc.name}」健康分 {acc.health_score}，需关注",
                    "action": "查看账号",
                    "href": "#/health",
                })

        # 3. 发送失败预警
        leads = self._repo.list_leads()
        failed_count = sum(1 for l in leads if l.status == "send_failed")
        if failed_count > 0:
            alerts.append({
                "level": "warn",
                "text": f"有 {failed_count} 条私信发送失败，建议检查账号状态后重试",
                "action": "查看线索",
                "href": "#/leads?status=send_failed",
            })

        # 4. 跟进逾期预警
        followups = self._repo.list_followups()
        overdue_count = sum(1 for f in followups if f.overdue and not f.done)
        if overdue_count > 0:
            alerts.append({
                "level": "warn",
                "text": f"有 {overdue_count} 条跟进已逾期，请及时处理",
                "action": "查看跟进",
                "href": "#/followups",
            })

        return alerts

    # ═══════════════════════════════════════════════════════
    # 最近活动（审计事件）
    # ═══════════════════════════════════════════════════════

    def _build_activity(self, limit: int = 6) -> list[dict]:
        events = self._repo.list_compliance_events(limit=limit)
        return [
            {
                "time": e.time or _fmt_display_time(e.created_at),
                "type": e.type,
                "level": e.level,
                "text": e.text,
            }
            for e in events
        ]
