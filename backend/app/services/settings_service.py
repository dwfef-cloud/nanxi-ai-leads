"""
SettingsService · 系统配置中心业务逻辑
========================================
AI / 话术策略 / 合规规则 / 采集 / 通知 五类配置的读写、热更新与连接测试。

存储：system_settings 表（key-value，category 区分）。
复杂类型（list/bool/float）经 JSON 序列化存入 value 字段。
每次配置变更写 ComplianceEvent 审计日志（type=config_change）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models.domain import ComplianceEvent
from app.repositories.base import Repository
from app.schemas.settings import (
    AISettings, AISettingsUpdate,
    AllSettings,
    ComplianceSettings, ComplianceSettingsUpdate,
    CrawlSettings, CrawlSettingsUpdate,
    NotificationSettings, NotificationSettingsUpdate,
    StrategySettings, StrategySettingsUpdate,
    TestAIRequest, TestAIResponse,
)

_CST = timezone(timedelta(hours=8))

# 分类常量
CAT_AI = "ai"
CAT_STRATEGY = "strategy"
CAT_COMPLIANCE = "compliance"
CAT_CRAWL = "crawl"
CAT_NOTIFICATION = "notification"

# 各分类的字段名集合（用于审计日志描述）
_CATEGORY_FIELDS: dict[str, list[str]] = {
    CAT_AI: ["provider", "api_key", "model", "temperature", "timeout"],
    CAT_STRATEGY: ["auto_score_threshold", "r2_switch_threshold", "daily_frequency_limit"],
    CAT_COMPLIANCE: ["r1_threshold", "r1_enabled", "r2_threshold", "r2_enabled", "r3_threshold", "r3_enabled"],
    CAT_CRAWL: ["default_keywords", "crawl_interval_minutes", "max_comments_per_video"],
    CAT_NOTIFICATION: ["new_dm_alert", "followup_reminder", "deal_alert"],
}


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _fmt_display_time(dt: datetime) -> str:
    return dt.astimezone(_CST).strftime("%m-%d %H:%M")


class SettingsService:
    """系统配置中心服务"""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        # AI 服务引用（用于热更新，由 dependencies 注入后设置）
        self._ai_service = None

    def set_ai_service(self, ai_service) -> None:
        """注入 AIService，用于 AI 配置更新后热更新"""
        self._ai_service = ai_service

    # ═══════════════════════════════════════════════════════
    # 序列化 / 反序列化
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _serialize(value: Any) -> str:
        """将值序列化为字符串。str 直接返回，其他类型 JSON 序列化。"""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _deserialize(raw: str | None, default: Any) -> Any:
        """从字符串反序列化。尝试 JSON 解析，失败则返回原始字符串或 default。"""
        if raw is None:
            return default
        # 尝试 JSON 解析（用于 list/bool/float/int）
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    def _read_category(self, category: str, model_cls: type) -> Any:
        """从 DB 读取一个分类的所有配置，构造 Pydantic 模型实例。"""
        raw = self._repo.get_system_settings(category)
        data: dict[str, Any] = {}
        for field_name in _CATEGORY_FIELDS[category]:
            if field_name in raw:
                # 用模型字段的默认值作为类型提示
                field_default = getattr(model_cls.model_fields[field_name], 'default', None)
                data[field_name] = self._deserialize(raw[field_name], field_default)
        return model_cls(**data)

    def _write_category(self, category: str, data: dict[str, Any]) -> list[str]:
        """将一个分类的字段写入 DB，返回实际变更的字段名列表。"""
        changed: list[str] = []
        current_raw = self._repo.get_system_settings(category)
        for key, value in data.items():
            if value is None:
                continue
            serialized = self._serialize(value)
            old = current_raw.get(key)
            if old != serialized:
                self._repo.set_system_setting(category, key, serialized)
                changed.append(key)
        return changed

    # ═══════════════════════════════════════════════════════
    # 审计日志
    # ═══════════════════════════════════════════════════════

    def _write_audit(self, category: str, changed_fields: list[str]) -> None:
        """写配置变更审计日志"""
        if not changed_fields:
            return
        category_labels = {
            CAT_AI: "AI配置",
            CAT_STRATEGY: "话术策略",
            CAT_COMPLIANCE: "合规规则",
            CAT_CRAWL: "采集配置",
            CAT_NOTIFICATION: "通知设置",
        }
        label = category_labels.get(category, category)
        # api_key 不记录具体值，只标记已变更
        display_fields = []
        for f in changed_fields:
            if f == "api_key":
                display_fields.append("api_key(已脱敏)")
            else:
                display_fields.append(f)
        text = f"配置变更 · {label}：修改了 {', '.join(display_fields)}"
        event = ComplianceEvent(
            type="config_change",
            level="info",
            text=text,
            time=_fmt_display_time(_now_cst()),
        )
        self._repo.save_compliance_event(event)

    # ═══════════════════════════════════════════════════════
    # 全部配置
    # ═══════════════════════════════════════════════════════

    def get_all_settings(self) -> AllSettings:
        """获取所有分类配置"""
        return AllSettings(
            ai=self.get_ai_settings(),
            strategy=self.get_strategy_settings(),
            compliance=self.get_compliance_settings(),
            crawl=self.get_crawl_settings(),
            notification=self.get_notification_settings(),
        )

    def update_settings(self, partial: dict[str, Any]) -> AllSettings:
        """部分更新配置。partial 结构为 {category: {field: value, ...}}。"""
        category_map = {
            "ai": (CAT_AI, AISettingsUpdate),
            "strategy": (CAT_STRATEGY, StrategySettingsUpdate),
            "compliance": (CAT_COMPLIANCE, ComplianceSettingsUpdate),
            "crawl": (CAT_CRAWL, CrawlSettingsUpdate),
            "notification": (CAT_NOTIFICATION, NotificationSettingsUpdate),
        }
        for cat_key, cat_data in partial.items():
            if cat_key not in category_map or not isinstance(cat_data, dict):
                continue
            category, update_cls = category_map[cat_key]
            # 过滤掉 None 值
            filtered = {k: v for k, v in cat_data.items() if v is not None}
            if not filtered:
                continue
            changed = self._write_category(category, filtered)
            self._write_audit(category, changed)
            # AI 配置变更后热更新
            if category == CAT_AI and self._ai_service is not None:
                self._ai_service.reload_config()
        return self.get_all_settings()

    # ═══════════════════════════════════════════════════════
    # AI 配置
    # ═══════════════════════════════════════════════════════

    def get_ai_settings(self) -> AISettings:
        return self._read_category(CAT_AI, AISettings)

    def update_ai_settings(self, data: AISettingsUpdate) -> AISettings:
        changed = self._write_category(CAT_AI, data.model_dump(exclude_none=True))
        self._write_audit(CAT_AI, changed)
        # 热更新 AI 服务
        if self._ai_service is not None:
            self._ai_service.reload_config()
        return self.get_ai_settings()

    # ═══════════════════════════════════════════════════════
    # 话术策略配置
    # ═══════════════════════════════════════════════════════

    def get_strategy_settings(self) -> StrategySettings:
        return self._read_category(CAT_STRATEGY, StrategySettings)

    def update_strategy_settings(self, data: StrategySettingsUpdate) -> StrategySettings:
        changed = self._write_category(CAT_STRATEGY, data.model_dump(exclude_none=True))
        self._write_audit(CAT_STRATEGY, changed)
        return self.get_strategy_settings()

    # ═══════════════════════════════════════════════════════
    # 合规规则配置
    # ═══════════════════════════════════════════════════════

    def get_compliance_settings(self) -> ComplianceSettings:
        return self._read_category(CAT_COMPLIANCE, ComplianceSettings)

    def update_compliance_settings(self, data: ComplianceSettingsUpdate) -> ComplianceSettings:
        changed = self._write_category(CAT_COMPLIANCE, data.model_dump(exclude_none=True))
        self._write_audit(CAT_COMPLIANCE, changed)
        return self.get_compliance_settings()

    # ═══════════════════════════════════════════════════════
    # 采集配置
    # ═══════════════════════════════════════════════════════

    def get_crawl_settings(self) -> CrawlSettings:
        return self._read_category(CAT_CRAWL, CrawlSettings)

    def update_crawl_settings(self, data: CrawlSettingsUpdate) -> CrawlSettings:
        changed = self._write_category(CAT_CRAWL, data.model_dump(exclude_none=True))
        self._write_audit(CAT_CRAWL, changed)
        return self.get_crawl_settings()

    # ═══════════════════════════════════════════════════════
    # 通知配置
    # ═══════════════════════════════════════════════════════

    def get_notification_settings(self) -> NotificationSettings:
        return self._read_category(CAT_NOTIFICATION, NotificationSettings)

    def update_notification_settings(self, data: NotificationSettingsUpdate) -> NotificationSettings:
        changed = self._write_category(CAT_NOTIFICATION, data.model_dump(exclude_none=True))
        self._write_audit(CAT_NOTIFICATION, changed)
        return self.get_notification_settings()

    # ═══════════════════════════════════════════════════════
    # AI 连接测试
    # ═══════════════════════════════════════════════════════

    def test_ai_connection(self, req: TestAIRequest) -> TestAIResponse:
        """发送最小请求验证 API Key 有效性，返回成功/失败/延迟。"""
        from urllib import error, request as urlrequest

        provider = req.provider.lower().strip()
        api_key = req.api_key.strip()
        model = req.model.strip()
        timeout = req.timeout

        if not api_key:
            return TestAIResponse(success=False, message="API Key 不能为空", latency_ms=0)

        # 构造最小测试请求（一条简单的 chat completion）
        if provider == "doubao":
            endpoint = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
            body = {
                "model": model or "ep-20240101-default",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
                "temperature": 0,
            }
        elif provider == "dashscope":
            endpoint = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
            body = {
                "model": model or "qwen-plus",
                "input": {"messages": [{"role": "user", "content": "ping"}]},
                "parameters": {"result_format": "message", "max_tokens": 5, "temperature": 0},
            }
        else:
            return TestAIResponse(success=False, message=f"不支持的 provider: {provider}", latency_ms=0)

        req_obj = urlrequest.Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        start = time.monotonic()
        try:
            with urlrequest.urlopen(req_obj, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                result = json.loads(raw)
            latency_ms = int((time.monotonic() - start) * 1000)

            # 验证返回结构
            if provider == "doubao":
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            else:
                content = result.get("output", {}).get("choices", [{}])[0].get("message", {}).get("content", "")

            if content is not None:
                return TestAIResponse(
                    success=True,
                    message=f"连接成功，响应正常（延迟 {latency_ms}ms）",
                    latency_ms=latency_ms,
                )
            return TestAIResponse(
                success=False,
                message=f"返回格式异常：{str(result)[:200]}",
                latency_ms=latency_ms,
            )
        except error.HTTPError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = str(exc)
            return TestAIResponse(
                success=False,
                message=f"HTTP {exc.code}：{detail}",
                latency_ms=latency_ms,
            )
        except (error.URLError, TimeoutError, OSError) as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            return TestAIResponse(
                success=False,
                message=f"网络/超时错误：{exc}",
                latency_ms=latency_ms,
            )
        except json.JSONDecodeError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            return TestAIResponse(
                success=False,
                message=f"返回内容不是合法 JSON：{exc}",
                latency_ms=latency_ms,
            )
