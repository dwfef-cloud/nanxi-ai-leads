from datetime import datetime

from pydantic import BaseModel


class ConversationCreate(BaseModel):
    lead_id: str
    account_name: str = "default"


class ConversationUpdate(BaseModel):
    status: str | None = None


class ConversationRead(BaseModel):
    id: str
    lead_id: str
    account_name: str
    status: str
    last_message_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessageCreate(BaseModel):
    conversation_id: str
    sender: str
    content: str
    is_ai_generated: bool = False


class DraftCreate(BaseModel):
    lead_nickname: str
    source_keyword: str
    user_note: str = ""


class MessageRead(BaseModel):
    id: str
    conversation_id: str
    sender: str
    content: str
    is_ai_generated: bool
    created_at: datetime

    model_config = {"from_attributes": True}
