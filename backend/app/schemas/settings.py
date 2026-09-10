"""
系统配置中心 · Pydantic Schema
================================
AI / 话术策略 / 合规规则 / 采集 / 通知 五类配置的请求与响应模型。

存储方式：system_settings 表（key-value，category 区分）。
复杂类型（list/bool/float）经 JSON 序列化存入 value 字段。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════
# AI 配置
# ═══════════════════════════════════════════════════════════

class AISettings(BaseModel):
    """AI 大模型配置"""
    provider: str = Field(default="doubao", description="提供商：doubao / dashscope")
    api_key: str = Field(default="", description="API Key（明文存储，读取时不脱敏）")
    model: str = Field(default="", description="模型名称/Endpoint ID")
    temperature: float = Field(default=0.7, ge=0, le=2, description="采样温度 0-2")
    timeout: int = Field(default=30, ge=1, le=120, description="请求超时（秒）")


class AISettingsUpdate(BaseModel):
    """AI 配置部分更新（所有字段可选）"""
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout: int | None = Field(default=None, ge=1, le=120)


# ═══════════════════════════════════════════════════════════
# 话术策略配置
# ═══════════════════════════════════════════════════════════

class StrategySettings(BaseModel):
    """话术策略与频控配置"""
    auto_score_threshold: float = Field(default=60.0, ge=0, le=100, description="自动评分阈值（≥此值自动标记高意向）")
    r2_switch_threshold: float = Field(default=8.0, ge=0, le=100, description="R2 话术切换转化率阈值（%）")
    daily_frequency_limit: int = Field(default=80, ge=1, le=500, description="单账号日频上限")


class StrategySettingsUpdate(BaseModel):
    auto_score_threshold: float | None = Field(default=None, ge=0, le=100)
    r2_switch_threshold: float | None = Field(default=None, ge=0, le=100)
    daily_frequency_limit: int | None = Field(default=None, ge=1, le=500)


# ═══════════════════════════════════════════════════════════
# 合规规则配置
# ═══════════════════════════════════════════════════════════

class ComplianceSettings(BaseModel):
    """合规规则 R1/R2/R3 阈值与开关"""
    r1_threshold: float = Field(default=80.0, description="R1 日频阈值")
    r1_enabled: bool = Field(default=True, description="R1 是否启用")
    r2_threshold: float = Field(default=8.0, description="R2 转化率阈值（%）")
    r2_enabled: bool = Field(default=True, description="R2 是否启用")
    r3_threshold: float = Field(default=3.0, description="R3 黑名单日增阈值（%）")
    r3_enabled: bool = Field(default=True, description="R3 是否启用")


class ComplianceSettingsUpdate(BaseModel):
    r1_threshold: float | None = None
    r1_enabled: bool | None = None
    r2_threshold: float | None = None
    r2_enabled: bool | None = None
    r3_threshold: float | None = None
    r3_enabled: bool | None = None


# ═══════════════════════════════════════════════════════════
# 采集配置
# ═══════════════════════════════════════════════════════════

class CrawlSettings(BaseModel):
    """采集任务默认配置"""
    default_keywords: list[str] = Field(default_factory=list, description="默认采集关键词")
    crawl_interval_minutes: int = Field(default=30, ge=1, le=1440, description="采集间隔（分钟）")
    max_comments_per_video: int = Field(default=100, ge=1, le=10000, description="单视频最大评论采集数")


class CrawlSettingsUpdate(BaseModel):
    default_keywords: list[str] | None = None
    crawl_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    max_comments_per_video: int | None = Field(default=None, ge=1, le=10000)


# ═══════════════════════════════════════════════════════════
# 通知配置
# ═══════════════════════════════════════════════════════════

class NotificationSettings(BaseModel):
    """通知提醒开关"""
    new_dm_alert: bool = Field(default=True, description="新私信提醒")
    followup_reminder: bool = Field(default=True, description="跟进提醒")
    deal_alert: bool = Field(default=True, description="成交提醒")


class NotificationSettingsUpdate(BaseModel):
    new_dm_alert: bool | None = None
    followup_reminder: bool | None = None
    deal_alert: bool | None = None


# ═══════════════════════════════════════════════════════════
# 组合模型
# ═══════════════════════════════════════════════════════════

class AllSettings(BaseModel):
    """全部配置组合"""
    ai: AISettings = Field(default_factory=AISettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    compliance: ComplianceSettings = Field(default_factory=ComplianceSettings)
    crawl: CrawlSettings = Field(default_factory=CrawlSettings)
    notification: NotificationSettings = Field(default_factory=NotificationSettings)


# ═══════════════════════════════════════════════════════════
# AI 连接测试
# ═══════════════════════════════════════════════════════════

class TestAIRequest(BaseModel):
    """测试 AI 连接请求"""
    provider: str = Field(default="doubao", description="提供商")
    api_key: str = Field(default="", description="API Key")
    model: str = Field(default="", description="模型名称")
    timeout: int = Field(default=10, ge=1, le=120, description="超时（秒）")


class TestAIResponse(BaseModel):
    """测试 AI 连接响应"""
    success: bool
    message: str
    latency_ms: int = Field(default=0)
