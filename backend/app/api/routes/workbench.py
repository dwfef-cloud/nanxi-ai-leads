from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.dependencies import get_repository, get_workbench_service
from app.models.domain import AudienceProfile, ProductKnowledge, ScriptStrategy, WeChatSettings
from app.schemas.analytics import WorkbenchResponse
from app.schemas.workbench import (
    AudienceProfileRead, AudienceProfileUpdate, ProductKnowledgeRead,
    ProductKnowledgeUpdate, ScriptStrategyRead, ScriptStrategyUpdate,
    WeChatSettingsRead, WeChatSettingsUpdate,
)
from app.services.workbench_service import WorkbenchService

router = APIRouter(prefix="/workbench", tags=["workbench"])


def stamp(payload: BaseModel) -> dict:
    return {**payload.model_dump(), "updated_at": datetime.now(timezone.utc)}


# ═══════════════════════════════════════════════════════════
# 工作台聚合（今日总览 + 预警 + 活动）
# ═══════════════════════════════════════════════════════════

@router.get("", response_model=WorkbenchResponse)
def get_workbench(
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    """今日运营总览（北极星导向）+ 预警 + 最近活动"""
    return service.get_workbench()


# ═══════════════════════════════════════════════════════════
# 工作台配置（已有，保留）
# ═══════════════════════════════════════════════════════════

@router.get("/product", response_model=ProductKnowledgeRead)
def get_product(repo=Depends(get_repository)):
    return repo.get_product_knowledge()


@router.put("/product", response_model=ProductKnowledgeRead)
def update_product(payload: ProductKnowledgeUpdate, repo=Depends(get_repository)):
    return repo.save_product_knowledge(ProductKnowledge(**stamp(payload)))


@router.get("/audience", response_model=AudienceProfileRead)
def get_audience(repo=Depends(get_repository)):
    return repo.get_audience_profile()


@router.put("/audience", response_model=AudienceProfileRead)
def update_audience(payload: AudienceProfileUpdate, repo=Depends(get_repository)):
    return repo.save_audience_profile(AudienceProfile(**stamp(payload)))


@router.get("/scripts", response_model=ScriptStrategyRead)
def get_scripts(repo=Depends(get_repository)):
    return repo.get_script_strategy()


@router.put("/scripts", response_model=ScriptStrategyRead)
def update_scripts(payload: ScriptStrategyUpdate, repo=Depends(get_repository)):
    return repo.save_script_strategy(ScriptStrategy(**stamp(payload)))


@router.get("/wechat", response_model=WeChatSettingsRead)
def get_wechat(repo=Depends(get_repository)):
    return repo.get_wechat_settings()


@router.put("/wechat", response_model=WeChatSettingsRead)
def update_wechat(payload: WeChatSettingsUpdate, repo=Depends(get_repository)):
    return repo.save_wechat_settings(WeChatSettings(**stamp(payload)))
