"""AI 路由 · 画像分析 / 智能草稿 / 评论回复建议 / 设置"""
from fastapi import APIRouter, Depends

from app.core.dependencies import get_ai_service
from app.schemas.ai import (
    AISettingsRead,
    AISettingsUpdate,
    AnalyzeRequest,
    AnalyzeResponse,
    CommentSuggestionRequest,
    CommentSuggestionResponse,
    DraftRequest,
    DraftResponse,
    ProfileAnalysisRequest,
    ProfileAnalysisResponse,
    SmartDraftRequest,
    SmartDraftResponse,
)
from app.services.ai_service import AIService

router = APIRouter(prefix="/ai", tags=["ai"])


# ═══════════════════════════════════════════════════════════
# 设置
# ═══════════════════════════════════════════════════════════

@router.get("/settings", response_model=AISettingsRead)
def get_settings(service: AIService = Depends(get_ai_service)) -> AISettingsRead:
    return service.get_settings()


@router.put("/settings", response_model=AISettingsRead)
def update_settings(req: AISettingsUpdate, service: AIService = Depends(get_ai_service)) -> AISettingsRead:
    return service.update_settings(req)


# ═══════════════════════════════════════════════════════════
# 新增：线索画像分析
# ═══════════════════════════════════════════════════════════

@router.post("/analyze", response_model=AnalyzeResponse)
def analyze_lead(req: AnalyzeRequest, service: AIService = Depends(get_ai_service)) -> AnalyzeResponse:
    """分析线索意向等级、需求标签、推荐话术和跟进建议，自动回写 Lead 画像字段。"""
    return service.analyze_lead(req)


# ═══════════════════════════════════════════════════════════
# 新增：智能草稿
# ═══════════════════════════════════════════════════════════

@router.post("/draft", response_model=SmartDraftResponse)
def smart_draft(req: SmartDraftRequest, service: AIService = Depends(get_ai_service)) -> SmartDraftResponse:
    """根据话术类别 + 用户画像 + 评论内容生成个性化回复草稿，不自动发送。"""
    return service.generate_smart_draft(req)


# ═══════════════════════════════════════════════════════════
# 新增：评论回复建议
# ═══════════════════════════════════════════════════════════

@router.post("/comment-suggestion", response_model=CommentSuggestionResponse)
def comment_suggestion(
    req: CommentSuggestionRequest, service: AIService = Depends(get_ai_service)
) -> CommentSuggestionResponse:
    """根据用户评论生成 3 条评论回复建议（钩子型/价值型/提问型）。"""
    return service.generate_comment_suggestions(req)


# ═══════════════════════════════════════════════════════════
# 兼容：原有接口（业务画像分析 / 旧草稿）
# ═══════════════════════════════════════════════════════════

@router.post("/profile-analysis", response_model=ProfileAnalysisResponse)
def profile_analysis(
    req: ProfileAnalysisRequest, service: AIService = Depends(get_ai_service)
) -> ProfileAnalysisResponse:
    """业务画像分析（原有接口，保留兼容）。"""
    return service.analyze_profile(req)


@router.post("/draft-legacy", response_model=DraftResponse)
def draft_legacy(req: DraftRequest, service: AIService = Depends(get_ai_service)) -> DraftResponse:
    """旧版硬编码草稿接口（保留兼容，新代码请用 POST /ai/draft）。"""
    drafts = service.draft_reply(req.lead_nickname, req.source_keyword, req.user_note)
    return DraftResponse(drafts=[item.content for item in drafts])
