"""M7 写入安全状态机（横切）—— 防重复评论 / 重复私信。

语义移植自 CreatorHub `_post_json`（TEST-CHECKLIST C 组）：
    sent_ok / sent_fail / uncertain（超时、5xx、200 空响应）
    uncertain → 停止重试 + 告警 + 人工确认

反面教材：阿里山「失败记录混进线索库」导致重复触达。
本模块只做判定与去重，不发起任何真实写入请求。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading


class WriteStatus(str, Enum):
    SENT_OK = "sent_ok"
    SENT_FAIL = "sent_fail"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class WriteResult:
    status: WriteStatus
    retryable: bool
    needs_alert: bool
    reason: str


def classify_response(
    *,
    status_code: int | None = None,
    body: str | dict | None = None,
    error: BaseException | None = None,
) -> WriteResult:
    """把一次写入尝试归一化为三态。"""
    if error is not None:
        if isinstance(error, (TimeoutError,)):
            return WriteResult(WriteStatus.UNCERTAIN, False, True, "timeout")
        return WriteResult(WriteStatus.SENT_FAIL, True, False, f"error:{type(error).__name__}")

    if status_code is None:
        return WriteResult(WriteStatus.UNCERTAIN, False, True, "no_response")

    if 200 <= status_code < 300:
        # 200 但空响应 / 缺确认字段 → 服务端可能已落库，绝不能重试
        if body is None or body == "" or body == {}:
            return WriteResult(WriteStatus.UNCERTAIN, False, True, "2xx_empty_body")
        if isinstance(body, dict) and body.get("ok") is False:
            return WriteResult(WriteStatus.SENT_FAIL, True, False, "server_declined")
        return WriteResult(WriteStatus.SENT_OK, False, False, "confirmed")

    if 400 <= status_code < 500:
        return WriteResult(WriteStatus.SENT_FAIL, True, False, f"client_{status_code}")

    return WriteResult(WriteStatus.UNCERTAIN, False, True, f"server_{status_code}")


class WriteSafetyGate:
    """按幂等键拦截重复写入。uncertain 的键永久占用，直到人工确认。"""

    def __init__(self) -> None:
        self._states: dict[str, WriteStatus] = {}
        self.pending_manual: set[str] = set()
        self._reservation_lock = threading.Lock()

    def can_attempt(self, key: str) -> tuple[bool, str]:
        current = self._states.get(key)
        if current == WriteStatus.SENT_OK:
            return False, "already_sent"
        if current == WriteStatus.UNCERTAIN:
            return False, "uncertain_pending_manual"
        if key in self.pending_manual:
            return False, "manual_review_required"
        return True, "allowed"

    def reserve(self, key: str) -> tuple[bool, str]:
        """Block a second attempt before the first driver call can time out."""
        with self._reservation_lock:
            allowed, reason = self.can_attempt(key)
            if allowed:
                self._states[key] = WriteStatus.UNCERTAIN
                self.pending_manual.add(key)
            return allowed, reason

    def record(self, key: str, result: WriteResult) -> WriteResult:
        self._states[key] = result.status
        if result.status == WriteStatus.UNCERTAIN:
            self.pending_manual.add(key)
        else:
            self.pending_manual.discard(key)
        return result

    def confirm_manual(self, key: str, *, sent: bool) -> None:
        """人工确认 uncertain：确认已发 → 视为 sent_ok；确认未发 → 允许重试。"""
        self.pending_manual.discard(key)
        if sent:
            self._states[key] = WriteStatus.SENT_OK
        else:
            self._states.pop(key, None)

    def state(self, key: str) -> str:
        return self._states.get(key, "none").value if key in self._states else "none"
