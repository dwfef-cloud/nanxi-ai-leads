"""
OnboardingService · 上手向导业务逻辑
=======================================
5 步配置聚合提交，保存到对应配置表：
  BusinessProfile / ProductKnowledge / AudienceProfile / ScriptStrategy / WeChatSettings

每步配置独立保存，返回成功保存的步骤列表。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.domain import (
    AudienceProfile, BusinessProfile, ProductKnowledge,
    ScriptStrategy, WeChatSettings,
)
from app.repositories.base import Repository
from app.schemas.onboarding import WizardPayload


class OnboardingService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def submit_wizard(self, payload: WizardPayload) -> dict:
        """提交上手向导 5 步配置

        逐步骤保存到对应配置表（单例 upsert），返回已保存的步骤名列表。
        """
        saved: list[str] = []
        now = datetime.now(timezone.utc)

        # 第1步：业务画像
        if payload.business is not None:
            profile = self._repo.get_business_profile()
            for field, value in payload.business.model_dump().items():
                setattr(profile, field, value)
            profile.updated_at = now
            self._repo.save_business_profile(profile)
            saved.append("business")

        # 第2步：产品知识库
        if payload.product is not None:
            pk = self._repo.get_product_knowledge()
            for field, value in payload.product.model_dump().items():
                setattr(pk, field, value)
            pk.updated_at = now
            self._repo.save_product_knowledge(pk)
            saved.append("product")

        # 第3步：目标客户
        if payload.audience is not None:
            ap = self._repo.get_audience_profile()
            for field, value in payload.audience.model_dump().items():
                setattr(ap, field, value)
            ap.updated_at = now
            self._repo.save_audience_profile(ap)
            saved.append("audience")

        # 第4步：话术策略
        if payload.scripts is not None:
            ss = self._repo.get_script_strategy()
            for field, value in payload.scripts.model_dump().items():
                setattr(ss, field, value)
            ss.updated_at = now
            self._repo.save_script_strategy(ss)
            saved.append("scripts")

        # 第5步：微信转化设置
        if payload.wechat is not None:
            ws = self._repo.get_wechat_settings()
            for field, value in payload.wechat.model_dump().items():
                setattr(ws, field, value)
            ws.updated_at = now
            self._repo.save_wechat_settings(ws)
            saved.append("wechat")

        return {
            "ok": True,
            "saved": saved,
            "message": f"已保存 {len(saved)} 步配置" if saved else "未提交任何配置",
        }
