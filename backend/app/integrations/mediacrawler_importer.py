from __future__ import annotations

from app.schemas.lead import LeadCreate


def adapt_douyin_comments(
    records: list[dict], keyword: str, task_id: str,
    intent_keywords: str = "", excluded_keywords: str = "",
) -> list[LeadCreate]:
    intents = [item.strip().lower() for item in intent_keywords.replace("，", ",").split(",") if item.strip()]
    excluded = [item.strip().lower() for item in excluded_keywords.replace("，", ",").split(",") if item.strip()]
    leads: list[LeadCreate] = []
    seen: set[str] = set()
    for item in records:
        comment_id = str(item.get("comment_id") or "")
        content = str(item.get("content") or "").strip()
        if not comment_id or not content:
            continue
        normalized = content.lower()
        if comment_id in seen or any(word in normalized for word in excluded):
            continue
        if intents and not any(word in normalized for word in intents):
            continue
        seen.add(comment_id)
        aweme_id = str(item.get("aweme_id") or "")
        leads.append(
            LeadCreate(
                nickname=str(item.get("nickname") or "抖音用户"),
                source_url=f"https://www.douyin.com/video/{aweme_id}" if aweme_id else "https://www.douyin.com",
                source_keyword=keyword,
                platform="douyin",
                external_id=f"douyin-comment-{comment_id}",
                tags=["mediacrawler", "douyin-comment", task_id],
                note=content,
            )
        )
    return leads
