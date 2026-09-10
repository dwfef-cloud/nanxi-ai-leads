from typing import Literal

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════
# 现有：设置 / 业务画像分析 / 草稿（保留兼容）
# ═══════════════════════════════════════════════════════════

class DraftRequest(BaseModel):
    lead_nickname: str
    source_keyword: str
    user_note: str = ""


class DraftResponse(BaseModel):
    drafts: list[str]


class ProfileAnalysisRequest(BaseModel):
    product: str
    industry: str = ""
    region: str = ""
    target_customer: str = ""
    selling_points: str = ""


class ProfileAnalysisResponse(BaseModel):
    needs: list[str]
    pain_points: list[str]
    search_keywords: list[str]
    intent_keywords: list[str]
    excluded_keywords: list[str]
    customer_language: list[str] = []


class AISettingsUpdate(BaseModel):
    provider: str = "dashscope"
    api_key: str = ""
    model: str = "qwen-plus"


class AISettingsRead(BaseModel):
    provider: str
    configured: bool
    masked_key: str
    model: str


# ═══════════════════════════════════════════════════════════
# 新增：线索画像分析 POST /api/ai/analyze
# ═══════════════════════════════════════════════════════════

class AnalyzeRequest(BaseModel):
    nickname: str = ""
    comment: str = ""
    video_title: str = ""
    source_keyword: str = ""


class AnalyzeResponse(BaseModel):
    intent_level: Literal["A", "B", "C"] = "C"
    customer_need: str = ""
    tags: list[str] = Field(default_factory=list)
    recommended_script_category: Literal[
        "comment", "private_message", "wechat_guide", "objection", "nurture"
    ] = "private_message"
    follow_up_suggestion: str = ""
    raw_reasoning: str = ""


# ═══════════════════════════════════════════════════════════
# 新增：智能草稿 POST /api/ai/draft
# ═══════════════════════════════════════════════════════════

DraftStyle = Literal["formal", "casual", "short"]


class SmartDraftRequest(BaseModel):
    lead_id: str
    script_category: Literal[
        "comment", "private_message", "wechat_guide", "objection", "nurture"
    ] = "private_message"
    user_comment: str = ""
    style: DraftStyle = "casual"


class SmartDraftResponse(BaseModel):
    draft: str
    style: DraftStyle
    lead_nickname: str = ""


# ═══════════════════════════════════════════════════════════
# 新增：评论回复建议 POST /api/ai/comment-suggestion
# ═══════════════════════════════════════════════════════════

class CommentSuggestionRequest(BaseModel):
    comment: str
    video_title: str = ""
    source_keyword: str = ""


class CommentSuggestionItem(BaseModel):
    type: Literal["hook", "value", "question"]
    label: str
    content: str


class CommentSuggestionResponse(BaseModel):
    suggestions: list[CommentSuggestionItem]
