"""私信队列 Schema · v1.0 契约对齐"""
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _Camel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


class DmQueueItem(_Camel):
    """私信队列项 · 执行器视角"""
    lead_id: str
    nickname: str
    hue: int
    account: str | None = None
    variant: str = "A"
    status: str  # pending_outreach / throttled / send_failed / sent / replied
    detail: str = ""
