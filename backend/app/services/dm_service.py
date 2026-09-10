"""私信队列 Service · 从 Lead 状态机派生"""
from app.models.domain import Lead
from app.repositories.base import Repository
from app.schemas.dm import DmQueueItem

# 私信队列包含的 5 种状态
_QUEUE_STATUSES = (
    "pending_outreach",
    "throttled",
    "send_failed",
    "sent",
    "replied",
)

# 各状态的详情文案模板
_DETAIL_MAP: dict[str, str] = {
    "pending_outreach": "计划今天 {time} 发送（随机间隔 3-8 分钟）",
    "throttled": "账号 R1 降速中，排队等待",
    "send_failed": "发送失败，可重试",
    "sent": "已发送，等待回复",
    "replied": "用户已回复，待人工跟进",
}


class DmService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def list_queue(self, status: str | None = None) -> list[DmQueueItem]:
        """私信发送队列（执行器视角）。

        从线索池派生：status 属于 pending_outreach/throttled/send_failed/sent/replied 的线索。
        支持按 status 过滤。
        """
        if status and status in _QUEUE_STATUSES:
            leads = self._repo.list_leads(status=status)
        else:
            # 全量：逐个状态查询后合并（Repository 不支持多状态 OR 查询）
            leads: list[Lead] = []
            for s in _QUEUE_STATUSES:
                leads.extend(self._repo.list_leads(status=s))

        return [self._to_queue_item(lead) for lead in leads]

    # ═══════════════════════════════════════════════════════
    # 内部转换
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _to_queue_item(lead: Lead) -> DmQueueItem:
        detail_template = _DETAIL_MAP.get(lead.status, "")
        if lead.status == "pending_outreach":
            # 生成一个计划发送时间（基于创建时间的小时+分钟，简单确定性）
            hour = 9 + (hash(lead.id) % 9)  # 9-17 点
            minute = hash(lead.id + "m") % 60
            detail = detail_template.format(time=f"{hour:02d}:{minute:02d}")
        else:
            detail = detail_template

        return DmQueueItem(
            lead_id=lead.id,
            nickname=lead.nickname,
            hue=lead.hue,
            account=lead.account,
            variant="A",  # 默认变体 A（Lead 模型无变体字段，执行器层后续接入）
            status=lead.status,
            detail=detail,
        )
