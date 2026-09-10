from datetime import datetime

from pydantic import BaseModel


class BusinessProfileUpdate(BaseModel):
    industry: str = ""
    product: str = ""
    service_area: str = ""
    target_customer: str = ""
    price_range: str = ""
    conversion_goal: str = "添加微信"
    tone: str = "专业、真诚"


class BusinessProfileRead(BusinessProfileUpdate):
    id: str
    updated_at: datetime

    model_config = {"from_attributes": True}
