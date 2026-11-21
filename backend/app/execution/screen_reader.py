"""M2 读屏采集引擎 —— 只滚动 + 读 DOM 可见文本，绝不拦截接口响应或构造签名。

合规边界（§1.3 约束 1）：
- BrowserPage 是抽象协议，只暴露导航/滚动/可见文本读取，不暴露 network/response/hook
- 所有节点过 assert_visible_nodes；含接口响应特征的字段直接拒绝
- raw_page 不持久化；产出只是结构化可见文本
- 固定限速 1-3 条/秒（READ_MIN_INTERVAL_MS），默认不启用拟人化随机节奏

离线可测：注入 FakePage 即可跑完整读屏流程，不需要 patchright、不需要真实平台请求。
"""
from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.execution.params import (
    ENABLE_HUMANIZED_PACING, READ, READ_MIN_INTERVAL_MS,
)
from app.execution.read_model import (
    SearchSeed, VisibleComment, assert_seed, assert_visible_nodes,
)

SELECTORS_PATH = Path(__file__).with_name("selectors.yaml")
# 必需键固化在代码里，不信任配置文件自己声明（配置被改坏时 fallback_required 也可能被删）
REQUIRED_SELECTORS: tuple[str, ...] = (
    "comment_item", "comment_text", "comment_nickname", "search_card", "search_link",
)

AlertFn = Callable[[str, str], None]


class SelectorInvalid(RuntimeError):
    """选择器配置缺失或失效。调用方应降级 + 告警，不崩溃。"""


class BrowserPage(Protocol):
    """只读页面协议。刻意不提供 response / request / evaluate 能力。"""

    def goto(self, url: str) -> None: ...
    def scroll_by(self, pixels: int) -> None: ...
    def visible_nodes(self, selector: str, field_map: dict[str, str]) -> list[dict]: ...
    def wait_ms(self, ms: int) -> None: ...


@dataclass
class ReadStats:
    pages_read: int = 0
    nodes_seen: int = 0
    nodes_kept: int = 0
    rejected: int = 0
    alerts: list[str] = field(default_factory=list)
    elapsed_ms: int = 0


