"""
上手向导路由 · 5 步配置聚合提交
==================================
POST /api/onboarding/wizard   提交上手向导配置，保存到对应配置表
"""
from fastapi import APIRouter, Depends

from app.core.dependencies import get_onboarding_service
from app.schemas.onboarding import WizardPayload, WizardResponse
from app.services.onboarding_service import OnboardingService

router = APIRouter(tags=["onboarding"])


@router.post("/onboarding/wizard", response_model=WizardResponse)
def submit_wizard(
    payload: WizardPayload,
    service: OnboardingService = Depends(get_onboarding_service),
) -> dict:
    """5 步上手向导聚合提交

    接收业务画像/产品知识库/目标客户/话术策略/微信设置的聚合对象，
    逐步骤保存到对应配置表（单例 upsert）。
    """
    return service.submit_wizard(payload)
