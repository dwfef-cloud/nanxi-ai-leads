from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from app.core.dependencies import get_repository
from app.models.domain import Lead, Task, LeadBehaviorEvent
from app.repositories.memory import MemoryRepository

router = APIRouter(prefix="/demo", tags=["demo"])


@router.post("/seed-report-data")
def seed_report_data(repo: MemoryRepository = Depends(get_repository)) -> dict:
    """Seed realistic seven-day data so the daily report can be reviewed end to end."""
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    samples = [
        ("深圳", "new", "C", 28, 0, "装修预算还没定，先了解一下", "全屋装修"),
        ("广州", "qualified", "B", 52, 0, "想看整屋设计案例和报价", "全屋装修"),
        ("深圳", "contacted", "B", 58, 0, "已经加联系方式，等方案", "旧房改造"),
        ("东莞", "replied", "A", 82, 0, "今年准备装修，想了解交付周期", "全屋装修"),
        ("惠州", "follow_up", "A", 88, 0, "预算 20 万左右，想本周看方案", "装修报价"),
        ("深圳", "won", "A", 96, 168000, "已确认全屋装修方案", "全屋装修"),
        ("广州", "lost", "B", 46, 0, "预算暂时不匹配", "装修报价"),
        ("佛山", "qualified", "B", 60, 0, "咨询小户型收纳和施工", "小户型装修"),
        ("深圳", "replied", "A", 78, 0, "想比较两套设计方案", "全屋装修"),
        ("中山", "follow_up", "A", 84, 0, "等家人确认后签约", "旧房改造"),
        ("广州", "won", "A", 94, 98000, "已签订旧房改造合同", "旧房改造"),
        ("东莞", "new", "C", 34, 0, "先收藏，后续再咨询", "装修灵感"),
    ]
    regions = ["深圳", "广州", "东莞", "惠州", "佛山", "中山"]
    keywords = ["全屋装修", "旧房改造", "装修报价", "小户型装修", "装修设计"]
    statuses = [("new", "C", 32, 0), ("qualified", "B", 54, 0), ("contacted", "B", 62, 0),
                ("replied", "A", 78, 0), ("follow_up", "A", 86, 0), ("won", "A", 96, 120000),
                ("lost", "B", 44, 0)]
    for offset in range(30):
        status, intent, score, amount = statuses[offset % len(statuses)]
        samples.append((regions[offset % len(regions)], status, intent, score, amount,
                         ["想了解预算和交付周期", "正在比较服务方案", "希望尽快安排沟通"][offset % 3],
                         keywords[offset % len(keywords)]))

    created = 0
    for index, (region, status, intent, score, amount, note, keyword) in enumerate(samples):
        external_id = f"report-demo-{index + 1}"
        if repo.find_lead_by_external("douyin", external_id):
            continue
        lead = Lead(
            nickname=f"日报客户{index + 1}",
            source_url=f"https://www.douyin.com/video/report-{index + 1}",
            source_keyword=keyword,
            platform="douyin",
            region=region,
            score=score,
            intent_level=intent,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            tags=["日报演示", keyword],
            note=note,
            customer_need=note,
            conversion_amount=amount,
            external_id=external_id,
            created_at=today - timedelta(days=(index % 30)),
        )
        repo.save_lead(lead)
        created += 1

    existing_event_leads = {event.lead_id for event in repo.list_behavior_events()}
    for lead in repo.list_leads():
        if lead.id in existing_event_leads:
            continue
        events = ["comment"]
        if lead.status in {"replied", "follow_up", "won"}:
            events.append("reply")
        if lead.status in {"follow_up", "won"}:
            events.append("follow_up")
        if lead.status == "won":
            events.append("won")
        for event_type in events:
            repo.save_behavior_event(LeadBehaviorEvent(lead_id=lead.id, event_type=event_type, content=lead.note, value=lead.conversion_amount if event_type == "won" else 0, created_at=lead.created_at))

    if not any(task.payload.get("demo_report") for task in repo.list_tasks()):
        repo.save_task(Task(
            task_type="media_crawler_live",
            payload={"demo_report": True, "keyword": "全屋装修、旧房改造、装修报价", "region": "广东"},
            status="done",
            created_at=today - timedelta(days=6),
        ))
    return {"created_leads": created, "total_leads": len(repo.list_leads()), "message": "日报演示数据已准备"}
