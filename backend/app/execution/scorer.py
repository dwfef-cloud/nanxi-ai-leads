"""M3 模型层 —— 双供应商 LLM 意向打分 + 超时降级。

架构决策 §0.3.5：禁止单点依赖（阿里山 /radar/ai/ 挂掉即瘫痪的教训）。
- 主供应商失败（网络/超时/5xx/解析失败）→ 自动切备用供应商
- 两家都失败 → 降级为规则层结果，标记 ai_degraded=True
- 超时 10s（§5 锁定值），密钥只从环境变量读，禁止硬编码

离线可测：transport 可注入，测试不需要真实网络与 API key。
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from app.execution.params import FILTER
from app.execution.rules import RuleVerdict

Transport = Callable[[str, dict[str, Any], dict[str, str], int], str]

PERSONA = (
    "你是短视频公开评论的意向研判助手。只判断文本表达的商业咨询意向强度，"
    "不做用户身份识别、不推测个人信息、不生成营销话术。"
)

PROMPT_TEMPLATE = (
    "请对下面这条公开评论打分。\n"
    "标签只能从 [副业, 求带, 咨询, 闲聊, 其他] 中选 1-2 个。\n"
    "严格返回 JSON，不要 markdown 代码块："
    '{"score": 0-100 的整数, "tags": ["咨询"], "reason": "20字以内理由"}\n\n'
    "评分口径：明确索要联系方式/报价/教程=80-100；表达需求或认可=50-79；"
    "纯闲聊或无意向=0-49。\n\n评论：__COMMENT__"
)


@dataclass(frozen=True)
class Provider:
    name: str
    endpoint: str
    model: str
    key_env: str

    def api_key(self) -> str:
        return os.getenv(self.key_env, "").strip()


DEFAULT_PROVIDERS: tuple[Provider, Provider] = (
    Provider("deepseek", "https://api.deepseek.com/chat/completions",
             "deepseek-chat", "DEEPSEEK_API_KEY"),
    Provider("ark", "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
             os.getenv("ARK_MODEL", "doubao-pro-32k"), "ARK_API_KEY"),
)


@dataclass(frozen=True)
class ScoreResult:
    intent_score: int
    intent_tags: tuple[str, ...]
    reason: str
    ai_degraded: bool
    provider: str | None
    attempts: tuple[str, ...]


def default_transport(url: str, payload: dict[str, Any],
                      headers: dict[str, str], timeout: int) -> str:
    req = request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}")


def parse_score_response(raw: str) -> tuple[int, tuple[str, ...], str] | None:
    """从 LLM 返回里解析打分。兼容 markdown 代码块与多余文字。"""
    if not raw:
        return None
    candidates = [raw.strip()]
    match = _JSON_BLOCK_RE.search(raw)
    if match:
        candidates.insert(0, match.group(0))
    for candidate in candidates:
        try:
            outer = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        content = outer
        if isinstance(outer, dict) and "choices" in outer:
            try:
                content = outer["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                return None
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (ValueError, TypeError):
                block = _JSON_BLOCK_RE.search(content)
                if not block:
                    continue
                try:
                    content = json.loads(block.group(0))
                except (ValueError, TypeError):
                    continue
        if not isinstance(content, dict) or "score" not in content:
            continue
        try:
            score = int(float(content["score"]))
        except (ValueError, TypeError):
            continue
        score = max(0, min(100, score))
        tags_value = content.get("tags") or []
        tags = tuple(str(t) for t in tags_value) if isinstance(tags_value, list) else (str(tags_value),)
        reason = str(content.get("reason") or "")[:100]
        return score, tags, reason
    return None


class IntentScorer:
    def __init__(
        self,
        providers: tuple[Provider, ...] | list[Provider] = DEFAULT_PROVIDERS,
        transport: Transport | None = None,
        timeout: int | None = None,
        threshold: int | None = None,
    ) -> None:
        if len(providers) < 2:
            raise ValueError("§0.3.5 禁止单点依赖：至少配置两个 LLM 供应商")
        self.providers = tuple(providers)
        self.transport: Transport = transport or default_transport
        self.timeout = FILTER.llm_timeout_s if timeout is None else int(timeout)
        self.threshold = FILTER.intent_threshold if threshold is None else int(threshold)

    def score(self, text: str, rule: RuleVerdict) -> ScoreResult:
        """规则层已判噪声时不调 LLM（省成本、防噪声入池）。"""
        if rule.is_noise:
            return ScoreResult(0, (), f"noise:{rule.reason}", False, None, ())

        attempts: list[str] = []
        for provider in self.providers:
            key = provider.api_key()
            if not key:
                attempts.append(f"{provider.name}:no_key")
                continue
            payload = {
                "model": provider.model,
                "messages": [
                    {"role": "system", "content": PERSONA},
                    {"role": "user", "content": PROMPT_TEMPLATE.replace("__COMMENT__", text[:500])},
                ],
                "temperature": 0.1,
                "max_tokens": 160,
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            }
            try:
                raw = self.transport(provider.endpoint, payload, headers, self.timeout)
            except (error.URLError, TimeoutError, OSError, ValueError) as exc:
                attempts.append(f"{provider.name}:{type(exc).__name__}")
                continue
            parsed = parse_score_response(raw)
            if parsed is None:
                attempts.append(f"{provider.name}:bad_response")
                continue
            score, tags, reason = parsed
            return ScoreResult(score, tags, reason, False, provider.name, tuple(attempts))

        # 双供应商都不可用 → 规则层兜底
        return ScoreResult(
            rule.base_score, rule.tags, "ai_degraded:fallback_to_rules",
            True, None, tuple(attempts),
        )

    def should_promote(self, result: ScoreResult) -> bool:
        return result.intent_score >= self.threshold
