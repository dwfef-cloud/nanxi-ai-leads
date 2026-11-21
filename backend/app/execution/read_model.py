"""§1.3 约束 1 的数据来源断言 —— 只接受「页面渲染的可见文本」。

这是合规边界在代码层的落点：任何来自接口响应拦截、签名函数 hook、
a_bogus 构造请求的数据，都无法通过 assert_visible_comment 进入后续链路。

判据（保守）：
- 只允许 3 个字段：nickname / text / time_text（时间展示文本）
- 出现任何接口响应特征字段（uid、sec_uid、cid、aweme_id、cursor、response、
  headers、signature、a_bogus、msToken 等）直接拒绝
- nickname 必须是展示名，不接受数字 uid 形态
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.execution.compliance import ComplianceViolation

VISIBLE_FIELDS = frozenset({"nickname", "text", "time_text"})

INTERFACE_MARKERS: frozenset[str] = frozenset({
    "uid", "sec_uid", "secUid", "cid", "aweme_id", "awemeId", "cursor", "has_more",
    "status_code", "response", "headers", "signature", "a_bogus", "abogus",
    "msToken", "mstoken", "X-Bogus", "xbogus", "verifyFp", "s_v_web_id",
    "comment_id", "digg_count", "create_time", "ip_label", "user", "raw", "json",
    "api", "endpoint", "payload", "request", "cookie", "token",
})

_NUMERIC_ID_RE = re.compile(r"^\d{6,}$")


@dataclass(frozen=True)
class VisibleComment:
    nickname: str
    text: str
    time_text: str = ""


@dataclass(frozen=True)
class SearchSeed:
    """搜索页只读种子：标题 / 作者 / 链接，不落库、不标识用户。"""
    title: str
    author: str
    video_url: str


def assert_visible_comment(node: Mapping[str, object]) -> VisibleComment:
    """校验并转换一个 DOM 文本节点。越界即抛，不允许降级放行。"""
    keys = set(node)
    unknown = keys - VISIBLE_FIELDS
    if unknown:
        markers = sorted(unknown & INTERFACE_MARKERS)
        hint = f"（命中接口响应特征: {markers}）" if markers else ""
        raise ComplianceViolation(f"非可见文本字段{hint}: {sorted(unknown)}")
    nickname = str(node.get("nickname") or "").strip()
    text = str(node.get("text") or "").strip()
    time_text = str(node.get("time_text") or "").strip()
    if not text:
        raise ComplianceViolation("评论文本为空，拒绝入库")
    if _NUMERIC_ID_RE.match(nickname):
        raise ComplianceViolation("nickname 为数字 uid 形态，疑似接口数据")
    return VisibleComment(nickname=nickname[:80], text=text[:2000], time_text=time_text[:40])


def assert_visible_nodes(nodes: list[Mapping[str, object]]) -> list[VisibleComment]:
    return [assert_visible_comment(node) for node in nodes]


def assert_seed(node: Mapping[str, object]) -> SearchSeed:
    keys = set(node)
    unknown = keys - {"title", "author", "video_url"}
    if unknown:
        raise ComplianceViolation(f"搜索种子含越界字段: {sorted(unknown)}")
    url = str(node.get("video_url") or "").strip()
    if not url.startswith(("https://", "http://")):
        raise ComplianceViolation("搜索种子链接非法")
    return SearchSeed(
        title=str(node.get("title") or "")[:200],
        author=str(node.get("author") or "")[:80],
        video_url=url,
    )
