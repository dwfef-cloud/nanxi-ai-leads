"""客户与商机 · Service 层

依赖 Repository 接口，不感知具体实现。
阶段推进必须记录变更日志（谁、什么时间、从什么阶段到什么阶段）。
"""
from datetime import datetime, timezone

from app.models.domain import Customer, CustomerLog
from app.repositories.base import Repository
from app.schemas.customer import CustomerCreate

# 7 阶段标签（用于日志文案）
STAGE_LABELS: dict[str, str] = {
    "added": "已加微",
    "measured": "已量房",
    "proposal": "方案中",
    "quoted": "已报价",
    "negotiating": "谈判中",
    "won": "已成交",
    "lost": "已流失",
}


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M")


class CustomerService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._notification_service = None  # P4-A 注入

    def set_notification_service(self, svc) -> None:
        """注入通知服务（P4-A），用于成交自动提醒"""
        self._notification_service = svc

    # ── 查询 ──

    def list_customers(
        self,
        stage: str | None = None,
        keyword: str | None = None,
    ) -> list[Customer]:
        customers = self._repo.list_customers(stage=stage)
        if keyword:
            needle = keyword.lower()
            customers = [
                c for c in customers
                if needle in c.name.lower()
                or (c.referrer and needle in c.referrer.lower())
            ]
        return customers

    def get_customer(self, customer_id: str) -> Customer:
        return self._repo.get_customer(customer_id)

    # ── 创建（含线索→客户转化） ──

    def create_customer(self, payload: CustomerCreate) -> Customer:
        customer = Customer(
            name=payload.name,
            source=payload.source,
            lead_id=payload.lead_id,
            est_value=payload.est_value,
            wechat_added_at=payload.wechat_added_at or datetime.now(timezone.utc),
            manual=payload.manual,
            referrer=payload.referrer,
            hue=payload.hue,
        )
        customer = self._repo.save_customer(customer)

        # 记录创建日志
        self._add_log(customer.id, "客户创建", "系统")

        # 线索→客户转化：关联 lead 后自动标记线索为已加微（若尚未标记）
        if payload.lead_id:
            try:
                lead = self._repo.get_lead(payload.lead_id)
                if lead.status != "wechat_added" and lead.status != "deal_won":
                    lead.status = "wechat_added"  # type: ignore[assignment]
                    lead.wechat_added_at = lead.wechat_added_at or datetime.now(timezone.utc)
                    lead.updated_at = datetime.now(timezone.utc)
                    self._repo.save_lead(lead)
                    self._add_log(customer.id, f"关联线索并标记已加微（线索ID: {payload.lead_id}）", "系统")
            except KeyError:
                self._add_log(customer.id, f"关联线索ID不存在: {payload.lead_id}", "系统")

        return customer

    # ── 阶段推进（记录变更日志） ──

    def advance_stage(self, customer_id: str, stage: str) -> Customer:
        customer = self._repo.get_customer(customer_id)
        old_stage = customer.stage
        customer.stage = stage  # type: ignore[assignment]
        customer.updated_at = datetime.now(timezone.utc)
        customer = self._repo.save_customer(customer)

        old_label = STAGE_LABELS.get(old_stage, old_stage)
        new_label = STAGE_LABELS.get(stage, stage)
        self._add_log(
            customer.id,
            f"阶段推进：{old_label} → {new_label}",
            "老板",
        )
        return customer

    # ── 成交录入（≤3次点击） ──

    def record_deal(self, customer_id: str, amount: float) -> Customer:
        customer = self._repo.get_customer(customer_id)
        customer.deal_amount = amount
        customer.deal_at = datetime.now(timezone.utc)
        customer.stage = "won"  # type: ignore[assignment]
        customer.updated_at = datetime.now(timezone.utc)
        customer = self._repo.save_customer(customer)

        self._add_log(customer.id, f"成交录入：¥{amount:,.0f}", "老板")

        # 同步更新关联线索的成交状态
        if customer.lead_id:
            try:
                lead = self._repo.get_lead(customer.lead_id)
                lead.status = "deal_won"  # type: ignore[assignment]
                lead.deal_amount = amount
                lead.deal_at = datetime.now(timezone.utc)
                lead.updated_at = datetime.now(timezone.utc)
                self._repo.save_lead(lead)
            except KeyError:
                pass

        # P4-A：成交通知
        if self._notification_service:
            self._notification_service.create(
                type="deal_won",
                title=f"成交喜报 · {customer.name}",
                content=f"成交金额 ¥{amount:,.0f}，恭喜开单！",
                level="success",
                related_type="customer",
                related_id=customer.id,
            )

        return customer

    # ── 流失标记 ──

    def mark_lost(self, customer_id: str, reason: str) -> Customer:
        customer = self._repo.get_customer(customer_id)
        customer.stage = "lost"  # type: ignore[assignment]
        customer.lost_reason = reason
        customer.lost_at = datetime.now(timezone.utc)
        customer.updated_at = datetime.now(timezone.utc)
        customer = self._repo.save_customer(customer)

        self._add_log(customer.id, f"标记流失：{reason}", "老板")
        return customer

    # ── 跟进日志 ──

    def list_logs(self, customer_id: str) -> list[CustomerLog]:
        return self._repo.list_customer_logs(customer_id)

    # ── 内部工具 ──

    def _add_log(self, customer_id: str, text: str, by: str = "系统") -> CustomerLog:
        log = CustomerLog(
            customer_id=customer_id,
            text=text,
            time=_now_str(),
            by=by,
        )
        return self._repo.save_customer_log(log)
