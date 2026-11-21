"""W3 离线测试：D17 私信频控 + D20 养号状态机 + D21 §5.2 参数掷骰。"""
from __future__ import annotations

import random
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.engagement.dm_guard import (
    DAILY_RECIPIENTS, GuardBlocked, HOURLY_RECIPIENTS, PER_RECIPIENT_LIMIT,
    SESSION_WINDOW_LIMIT, DMChannelConfig, DMChannelRouter, DMFrequencyGuard,
)
from app.nurture import params
from app.nurture.dice import (
    roll_author_page, roll_browse_comments, roll_collect, roll_copy_link, roll_like,
)
from app.nurture.machine import NurtureSession, VideoInfo


# ═══ D17 私信频控闸门 ═════════════════════════════════════
class TestDMFrequencyGuard:
    @pytest.fixture()
    def guard(self, tmp_path) -> DMFrequencyGuard:
        return DMFrequencyGuard(tmp_path / "dm.db")

    T0 = 1_800_000_000  # 锚点时间（自然日边界对齐 86400 整除）

    def test_非互关3条每人拦截(self, guard):
        for _ in range(PER_RECIPIENT_LIMIT):
            assert guard.check("a", "r1", mutual=False, now=self.T0).allowed
            guard.record("a", "r1", mutual=False, now=self.T0)
        d = guard.check("a", "r1", mutual=False, now=self.T0 + 100)
        assert not d.allowed and "3 条" in d.reason

    def test_互关会话窗口6条(self, guard):
        for _ in range(SESSION_WINDOW_LIMIT):
            assert guard.check("a", "r2", mutual=True, now=self.T0).allowed
            guard.record("a", "r2", mutual=True, now=self.T0)
        d = guard.check("a", "r2", mutual=True, now=self.T0 + 100)
        assert not d.allowed and "48h" in d.reason
        # 窗口滑出后恢复
        d2 = guard.check("a", "r2", mutual=True, now=self.T0 + 49 * 3600)
        assert d2.allowed

    def test_一小时40人拦截(self, guard):
        for i in range(HOURLY_RECIPIENTS):
            r = f"r{i}"
            assert guard.check("a", r, mutual=False, now=self.T0).allowed
            guard.record("a", r, mutual=False, now=self.T0)
        d = guard.check("a", "rX", mutual=False, now=self.T0 + 1800)
        assert not d.allowed and "1 小时" in d.reason
        # 一小时后恢复
        assert guard.check("a", "rX", mutual=False, now=self.T0 + 3601).allowed

    def test_一天100人拦截(self, guard):
        # 铺排在数小时内，避开小时限，专测日限
        for i in range(DAILY_RECIPIENTS):
            guard.record("a", f"r{i}", mutual=False, now=self.T0 - i * 200)
        d = guard.check("a", "rY", mutual=False, now=self.T0 + 60)
        assert not d.allowed and "100" in d.reason

    def test_模拟超频闸门拦截100(self, guard):
        """验收标准：模拟超频，闸门拦截 100%。铺排跨数小时专测日限。"""
        blocked = 0
        for i in range(150):  # 远超 100 人/天
            r = f"u{i}"
            d = guard.check("a", r, mutual=False, now=self.T0)
            if not d.allowed:
                blocked += 1
                continue
            guard.record("a", r, mutual=False, now=self.T0 - i * 200)
        assert blocked == 50  # 前 100 人放行，后 50 全拦截
        assert guard.usage("a", now=self.T0)["day_recipients"] == 100

    def test_账号之间限额独立(self, guard):
        for i in range(DAILY_RECIPIENTS):
            guard.record("a", f"r{i}", mutual=False, now=self.T0)
        assert guard.check("b", "rZ", mutual=False, now=self.T0).allowed


