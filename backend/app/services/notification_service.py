"""通知提醒 Service（P4-A）

依赖 Repository 接口，不感知底层实现。
聚合全系统事件为统一通知流：新私信 / 跟进到期逾期 / 成交 / 加微 / 账号预警 / 系统。

触发方式：
  1. 各业务服务在关键事件点直接调用 create(...)
  2. check_followup_due() 在请求时被动触发，扫描跟进到期/逾期并自动建通知
     （按 related_id 去重，避免每请求重复刷屏）
"""
from __future__ import annotations

from app.models.domain import Notification, now_utc
from app.repositories.base import Repository


class NotificationService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    # ── 创建 ──

    def create(
        self,
        type: str,
        title: str,
        content: str,
        level: str = "info",
        related_type: str | None = None,
        related_id: str | None = None,
    ) -> Notification:
        """创建一条通知"""
        notification = Notification(
            type=type,
            title=title,
            content=content,
            level=level,
            related_type=related_type,
            related_id=related_id,
        )
        return self._repo.create_notification(notification)

    # ── 查询 ──

    def list(self, unread_only: bool = False, limit: int = 50) -> list[Notification]:
        return self._repo.list_notifications(unread_only=unread_only, limit=limit, offset=0)

    def unread_count(self) -> int:
        return self._repo.get_unread_count()

    # ── 已读 ──

    def mark_read(self, notification_id: str) -> None:
        self._repo.mark_notification_read(notification_id)

    def mark_all_read(self) -> None:
        self._repo.mark_all_read()

    # ── 跟进到期/逾期自动巡检（请求时触发） ──

    def check_followup_due(self) -> None:
        """扫描未完成跟进，为到期/逾期项自动建通知。

        按 (related_id, type) 去重：同一条跟进的逾期通知只建一次。
        """
        try:
            followups = self._repo.list_followups(done=False)
        except Exception:
            return

        # 已存在的「跟进类」通知集合：(related_id, type)
        existing = self._repo.list_notifications(limit=500)
        notified = {
            (n.related_id, n.type)
            for n in existing
            if n.related_type == "followup" and n.related_id
        }

        for fu in followups:
            if fu.overdue and not fu.done:
                key = (fu.id, "followup_overdue")
                if key in notified:
                    continue
                self.create(
                    type="followup_overdue",
                    title=f"跟进逾期 · {fu.customer_name or '未知客户'}",
                    content=f"{fu.type or '跟进'}：{fu.text or ''}（{fu.due or '已逾期'}）",
                    level="error",
                    related_type="followup",
                    related_id=fu.id,
                )
            elif (not fu.overdue) and fu.due and "今天" in fu.due and not fu.done:
                key = (fu.id, "followup_due")
                if key in notified:
                    continue
                self.create(
                    type="followup_due",
                    title=f"今日跟进 · {fu.customer_name or '未知客户'}",
                    content=f"{fu.type or '跟进'}：{fu.text or ''}（{fu.due}）",
                    level="warning",
                    related_type="followup",
                    related_id=fu.id,
                )
