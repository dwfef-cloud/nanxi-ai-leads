"""AI 大模型服务 · 支持 DashScope（通义千问）和豆包（Doubao/Volcengine Ark）

设计要点：
- 双 Provider 通过环境变量 AI_PROVIDER 切换（dashscope | doubao）
- API Key 分别从 DASHSCOPE_API_KEY / DOUBAO_API_KEY 读取
- 未配置 Key 时所有生成接口返回 503 + 明确提示，不崩溃
- 使用标准库 urllib.request（无额外依赖），统一 10s 超时
- 所有 LLM 调用走 _call_llm 统一入口，异常归一化为 HTTPException
"""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib import error, request

from fastapi import HTTPException

from app.schemas.ai import (
    AISettingsRead,
    AISettingsUpdate,
    AnalyzeRequest,
    AnalyzeResponse,
    CommentSuggestionItem,
    CommentSuggestionRequest,
    CommentSuggestionResponse,
    DraftRequest,
    DraftResponse,
    ProfileAnalysisRequest,
    ProfileAnalysisResponse,
    SmartDraftRequest,
    SmartDraftResponse,
)
from app.schemas.conversation import MessageCreate

# ── 常量 ──────────────────────────────────────────────────────
DASHSCOPE_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
DOUBAO_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DEFAULT_TIMEOUT = 10  # 秒

SCRIPT_CATEGORY_LABELS = {
    "comment": "评论区回复",
    "private_message": "私信",
    "wechat_guide": "微信引导",
    "objection": "异议处理",
    "nurture": "加微后培育",
}

STYLE_LABELS = {
    "formal": "更正式",
    "casual": "更口语",
    "short": "更简短",
}


