from datetime import datetime, timezone

from app.models.domain import Lead
from app.repositories.base import Repository
from app.schemas.lead import LeadCreate, LeadUpdate
from app.integrations.mediacrawler_adapter import adapt_media_crawler_payload


class LeadService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._notification_service = None  # P4-A 注入

    def set_notification_service(self, svc) -> None:
        """注入通知服务（P4-A），用于加微自动提醒"""
        self._notification_service = svc

    def list_leads(self) -> list[Lead]:
        return self._repo.list_leads()

    def filter_leads(
        self,
        status: str | None = None,
        source: str | None = None,
        min_score: int | None = None,
        keyword: str | None = None,
    ) -> list[Lead]:
        return self._repo.list_leads(status=status, source=source, min_score=min_score, keyword=keyword)

    def get_lead(self, lead_id: str) -> Lead:
        return self._repo.get_lead(lead_id)

    def create_lead(self, payload: LeadCreate) -> Lead:
        if payload.external_id:
            existing = self._repo.find_lead_by_external(payload.platform, payload.external_id)
            if existing is not None:
                return existing
        lead = Lead(
            nickname=payload.nickname,
            source=payload.source,
            comment=payload.comment,
            video=payload.video,
            source_url=payload.source_url,
            source_keyword=payload.source_keyword,
            referral_note=payload.referral_note,
            platform=payload.platform,
            region=payload.region,
            tags=payload.tags,
            note=payload.note,
            customer_need=payload.customer_need,
            external_id=payload.external_id,
            hue=payload.hue,
            account=payload.account,
        )
        lead.score = self._score_lead(lead)
        return self._repo.save_lead(lead)

    def update_lead(self, lead_id: str, payload: LeadUpdate) -> Lead:
        lead = self._repo.get_lead(lead_id)
        # ── 基础 ──
        if payload.nickname is not None:
            lead.nickname = payload.nickname
        if payload.region is not None:
            lead.region = payload.region
        if payload.note is not None:
            lead.note = payload.note
        if payload.customer_need is not None:
            lead.customer_need = payload.customer_need
        if payload.tags is not None:
            lead.tags = payload.tags
        # ── 意向与评分 ──
        if payload.score is not None:
            lead.score = payload.score
        if payload.intent_level is not None:
            lead.intent_level = payload.intent_level  # type: ignore[assignment]
        if payload.intent is not None:
            lead.intent = payload.intent  # type: ignore[assignment]
        # ── 状态机 ──
        if payload.status is not None:
            lead.status = payload.status  # type: ignore[assignment]
        if payload.account is not None:
            lead.account = payload.account
        if payload.throttled_note is not None:
            lead.throttled_note = payload.throttled_note
        if payload.fail_note is not None:
            lead.fail_note = payload.fail_note
        if payload.reject_reason is not None:
            lead.reject_reason = payload.reject_reason
        # ── 转化里程碑 ──
        if payload.wechat_added_at is not None:
            lead.wechat_added_at = payload.wechat_added_at
        if payload.manual is not None:
            lead.manual = payload.manual
        if payload.deal_amount is not None:
            lead.deal_amount = payload.deal_amount
        if payload.deal_at is not None:
            lead.deal_at = payload.deal_at
        # ── 跟进 ──
        if payload.next_followup_at is not None:
            lead.next_followup_at = payload.next_followup_at
        if payload.lost_reason is not None:
            lead.lost_reason = payload.lost_reason
        if payload.conversion_amount is not None:
            lead.conversion_amount = payload.conversion_amount
        lead.updated_at = datetime.now(timezone.utc)
        return self._repo.save_lead(lead)

    def mark_wechat_added(self, lead_id: str, manual: bool = True) -> Lead:
        """标记加微（IMP-002 降级入口）"""
        lead = self._repo.get_lead(lead_id)
        lead.status = "wechat_added"  # type: ignore[assignment]
        lead.wechat_added_at = datetime.now(timezone.utc)
        lead.manual = manual
        lead.updated_at = datetime.now(timezone.utc)
        saved = self._repo.save_lead(lead)

        # P4-A：加微通知
        if self._notification_service:
            self._notification_service.create(
                type="wechat_added",
                title=f"加微成功 · {lead.nickname}",
                content="线索已添加企业微信，可进入客户培育 SOP。",
                level="success",
                related_type="lead",
                related_id=lead.id,
            )
        return saved

    def import_external_leads(self, leads: list[LeadCreate]) -> list[Lead]:
        imported: list[Lead] = []
        for lead_payload in leads:
            imported.append(self.create_lead(lead_payload))
        return imported

    def import_media_crawler_payloads(self, payloads: list[dict]) -> list[Lead]:
        leads = [adapt_media_crawler_payload(payload) for payload in payloads]
        return self.import_external_leads(leads)

    def import_media_crawler_text(self, content: str) -> list[Lead]:
        import json

        text = content.strip()
        if not text:
            return []
        if text.startswith("["):
            payloads = json.loads(text)
        else:
            payloads = [json.loads(line) for line in text.splitlines() if line.strip()]
        return self.import_media_crawler_payloads(payloads)

    def _score_lead(self, lead: Lead) -> int:
        score = 0
        keywords = f"{lead.nickname} {lead.comment} {lead.source_keyword} {lead.note}".lower()
        if any(token in keywords for token in ["多少钱", "价格", "报价", "怎么做", "联系", "推荐", "求", "私"]):
            score += 30
        if any(token in keywords for token in ["装修", "获客", "投放", "咨询", "合作", "翻新", "改造", "定制"]):
            score += 20
        if lead.source == "inbound_dm":
            score += 25  # 主动私信意向最高
        if lead.source == "referral":
            score += 20  # 转介绍意向高
        score += min(len(lead.tags) * 5, 20)
        final_score = min(score, 100)
        lead.intent_level = "A" if final_score >= 70 else "B" if final_score >= 40 else "C"
        lead.intent = "high" if final_score >= 60 else "mid" if final_score >= 30 else "low"
        return final_score
