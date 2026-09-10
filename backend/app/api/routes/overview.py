from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.core.dependencies import get_repository

router = APIRouter(prefix="/overview", tags=["overview"])


@router.get("")
def overview(repo=Depends(get_repository)) -> dict[str, int]:
    summary = repo.summary()
    now = datetime.now(timezone.utc)
    leads = repo.list_leads()
    summary.update(
        {
            "new_leads": sum(lead.status == "new" for lead in leads),
            "high_intent": sum(lead.intent_level == "A" for lead in leads),
            "qualified": sum(lead.intent_level in {"A", "B"} for lead in leads),
            "contacted": sum(lead.status in {"contacted", "replied", "follow_up", "won"} for lead in leads),
            "pending_followups": sum(
                lead.next_followup_at is not None and lead.next_followup_at <= now
                for lead in leads
            ),
            "won": sum(lead.status == "won" for lead in leads),
            "won_amount": sum(lead.conversion_amount for lead in leads if lead.status == "won"),
        }
    )
    return summary
