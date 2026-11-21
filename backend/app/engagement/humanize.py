"""D11 拟人化节奏执行器 —— §5.1 锁定参数的随机化（只产决策，不做任何 IO）。

参数来源：app.execution.params.ENGAGEMENT（§5.1 锁定值，禁止凭感觉改）。
- 逐字输入延迟 80-200ms 随机
- AI 生成后提交停顿 20-27s
- 评论获客冷却 300-600s 随机
- 评论区探测 12-13s/轮
- 输入框坐标每次实时计算，不缓存（§5.1「三轮坐标不同」的观测教训）

本模块只生成时间/坐标决策；是否 sleep 由调用方（worker）决定，
离线测试注入 rng 即可完全确定性复现。
"""
from __future__ import annotations

import random
from typing import Protocol

from app.execution.params import ENGAGEMENT


class Rng(Protocol):
    def uniform(self, a: float, b: float) -> float: ...
    def randint(self, a: int, b: int) -> int: ...


def keystroke_delay_ms(rng: Rng | None = None) -> int:
    """逐字输入延迟：80-200ms 随机。"""
    rng = rng or random
    return rng.randint(ENGAGEMENT.keystroke_delay_min_ms, ENGAGEMENT.keystroke_delay_max_ms)


def submit_pause_s(rng: Rng | None = None) -> float:
    """AI 生成完成后、提交前的停顿：20-27s（实测值）。"""
    rng = rng or random
    return rng.uniform(ENGAGEMENT.ai_submit_pause_min_s, ENGAGEMENT.ai_submit_pause_max_s)


def comment_cooldown_s(rng: Rng | None = None) -> int:
    """评论获客冷却：300-600s 随机（实测 409/509/365s）。"""
    rng = rng or random
    return rng.randint(ENGAGEMENT.comment_cooldown_min_s, ENGAGEMENT.comment_cooldown_max_s)


def probe_interval_s(rng: Rng | None = None) -> int:
    """评论区探测轮询间隔：12-13s。"""
    rng = rng or random
    return rng.randint(ENGAGEMENT.comment_probe_min_s, ENGAGEMENT.comment_probe_max_s)


# 输入框点击点在矩形内的归一化范围（避开边缘 20% 区域，贴近真人落点）
_BOX_INSET = 0.20


def input_box_point(
    rect: tuple[int, int, int, int], rng: Rng | None = None
) -> tuple[int, int]:
    """根据输入框当前 rect 计算一个点击点。

    rect = (x, y, width, height)。每次调用重新计算（实时定位），
    绝不缓存坐标 —— 调用方必须在每次动作前重新取 rect 并调本函数。
    """
    rng = rng or random
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        raise ValueError("输入框 rect 尺寸非法")
    dx_min, dx_max = w * _BOX_INSET, w * (1 - _BOX_INSET)
    dy_min, dy_max = h * _BOX_INSET, h * (1 - _BOX_INSET)
    return (
        int(x + rng.uniform(dx_min, dx_max)),
        int(y + rng.uniform(dy_min, dy_max)),
    )


def pacing_profile() -> dict[str, object]:
    """当前节奏参数快照（供管理面/测试对账 §5.1）。"""
    return {
        "keystroke_delay_ms": [ENGAGEMENT.keystroke_delay_min_ms, ENGAGEMENT.keystroke_delay_max_ms],
        "submit_pause_s": [ENGAGEMENT.ai_submit_pause_min_s, ENGAGEMENT.ai_submit_pause_max_s],
        "comment_cooldown_s": [ENGAGEMENT.comment_cooldown_min_s, ENGAGEMENT.comment_cooldown_max_s],
        "probe_interval_s": [ENGAGEMENT.comment_probe_min_s, ENGAGEMENT.comment_probe_max_s],
        "input_box_recompute_each_time": ENGAGEMENT.input_box_recompute_each_time,
    }