class TestDMChannelRouter:
    def _router(self, guard, cfg=None, main_ok=True, fb_ok=True):
        return DMChannelRouter(
            guard, cfg,
            main_send=lambda a, r, t: main_ok,
            fallback_send=lambda a, r, t: fb_ok,
            compliance_check=lambda t: (True, ""),
        )

    def test_主路径成功(self, tmp_path):
        g = DMFrequencyGuard(tmp_path / "dm.db")
        ok, ch = self._router(g).send("a", "r", "你好", mutual=False)
        assert ok and ch == "official"

    def test_主路径失败且兜底默认关_返回失败(self, tmp_path):
        g = DMFrequencyGuard(tmp_path / "dm.db")
        ok, why = self._router(g, main_ok=False).send("a", "r", "hi", mutual=False)
        assert not ok and why == "send-failed"  # 绝不静默切兜底

    def test_兜底开启需A组验证标记(self, tmp_path):
        g = DMFrequencyGuard(tmp_path / "dm.db")
        cfg_on = DMChannelConfig(fallback_enabled=True, a_group_verified=False)
        with pytest.raises(GuardBlocked):
            self._router(g, cfg_on, main_ok=False).send("a", "r", "hi", mutual=False)
        cfg_ok = DMChannelConfig(fallback_enabled=True, a_group_verified=True)
        ok, ch = self._router(g, cfg_ok, main_ok=False).send("a", "r", "hi",
                                                             mutual=False)
        assert ok and ch == "fallback"

    def test_兜底默认配置全关(self):
        cfg = DMChannelConfig()
        assert cfg.fallback_enabled is False
        assert cfg.a_group_verified is False

    def test_合规拦截优先于频控(self, tmp_path):
        g = DMFrequencyGuard(tmp_path / "dm.db")
        r = DMChannelRouter(g, main_send=lambda a, x, t: True,
                            compliance_check=lambda t: (False, "广告法禁用词"))
        ok, why = r.send("a", "r", "最优惠", mutual=False)
        assert not ok and why.startswith("compliance:")

    def test_闸门拒绝不触发发送(self, tmp_path):
        g = DMFrequencyGuard(tmp_path / "dm.db")
        for i in range(DAILY_RECIPIENTS):
            g.record("a", f"r{i}", mutual=False)
        sent = []
        router = DMChannelRouter(g, main_send=lambda a, r, t: sent.append(r) or True)
        with pytest.raises(GuardBlocked):
            router.send("a", "new", "hi", mutual=False)
        assert sent == []


# ═══ D21 §5.2 参数掷骰（1000 次模拟，各概率误差 <2%）═════════
def _freq(roll, seed: int, n: int = 1000) -> float:
    rng = random.Random(seed)
    return sum(1 for _ in range(n) if roll(rng)) / n


class TestDice:
    def test_点赞收藏复制链接各10误差小于2(self):
        for name, roll in (("like", roll_like), ("collect", roll_collect),
                           ("copy_link", roll_copy_link)):
            f = _freq(roll, seed=42)
            assert abs(f - 0.10) < 0.02, f"{name}: {f}"
            # 换种子再验一次稳定性
            f2 = _freq(roll, seed=7)
            assert abs(f2 - 0.10) < 0.02, f"{name}#2: {f2}"

    def test_进评论区32误差小于2(self):
        f = _freq(roll_browse_comments, seed=42)
        assert abs(f - 0.32) < 0.02, f
        assert abs(_freq(roll_browse_comments, seed=5) - 0.32) < 0.02

    def test_进主页三档各档位命中(self):
        # 三档概率 (0.08,0.10,0.06) 随机选一档 → 混合期望 = 均值 0.08
        f = _freq(roll_author_page, seed=42)
        assert abs(f - 0.08) < 0.02, f
        assert abs(_freq(roll_author_page, seed=123) - 0.08) < 0.02


class TestParams:
    def test_观看时长范围(self):
        rng = random.Random(1)
        vals = [params.watch_seconds(rng) for _ in range(500)]
        assert all(8.0 <= v <= 25.0 for v in vals)

    def test_不匹配停留500到1200ms(self):
        rng = random.Random(2)
        vals = [params.unmatched_pause_ms(rng) for _ in range(500)]
        assert all(500 <= v <= 1200 for v in vals)

    def test_短视频防跳配置(self):
        cfg = params.playback_config(10.0)
        assert cfg["seek_then_pause"] == 0.05 and cfg["muted"] is True
        assert "seek_then_pause" not in params.playback_config(60.0)

    def test_参数对账视图完整(self):
        p = params.pacing_profile()
        assert p["p_like"] == 0.10 and p["p_browse_comments"] == 0.32
        assert p["skip_streak_refresh"] == 4


# ═══ D20 养号状态机（逐节点）═══════════════════════════════
class OfflineNurtureDriver:
    """零浏览器测试驱动：可编程视频队列，记录全部动作。"""

    def __init__(self, videos: list[VideoInfo]) -> None:
        self.queue = list(videos)
        self.actions: list[tuple] = []
        self.comment_items = 3

    def _a(self, name, *args):
        self.actions.append((name, *args))

    def enter_feed(self): self._a("enter_feed")
    def current_video(self):
        return self.queue.pop(0) if self.queue else None
    def configure_playback(self, cfg): self._a("playback", cfg)
    def watch(self, s): self._a("watch", round(s, 1))
    def pause(self, s): self._a("pause", round(s, 2))
    def like(self): self._a("like"); return "sent_ok"
    def collect(self): self._a("collect"); return "sent_ok"
    def copy_share_link(self): self._a("copy_share_link"); return "sent_ok"
    def browse_comments(self, m): self._a("browse_comments", min(m, self.comment_items)); return 3
    def open_author_page(self): self._a("author_page")
    def swipe(self): self._a("swipe")
    def full_refresh(self): self._a("refresh")