class AIService:
    def __init__(self) -> None:
        # 环境变量作为默认值/兜底
        self._provider = os.getenv("AI_PROVIDER", "dashscope").lower().strip()
        if self._provider not in ("dashscope", "doubao"):
            self._provider = "dashscope"
        self._dashscope_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self._doubao_key = os.getenv("DOUBAO_API_KEY", "").strip()
        self._model = os.getenv("DASHSCOPE_MODEL", "qwen-plus").strip() or "qwen-plus"
        self._doubao_model = os.getenv("DOUBAO_MODEL", "").strip()
        # 可配置参数（settings_service 热更新覆盖）
        self._temperature = 0.7
        self._timeout = DEFAULT_TIMEOUT
        # SettingsService 引用（用于热更新读取配置）
        self._settings_service = None
        # 可选：注入 LeadService 用于自动回写线索画像
        self._lead_service = None

    # ── 外部注入 ──────────────────────────────────────────────
    def set_lead_service(self, lead_service) -> None:
        """注入 LeadService，用于 analyze 后自动回写 intent_level/tags/customer_need"""
        self._lead_service = lead_service

    def set_settings_service(self, settings_service) -> None:
        """注入 SettingsService，用于配置热更新读取"""
        self._settings_service = settings_service
        # 注入后立即从配置中心加载一次（覆盖环境变量默认值）
        self.reload_config()

    # ── 配置热更新 ────────────────────────────────────────────
    def reload_config(self) -> None:
        """从 settings_service 重新加载 AI 配置，使新配置立即生效。

        若 settings_service 未注入或读取失败，保持当前配置不变。
        环境变量仅作为初始默认值，配置中心的值优先。
        """
        if self._settings_service is None:
            return
        try:
            ai_cfg = self._settings_service.get_ai_settings()
        except Exception:
            # 读取失败不影响现有运行
            return
        if ai_cfg.provider and ai_cfg.provider in ("dashscope", "doubao"):
            self._provider = ai_cfg.provider
        # API Key：仅在配置中心有值时覆盖（空值保留环境变量兜底）
        if ai_cfg.api_key:
            if self._provider == "doubao":
                self._doubao_key = ai_cfg.api_key
            else:
                self._dashscope_key = ai_cfg.api_key
        if ai_cfg.model:
            if self._provider == "doubao":
                self._doubao_model = ai_cfg.model
            else:
                self._model = ai_cfg.model
        if ai_cfg.temperature is not None:
            self._temperature = ai_cfg.temperature
        if ai_cfg.timeout is not None:
            self._timeout = ai_cfg.timeout

    # ── 设置 ──────────────────────────────────────────────────
    def get_settings(self) -> AISettingsRead:
        key = self._active_key()
        masked = ""
        if key:
            masked = (key[:4] + "****" + key[-4:]) if len(key) > 8 else "****"
        return AISettingsRead(
            provider=self._provider,
            configured=bool(key),
            masked_key=masked,
            model=self._active_model(),
        )

    def update_settings(self, payload: AISettingsUpdate) -> AISettingsRead:
        if payload.provider in {"dashscope", "doubao"}:
            self._provider = payload.provider
        if payload.api_key.strip():
            if self._provider == "doubao":
                self._doubao_key = payload.api_key.strip()
            else:
                self._dashscope_key = payload.api_key.strip()
        if payload.model.strip():
            if self._provider == "doubao":
                self._doubao_model = payload.model.strip()
            else:
                self._model = payload.model.strip()
        return self.get_settings()

    # ── 内部工具 ──────────────────────────────────────────────
    def _active_key(self) -> str:
        return self._doubao_key if self._provider == "doubao" else self._dashscope_key

    def _active_model(self) -> str:
        if self._provider == "doubao":
            return self._doubao_model or "ep-20240101-default"
        return self._model

    def _ensure_configured(self) -> None:
        """未配置 Key 时抛出 503，所有生成接口统一调用"""
        if not self._active_key():
            key_name = "DOUBAO_API_KEY" if self._provider == "doubao" else "DASHSCOPE_API_KEY"
            raise HTTPException(
                status_code=503,
                detail=f"请配置 API Key（环境变量 {key_name} 或在设置页填写），当前 AI 服务不可用",
            )

    def _call_llm(self, prompt: str, system: str = "", timeout: int | None = None) -> str:
        """统一 LLM 调用入口，返回纯文本内容。

        timeout 为 None 时使用 self._timeout（配置中心可配）。
        temperature 使用 self._temperature（配置中心可配）。

        异常归一化：
        - 网络/超时 → 502
        - HTTP 错误 → 502（含上游状态码）
        - 返回格式异常 → 502
        """
        self._ensure_configured()
        api_key = self._active_key()
        effective_timeout = timeout if timeout is not None else self._timeout

        if self._provider == "doubao":
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            body = {
                "model": self._active_model(),
                "messages": messages,
                "temperature": self._temperature,
            }
            endpoint = DOUBAO_ENDPOINT
        else:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            body = {
                "model": self._active_model(),
                "input": {"messages": messages},
                "parameters": {"result_format": "message", "temperature": self._temperature},
            }
            endpoint = DASHSCOPE_ENDPOINT

        req = request.Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=effective_timeout) as resp:
                raw = resp.read().decode("utf-8")
                result = json.loads(raw)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300] if exc.fp else str(exc)
            raise HTTPException(status_code=502, detail=f"AI 服务上游错误（HTTP {exc.code}）：{detail}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise HTTPException(status_code=502, detail=f"AI 服务调用失败（网络/超时）：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="AI 服务返回内容不是合法 JSON") from exc

        try:
            if self._provider == "doubao":
                content = result["choices"][0]["message"]["content"]
            else:
                content = result["output"]["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise HTTPException(status_code=502, detail=f"AI 服务返回格式不符合预期：{str(result)[:200]}") from exc

        return content.strip()

    @staticmethod
    def _extract_json(text: str) -> Any:
        """从 LLM 输出中提取 JSON 对象/数组，兼容 Markdown 代码块包裹。"""
        text = text.strip()
        # 去除 ```json ... ``` 包裹
        fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        # 找到第一个 { 或 [ 到最后一个 } 或 ]
        start = min(
            (text.find(c) for c in "{[" if text.find(c) >= 0),
            default=-1,
        )
        if start < 0:
            raise ValueError("未找到 JSON 内容")
        # 尝试从 start 开始逐步截取解析
        for end in range(len(text), start, -1):
            candidate = text[start:end]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        raise ValueError("无法解析 JSON")

    # ═══════════════════════════════════════════════════════════
    # 现有接口（保留兼容）
    # ═══════════════════════════════════════════════════════════

    def analyze_profile(self, payload: ProfileAnalysisRequest) -> ProfileAnalysisResponse:
        """业务画像分析（原有接口，保留兼容）"""
        prompt = f"""你是获客策略专家。请根据以下业务信息，生成抖音评论区获客规则。
产品：{payload.product}
行业：{payload.industry}
地区：{payload.region}
目标客户：{payload.target_customer}
产品卖点：{payload.selling_points}
只返回 JSON，不要 Markdown，字段必须是 needs、pain_points、search_keywords、intent_keywords、excluded_keywords、customer_language；每个字段都是 3-8 条简短中文字符串。"""
        content = self._call_llm(prompt, system="你是获客策略专家。", timeout=45)
        try:
            parsed = self._extract_json(content)
            return ProfileAnalysisResponse(**parsed)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=502, detail=f"业务画像分析结果解析失败：{exc}") from exc

    def draft_reply(self, lead_nickname: str, source_keyword: str, user_note: str = "") -> list[MessageCreate]:
        """原有硬编码草稿接口（保留兼容，新代码请用 generate_smart_draft）"""
        base = f"看到你关注{source_keyword}，"
        if user_note:
            base += f"你提到“{user_note}”，"
        base += "我这边可以先帮你梳理下适合你的方向。"
        return [
            MessageCreate(conversation_id="", sender="ai", content=base + "如果方便，我可以先给你一个简单建议。", is_ai_generated=True),
            MessageCreate(conversation_id="", sender="ai", content=base + "你也可以把具体需求发我，我给你初步看一下。", is_ai_generated=True),
            MessageCreate(conversation_id="", sender="ai", content=base + "我先不打扰你，后面需要的话我再补充细节。", is_ai_generated=True),
        ]

    # ═══════════════════════════════════════════════════════════
    # 新增：线索画像分析
    # ═══════════════════════════════════════════════════════════

    def analyze_lead(self, payload: AnalyzeRequest) -> AnalyzeResponse:
        """分析单条线索的意向等级、需求标签、推荐话术和跟进建议。

        成功后若已注入 lead_service，自动回写 Lead.intent_level / tags / customer_need。
        """
        prompt = f"""你是抖音获客线索分析专家。请根据以下线索信息，判断客户意向并给出跟进策略。

【线索信息】
- 昵称：{payload.nickname or '（未知）'}
- 评论内容：{payload.comment or '（无）'}
- 来源视频标题：{payload.video_title or '（无）'}
- 来源关键词：{payload.source_keyword or '（无）'}

【分析要求】
1. 意向等级 intent_level：A=高意向（明确询价/求推荐/主动私信），B=中意向（表达兴趣/提问相关），C=低意向（泛泛评论/无明确需求）
2. customer_need：一句话总结客户核心需求（不超过30字）
3. tags：3-6个需求标签（简短中文，如"老房翻新""预算敏感""求报价"）
4. recommended_script_category：推荐话术类别，只能是 comment / private_message / wechat_guide / objection / nurture 之一
5. follow_up_suggestion：一句话可执行的跟进建议（不超过40字）
6. raw_reasoning：简要说明判断依据（不超过50字）

【输出格式】
只返回 JSON 对象，不要 Markdown 代码块，字段如下：
{{"intent_level":"A|B|C","customer_need":"...","tags":["..."],"recommended_script_category":"...","follow_up_suggestion":"...","raw_reasoning":"..."}}"""

        content = self._call_llm(prompt, system="你是专业的抖音获客线索分析专家，输出严格的 JSON。", timeout=DEFAULT_TIMEOUT)
        try:
            parsed = self._extract_json(content)
            # 字段兜底
            intent = str(parsed.get("intent_level", "C")).upper()
            if intent not in ("A", "B", "C"):
                intent = "C"
            cat = parsed.get("recommended_script_category", "private_message")
            if cat not in ("comment", "private_message", "wechat_guide", "objection", "nurture"):
                cat = "private_message"
            tags = parsed.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            resp = AnalyzeResponse(
                intent_level=intent,  # type: ignore[arg-type]
                customer_need=str(parsed.get("customer_need", "")),
                tags=[str(t) for t in tags][:8],
                recommended_script_category=cat,  # type: ignore[arg-type]
                follow_up_suggestion=str(parsed.get("follow_up_suggestion", "")),
                raw_reasoning=str(parsed.get("raw_reasoning", "")),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=502, detail=f"线索画像分析结果解析失败：{exc}") from exc

        # 自动回写 Lead（若已注入 lead_service 且能通过 nickname 匹配）
        self._maybe_update_lead(payload, resp)
        return resp

    def _maybe_update_lead(self, payload: AnalyzeRequest, resp: AnalyzeResponse) -> None:
        """尝试根据 nickname 匹配线索并回写画像字段。失败静默忽略，不影响主流程。"""
        if self._lead_service is None or not payload.nickname:
            return
        try:
            from app.schemas.lead import LeadUpdate

            leads = self._lead_service.list_leads()
            matched = None
            for lead in leads:
                if lead.nickname == payload.nickname:
                    matched = lead
                    break
            if matched is None:
                return
            update = LeadUpdate(
                intent_level=resp.intent_level,
                tags=resp.tags if resp.tags else None,
                customer_need=resp.customer_need or None,
            )
            self._lead_service.update_lead(matched.id, update)
        except Exception:
            # 回写失败不影响分析结果返回
            pass

    # ═══════════════════════════════════════════════════════════
    # 新增：智能草稿
    # ═══════════════════════════════════════════════════════════

    def generate_smart_draft(self, payload: SmartDraftRequest) -> SmartDraftResponse:
        """根据话术类别 + 用户画像 + 评论内容，生成个性化回复草稿。

        不自动发送，仅返回草稿文本供人工确认。
        """
        # 拉取线索画像（若可用）
        lead_nickname = ""
        lead_tags: list[str] = []
        lead_need = ""
        lead_comment = ""
        if self._lead_service is not None:
            try:
                lead = self._lead_service.get_lead(payload.lead_id)
                lead_nickname = lead.nickname
                lead_tags = list(lead.tags or [])
                lead_need = lead.customer_need or ""
                lead_comment = lead.comment or ""
            except Exception:
                pass

        style_label = STYLE_LABELS.get(payload.style, "更口语")
        category_label = SCRIPT_CATEGORY_LABELS.get(payload.script_category, "私信")
        user_comment = payload.user_comment or lead_comment

        prompt = f"""你是抖音私域获客的话术专家。请根据以下信息生成一条{category_label}回复草稿。

【对方信息】
- 昵称：{lead_nickname or '（未知）'}
- 客户需求：{lead_need or '（未知）'}
- 画像标签：{', '.join(lead_tags) if lead_tags else '（无）'}
- 对方最新评论/消息：{user_comment or '（无）'}

【生成要求】
- 话术类别：{category_label}
- 风格：{style_label}（{'正式专业、用词规范' if payload.style == 'formal' else '自然亲切、像朋友聊天' if payload.style == 'casual' else '精简有力、不超过30字'}）
- 必须结合对方的评论内容和需求，不要泛泛而谈
- 不要直接发微信号或联系方式，引导对方主动提问或私信
- 语气真诚，避免营销感过重
- 只返回草稿文本本身，不要任何解释、前缀或引号"""

        content = self._call_llm(prompt, system="你是专业的抖音私域获客话术专家，擅长撰写高转化率的回复。", timeout=DEFAULT_TIMEOUT)
        # 去除可能的引号包裹
        content = content.strip().strip('"').strip("'").strip()

        return SmartDraftResponse(
            draft=content,
            style=payload.style,
            lead_nickname=lead_nickname,
        )

    # ═══════════════════════════════════════════════════════════
    # 新增：评论回复建议
    # ═══════════════════════════════════════════════════════════

    def generate_comment_suggestions(self, payload: CommentSuggestionRequest) -> CommentSuggestionResponse:
        """根据用户评论生成 3 条评论回复建议：钩子型 / 价值型 / 提问型。"""
        prompt = f"""你是抖音评论区运营专家。请根据以下用户评论，生成3条不同风格的回复建议。

【用户评论】
{payload.comment}

【来源视频】{payload.video_title or '（未知）'}
【来源关键词】{payload.source_keyword or '（无）'}

【3种风格要求】
1. 钩子型（hook）：留下悬念或福利钩子，引导对方主动私信或关注，不超过30字
2. 价值型（value）：直接给出有用信息或专业观点，建立信任感，不超过40字
3. 提问型（question）：用开放式问题引导对方继续表达需求，不超过25字

【输出格式】
只返回 JSON 数组，不要 Markdown，每个元素包含 type（hook/value/question）和 content 字段：
[{{"type":"hook","content":"..."}},{{"type":"value","content":"..."}},{{"type":"question","content":"..."}}]"""

        content = self._call_llm(prompt, system="你是专业的抖音评论区运营专家，擅长撰写高互动率的评论回复。", timeout=DEFAULT_TIMEOUT)
        try:
            parsed = self._extract_json(content)
            if not isinstance(parsed, list):
                raise ValueError("返回不是数组")
            items: list[CommentSuggestionItem] = []
            type_labels = {"hook": "钩子型", "value": "价值型", "question": "提问型"}
            # 按 hook → value → question 排序
            order = {"hook": 0, "value": 1, "question": 2}
            parsed_sorted = sorted(parsed, key=lambda x: order.get(str(x.get("type", "")), 9))
            for item in parsed_sorted:
                t = str(item.get("type", "")).lower()
                if t not in ("hook", "value", "question"):
                    continue
                items.append(CommentSuggestionItem(
                    type=t,  # type: ignore[arg-type]
                    label=type_labels.get(t, t),
                    content=str(item.get("content", "")).strip(),
                ))
            # 不足3条时兜底
            if len(items) < 3:
                fallback = [
                    CommentSuggestionItem(type="hook", label="钩子型", content="感兴趣的话可以私信我，给你发详细资料～"),
                    CommentSuggestionItem(type="value", label="价值型", content="这个问题很多朋友都问过，核心是先明确需求再对比方案。"),
                    CommentSuggestionItem(type="question", label="提问型", content="你目前最看重哪方面呢？可以说说你的具体情况～"),
                ]
                existing_types = {i.type for i in items}
                for fb in fallback:
                    if fb.type not in existing_types:
                        items.append(fb)
                items = sorted(items, key=lambda x: order.get(x.type, 9))[:3]
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=502, detail=f"评论回复建议解析失败：{exc}") from exc

        return CommentSuggestionResponse(suggestions=items)
