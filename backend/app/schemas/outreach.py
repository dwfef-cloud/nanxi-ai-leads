from pydantic import BaseModel


class OutreachPlanRead(BaseModel):
    lead_id: str
    platform: str
    stage: str
    requires_confirmation: bool
    steps: list[dict]


class OutreachExecuteRequest(BaseModel):
    step: str
    content: str
    confirmed: bool = False
