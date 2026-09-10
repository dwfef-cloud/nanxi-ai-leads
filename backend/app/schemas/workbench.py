from datetime import datetime

from pydantic import BaseModel


class ProductKnowledgeUpdate(BaseModel):
    product_name: str = ""
    description: str = ""
    selling_points: str = ""
    target_customers: str = ""
    price_range: str = ""
    faq: str = ""
    forbidden_claims: str = ""


class ProductKnowledgeRead(ProductKnowledgeUpdate):
    id: str
    updated_at: datetime
    model_config = {"from_attributes": True}


class AudienceProfileUpdate(BaseModel):
    name: str = ""
    industry: str = ""
    region: str = ""
    needs: str = ""
    pain_points: str = ""
    intent_keywords: str = ""
    excluded_keywords: str = ""


class AudienceProfileRead(AudienceProfileUpdate):
    id: str
    updated_at: datetime
    model_config = {"from_attributes": True}


class ScriptStrategyUpdate(BaseModel):
    comment_script: str = ""
    private_message_script: str = ""
    wechat_script: str = ""
    objection_script: str = ""


class ScriptStrategyRead(ScriptStrategyUpdate):
    id: str
    updated_at: datetime
    model_config = {"from_attributes": True}


class WeChatSettingsUpdate(BaseModel):
    wechat_id: str = ""
    guide_timing: str = "客户明确表达兴趣后"
    guide_reason: str = "发送详细方案和案例"
    compliance_note: str = ""


class WeChatSettingsRead(WeChatSettingsUpdate):
    id: str
    updated_at: datetime
    model_config = {"from_attributes": True}
