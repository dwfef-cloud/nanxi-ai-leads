from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_repository
from app.repositories.memory import MemoryRepository

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _pareto(values: dict[str, int]) -> list[dict[str, int | str]]:
    total = sum(values.values()) or 1
    accumulated = 0
    result = []
    for name, count in sorted(values.items(), key=lambda item: item[1], reverse=True):
        accumulated += count
        result.append({"name": name, "count": count, "cumulative_rate": round(accumulated / total * 100)})
    return result


@router.get("/report")
def report(days: int = Query(30, ge=7, le=90), repo: MemoryRepository = Depends(get_repository)) -> dict:
    leads = repo.list_leads()
    events = repo.list_behavior_events()
    event_counts = defaultdict(int)
    for event in events:
        event_counts[event.event_type] += 1
    source: dict[str, dict[str, int | float]] = defaultdict(lambda: {"leads": 0, "high_intent": 0, "won": 0, "amount": 0})
    region: dict[str, dict[str, int]] = defaultdict(lambda: {"leads": 0, "high_intent": 0, "won": 0})
    daily: dict[str, int] = defaultdict(int)
    pain_points: dict[str, int] = defaultdict(int)
    for lead in leads:
        day = lead.created_at.astimezone(timezone.utc).date().isoformat()
        daily[day] += 1
        pain_points[lead.customer_need or lead.note or "未记录需求"] += 1
        item = source[lead.source_keyword or "未标记"]
        item["leads"] += 1
        item["high_intent"] += lead.intent_level == "A"
        item["won"] += lead.status == "won"
        item["amount"] += lead.conversion_amount if lead.status == "won" else 0
        area = region[lead.region or "未填写"]
        area["leads"] += 1
        area["high_intent"] += lead.intent_level == "A"
        area["won"] += lead.status == "won"
    stages = {
        "leads": len(leads),
        "qualified": sum(lead.status in {"qualified", "contacted", "replied", "follow_up", "won"} for lead in leads),
        "replied": event_counts.get("reply", 0) or sum(lead.status in {"replied", "follow_up", "won"} for lead in leads),
        "follow_up": event_counts.get("follow_up", 0) or sum(lead.status in {"follow_up", "won"} for lead in leads),
        "won": event_counts.get("won", 0) or sum(lead.status == "won" for lead in leads),
    }
    events_by_lead: dict[str, list] = defaultdict(list)
    for event in events:
        events_by_lead[event.lead_id].append(event)
    rfm = [{
        "lead_id": lead.id,
        "nickname": lead.nickname,
        "recency": 3 if any(event.event_type in {"reply", "follow_up", "won"} for event in events_by_lead[lead.id]) else 1,
        "frequency": 3 if len(events_by_lead[lead.id]) >= 3 else 2 if len(events_by_lead[lead.id]) >= 2 else 1,
        "monetary": 3 if sum(event.value for event in events_by_lead[lead.id]) > 0 or lead.conversion_amount else 2 if lead.intent_level == "A" else 1,
        "segment": "高价值客户" if lead.status == "won" else "高潜待推进" if lead.intent_level == "A" else "持续培育" if lead.intent_level == "B" else "待激活",
    } for lead in sorted(leads, key=lambda item: (item.intent_level, item.conversion_amount), reverse=True)]
    def emotion_for(lead) -> str:
        text = f"{lead.note} {lead.customer_need}".lower()
        if any(token in text for token in ["想", "预算", "报价", "准备", "尽快", "确认"]):
            return "积极明确"
        if any(token in text for token in ["比较", "了解", "看看", "考虑"]):
            return "观望比较"
        return "低意向" if lead.intent_level not in {"A", "B"} else "积极明确"

    emotions = {key: sum(emotion_for(item) == key for item in leads) for key in ("积极明确", "观望比较", "低意向")}
    cohorts = []
    for day, size in sorted(daily.items()):
        members = [item for item in leads if item.created_at.astimezone(timezone.utc).date().isoformat() == day]
        member_events = {item.id: events_by_lead.get(item.id, []) for item in members}
        def retained_within(lead_id: str, limit: int) -> bool:
            events_for_lead = member_events[lead_id]
            return any(0 < (event.created_at - next(item.created_at for item in members if item.id == lead_id)).days <= limit for event in events_for_lead)
        cohorts.append({
            "date": day,
            "users": size,
            "d1": round(sum(retained_within(item.id, 1) for item in members) / size * 100) if events else round(sum(item.status != "new" for item in members) / size * 100) if size else 0,
            "d3": round(sum(retained_within(item.id, 3) for item in members) / size * 100) if events else round(sum(item.status in {"replied", "follow_up", "won"} for item in members) / size * 100) if size else 0,
            "d7": round(sum(retained_within(item.id, 7) for item in members) / size * 100) if events else round(sum(item.status == "won" for item in members) / size * 100) if size else 0,
            "basis": "基于用户行为事件" if events else "当前阶段代理留存",
        })
    daily_rows = [{"date": key, "leads": value} for key, value in sorted(daily.items())][-days:]
    cohort_rows = cohorts[-days:]
    top_source = max(source.items(), key=lambda item: item[1]["won"], default=("未标记", {"won": 0}))[0]
    top_pain = max(pain_points.items(), key=lambda item: item[1], default=("未记录需求", 0))[0]
    return {
        "generated_at": datetime.now(timezone.utc),
        "summary": {"leads": len(leads), "high_intent": sum(x.intent_level == "A" for x in leads), "won_amount": sum(x.conversion_amount for x in leads if x.status == "won")},
        "period_days": days,
        "daily": daily_rows,
        "funnel": stages,
        "sources": [{"name": key, **value, "high_intent_rate": round(value["high_intent"] / value["leads"] * 100) if value["leads"] else 0, "conversion_rate": round(value["won"] / value["leads"] * 100) if value["leads"] else 0} for key, value in sorted(source.items(), key=lambda item: item[1]["leads"], reverse=True)],
        "regions": [{"name": key, **value, "high_intent_rate": round(value["high_intent"] / value["leads"] * 100) if value["leads"] else 0, "conversion_rate": round(value["won"] / value["leads"] * 100) if value["leads"] else 0} for key, value in sorted(region.items(), key=lambda item: item[1]["leads"], reverse=True)],
        "rfm": rfm,
        "emotions": emotions,
        "emotion_basis": "基于用户原声关键词与意向等级兜底",
        "pain_points": _pareto(pain_points),
        "cohorts": cohort_rows,
        "insights": [
            f"当前有 {sum(item.intent_level == 'A' for item in leads)} 位 A 级客户需要优先跟进。",
            f"成交表现最好的来源是“{top_source}”，建议继续观察其转化质量。",
            f"用户反馈中出现最多的需求是“{top_pain}”，可用于下一轮内容和话术优化。",
        ],
        "path": [
            {"from": "内容线索", "to": "评论互动", "value": len(leads)},
            {"from": "评论互动", "to": "私信回复", "value": stages["replied"]},
            {"from": "私信回复", "to": "跟进", "value": stages["follow_up"]},
            {"from": "跟进", "to": "成交", "value": stages["won"]},
        ],
        "event_counts": dict(event_counts),
    }
