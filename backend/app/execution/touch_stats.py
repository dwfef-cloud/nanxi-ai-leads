"""线索触达账本（只读聚合）—— 修掉池统计里的假数据（D26 收尾 / D28 前置）。

为什么单独一个模块
------------------
触达事实散落在多张表，且历史上混入过自动化测试记录：

    dm_send_results 全表 lead_id='__test__'、内容「模拟发送成功…」

旧实现直接 ``COUNT(*) WHERE status='success'``，于是线索池统计显示
「私信 19 条」——**一条真实发送都没有**。这属于 §「全部页面无假数据」
验收线以内的缺陷，必须由数据层解决，不能靠前端遮盖。

因此本模块的口径是：
  1. 只认真实业务记录，测试/模拟记录一律剔除（逐个来源显式判定）；
  2. 每个「触达类型」标注数据来源与是否可用，无来源如实报 0 并在
     note 里说明，不猜、不补、不用状态字段冒充动作次数；
  3. 只读（``mode=ro``），失败降级 ``PRAGMA query_only=ON``。

触达类型与权威来源
------------------
================  ==========================================  ==========
类型              权威来源                                    当前状态
================  ==========================================  ==========
reply             comment_tasks.status ∈ {replied,user_replied} 可用
message           dm_send_results.status='success'（去测试）    可用（现为 0）
like              account_events.action ∈ {like_sent,like}      无来源 → 0
follow            account_events.action ∈ {follow_sent,...}     无来源 → 0
profileComment    account_events.action ∈ {profile_comment_*}   无来源 → 0
================  ==========================================  ==========

刻意不做的事：
  - ``behavior_events.comment_sent`` 与 ``comment_tasks.replied`` 是同一动作的
    镜像（8 条事件对应同一批任务），两者相加会双计，故只认 comment_tasks；
  - ``behavior_events`` 的 ``comment`` / ``reply`` 是**采集到的线索侧内容**，
    不是我们发出的动作，不能计入触达。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping

TOUCH_KINDS: tuple[str, ...] = ("like", "reply", "profileComment", "follow", "message")

# 前端筛选标签 → 账本类型
LABEL_TO_KIND: dict[str, str] = {
    "点赞": "like", "like": "like",
    "回复": "reply", "评论回复": "reply", "reply": "reply",
    "首页评论": "profileComment", "主页评论": "profileComment",
    "profileComment": "profileComment", "profile_comment": "profileComment",
    "关注": "follow", "follow": "follow",
    "私信": "message", "message": "message", "dm": "message",
}

# account_events.action → 账本类型（comment_sent 故意不在表内，见模块注释）
ACTION_TO_KIND: dict[str, str] = {
    "like_sent": "like", "like": "like",
    "follow_sent": "follow", "follow": "follow",
    "profile_comment_sent": "profileComment",
    "profile_comment": "profileComment",
    "profileComment": "profileComment",
}

# 来源可用性说明（给页面 note 用，避免「0 条」被误解成「功能坏了」）
SOURCE_DESC: dict[str, str] = {
    "reply": "comment_tasks 回复完成记录",
    "message": "dm_send_results 成功记录（已剔除测试数据）",
    "like": "account_events 点赞事件（暂无记录源）",
    "follow": "account_events 关注事件（暂无记录源）",
    "profileComment": "account_events 主页评论事件（暂无记录源）",
}

_LEAD_ID_IN_DETAIL = re.compile(r"lead_id=([A-Za-z0-9_-]{3,64})")
_TEST_TOKENS = ("__test__", "test-", "test_", "-test", "_test", "mock", "demo", "示例")

REPLY_STATUSES = ("replied", "user_replied")


def blank_counts() -> dict[str, int]:
    return {kind: 0 for kind in TOUCH_KINDS}


def is_test_lead_id(value: Any) -> bool:
    """测试/示例 lead_id 判定。真实 leads.id 为 32 位十六进制，不含 t/s 等字符。"""
    text = str(value or "").strip().lower()
    if not text:
        return True
    return any(token in text for token in _TEST_TOKENS)


def _is_mock_text(*values: Any) -> bool:
    joined = " ".join(str(v or "") for v in values)
    return "模拟" in joined or "mock" in joined.lower() or "测试数据" in joined


class TouchLedger:
    """按 lead 聚合真实触达次数。实例只持有库路径，连接随用随关。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db = str(db_path)

    # ── 基础设施 ────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        path = Path(self._db)
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        except (sqlite3.Error, ValueError):
            conn = sqlite3.connect(str(path), timeout=5)
            conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _table_exists(self, name: str) -> bool:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    # ── 单源读取 ────────────────────────────────────────────────
    def _reply_counts(self) -> dict[str, int]:
        if not self._table_exists("comment_tasks"):
            return {}
        placeholders = ",".join("?" * len(REPLY_STATUSES))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT lead_id, COUNT(*) AS n FROM comment_tasks "
                f"WHERE status IN ({placeholders}) AND lead_id IS NOT NULL GROUP BY lead_id",
                REPLY_STATUSES,
            ).fetchall()
        return {r["lead_id"]: int(r["n"]) for r in rows if not is_test_lead_id(r["lead_id"])}

    def _message_counts(self) -> tuple[dict[str, int], int]:
        """返回 (真实成功私信, 被剔除的测试记录数)。"""
        if not self._table_exists("dm_send_results"):
            return {}, 0
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT lead_id, content, message, COUNT(*) AS n FROM dm_send_results "
                "WHERE status='success' GROUP BY lead_id, content, message"
            ).fetchall()
        real: dict[str, int] = {}
        dropped = 0
        for row in rows:
            count = int(row["n"])
            if is_test_lead_id(row["lead_id"]) or _is_mock_text(row["content"], row["message"]):
                dropped += count
                continue
            real[row["lead_id"]] = real.get(row["lead_id"], 0) + count
        return real, dropped

    def _event_counts(self) -> dict[str, dict[str, int]]:
        """account_events 里的点赞/关注/主页评论事件（lead_id 编码在 detail 文本）。"""
        result: dict[str, dict[str, int]] = {}
        if not self._table_exists("account_events"):
            return result
        actions = tuple(ACTION_TO_KIND)
        placeholders = ",".join("?" * len(actions))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT action, detail FROM account_events WHERE action IN ({placeholders})",
                actions,
            ).fetchall()
        for row in rows:
            kind = ACTION_TO_KIND.get(str(row["action"] or ""))
            match = _LEAD_ID_IN_DETAIL.search(str(row["detail"] or ""))
            if not kind or not match:
                continue
            lead_id = match.group(1)
            if is_test_lead_id(lead_id):
                continue
            bucket = result.setdefault(kind, {})
            bucket[lead_id] = bucket.get(lead_id, 0) + 1
        return result

    def _all(self) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
        """{lead_id: {kind: n}} + 元信息（被剔除的测试记录数）。"""
        by_lead: dict[str, dict[str, int]] = {}

        def bump(lead_id: str, kind: str, count: int) -> None:
            row = by_lead.setdefault(lead_id, blank_counts())
            row[kind] += count

        for lead_id, count in self._reply_counts().items():
            bump(lead_id, "reply", count)
        messages, dropped = self._message_counts()
        for lead_id, count in messages.items():
            bump(lead_id, "message", count)
        for kind, bucket in self._event_counts().items():
            for lead_id, count in bucket.items():
                bump(lead_id, kind, count)
        return by_lead, {"dropped_test_records": dropped}

    # ── 对外接口 ────────────────────────────────────────────────
    def per_lead(self, lead_ids: Iterable[Any]) -> dict[str, dict[str, int]]:
        """批量取若干线索的触达次数。未命中的 id 返回全 0 结构。"""
        wanted = [str(i) for i in lead_ids if i not in (None, "")]
        if not wanted:
            return {}
        by_lead, _ = self._all()
        return {lead_id: dict(by_lead.get(lead_id) or blank_counts()) for lead_id in wanted}

    def totals(self) -> dict[str, int]:
        by_lead, _ = self._all()
        sums = blank_counts()
        for counts in by_lead.values():
            for kind, value in counts.items():
                sums[kind] += value
        return sums

    def touched_ids(self) -> set[str]:
        by_lead, _ = self._all()
        return {lead_id for lead_id, counts in by_lead.items() if sum(counts.values()) > 0}

    def ids_with(self, kinds: Iterable[str]) -> set[str]:
        """有过指定类型触达的 lead 集合（用于筛选）。"""
        wanted = {LABEL_TO_KIND.get(str(k), str(k)) for k in kinds if k not in (None, "")}
        wanted &= set(TOUCH_KINDS)
        if not wanted:
            return set()
        by_lead, _ = self._all()
        return {
            lead_id for lead_id, counts in by_lead.items()
            if any(counts.get(kind, 0) > 0 for kind in wanted)
        }

    def report(self) -> dict[str, Any]:
        """数据来源可用性报告：哪类有源、哪类恒 0、剔除了多少假数据。"""
        _, meta = self._all()
        available = self._available_sources()
        return {
            "sources": {kind: SOURCE_DESC[kind] for kind in TOUCH_KINDS},
            "available": available,
            "missing": [kind for kind in TOUCH_KINDS if not available.get(kind)],
            "dropped_test_records": meta["dropped_test_records"],
        }

    def _available_sources(self) -> dict[str, bool]:
        """逐类型判断是否有真实记录源（不能因为某类有记录就把同类全部标为可用）。"""
        available = {
            "reply": self._table_exists("comment_tasks"),
            "message": self._table_exists("dm_send_results"),
            "like": False, "follow": False, "profileComment": False,
        }
        if not self._table_exists("account_events"):
            return available
        groups = {
            "like": ("like_sent", "like"),
            "follow": ("follow_sent", "follow"),
            "profileComment": ("profile_comment_sent", "profile_comment", "profileComment"),
        }
        try:
            with closing(self._connect()) as conn:
                for kind, actions in groups.items():
                    placeholders = ",".join("?" * len(actions))
                    row = conn.execute(
                        f"SELECT 1 FROM account_events WHERE action IN ({placeholders}) LIMIT 1",
                        actions,
                    ).fetchone()
                    available[kind] = row is not None
        except sqlite3.Error:
            pass
        return available


def counts_to_flags(counts: Mapping[str, int]) -> dict[str, Any]:
    """账本次数 → 页面布尔标记。"""
    return {
        "liked": counts.get("like", 0) > 0,
        "replied": counts.get("reply", 0) > 0,
        "followed": counts.get("follow", 0) > 0,
        "messaged": counts.get("message", 0) > 0,
    }
