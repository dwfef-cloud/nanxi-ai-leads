"""§5 关键参数规格表 —— DESIGN-DOC.md 的唯一代码化来源。

来源：阿里山活体观测（_backend/LIVE-OBSERVATION.md）。参数锁定，禁止凭感觉改：
变更流程 = 评审 → 回写 DESIGN-DOC.md §5 → 更新 LOCKED_VALUES → 跑 test_execution_params.py。

合规说明：观测值中包含拟人化随机节奏参数。本模块只做「规格固化」，
是否启用由 ENABLE_HUMANIZED_PACING 控制，默认 False。
本轮执行层只使用固定限速（READ_MIN_INTERVAL_MS），不启用拟人随机化。
"""
from __future__ import annotations

from dataclasses import dataclass

TTL_DAYS = 90
TTL_SECONDS = TTL_DAYS * 24 * 3600


@dataclass(frozen=True)
class ReadParams:
    """M2 读屏：1-3 条/秒，取上限换算为固定最小间隔（合规限速，不是拟人伪装）。"""
    max_items_per_second: int = 3
    scroll_step_min_px: int = 400
    scroll_step_max_px: int = 800
    scroll_wait_min_ms: int = 600
    scroll_wait_max_ms: int = 1500
    page_dwell_min_s: int = 8
    page_dwell_max_s: int = 25


@dataclass(frozen=True)
class EngagementParams:
    """§5.1 互动参数（实测值）。"""
    comment_cooldown_min_s: int = 300
    comment_cooldown_max_s: int = 600
    comment_probe_min_s: int = 12
    comment_probe_max_s: int = 13
    input_box_recompute_each_time: bool = True
    ai_submit_pause_min_s: int = 20
    ai_submit_pause_max_s: int = 27
    keystroke_delay_min_ms: int = 80
    keystroke_delay_max_ms: int = 200


@dataclass(frozen=True)
class NurtureParams:
    """§5.2 养号参数（实测值）。"""
    watch_min_s: int = 8
    watch_max_s: int = 25
    like_probability: float = 0.10
    favorite_probability: float = 0.10
    share_probability: float = 0.10
    open_comments_probability: float = 0.32
    comments_browse_limit: int = 20
    pre_end_interaction_window_s: int = 14
    profile_entry_probabilities: tuple[float, float, float] = (0.08, 0.10, 0.06)
    muted: bool = True
    autoplay: bool = False
    loop: bool = False
    short_video_max_s: int = 15
    short_video_seek_s: float = 0.05
    max_consecutive_skips: int = 4


@dataclass(frozen=True)
class DmFrequencyLimits:
    """§5.3 官方私信频控，写死，不可配置放开。"""
    non_follower_per_person: int = 3
    per_day_people: int = 100
    per_hour_people: int = 40
    session_window_hours: int = 48
    session_window_messages: int = 6


@dataclass(frozen=True)
class FilterParams:
    """M3 筛选参数。"""
    intent_threshold: int = 70
    llm_timeout_s: int = 10
    min_effective_chars: int = 2


READ = ReadParams()
ENGAGEMENT = EngagementParams()
NURTURE = NurtureParams()
DM_LIMITS = DmFrequencyLimits()
FILTER = FilterParams()

# 固定限速：1-3 条/秒 → 至少 334ms/条（向上取整，宁慢勿快）
READ_MIN_INTERVAL_MS = 1000 // READ.max_items_per_second + 1

# 拟人化节奏总开关。启用前须走评审并复核 §1.3 合规三约束。
ENABLE_HUMANIZED_PACING = False

LOCKED_VALUES: dict[str, object] = {
    "ttl_days": TTL_DAYS,
    "read.max_items_per_second": READ.max_items_per_second,
    "read.scroll_step_min_px": READ.scroll_step_min_px,
    "read.scroll_step_max_px": READ.scroll_step_max_px,
    "read.page_dwell_min_s": READ.page_dwell_min_s,
    "read.page_dwell_max_s": READ.page_dwell_max_s,
    "engage.comment_cooldown_min_s": ENGAGEMENT.comment_cooldown_min_s,
    "engage.comment_cooldown_max_s": ENGAGEMENT.comment_cooldown_max_s,
    "engage.ai_submit_pause_min_s": ENGAGEMENT.ai_submit_pause_min_s,
    "engage.ai_submit_pause_max_s": ENGAGEMENT.ai_submit_pause_max_s,
    "engage.keystroke_delay_min_ms": ENGAGEMENT.keystroke_delay_min_ms,
    "engage.keystroke_delay_max_ms": ENGAGEMENT.keystroke_delay_max_ms,
    "nurture.watch_min_s": NURTURE.watch_min_s,
    "nurture.watch_max_s": NURTURE.watch_max_s,
    "nurture.like_probability": NURTURE.like_probability,
    "nurture.favorite_probability": NURTURE.favorite_probability,
    "nurture.share_probability": NURTURE.share_probability,
    "nurture.open_comments_probability": NURTURE.open_comments_probability,
    "nurture.comments_browse_limit": NURTURE.comments_browse_limit,
    "nurture.pre_end_interaction_window_s": NURTURE.pre_end_interaction_window_s,
    "nurture.profile_entry_probabilities": NURTURE.profile_entry_probabilities,
    "nurture.max_consecutive_skips": NURTURE.max_consecutive_skips,
    "dm.non_follower_per_person": DM_LIMITS.non_follower_per_person,
    "dm.per_day_people": DM_LIMITS.per_day_people,
    "dm.per_hour_people": DM_LIMITS.per_hour_people,
    "dm.session_window_hours": DM_LIMITS.session_window_hours,
    "dm.session_window_messages": DM_LIMITS.session_window_messages,
    "filter.intent_threshold": FILTER.intent_threshold,
    "filter.llm_timeout_s": FILTER.llm_timeout_s,
}


def validate_locked(expected: dict[str, object]) -> list[str]:
    """与文档规格对照，返回不一致项（供 CI / 测试调用）。"""
    issues: list[str] = []
    for key, value in expected.items():
        if key not in LOCKED_VALUES:
            issues.append(f"缺少参数: {key}")
        elif LOCKED_VALUES[key] != value:
            issues.append(f"{key}: 期望 {value!r}, 实际 {LOCKED_VALUES[key]!r}")
    return issues
