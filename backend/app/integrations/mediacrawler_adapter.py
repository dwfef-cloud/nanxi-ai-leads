from __future__ import annotations

from dataclasses import dataclass

from app.schemas.lead import LeadCreate


@dataclass(slots=True)
class MediaCrawlerLead:
    nickname: str
    source_url: str
    source_keyword: str
    platform: str = "douyin"
    external_id: str = ""
    tags: list[str] | None = None
    note: str = ""

    def to_lead_create(self) -> LeadCreate:
        return LeadCreate(
            nickname=self.nickname,
            source_url=self.source_url,
            source_keyword=self.source_keyword,
            platform=self.platform,
            external_id=self.external_id,
            tags=self.tags or [],
            note=self.note,
        )


def adapt_media_crawler_payload(payload: dict) -> LeadCreate:
    return MediaCrawlerLead(
        nickname=payload.get("nickname", ""),
        source_url=payload.get("source_url", ""),
        source_keyword=payload.get("source_keyword", ""),
        platform=payload.get("platform", "douyin"),
        external_id=payload.get("external_id", ""),
        tags=list(payload.get("tags", []) or []),
        note=payload.get("note", ""),
    ).to_lead_create()
