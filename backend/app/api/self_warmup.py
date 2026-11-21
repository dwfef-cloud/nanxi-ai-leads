"""自动回复任务详情页的数据接口。

两路事件来源：
  1) autoreply_events 表 —— 自动回复运行时真实监听到的互动（私信等），带处理结论；
  2) tasks.payload.events —— 离线模拟/历史事件，用于没有真机运行时的演示与回归。
界面按同一套字段消费（eventType/nickname/content/status/actions/createdAt），
真实事件排在前面；没有真实事件时如实返回空态，不拿模拟数据充数。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.api import task_logs, task_store


# 运行时结论 → 前端认的状态词（user_stats 按 success/sent/replied 计成功）
_OUTCOME_STATUS = {
    "acted": "success",
    "matched": "recorded",
    "filtered": "ignored",
    "dedup": "ignored",
    "skipped": "ignored",
    "failed": "failed",
}


def _runtime_events(db_path, task_id: str) -> list[dict]:
    """读自动回复运行时的真实事件（最新在前）。表不存在时返回空，不报错。"""
    if not task_id:
        return []
    try:
        con = sqlite3.connect(str(db_path), timeout=15)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM autoreply_events WHERE task_id=?"
                           " ORDER BY id DESC LIMIT 1000", (task_id,)).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for row in rows:
        try:
            actions = json.loads(row["actions"] or "[]")
        except (TypeError, ValueError):
            actions = []
        source = str(row["source"] or "dm")
        event_type = "message" if source == "dm" else source
        out.append({
            "id": f"rt-{row['id']}",
            "eventType": event_type,
            "category": event_type,
            "nickname": str(row["nickname"] or row["conversation"] or ""),
            "content": str(row["content"] or ""),
            "accountId": str(row["account_id"] or ""),
            "status": _OUTCOME_STATUS.get(str(row["outcome"]), "recorded"),
            "outcome": str(row["outcome"] or ""),
            "reason": str(row["note"] or ""),
            "actions": actions if isinstance(actions, list) else [],
            "createdAt": int(row["created_ms"] or 0),
            "isGroupChat": "群聊" in str(row["note"] or ""),
            "source": "runtime",
        })
    return out


def _all_events(db_path, task_id: str) -> list[dict]:
    return _runtime_events(db_path, task_id) + _events(_task(db_path, task_id))


def _task(db_path, task_id: Any) -> dict | None:
    return task_store.get_task(db_path, "warmup", str(task_id or "").strip())


def _events(record: dict | None) -> list[dict]:
    if not record:
        return []
    raw = record.get("events")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _text(event: dict, *keys: str) -> str:
    for key in keys:
        value = event.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def list_events(db_path, args: dict | None = None) -> dict:
    args = args if isinstance(args, dict) else {}
    task_id = str(args.get("taskId") or "").strip()
    runtime = _runtime_events(db_path, task_id)
    events = runtime + _events(_task(db_path, task_id))
    keyword = str(args.get("keyword") or args.get("search") or "").strip().casefold()
    category = str(args.get("category") or "").strip()
    event_type = str(args.get("eventType") or "").strip()
    status = str(args.get("status") or "").strip()
    selected = []
    for index, event in enumerate(events):
        event_id = str(event.get("id") or f"{task_id}:event:{index}")
        event["id"] = event_id
        if category and str(event.get("category") or event.get("eventType") or "") != category:
            continue
        if event_type and str(event.get("eventType") or "") != event_type:
            continue
        if status and str(event.get("status") or "") != status:
            continue
        if keyword and keyword not in " ".join(str(v) for v in event.values()).casefold():
            continue
        selected.append(event)
    try:
        offset = max(0, int(args.get("offset") or 0))
        limit = min(200, max(1, int(args.get("limit") or 50)))
    except (TypeError, ValueError):
        offset, limit = 0, 50
    if runtime:
        note = f"来自自动回复运行时：真实监听到的互动与处理结论（{len(runtime)} 条）"
    elif selected:
        note = "仅包含任务记录里的离线模拟事件；真机运行后这里会显示真实互动"
    else:
        note = "还没有监听到互动：确认任务已启动，且账号有新私信"
    return {"success": True, "events": selected[offset:offset + limit],
            "items": selected[offset:offset + limit], "total": len(selected),
            "sourceAvailable": bool(runtime), "note": note}


def _filtered_events(db_path, args: dict) -> list[dict]:
    task_id = str(args.get("taskId") or "").strip()
    events = _all_events(db_path, task_id)
    keyword = str(args.get("keyword") or "").strip().casefold()
    return [event for event in events
            if not keyword or keyword in " ".join(str(v) for v in event.values()).casefold()]


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def user_stats(db_path, args: dict | None = None) -> dict:
    args = args if isinstance(args, dict) else {}
    events = _filtered_events(db_path, args)
    grouped: dict[str, dict] = {}
    for event in events:
        name = _text(event, "nickname", "userName", "username") or "未知用户"
        row = grouped.setdefault(name, {"nickname": name, "total": 0, "success": 0,
                                        "failed": 0, "replies": 0})
        row["total"] += 1
        state = str(event.get("status") or "").lower()
        row["success"] += state in {"success", "sent", "sent_ok", "replied"}
        row["failed"] += state in {"failed", "error"}
        row["replies"] += _count(event.get("replyCount") or event.get("rounds"))
    items = sorted(grouped.values(), key=lambda row: (-row["total"], row["nickname"]))
    return {"success": True, "items": items, "total": len(items)}


def account_stats(db_path, args: dict | None = None) -> dict:
    args = args if isinstance(args, dict) else {}
    events = _filtered_events(db_path, args)
    grouped: dict[str, dict] = {}
    for event in events:
        account = _text(event, "accountName", "accountId", "account") or "未知账号"
        row = grouped.setdefault(account, {"accountName": account, "total": 0,
                                           "success": 0, "failed": 0})
        row["total"] += 1
        state = str(event.get("status") or "").lower()
        row["success"] += state in {"success", "sent", "sent_ok", "replied"}
        row["failed"] += state in {"failed", "error"}
    return {"success": True, "items": list(grouped.values()), "total": len(grouped)}


def delete_events(db_path, args: dict | None = None) -> dict:
    args = args if isinstance(args, dict) else {}
    task_id = str(args.get("taskId") or "").strip()
    record = _task(db_path, task_id)
    if not record:
        return {"success": False, "error": "找不到任务记录"}
    events = _events(record)
    wanted = {str(value) for value in (args.get("eventIds") or [])}
    clear_all = args.get("clearAll") is True
    kept = [event for index, event in enumerate(events)
            if not clear_all and str(event.get("id") or f"{task_id}:event:{index}") not in wanted]
    removed = len(events) - len(kept)
    record["events"] = kept
    saved = task_store.save_task(db_path, "warmup", record)
    return {"success": bool(saved.get("success")), "deleted": removed,
            **({} if saved.get("success") else {"error": saved.get("error")})}


def clear_category(db_path, args: dict | None = None) -> dict:
    args = args if isinstance(args, dict) else {}
    task_id = str(args.get("taskId") or "").strip()
    category = str(args.get("category") or "").strip()
    record = _task(db_path, task_id)
    if not record:
        return {"ok": False, "reason": "找不到任务记录"}
    events = _events(record)
    kept = [event for event in events
            if str(event.get("category") or event.get("eventType") or "") != category]
    record["events"] = kept
    saved = task_store.save_task(db_path, "warmup", record)
    return {"ok": bool(saved.get("success")), "deleted": len(events) - len(kept),
            **({} if saved.get("success") else {"reason": saved.get("error")})}


def delete_tasks(db_path, args: dict | list | tuple | str | None = None) -> dict:
    # 壳的调用形如 invoke(ch, [idArray])：非 RAW_ARGS 通道经 _arg 归一后，
    # 传进来的可能是裸数组 / 裸字符串 / {"taskIds":[…]}，都要接住；
    # 否则裸数组会落进 {} → ids 为空 → 假成功（弹「已删除」实际删 0 条，2026-11-20 用户实测）。
    if isinstance(args, dict):
        raw_ids = args.get("taskIds") or args.get("ids") or []
    elif args is None:
        raw_ids = []
    else:
        raw_ids = args
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    # 展开嵌套数组（invoke(ch, [ids]) 会包一层）
    ids: list = []
    stack = list(raw_ids) if isinstance(raw_ids, (list, tuple)) else []
    guard = 0
    while stack and guard < 2000:
        guard += 1
        item = stack.pop(0)
        if isinstance(item, (list, tuple)):
            stack = list(item) + stack
        else:
            ids.append(item)
    deleted = []
    skipped_running = []
    skipped_missing = []
    for raw_id in ids if isinstance(ids, list) else []:
        task_id = str(raw_id or "").strip()
        if not task_id:
            continue
        from app.runtime import registry
        if task_id in registry.active_ids("warmup"):
            # 真在跑的不能删；但只跳过这一条，其余照删（2026-11-20 用户实测：
            # 只要有一条被跳过就整体 success:false → 前端中止、列表不刷新，
            # 已删掉的部分看起来像「没删掉」）。
            skipped_running.append(task_id)
            continue
        if task_store.delete_task(db_path, "warmup", task_id):
            deleted.append(task_id)
        else:
            skipped_missing.append(task_id)
    skipped = skipped_running + skipped_missing
    if deleted:
        result = {"success": True, "deleted": deleted, "skipped": skipped,
                  "count": len(deleted)}
        if skipped_running:
            result["partial"] = True
            result["message"] = (
                f"已删除 {len(deleted)} 条；{len(skipped_running)} 条运行中未删除（需先停止）"
                + (f"；{len(skipped_missing)} 条不存在" if skipped_missing else "")
            )
        return result
    if skipped_running:
        return {"success": False, "deleted": [], "skipped": skipped, "count": 0,
                "error": "所选任务都在运行中，无法删除；请先停止任务再删除"}
    return {"success": False, "deleted": [], "skipped": skipped, "count": 0,
            "error": "所选任务不存在或已被删除，请刷新列表后重试"}


def retry_events(_: dict | None = None) -> dict:
    return {"success": False, "code": "reply_executor_unavailable",
            "error": "自动回复补做未接通：当前没有可验证的平台通知事件读取器，未执行、未发送"}


def stop_retry(_: dict | None = None) -> dict:
    return {"success": True, "stopped": 0, "note": "没有运行中的补做执行器"}


def running_retry_ids(_: dict | None = None) -> list[str]:
    return []


def clear_user_rounds(_: dict | None = None) -> dict:
    return {"ok": False, "reason": "回复轮次尚无真实事件数据源，未清空任何记录"}


def clear_logs(db_path, args: dict | None = None) -> dict:
    args = args if isinstance(args, dict) else {}
    task_id = str(args.get("taskId") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少 taskId"}
    return {"success": True, "deleted": task_logs.clear(db_path, task_id)}


def logs(db_path, args: dict | None = None) -> dict:
    args = args if isinstance(args, dict) else {}
    task_id = str(args.get("taskId") or "").strip()
    return {"success": True, "logs": task_logs.list_for(db_path, task_id) if task_id else []}
