"""§5.2 养号参数全表（LIVE-OBSERVATION 实测值，改动需评审回写文档）。"""
from __future__ import annotations

from typing import Protocol


class Rng(Protocol):
    def uniform(self, a: float, b: float) -> float: ...
    def random(self) -> float: ...
    def choice(self, seq): ...


# 观看时长（秒）
WATCH_MIN_S = 8.0
WATCH_MAX_S = 25.0

# 互动概率（独立掷骰）
P_LIKE = 0.10
P_COLLECT = 0.10
P_SHARE = 0.10
P_BROWSE_COMMENTS = 0.32
COMMENT_BROWSE_MAX = 20      # 评论区最多浏览条数

# 视频结束前互动窗口（秒）
INTERACT_WINDOW_BEFORE_END_S = 14.0

# 划走后进作者主页概率（三档观测值，掷骰时随机选一档）
AUTHOR_PAGE_PROBS = (0.08, 0.10, 0.06)

# 不匹配停留（毫秒）
UNMATCHED_PAUSE_MS = (500, 1200)

# 反停滞：连续跳过 N 次整页刷新
SKIP_STREAK_REFRESH = 4

# 防跳播放设置
PLAYBACK = {"muted": True, "autoplay": False, "loop": False,
            "short_video_seek_s": 0.05, "short_video_max_s": 15.0}

# ≤15s 短视频：跳到 0.05s 后 pause
def short_video_seek_s() -> float:
    return PLAYBACK["short_video_seek_s"]


def watch_seconds(rng: Rng) -> float:
    return rng.uniform(WATCH_MIN_S, WATCH_MAX_S)


def unmatched_pause_ms(rng: Rng) -> float:
    return rng.uniform(*UNMATCHED_PAUSE_MS)


def playback_config(duration_s: float) -> dict:
    """返回该视频应施加的播放配置（§5.2 防跳设置）。"""
    cfg = dict(PLAYBACK)
    if 0 < duration_s <= PLAYBACK["short_video_max_s"]:
        cfg["seek_then_pause"] = PLAYBACK["short_video_seek_s"]
    return cfg


def pacing_profile() -> dict:
    """参数对账视图（运维/验收用）。"""
    return {
        "watch_s": [WATCH_MIN_S, WATCH_MAX_S],
        "p_like": P_LIKE, "p_collect": P_COLLECT, "p_share": P_SHARE,
        "p_browse_comments": P_BROWSE_COMMENTS,
        "comment_browse_max": COMMENT_BROWSE_MAX,
        "interact_window_before_end_s": INTERACT_WINDOW_BEFORE_END_S,
        "author_page_probs": list(AUTHOR_PAGE_PROBS),
        "unmatched_pause_ms": list(UNMATCHED_PAUSE_MS),
        "skip_streak_refresh": SKIP_STREAK_REFRESH,
        "playback": PLAYBACK,
    }
