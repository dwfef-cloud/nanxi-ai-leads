"""D9 + D10 互动 worker —— 执行前三查 + 写入安全状态机接线。

三查（DESIGN-DOC §3 M4，执行任一动作前）：
  1. 账号配额余量（QuotaLedger.can_do）
  2. 冷却到期（评论冷却 300-600s 随机，§5.1）
  3. 当日封号风险事件（risk_check 可注入，默认无）

写入安全（M7）：复用 execution.write_safety 的三态机 ——
  sent_ok / sent_fail / uncertain；uncertain → 停止重试 + 告警 + 人工确认，
  幂等键 = f"{action}:{account_id}:{candidate_id}"，防重复评论。

评论内容只来自 AICommentGenerator（生成失败 → 跳过，不用模板兜底）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.engagement.ai_comment import AICommentGenerator
from app.engagement.engine import EngineManager
from app.engagement.humanize import comment_cooldown_s
from app.engagement.quotas import QuotaLedger
from app.execution.store import ExecutionStore
from app.execution.write_safety import (
    WriteSafetyGate, WriteResult, WriteStatus, classify_response,
)

AlertFn = Callable[[str, str], None]


def _noop_alert(code: str, message: str) -> None:  # pragma: no cover
    pass


@dataclass
class ActionResult:
    candidate_id: int
    action: str
    status: str            # sent_ok / sent_fail / uncertain / skipped_*
    reason: str
    content: str = ""


@dataclass
class WorkerDeps:
    store: ExecutionStore
    ledger: QuotaLedger
    engine: EngineManager
    gate: WriteSafetyGate = field(default_factory=WriteSafetyGate)
    alert: AlertFn = _noop_alert
    risk_check: Callable[[int], list[str]] | None = None  # 返回当日风险事件列表


def _precheck(deps: WorkerDeps, account_id: int, metric: str,
              *, now: int | None = None) -> str | None:
    """执行前三查。返回 None = 通过；返回字符串 = 跳过原因。"""
    ok, why = deps.ledger.can_do(account_id, metric, now=now)
    if not ok:
        return why
    if deps.risk_check is not None:
        events = deps.risk_check(account_id) or []
        if events:
            return f"risk_event:{events[0]}"
    return None


class LikeWorker:
    """对候选关联视频点赞。成功后记 like 账本。"""

    def __init__(self, deps: WorkerDeps) -> None:
        self.deps = deps

    def run_one(self, candidate: dict, account_id: int, *, now: int | None = None) -> ActionResult:
        ts = int(time.time()) if now is None else int(now)
        cid = int(candidate["id"])
        video_url = str(candidate.get("video_url") or "")
        if not video_url:
            return ActionResult(cid, "like", "skipped_no_url", "candidate 无 video_url")

        blocked = _precheck(self.deps, account_id, "like", now=ts)
        if blocked:
            return ActionResult(cid, "like", "skipped_quota" if blocked.startswith("quota") else "skipped_risk", blocked)

        gate_key = f"like:{account_id}:{cid}"
        can, why = self.deps.gate.can_attempt(gate_key)
        if not can:
            return ActionResult(cid, "like", "skipped_dup", why)

        lease = self.deps.engine.lease_of(account_id, now=ts)
        if lease is None:
            return ActionResult(cid, "like", "skipped_no_lease", "账号无活跃租约，先 acquire")
        try:
            status_code, body = lease.driver.like(video_url)
        except NotImplementedError as exc:
            return ActionResult(cid, "like", "skipped_unavailable", str(exc))
        result = classify_response(status_code=status_code, body=body)
        self.deps.gate.record(gate_key, result)
        if result.status is WriteStatus.SENT_OK:
            self.deps.ledger.consume(account_id, "like", now=ts)
        elif result.needs_alert:
            self.deps.alert("like_uncertain", f"{gate_key} {result.reason}")
        return ActionResult(cid, "like", result.status.value, result.reason)


class CommentWorker:
    """对候选发表 AI 生成的评论。三查 + AI 生成 + 写入安全 + 冷却随机器。"""

    def __init__(self, deps: WorkerDeps, generator: AICommentGenerator | None = None) -> None:
        self.deps = deps
        self.generator = generator or AICommentGenerator()
        # 账号 → 冷却到期时间戳（冷却值 300-600s 随机，发出评论时掷骰生成）
        self._cooldown_until: dict[int, int] = {}

    def cooldown_remaining(self, account_id: int, *, now: int | None = None) -> int:
        ts = int(time.time()) if now is None else int(now)
        return max(0, self._cooldown_until.get(account_id, 0) - ts)

    def run_one(self, candidate: dict, account_id: int, *, now: int | None = None,
                generator: AICommentGenerator | None = None) -> ActionResult:
        ts = int(time.time()) if now is None else int(now)
        cid = int(candidate["id"])
        video_url = str(candidate.get("video_url") or "")
        if not video_url:
            return ActionResult(cid, "comment", "skipped_no_url", "candidate 无 video_url")

        # 1) 三查（含评论冷却）
        blocked = _precheck(self.deps, account_id, "comment", now=ts)
        if blocked:
            return ActionResult(cid, "comment", "skipped_quota" if blocked.startswith("quota") else "skipped_risk", blocked)
        remain = self.cooldown_remaining(account_id, now=ts)
        if remain > 0:
            return ActionResult(cid, "comment", "skipped_cooldown", f"cooldown {remain}s")

        # 2) 幂等闸门（AI 生成在前会浪费调用，先过闸门）
        gate_key = f"comment:{account_id}:{cid}"
        can, why = self.deps.gate.can_attempt(gate_key)
        if not can:
            return ActionResult(cid, "comment", "skipped_dup", why)

        # 3) AI 生成：失败即跳过，绝不模板兜底
        gen = generator or self.generator
        ai = gen.generate(comment_text=str(candidate.get("comment_text") or ""))
        if ai is None:
            return ActionResult(cid, "comment", "skipped_no_ai", "AI 生成失败/未通过自检，宁可不评")

        # 4) 驱动写入 → 三态归一
        lease = self.deps.engine.lease_of(account_id, now=ts)
        if lease is None:
            return ActionResult(cid, "comment", "skipped_no_lease", "账号无活跃租约，先 acquire")
        try:
            status_code, body = lease.driver.comment(video_url, ai.text)
        except NotImplementedError as exc:
            return ActionResult(cid, "comment", "skipped_unavailable", str(exc))
        result: WriteResult = classify_response(status_code=status_code, body=body)
        self.deps.gate.record(gate_key, result)

        # 5) 落库 + 记账 + 冷却掷骰
        self.deps.store.log_send(candidate_id=cid, content=ai.text,
                                 status=result.status.value, account_id=account_id, now=ts)
        if result.status is WriteStatus.SENT_OK:
            self.deps.ledger.consume(account_id, "comment", now=ts)
            self._cooldown_until[account_id] = ts + comment_cooldown_s()
        elif result.needs_alert:
            self.deps.alert("comment_uncertain", f"{gate_key} {result.reason}")
        return ActionResult(cid, "comment", result.status.value, result.reason, ai.text)

    def confirm_uncertain(self, account_id: int, candidate_id: int, *, sent: bool) -> None:
        """人工确认 uncertain 写入（管理面调用）。"""
        self.deps.gate.confirm_manual(f"comment:{account_id}:{candidate_id}", sent=sent)
