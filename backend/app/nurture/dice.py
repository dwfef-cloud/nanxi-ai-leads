"""互动独立掷骰（§5.2）：点赞/收藏/复制链接各 10%，进评论区 32%。"""
from __future__ import annotations

from typing import Protocol

from app.nurture.params import (
    AUTHOR_PAGE_PROBS, P_BROWSE_COMMENTS, P_COLLECT, P_LIKE, P_SHARE,
)


class Rng(Protocol):
    def random(self) -> float: ...
    def choice(self, seq): ...


def _roll(rng: Rng, p: float) -> bool:
    return rng.random() < p


def roll_like(rng: Rng) -> bool:
    return _roll(rng, P_LIKE)


def roll_collect(rng: Rng) -> bool:
    return _roll(rng, P_COLLECT)


def roll_copy_link(rng: Rng) -> bool:
    return _roll(rng, P_SHARE)


roll_share = roll_copy_link  # Legacy configuration name; no platform repost is performed.


def roll_browse_comments(rng: Rng) -> bool:
    return _roll(rng, P_BROWSE_COMMENTS)


def roll_author_page(rng: Rng) -> bool:
    """三档观测概率中随机选一档再掷骰。"""
    return _roll(rng, rng.choice(AUTHOR_PAGE_PROBS))
