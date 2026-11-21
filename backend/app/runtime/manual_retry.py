"""User-triggered, scope-bound retries for selected monitor leads."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.api import event_bus, task_store
from app.execution.live_scope import load_scope
from app.execution.persistent_gate import PersistentWriteSafetyGate
from app.runtime import registry, runners

_LOCK = threading.Lock()
_ACTIVE: dict[str, threading.Event] = {}
_ALLOWED = {"like-comment", "reply-comment", "send-dm", "follow-user", "share-comment"}
_MAX_RECORDS = 20
_MAX_ACTIONS = 50


def active_ids() -> list[str]:
    with _LOCK:
        return sorted(_ACTIVE)


def stop(task_id: str) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    with _LOCK:
        event = _ACTIVE.get(task_id)
        if event:
            event.set()
    return {"success": True, "stopped": bool(event),
            "note": "已下发停止" if event else "当前没有正在执行的补做互动"}


def _publish(task_id: str, running: bool) -> None:
    try:
        event_bus.publish("monitor-task-event", {
            "type": "manual-retry-state", "taskId": task_id, "running": running,
        })
    except Exception:  # noqa: BLE001 progress notification must not alter execution
        pass


def _lead_rows(db_path: Path, task_id: str, record_ids: list[str]) -> list[dict]:
    placeholders = ",".join("?" for _ in record_ids)
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id,nickname,comment,source_url,video_id,comment_id,comment_user_id,account "
            "FROM leads WHERE source='monitor_comment' AND source_task_id=? "
            f"AND id IN ({placeholders}) ORDER BY created_at DESC",
            (task_id, *record_ids),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def _reviewed_actions(scope: dict, row: dict) -> list[dict]:
    if not row.get("source_url") or not row.get("comment_id"):
        return []
    matches = []
    for action in scope.get("reviewedActions", []):
        if not isinstance(action, dict) or action.get("type") not in _ALLOWED:
            continue
        if (action.get("videoUrl") != row.get("source_url")
                or str(action.get("commentId") or "") != str(row.get("comment_id") or "")
                or action.get("commentAuthor") != row.get("nickname")
                or action.get("sourceText") != row.get("comment")):
            continue
        if (action.get("recipientId") and str(action.get("recipientId"))
                != str(row.get("comment_user_id") or "")):
            continue
        if action.get("type") == "like-comment" and action.get("text") != row.get("comment"):
            continue
        if action.get("type") == "send-dm" and (
                not row.get("comment_user_id")
                or str(action.get("recipientId") or "") != str(row.get("comment_user_id") or "")):
            continue
        matches.append(action)
    return matches


def retry(db_path, payload: Any) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    task_id = str(payload.get("taskId") or "").strip()
    raw_ids = payload.get("recordIds")
    if not task_id or not isinstance(raw_ids, list) or not raw_ids:
        return {"success": False, "error": "请选择本监控任务中的线索"}
    record_ids = list(dict.fromkeys(str(value).strip() for value in raw_ids
                                    if isinstance(value, (str, int)) and str(value).strip()))
    if not record_ids or len(record_ids) > _MAX_RECORDS:
        return {"success": False, "error": f"每批请选择 1 到 {_MAX_RECORDS} 条线索"}
    if payload.get("confirmedUnsent") is not True and type(payload.get("confirmedUnsent")) is not bool:
        return {"success": False, "error": "确认状态无效"}

    rec = task_store.get_task(db_path, "monitor", task_id)
    if not rec:
        return {"success": False, "error": "监控任务不存在或已删除"}
    from app.runtime import store
    store.init(db_path)
    cfg = runners.task_config(rec)
    account_id = runners.resolve_account(db_path, rec, cfg)
    scope = load_scope(account_id=account_id, task_id=task_id)
    if (not scope or scope.get("accountId") != account_id
            or scope.get("leadgenTaskId") != task_id
            or not isinstance(scope.get("reviewedActions"), list)):
        return {"success": False, "error": "缺少当前监控任务、账号和目标完全匹配的有效人工确认清单；未执行"}

    rows = _lead_rows(Path(db_path), task_id, record_ids)
    if len(rows) != len(record_ids):
        return {"success": False, "error": "所选线索中有记录不属于当前监控任务，已取消整批"}
    if any(str(row.get("account") or "") != account_id for row in rows):
        return {"success": False, "error": "所选线索的执行账号与当前确认账号不一致，已取消整批"}
    if not registry.claim_account(account_id, "monitor-retry:" + task_id):
        return {"success": False, "error": "该账号正在执行其他任务，请等待结束后再补做"}
    stop_event = threading.Event()
    with _LOCK:
        if task_id in _ACTIVE:
            registry.release_account(account_id, "monitor-retry:" + task_id)
            return {"success": False, "error": "该任务已有补做操作正在运行"}
        _ACTIVE[task_id] = stop_event
    _publish(task_id, True)

    totals = {"processed": 0, "completed": 0, "failed": 0, "skipped": 0}
    try:
        from app.execution.live_leadgen import perform
        from app.execution.leadgen_media import EXTRA_FIELDS
        from app.execution.live_workflow import action_key

        actions_by_row = [(row, _reviewed_actions(scope, row)) for row in rows]
        if any(not actions for _, actions in actions_by_row):
            return {"success": False, "error": "至少一条所选评论没有完全匹配的已确认互动；整批未执行"}
        planned = [(row, action) for row, actions in actions_by_row for action in actions]
        if len(planned) > _MAX_ACTIONS:
            return {"success": False, "error": f"本批动作超过 {_MAX_ACTIONS} 条安全上限，已取消整批"}

        gate = PersistentWriteSafetyGate(db_path, namespace="live")
        for row, action in planned:
            if stop_event.is_set():
                break
            key = action_key(account_id, {
                "type": action["type"], "videoUrl": action["videoUrl"],
                "recipientId": action.get("recipientId", ""),
                "commentId": action.get("commentId", ""),
            })
            if gate.state(key) == "uncertain":
                if payload.get("confirmedUnsent") is not True:
                    totals["skipped"] += 1
                    continue
                gate.confirm_manual(key, sent=False)
            if not gate.can_attempt(key)[0]:
                totals["skipped"] += 1
                continue
            result = perform(
                db_path, action["type"], account_id=account_id, task_id=task_id,
                video_url=action["videoUrl"], text=str(action.get("text") or ""),
                comment_id=str(action.get("commentId") or ""),
                comment_author=str(action.get("commentAuthor") or ""),
                recipient_id=str(action.get("recipientId") or ""),
                extras={key: action[key] for key in EXTRA_FIELDS if action.get(key)},
                stopped=stop_event.is_set,
            )
            totals["processed"] += 1
            if result.get("ok") is True and result.get("status") == "sent_ok":
                totals["completed"] += 1
            elif result.get("status") in {"skipped", "blocked"}:
                totals["skipped"] += 1
            else:
                totals["failed"] += 1
        return {"success": True, **totals, "stopped": stop_event.is_set()}
    except Exception:  # noqa: BLE001 no DB/browser details should leak to renderer
        return {"success": False, "error": "补做执行失败，请查看监控运行日志；未自动重试"}
    finally:
        with _LOCK:
            _ACTIVE.pop(task_id, None)
        registry.release_account(account_id, "monitor-retry:" + task_id)
        _publish(task_id, False)
