"""D13 AI 评论生成 —— persona 化 + 广告法措辞自检 + 失败即跳过。

规格（DESIGN-DOC §3 M4 / D13）：
- 评论内容由 AI 生成（persona 化，**禁营销话术模板**）；
- 内容必须通过本地广告法措辞自检（禁用词 + 形态校验）；
- LLM 超时/失败：**不降级为模板群发**，返回 None → 该候选跳过
  （同 §0.3.6「宁可不养不可乱养」的取向，宁可不评不可乱评）；
- 双供应商（复用 M3 的 Provider/transport 模式），禁止单点依赖。

合规：生成内容为对公开内容的真诚回应，不引导私信、不留联系方式、不夸大宣传。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.execution.params import FILTER
from app.execution.scorer import DEFAULT_PROVIDERS, Provider, default_transport

Transport = default_transport  # 与 scorer 一致的 transport 可注入约定

PERSONA = (
    "你是抖音上一位真实的同行创作者，在别人的公开视频评论区真诚回应。"
    "要求：只谈内容本身（观点/细节/感受），口语化、像真人随手写的；"
    "10-30 个字；禁止出现任何联系方式、引流话术、营销用语、夸大宣传；"
    "禁止复述原评论原文；不要使用感叹号超过 1 个。"
)

PROMPT_TEMPLATE = (
    "请以指定人设，针对这条公开评论所在的视频语境，写一条自然的评论回复。\n"
    "严格返回 JSON，不要 markdown 代码块：{\"text\": \"评论内容\"}\n\n"
    "人设要求：__PERSONA__\n\n"
    "原评论：__COMMENT__\n"
)

# 广告法 / 引流自检禁用词（本地静态词表，够覆盖措辞层；上线前可由 compliance_service 补充）
BANNED_PHRASES: tuple[str, ...] = (
    "最好", "第一", "国家级", "顶级", "全网最低", "史上最",
    "加微信", "加V", "加v", "私信我", "点我头像", "点击链接",
    "限时优惠", "免费领取", "扫码", "立即购买", "秒杀", "代运营",
)

# 形态校验：过短/复读/纯符号视为无效
MIN_LEN, MAX_LEN = 5, 40


@dataclass(frozen=True)
class AIComment:
    text: str
    provider: str
    attempts: tuple[str, ...]


class AICommentGenerator:
    """双供应商评论生成器。transport 可注入，离线测试无需网络。"""

    def __init__(
        self,
        providers: tuple[Provider, ...] = DEFAULT_PROVIDERS,
        transport: Any = default_transport,
        timeout: int = FILTER.llm_timeout_s,
        persona: str = PERSONA,
    ) -> None:
        self.providers = providers
        self.transport = transport
        self.timeout = timeout
        self.persona = persona

    def generate(self, *, comment_text: str, video_title: str = "") -> AIComment | None:
        """生成一条评论。全部供应商失败或产出不自检 → None（跳过，不降级）。"""
        if not str(comment_text or "").strip():
            return None
        prompt = PROMPT_TEMPLATE.replace("__PERSONA__", self.persona) \
                                .replace("__COMMENT__", str(comment_text)[:200])
        attempts: list[str] = []
        for provider in self.providers:
            if not provider.api_key():
                attempts.append(f"{provider.name}:no_key")
                continue
            try:
                body = json.dumps({
                    "model": provider.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.9,
                }, ensure_ascii=False).encode("utf-8")
                raw = self.transport(
                    provider.endpoint, {"body": body},
                    {"Authorization": f"Bearer {provider.api_key()}",
                     "Content-Type": "application/json"},
                    self.timeout,
                )
                text = self._extract(raw)
                if text is None or not self.validate(text):
                    attempts.append(f"{provider.name}:rejected")
                    continue
                return AIComment(text=text, provider=provider.name, attempts=tuple(attempts))
            except Exception as exc:  # 网络/超时/解析失败 → 切下一家
                attempts.append(f"{provider.name}:{type(exc).__name__}")
        return None

    # ── 自检 ────────────────────────────────────────────────
    @staticmethod
    def validate(text: str) -> bool:
        """广告法措辞 + 形态自检（布尔版；需要原因时用 check）。"""
        return AICommentGenerator.check(text)[0]

    @staticmethod
    def check(text: str) -> tuple[bool, str]:
        t = (text or "").strip()
        if len(t) < MIN_LEN:
            return False, f"too_short(<{MIN_LEN})"
        if len(t) > MAX_LEN:
            return False, f"too_long(>{MAX_LEN})"
        for phrase in BANNED_PHRASES:
            if phrase in t:
                return False, f"banned:{phrase}"
        if re.fullmatch(r"(.)\1{2,}", t) or not re.search(r"[\u4e00-\u9fa5A-Za-z]", t):
            return False, "noise"
        return True, "ok"

    @staticmethod
    def _extract(raw: str) -> str | None:
        try:
            data = json.loads(raw)
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            m = re.search(r"\{.*\}", text or "", re.S)
            if m:
                return str(json.loads(m.group(0)).get("text", "")).strip() or None
            return (text or "").strip() or None
        except (json.JSONDecodeError, IndexError, KeyError, AttributeError, TypeError):
            return None
