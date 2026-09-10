"""系统配置中心路由 · AI / 策略 / 合规 / 采集 / 通知 五类配置的读写与测试"""
from fastapi import APIRouter, Depends

from app.core.dependencies import get_settings_service
from app.schemas.settings import (
    AISettings, AISettingsUpdate,
    AllSettings,
    ComplianceSettings, ComplianceSettingsUpdate,
    CrawlSettings, CrawlSettingsUpdate,
    NotificationSettings, NotificationSettingsUpdate,
    StrategySettings, StrategySettingsUpdate,
    TestAIRequest, TestAIResponse,
)
from app.services.settings_service import SettingsService

router = APIRouter(prefix="/settings", tags=["settings"])


# ═══════════════════════════════════════════════════════════
# 全部配置
# ═══════════════════════════════════════════════════════════

@router.get("", response_model=AllSettings)
def get_all_settings(service: SettingsService = Depends(get_settings_service)) -> AllSettings:
    """获取所有分类配置"""
    return service.get_all_settings()


@router.put("", response_model=AllSettings)
def update_settings(
    payload: dict,
    service: SettingsService = Depends(get_settings_service),
) -> AllSettings:
    """部分更新配置。body 为 JSON，只传需要改的字段。

    结构示例：{"ai": {"temperature": 0.8}, "strategy": {"daily_frequency_limit": 60}}
    """
    return service.update_settings(payload)


# ═══════════════════════════════════════════════════════════
# AI 配置
# ═══════════════════════════════════════════════════════════

@router.get("/ai", response_model=AISettings)
def get_ai_settings(service: SettingsService = Depends(get_settings_service)) -> AISettings:
    """获取 AI 配置"""
    return service.get_ai_settings()


@router.put("/ai", response_model=AISettings)
def update_ai_settings(
    payload: AISettingsUpdate,
    service: SettingsService = Depends(get_settings_service),
) -> AISettings:
    """更新 AI 配置（保存后热更新生效）"""
    return service.update_ai_settings(payload)


# ═══════════════════════════════════════════════════════════
# 话术策略配置
# ═══════════════════════════════════════════════════════════

@router.get("/strategy", response_model=StrategySettings)
def get_strategy_settings(service: SettingsService = Depends(get_settings_service)) -> StrategySettings:
    """获取话术策略配置"""
    return service.get_strategy_settings()


@router.put("/strategy", response_model=StrategySettings)
def update_strategy_settings(
    payload: StrategySettingsUpdate,
    service: SettingsService = Depends(get_settings_service),
) -> StrategySettings:
    """更新话术策略配置"""
    return service.update_strategy_settings(payload)


# ═══════════════════════════════════════════════════════════
# 合规规则配置
# ═══════════════════════════════════════════════════════════

@router.get("/compliance", response_model=ComplianceSettings)
def get_compliance_settings(service: SettingsService = Depends(get_settings_service)) -> ComplianceSettings:
    """获取合规规则配置"""
    return service.get_compliance_settings()


@router.put("/compliance", response_model=ComplianceSettings)
def update_compliance_settings(
    payload: ComplianceSettingsUpdate,
    service: SettingsService = Depends(get_settings_service),
) -> ComplianceSettings:
    """更新合规规则配置"""
    return service.update_compliance_settings(payload)


# ═══════════════════════════════════════════════════════════
# 采集配置
# ═══════════════════════════════════════════════════════════

@router.get("/crawl", response_model=CrawlSettings)
def get_crawl_settings(service: SettingsService = Depends(get_settings_service)) -> CrawlSettings:
    """获取采集配置"""
    return service.get_crawl_settings()


@router.put("/crawl", response_model=CrawlSettings)
def update_crawl_settings(
    payload: CrawlSettingsUpdate,
    service: SettingsService = Depends(get_settings_service),
) -> CrawlSettings:
    """更新采集配置"""
    return service.update_crawl_settings(payload)


# ═══════════════════════════════════════════════════════════
# 通知配置
# ═══════════════════════════════════════════════════════════

@router.get("/notification", response_model=NotificationSettings)
def get_notification_settings(service: SettingsService = Depends(get_settings_service)) -> NotificationSettings:
    """获取通知配置"""
    return service.get_notification_settings()


@router.put("/notification", response_model=NotificationSettings)
def update_notification_settings(
    payload: NotificationSettingsUpdate,
    service: SettingsService = Depends(get_settings_service),
) -> NotificationSettings:
    """更新通知配置"""
    return service.update_notification_settings(payload)


# ═══════════════════════════════════════════════════════════
# AI 连接测试
# ═══════════════════════════════════════════════════════════

@router.post("/test-ai", response_model=TestAIResponse)
def test_ai_connection(
    payload: TestAIRequest,
    service: SettingsService = Depends(get_settings_service),
) -> TestAIResponse:
    """测试 AI 连接（验证 API Key 有效性，返回成功/失败/延迟）"""
    return service.test_ai_connection(payload)
