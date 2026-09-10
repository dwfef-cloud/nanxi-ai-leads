"""
DashboardService · 分析域业务逻辑
===================================
六级转化漏斗 / 触点归因 / 合规风险仪表盘 / 全局运行态

依赖 Repository 接口，不感知底层实现。
在 Repository 聚合查询基础上做 Service 层增强：
  - 漏斗：补全 14 天加微趋势
  - 归因：修正中文标签 + 内容榜 topVideos
  - 合规：序列化 Rule/Event 为前端契约格式
  - 运行态：序列化 RuntimeState
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.models.domain import ComplianceEvent, ComplianceRule, Lead, RuntimeState
from app.repositories.base import Repository

# 东八区（本地工具默认时区）
_CST = timezone(timedelta(hours=8))

# 触点来源中文标签
_SOURCE_LABELS: dict[str, str] = {
    "own_comment": "自有视频评论区",
    "competitor": "对标账号监控",
    "inbound_dm": "用户主动私信",
    "fan_dm": "粉丝私信",
    "lead_card": "留资卡",
    "referral": "老客转介绍",
}


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _fmt_display_time(dt: datetime) -> str:
    """格式化为前端展示用 'MM-DD HH:MM'"""
    return dt.astimezone(_CST).strftime("%m-%d %H:%M")


class DashboardService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    # ═══════════════════════════════════════════════════════
    # 1. 转化漏斗
    # ═══════════════════════════════════════════════════════

    def get_funnel(self, window_days: int = 30) -> dict:
        """六级转化漏斗 + 成本 + 14天加微趋势"""
        result = self._repo.funnel_stats(window_days=window_days)
        # Service 层增强：补全 14 天加微趋势
        result["wechatTrend"] = self._build_wechat_trend(days=14)
        return result

    def _build_wechat_trend(self, days: int = 14) -> list[dict]:
        """从 Lead.wechat_added_at 聚合最近 N 天加微数"""
        leads = self._repo.list_leads()
        today = _now_cst().date()
        # 初始化每天为 0
        trend_map: dict[str, int] = {}
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            trend_map[d.strftime("%m-%d")] = 0
        # 统计加微
        for lead in leads:
            if lead.wechat_added_at is None:
                continue
            local_date = lead.wechat_added_at.astimezone(_CST).date()
            key = local_date.strftime("%m-%d")
            if key in trend_map:
                trend_map[key] += 1
        return [{"date": k, "v": v} for k, v in trend_map.items()]

    # ═══════════════════════════════════════════════════════
    # 2. 触点归因
    # ═══════════════════════════════════════════════════════

    def get_attribution(self, window_days: int = 30) -> dict:
        """6 触点归因 + 内容榜"""
        result = self._repo.attribution_stats(window_days=window_days)
        # Service 层增强：修正中文标签
        for item in result.get("bySource", []):
            item["label"] = _SOURCE_LABELS.get(item["source"], item["source"])
        # Service 层增强：内容榜 topVideos
        result["topVideos"] = self._build_top_videos()
        return result

    def _build_top_videos(self, limit: int = 10) -> list[dict]:
        """从 Lead.video 聚合内容榜"""
        leads = self._repo.list_leads()
        video_map: dict[str, dict] = defaultdict(lambda: {"leads": 0, "wechat": 0, "dealAmount": 0.0})
        for lead in leads:
            title = lead.video or "未标记视频"
            entry = video_map[title]
            entry["leads"] += 1
            if lead.status in ("wechat_added", "deal_won"):
                entry["wechat"] += 1
            if lead.status == "deal_won" and lead.deal_amount:
                entry["dealAmount"] += lead.deal_amount
        # 按线索数排序，取 top N
        sorted_videos = sorted(video_map.items(), key=lambda x: x[1]["leads"], reverse=True)[:limit]
        return [
            {"video": title, "leads": data["leads"], "wechat": data["wechat"], "dealAmount": data["dealAmount"]}
            for title, data in sorted_videos
        ]

    # ═══════════════════════════════════════════════════════
    # 3. 合规风险仪表盘
    # ═══════════════════════════════════════════════════════

    def get_compliance(self) -> dict:
        """合规风险总览 + R1/R2/R3 规则 + 审计日志"""
        result = self._repo.compliance_summary()
        # 序列化规则
        result["rules"] = [self._serialize_rule(r) for r in result.get("rules", [])]
        # 序列化审计事件
        result["audit"] = [self._serialize_event(e) for e in result.get("audit", [])]
        return result

    @staticmethod
    def _serialize_rule(rule: ComplianceRule) -> dict:
        return {
            "id": rule.rule_id,
            "desc": rule.desc,
            "status": rule.status,
            "hitAt": rule.hit_at,
            "target": rule.target,
        }

    @staticmethod
    def _serialize_event(event: ComplianceEvent) -> dict:
        time_str = event.time or _fmt_display_time(event.created_at)
        return {
            "time": time_str,
            "type": event.type,
            "level": event.level,
            "text": event.text,
        }

    # ═══════════════════════════════════════════════════════
    # 4. 全局运行态
    # ═══════════════════════════════════════════════════════

    def get_runtime(self) -> dict:
        """全局运行态：安全模式 + 任务运行状态"""
        state = self._repo.get_runtime()
        return self._serialize_runtime(state)

    @staticmethod
    def _serialize_runtime(state: RuntimeState) -> dict:
        return {
            "safeMode": {
                "active": state.safe_mode_active,
                "reason": state.safe_mode_reason,
                "triggeredAt": state.safe_mode_triggered_at,
                "triggerSource": state.safe_mode_trigger_source,
            },
            "taskRunning": state.task_running,
        }
