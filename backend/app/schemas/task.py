from datetime import datetime

from pydantic import BaseModel


class TaskCreate(BaseModel):
    task_type: str
    payload: dict


class TaskUpdate(BaseModel):
    status: str | None = None
    error_message: str | None = None


class TaskRead(BaseModel):
    id: str
    task_type: str
    payload: dict
    status: str
    error_message: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
