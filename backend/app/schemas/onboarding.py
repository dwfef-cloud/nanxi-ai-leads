"""
上手向导 Schema · 5 步配置聚合
"""
from pydantic import BaseModel

from app.schemas.business import BusinessProfileUpdate
from app.schemas.workbench import (
    AudienceProfileUpdate, ProductKnowledgeUpdate,
    ScriptStrategyUpdate, WeChatSettingsUpdate,
)


class WizardPayload(BaseModel):
    """5 步上手向导聚合配置对象

    每一步对应一个配置表单，均为可选（用户可能只完成部分步骤）。
    """
    business: BusinessProfileUpdate | None = None    # 第1步：业务画像
    product: ProductKnowledgeUpdate | None = None     # 第2步：产品知识库
    audience: AudienceProfileUpdate | None = None     # 第3步：目标客户
    scripts: ScriptStrategyUpdate | None = None       # 第4步：话术策略
    wechat: WeChatSettingsUpdate | None = None        # 第5步：微信转化设置


class WizardResponse(BaseModel):
    ok: bool
    saved: list[str]
    message: str = ""