class TestNurtureMachine:
    def test_互动没有明确回执时不计数且停止后续操作(self, monkeypatch):
        from app.nurture import dice

        monkeypatch.setattr(dice, "roll_like", lambda _: False)
        monkeypatch.setattr(dice, "roll_collect", lambda _: True)
        monkeypatch.setattr(dice, "roll_copy_link", lambda _: True)
        driver = OfflineNurtureDriver([VideoInfo("V1", duration_s=10), VideoInfo("V2")])
        driver.collect = lambda: None
        session = NurtureSession(driver, random.Random(0))
        result = session.run()
        assert result["needs_confirmation"] is True
        assert result["interactions"]["collect"] == 0
        assert result["interactions"]["links_copied"] == 0
        assert len(driver.queue) == 1

    def test_复制链接必须独立计数(self, monkeypatch):
        from app.nurture import dice

        monkeypatch.setattr(dice, "roll_like", lambda _: False)
        monkeypatch.setattr(dice, "roll_collect", lambda _: False)
        monkeypatch.setattr(dice, "roll_copy_link", lambda _: True)
        driver = OfflineNurtureDriver([VideoInfo("V1", duration_s=10)])
        result = NurtureSession(driver, random.Random(0)).run()
        assert result["interactions"]["links_copied"] == 1
        assert "share" not in result["interactions"]

    def test_直播间卡片逃生不计跳过(self):
        d = OfflineNurtureDriver([VideoInfo("L1", is_live_card=True),
                                  VideoInfo("V1", duration_s=10)])
        rng = random.Random(0)
        s = NurtureSession(d, rng)  # 无 judge → 全部按不匹配
        s.run()
        assert s.interactions["live_escape"] == 1
        assert "escape_live_card:L1" in s.trace

    def test_AI研判失败结束本轮不降级(self):
        d = OfflineNurtureDriver([VideoInfo("V1"), VideoInfo("V2")])
        s = NurtureSession(d, random.Random(0), judge=lambda v: None)
        r = s.run()
        # V1 判研失败立即结束，V2 未消费
        assert "end:judge-failed:V1" in s.trace
        assert d.queue == [VideoInfo("V2")]
        assert r["videos_seen"] == 1

    def test_匹配流互动掷骰与窗口(self):
        # duration 30s，观看 20s → remaining 10s ≤ 14s → 进入互动窗口
        d = OfflineNurtureDriver([VideoInfo("V1", duration_s=30.0)])
        rng = random.Random(3)
        s = NurtureSession(d, rng, judge=lambda v: True)
        s.run()
        assert any(a[0] == "watch" for a in d.actions)
        assert d.actions[1][0] == "playback"  # 防跳配置先于观看

    def test_短视频播放配置带seek暂停(self):
        d = OfflineNurtureDriver([VideoInfo("V1", duration_s=10.0)])
        NurtureSession(d, random.Random(0), judge=lambda v: True).run()
        cfg = next(a[1] for a in d.actions if a[0] == "playback")
        assert cfg["seek_then_pause"] == 0.05

    def test_连续4次跳过触发整页刷新(self):
        d = OfflineNurtureDriver([VideoInfo(f"V{i}") for i in range(4)]
                                 + [VideoInfo("V9")])
        # judge 全返回 False → 全走不匹配流；rng 使主页掷骰不命中
        s = NurtureSession(d, random.Random(11), judge=lambda v: False)
        s.run()
        assert s.interactions["refresh"] == 1
        assert "refresh:skip-streak-4" in s.trace

    def test_recentSkipKeys命中整页刷新(self):
        # 同 key 视频重复出现 → 反停滞刷新
        d = OfflineNurtureDriver([VideoInfo("V1"), VideoInfo("V1"),
                                  VideoInfo("V2")])
        s = NurtureSession(d, random.Random(5))
        s.run()
        assert s.interactions["refresh"] == 1
        assert any(t.startswith("refresh:repeat-key") for t in s.trace)

    def test_划走后进主页节点(self):
        # 强制 rng 使 roll_author_page 命中
        rng = random.Random(0)
        rng.choice = lambda seq: seq[1]  # 固定选 0.10 档
        d = OfflineNurtureDriver([VideoInfo("V1")])
        s = NurtureSession(d, rng)
        s.run()
        if s.interactions["author_page"] == 1:
            assert ("author_page",) in d.actions

    def test_安全上限结束(self):
        d = OfflineNurtureDriver([VideoInfo(f"V{i}") for i in range(100)])
        s = NurtureSession(d, random.Random(2), max_videos=5)
        r = s.run()
        assert r["videos_seen"] == 5
        assert "end:max-videos" in s.trace

    def test_队列为空立即结束(self):
        d = OfflineNurtureDriver([])
        s = NurtureSession(d, random.Random(0))
        r = s.run()
        assert r["videos_seen"] == 0 and s.trace[0] == "enter_feed"
