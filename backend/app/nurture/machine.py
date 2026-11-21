"""D20 养号状态机 —— NurtureSession，逐节点可单测。

节点：
    enter_feed / escape_live_card / judge / watch / interact_window /
    swipe / author_page / refresh / end

驱动协议 NurtureDriver 由实现方注入（生产 = patchright 视图驱动，
测试 = OfflineNurtureDriver）；本模块只编排决策，不碰浏览器。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.nurture import dice, params
from app.nurture.params import Rng


@dataclass
class VideoInfo:
    key: str                       # 视频唯一键（反停滞 + recentSkipKeys）
    duration_s: float = 30.0
    is_live_card: bool = False
    author: str = ""


class Judge(Protocol):
    """AI 人设研判。返回 True=匹配 / False=不匹配 / None=研判失败。"""

    def __call__(self, video: VideoInfo) -> bool | None: ...


class NurtureDriver(Protocol):
    def enter_feed(self) -> None: ...
    def current_video(self) -> VideoInfo | None: ...
    def configure_playback(self, cfg: dict) -> None: ...
    def watch(self, seconds: float) -> None: ...
    def pause(self, seconds: float) -> None: ...
    def like(self) -> str: ...
    def collect(self) -> str: ...
    def copy_share_link(self) -> str: ...
    def browse_comments(self, max_items: int) -> int: ...
    def open_author_page(self) -> None: ...
    def swipe(self) -> None: ...
    def full_refresh(self) -> None: ...


@dataclass
class NurtureSession:
    """一轮养号会话。run() 驱动状态机，trace 记录全部节点供验收/单测。"""

    driver: NurtureDriver
    rng: Rng
    judge: Judge | None = None       # None = 不启用 AI 研判（全部按不匹配处理）
    max_videos: int = 30             # 安全上限
    trace: list[str] = field(default_factory=list)
    interactions: dict[str, int] = field(default_factory=lambda: {
        "like": 0, "collect": 0, "links_copied": 0, "comment_browse": 0,
        "author_page": 0, "live_escape": 0, "refresh": 0,
    })
    _skip_streak: int = 0
    _seen_keys: set[str] = field(default_factory=set)
    _end_reason: str = ""

    # ── 主循环 ───────────────────────────────────────────────
    def run(self) -> dict:
        self.trace.append("enter_feed")
        self.driver.enter_feed()
        for _ in range(self.max_videos):
            video = self.driver.current_video()
            if video is None:
                self.trace.append("end:no-video")
                break
            # 直播间卡片逃生：划走，不计跳过
            if video.is_live_card:
                self.trace.append(f"escape_live_card:{video.key}")
                self.interactions["live_escape"] += 1
                self.driver.swipe()
                continue
            # 反停滞：recentSkipKeys 命中 → 整页刷新
            if video.key in self._seen_keys:
                self.trace.append(f"refresh:repeat-key:{video.key}")
                self.interactions["refresh"] += 1
                self._skip_streak = 0
                self.driver.full_refresh()
                continue
            self._seen_keys.add(video.key)
            # AI 研判（可选）。失败 = None → 结束本轮，不降级为无脑刷
            matched = True
            if self.judge is not None:
                matched = self.judge(video)
                if matched is None:
                    self.trace.append(f"end:judge-failed:{video.key}")
                    break
            if matched:
                self._matched_flow(video)
            else:
                self._unmatched_flow(video)
            if self._end_reason:
                self.trace.append("end:" + self._end_reason)
                break
        else:
            self.trace.append("end:max-videos")
        self.trace.append("end")
        return {"trace": self.trace, "interactions": self.interactions,
                "videos_seen": len(self._seen_keys),
                "needs_confirmation": bool(self._end_reason)}

    # ── 匹配流：观看 → 窗口掷骰 → 划走 ─────────────────────
    def _matched_flow(self, video: VideoInfo) -> None:
        secs = params.watch_seconds(self.rng)
        self.trace.append(f"watch:{video.key}:{secs:.1f}s")
        self.driver.configure_playback(params.playback_config(video.duration_s))
        self.driver.watch(secs)
        # 视频结束前 14s 互动窗口：独立掷骰（互不排斥）
        if self._in_interact_window(video, secs):
            if dice.roll_like(self.rng):
                self._perform("like", self.driver.like)
                if self._end_reason:
                    return
            if dice.roll_collect(self.rng):
                self._perform("collect", self.driver.collect)
                if self._end_reason:
                    return
            if dice.roll_copy_link(self.rng):
                self._perform("links_copied", self.driver.copy_share_link)
                if self._end_reason:
                    return
            if dice.roll_browse_comments(self.rng):
                n = self.driver.browse_comments(params.COMMENT_BROWSE_MAX)
                self.interactions["comment_browse"] += 1
                self.trace.append(f"interact:browse-comments:{n}")
        self._after_swipe()

    def _perform(self, action: str, execute) -> None:
        # A driver method returning normally is not proof of a platform action.
        try:
            status = execute()
        except Exception:
            status = "uncertain"
        if status == "sent_ok":
            self.interactions[action] += 1
            self.trace.append("interact:" + action)
        elif status in {"sent_fail", "already_done", "skipped"}:
            self.trace.append(f"interact:{action}:{status}")
        else:
            self.trace.append(f"interact:{action}:uncertain")
            self._end_reason = f"uncertain:{action}"

    # ── 不匹配流：停 500-1200ms → 划走 ─────────────────────
    def _unmatched_flow(self, video: VideoInfo) -> None:
        ms = params.unmatched_pause_ms(self.rng)
        self.trace.append(f"skip:{video.key}:{ms:.0f}ms")
        self.driver.pause(ms / 1000)
        self._skip_streak += 1
        if self._skip_streak >= params.SKIP_STREAK_REFRESH:
            self.trace.append("refresh:skip-streak-4")
            self.interactions["refresh"] += 1
            self._skip_streak = 0
            self.driver.full_refresh()
        self._after_swipe()

    # ── 划走 + 主页掷骰 ─────────────────────────────────────
    def _after_swipe(self) -> None:
        self.driver.swipe()
        self.trace.append("swipe")
        if dice.roll_author_page(self.rng):
            self.driver.open_author_page()
            self.interactions["author_page"] += 1
            self.trace.append("author-page")
            self.driver.swipe()          # 看完主页回推荐流

    @staticmethod
    def _in_interact_window(video: VideoInfo, watched_s: float) -> bool:
        """观看时长覆盖了「结束前 14s 窗口」才算进入互动窗口。"""
        remaining = video.duration_s - watched_s
        return remaining <= params.INTERACT_WINDOW_BEFORE_END_S
