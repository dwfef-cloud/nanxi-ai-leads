"""Offline contract tests for engagement actions; never opens a browser or queues commands.

The injected driver is a fixture, not a production transport. Results are deliberately
named ``simulated_*`` so they cannot be confused with platform-confirmed actions.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable
from urllib.parse import urlparse

from app.execution.write_safety import WriteResult, WriteSafetyGate, WriteStatus


WRITE_ACTIONS = frozenset({"post_comment", "reply_comment", "dm", "favorite"})
ALL_ACTIONS = WRITE_ACTIONS | {"copy_share_link"}


@dataclass(frozen=True)
class SimulatedAction:
    action: str
    account_id: str
    target: str
    text: str = ""
    video_url: str = ""


def _video_id(url: str) -> str:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return ""
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme != "https" or parsed.hostname != "www.douyin.com"
            or parsed.username or parsed.password or port not in {None, 443}
            or len(parts) != 2 or parts[0] != "video"
            or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) < 6):
        return ""
    return parts[1]


def validate(action: SimulatedAction) -> str:
    if not isinstance(action, SimulatedAction) or not all(isinstance(v, str) for v in (
            action.action, action.account_id, action.target, action.text, action.video_url)):
        return "invalid_action_shape"
    if action.action not in ALL_ACTIONS:
        return "unsupported_action"
    if not action.account_id.strip() or not action.target.strip():
        return "missing_account_or_target"
    if action.action in {"post_comment", "favorite", "copy_share_link"}:
        if not _video_id(action.target):
            return "invalid_video_url"
    if action.video_url and (action.action != "reply_comment" or not _video_id(action.video_url)):
        return "invalid_reply_video"
    if (action.action in {"reply_comment", "dm"}
            and (len(action.target) > 120 or not all(
                c.isascii() and (c.isalnum() or c in "_:-") for c in action.target))):
        return "invalid_target"
    if action.action in {"post_comment", "reply_comment", "dm"}:
        size = len(action.text.strip())
        limit = 500 if action.action == "dm" else 200
        minimum = 5 if action.action == "post_comment" else 1
        if not minimum <= size <= limit:
            return "invalid_text_length"
    elif action.text.strip():
        return "unexpected_text"
    return ""


def classify_fixture(action: SimulatedAction, receipt: dict | None) -> str:
    """Strict fixture contract: an HTTP success alone is never proof of a write."""
    if not isinstance(receipt, dict):
        return "uncertain"
    if action.action == "copy_share_link":
        return "simulated_ok" if (receipt.get("copied") is True
                                  and receipt.get("url") == action.target) else "uncertain"
    status = receipt.get("http_status")
    body = receipt.get("body")
    if type(status) is not int or not isinstance(body, dict):
        return "uncertain"
    code = body.get("status_code")
    if type(code) is not int:
        return "uncertain"
    if status >= 500 or status < 200 or 300 <= status < 400:
        return "uncertain"
    if not 200 <= status < 300 or code != 0:
        return "simulated_fail"
    if action.action in {"post_comment", "reply_comment"}:
        comment = body.get("comment")
        confirmed = isinstance(comment, dict) and any(
            isinstance(comment.get(key), str) and bool(comment[key].strip()) for key in ("cid", "id"))
    elif action.action == "dm":
        message = body.get("message")
        confirmed = isinstance(message, dict) and isinstance(message.get("id"), str) and bool(message["id"].strip())
    else:
        confirmed = body.get("is_favorite") is True
    return "simulated_ok" if confirmed else "uncertain"


class OfflineSimulator:
    def __init__(self, fixture: Callable[[SimulatedAction], dict | None], *, gate=None) -> None:
        if gate is not None and getattr(gate, "namespace", "offline") != "offline":
            raise ValueError("offline fixtures cannot write to the live ledger")
        self._fixture = fixture
        self._gate = gate if gate is not None else WriteSafetyGate()
        self.counts = {name: 0 for name in ALL_ACTIONS}

    def run(self, action: SimulatedAction) -> str:
        error = validate(action)
        if error:
            return f"skipped_{error}"
        # Query strings/fragments must not allow the same video to be commented twice.
        target = (_video_id(action.target) if action.action in {
            "post_comment", "favorite", "copy_share_link"} else action.target)
        key = json.dumps([action.action, action.account_id.strip(), target], ensure_ascii=False)
        allowed, reason = self._gate.reserve(key)
        if not allowed:
            return f"skipped_{reason}"
        token = (self._gate.reservation_token(key)
                 if hasattr(self._gate, "reservation_token") else None)
        try:
            receipt = self._fixture(action)
        except Exception:
            receipt = None
        result = classify_fixture(action, receipt)
        if result == "simulated_ok":
            record = WriteResult(WriteStatus.SENT_OK, False, False, "fixture_confirmed")
        elif result == "simulated_fail":
            record = WriteResult(WriteStatus.SENT_FAIL, True, False, "fixture_declined")
        else:
            record = WriteResult(WriteStatus.UNCERTAIN, False, True, "fixture_uncertain")
        if token is not None:
            self._gate.record(key, record, token=token)
        else:
            self._gate.record(key, record)
        if result == "simulated_ok":
            self.counts[action.action] += 1
        return result
