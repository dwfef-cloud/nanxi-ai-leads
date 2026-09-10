"""今日跟进 · Service 层

依赖 Repository 接口，不感知具体实现。
三段式待办：overdue（逾期）/ today（今天）/ upcoming（以后）。
"""
from app.models.domain import FollowUp
from app.repositories.base import Repository
from app.schemas.followup import FollowUpCreate


class FollowUpService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def list_followups(self, date: str | None = None) -> list[FollowUp]:
        """跟进待办列表

        date: 'overdue' / 'today' / 'upcoming' / None(全部)
        """
        return self._repo.list_followups(date=date)

    def create_followup(self, payload: FollowUpCreate) -> FollowUp:
        """新建跟进待办"""
        followup = FollowUp(
            customer_id=payload.customer_id,
            customer_name=payload.customer_name,
            type=payload.type,
            text=payload.text,
            due=payload.due,
            overdue=payload.overdue,
            done=False,
        )
        return self._repo.save_followup(followup)

    def complete_followup(self, followup_id: str, note: str | None = None) -> FollowUp:
        """标记完成（记录完成备注）"""
        followup = self._repo.get_followup(followup_id)
        followup.done = True
        if note:
            followup.result = note
        return self._repo.save_followup(followup)