def load_selectors(path: str | Path | None = None) -> dict:
    """加载选择器配置。JSON 语法（YAML 子集），零第三方依赖。"""
    target = Path(path) if path else SELECTORS_PATH
    if not target.is_file():
        raise SelectorInvalid(f"选择器配置不存在: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise SelectorInvalid(f"选择器配置解析失败: {exc}") from exc
    missing = [key for key in REQUIRED_SELECTORS if not data.get(key)]
    if missing:
        raise SelectorInvalid(f"必需选择器缺失或为空: {missing}")
    return data


class ScreenReader:
    def __init__(
        self,
        page: BrowserPage,
        selectors: dict | None = None,
        *,
        alert: AlertFn | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        humanized_pacing: bool = ENABLE_HUMANIZED_PACING,
    ) -> None:
        self.page = page
        self.selectors = selectors if selectors is not None else load_selectors()
        self.alert = alert or (lambda code, msg: None)
        self.sleeper = sleeper
        self.rng = rng or random.Random()
        self.humanized_pacing = humanized_pacing
        self.stats = ReadStats()

    def _emit_alert(self, code: str, message: str) -> None:
        self.stats.alerts.append(f"{code}:{message}")
        self.alert(code, message)

    def _scroll_step(self) -> tuple[int, int]:
        """返回 (滚动像素, 步间等待毫秒)。默认固定保守值，不做拟人随机化。"""
        if self.humanized_pacing:
            px = self.rng.randint(READ.scroll_step_min_px, READ.scroll_step_max_px)
            wait = self.rng.randint(READ.scroll_wait_min_ms, READ.scroll_wait_max_ms)
        else:
            px, wait = READ.scroll_step_min_px, READ.scroll_wait_max_ms
        return px, max(wait, READ_MIN_INTERVAL_MS)

    def _field_map(self) -> dict[str, str]:
        s = self.selectors
        return {
            "nickname": s["comment_nickname"],
            "text": s["comment_text"],
            "time_text": s["comment_time"],
        }

    def read_video_comments(self, video_url: str, max_n: int = 50) -> list[VisibleComment]:
        """读取单个视频的可见评论文本。失败降级为空列表 + 告警，不抛崩溃。"""
        started = time.monotonic()
        results: list[VisibleComment] = []
        item_selector = self.selectors.get("comment_item")
        if not item_selector:
            self._emit_alert("SELECTOR_MISSING", "comment_item 为空，本轮采集降级")
            return results

        try:
            self.page.goto(video_url)
        except Exception as exc:  # 页面导航失败一律降级，不中断流水线
            self._emit_alert("NAV_FAILED", f"{type(exc).__name__}: {exc}")
            return results

        seen_hashes: set[str] = set()
        max_rounds = 20
        for round_index in range(max_rounds):
            if len(results) >= max_n:
                break
            try:
                raw_nodes = self.page.visible_nodes(item_selector, self._field_map())
            except Exception as exc:
                self._emit_alert("SELECTOR_INVALID", f"{type(exc).__name__}: {exc}")
                break

            if round_index > 0 and not raw_nodes:
                self._emit_alert("SELECTOR_INVALID", "评论区无可见节点，疑似改版")
                break

            kept_this_round = 0
            new_this_round = 0
            for raw in raw_nodes:
                # 按原始节点指纹去重：滚动后重复出现的 DOM 节点不重复计数/不重复拒绝
                fingerprint = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
                if fingerprint in seen_hashes:
                    continue
                seen_hashes.add(fingerprint)
                new_this_round += 1
                self.stats.nodes_seen += 1
                try:
                    comment = assert_visible_nodes([raw])[0]
                except Exception:
                    self.stats.rejected += 1
                    continue
                key = f"{comment.nickname}|{comment.text}"
                results.append(comment)
                kept_this_round += 1
                # 固定限速：产出每条至少间隔 READ_MIN_INTERVAL_MS
                self.sleeper(READ_MIN_INTERVAL_MS / 1000)
                if len(results) >= max_n:
                    break

            self.stats.pages_read += 1
            if new_this_round == 0 and round_index > 0:
                break
            px, wait_ms = self._scroll_step()
            self.page.scroll_by(px)
            self.page.wait_ms(wait_ms)

        self.stats.nodes_kept = len(results)
        self.stats.elapsed_ms = int((time.monotonic() - started) * 1000)
        return results

    def read_search_results(self, keyword: str, max_n: int = 20) -> list[SearchSeed]:
        """搜索页只读种子（标题/作者/链接），不落库、不标识用户。"""
        card_selector = self.selectors.get("search_card")
        link_selector = self.selectors.get("search_link")
        if not card_selector or not link_selector:
            self._emit_alert("SELECTOR_MISSING", "搜索页选择器为空，跳过种子读取")
            return []
        field_map = {
            "title": self.selectors.get("search_title", "h3"),
            "author": self.selectors.get("search_author", ".author"),
            "video_url": link_selector,
        }
        try:
            raw_nodes = self.page.visible_nodes(card_selector, field_map)
        except Exception as exc:
            self._emit_alert("SELECTOR_INVALID", f"搜索页读取失败: {type(exc).__name__}")
            return []

        seeds: list[SearchSeed] = []
        for raw in raw_nodes[:max_n]:
            # visible_nodes 对链接字段可能返回 href 属性而非文本，统一规整
            candidate = dict(raw)
            url = str(candidate.get("video_url") or "")
            if url.startswith("/video/"):
                candidate["video_url"] = f"https://www.douyin.com{url}"
            try:
                seeds.append(assert_seed(candidate))
            except Exception:
                self.stats.rejected += 1
        return seeds
