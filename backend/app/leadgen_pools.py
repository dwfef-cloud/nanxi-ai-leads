"""评论获客运行时资源池：搜索关键词 / 话题链接 / 指定视频。

壳的启动流程在创建任务前必须先初始化资源池（init-leadgen-*-pool），
账号运行时再逐个领取关键词（claim-leadgen-keyword）。此前这些通道未接线，
启动流程第一步就收到「功能未开通」直接中止——这就是用户点「开始」后
任务不启动、实况大屏无运行记录的根因。

存储落在 lead_system.db 的 leadgen_pools 表（跨重启可恢复，resume 语义）。
release(preserve=true) 保留池内容并清空占用，供「重启/继续任务」复用。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()

_KINDS = ("keyword", "hashtag", "specific")


def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _ensure(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS leadgen_pools (
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (task_id, kind)
        )"""
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(con: sqlite3.Connection, task_id: str, kind: str) -> dict | None:
    row = con.execute(
        "SELECT payload FROM leadgen_pools WHERE task_id=? AND kind=?", (task_id, kind)
    ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save(con: sqlite3.Connection, task_id: str, kind: str, payload: dict) -> None:
    con.execute(
        """INSERT INTO leadgen_pools(task_id, kind, payload, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(task_id, kind) DO UPDATE SET payload=excluded.payload,
               updated_at=excluded.updated_at""",
        (task_id, kind, json.dumps(payload, ensure_ascii=False), _now()),
    )


# 关键词分隔符：中英文逗号 / 顿号 / 分号 / 斜杠 / 竖线 / 任意空白。
# 2026-11-19 真机踩到：用户在设置页把三个词写成「多少钱 / 怎么做 / 怎么联系」，
# 旧实现只按逗号和换行切，前端按空白再切一次之后，斜杠本身成了一条独立关键词
# 进了池子（池里能看到 value="/"），账号领到它就去抖音搜一个斜杠，白跑一轮。
_SEPARATORS = re.compile(r"[,，、;；/|｜\s]+")
# 纯标点（不含任何字母、数字、汉字）的值没有搜索意义，一律丢弃。
_PUNCT_ONLY = re.compile(r"^[\W_]+$")


def _meaningful(value: Any) -> bool:
    s = str(value or "").strip()
    return bool(s) and not _PUNCT_ONLY.match(s)


def _clean_values(values: Any) -> list[str]:
    out: list[str] = []
    if isinstance(values, str):
        values = _SEPARATORS.split(values)
    if not isinstance(values, (list, tuple)):
        return out
    for v in values:
        # 列表里也可能夹着未拆开的「词/词」，逐项再拆一次
        for part in _SEPARATORS.split(str(v)):
            s = part.strip()
            if _meaningful(s) and s not in out:
                out.append(s)
    return out


def _summary(payload: dict) -> dict:
    items = payload.get("items") or []
    remaining = sum(1 for it in items if it.get("status") == "pending")
    return {"total": len(items), "remaining": remaining}


def init_pool(db_path, task_id: str, kind: str, values: Any, resume: bool = False) -> dict:
    """初始化（或恢复）一个池。resume=true 时保留原占用，仅补齐缺失项。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"error": "缺少 leadgenTaskId"}
    if kind not in _KINDS:
        return {"error": f"未知资源池类型 {kind}"}
    values = _clean_values(values)
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        old = _load(con, task_id, kind)
        if resume and old is not None:
            # 旧池里可能残留历史脏值（例如分隔符被当成关键词的「/」），
            # 恢复时一并剔除，否则它还会被领走去搜一次。
            items = [it for it in (old.get("items") or [])
                     if isinstance(it, dict) and _meaningful(it.get("value"))]
            known = {it.get("value") for it in items}
            for v in values:
                if v not in known:
                    items.append({"value": v, "status": "pending", "claimed_by": None})
            # 恢复时把上次运行中未完成的占用释放回待领取
            restored = sum(1 for it in items if it.get("status") == "claimed")
            for it in items:
                if it.get("status") == "claimed":
                    it["status"] = "pending"
                    it["claimed_by"] = None
            payload = {"items": items, "token": old.get("token") or str(uuid.uuid4())}
            _save(con, task_id, kind, payload)
            out = _summary(payload)
            out.update({"resumed": True, "restoredPending": restored,
                        "token": payload["token"]})
            return out
        items = [{"value": v, "status": "pending", "claimed_by": None} for v in values]
        payload = {"items": items, "token": str(uuid.uuid4())}
        _save(con, task_id, kind, payload)
        out = _summary(payload)
        out.update({"resumed": False, "restoredPending": 0, "token": payload["token"]})
        return out


def claim_keyword(db_path, task_id: str, account_id: str, reuse_active: bool = False) -> dict:
    """账号领取一个搜索关键词。重复领取（续跑）返回同一关键词。"""
    task_id = str(task_id or "").strip()
    account_id = str(account_id or "").strip()
    if not task_id or not account_id:
        return {"error": "缺少 leadgenTaskId 或 accountId"}
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        payload = _load(con, task_id, "keyword")
        items = (payload or {}).get("items") or []
        for it in items:
            if it.get("claimed_by") == account_id and it.get("status") == "claimed":
                return {"keyword": it["value"], "resumed": True}
        for it in items:
            if it.get("status") == "pending":
                # 兜底：历史池里若还留着纯标点项，跳过而不是让账号去搜它
                if not _meaningful(it.get("value")):
                    continue
                it["status"] = "claimed"
                it["claimed_by"] = account_id
                _save(con, task_id, "keyword", payload)
                return {"keyword": it["value"], "resumed": False}
        if reuse_active:
            # 池已空：允许多账号共用已领取的关键词（壳的 reuseActive 语义）
            for it in items:
                if it.get("status") == "claimed" and _meaningful(it.get("value")):
                    return {"keyword": it["value"], "resumed": True, "shared": True}
        return {"error": "搜索关键词已用完，请在任务配置中补充关键词"}


def release_pool(db_path, task_id: str, kind: str, preserve: bool = True) -> dict:
    """释放池。preserve=true 保留内容并清空占用（重启/继续任务用）；否则删除。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"error": "缺少 leadgenTaskId"}
    if kind not in _KINDS:
        return {"error": f"未知资源池类型 {kind}"}
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        if not preserve:
            con.execute(
                "DELETE FROM leadgen_pools WHERE task_id=? AND kind=?", (task_id, kind)
            )
            return {"success": True, "preserved": False}
        payload = _load(con, task_id, kind)
        if payload is not None:
            for it in payload.get("items") or []:
                if it.get("status") == "claimed":
                    it["status"] = "pending"
                    it["claimed_by"] = None
            _save(con, task_id, kind, payload)
        return {"success": True, "preserved": True}


# ---------------------------------------------------------------------------
# 任务记录（save-leadgen-task / update-leadgen-task）
# ---------------------------------------------------------------------------

_RECORD_CATEGORY = "leadgen_tasks"
_RECORD_KEY = "records"


def _kv_get(con: sqlite3.Connection, category: str, key: str) -> str | None:
    try:
        row = con.execute(
            "SELECT value FROM system_settings WHERE category=? AND key=?",
            (category, key),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["value"] if row else None


def _kv_set(con: sqlite3.Connection, category: str, key: str, value: str) -> None:
    con.execute(
        """INSERT INTO system_settings(category, key, value, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(category, key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""",
        (category, key, value, _now()),
    )


def _load_records(con: sqlite3.Connection) -> dict[str, dict]:
    raw = _kv_get(con, _RECORD_CATEGORY, _RECORD_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_task_record(db_path, record: dict, *, preserve_live_run: bool = False) -> dict:
    """保存/覆盖一条评论获客任务记录（壳的 taskRecord 全量 JSON）。"""
    task_id = str((record or {}).get("id") or "").strip()
    if not task_id:
        return {"error": "任务记录缺少 id"}
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS system_settings (
                       category TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
                       updated_at TEXT, PRIMARY KEY (category, key))"""
            )
        except sqlite3.OperationalError:
            pass
        records = _load_records(con)
        if preserve_live_run:
            previous = records.get(task_id, {})
            run_id = previous.get("liveReviewedRunId")
            reviewed = previous.get("reviewedAccounts", {})
            known_runs = {run_id} | {r.get("runTaskId") for r in reviewed.values()}
            if reviewed:
                record = {**record, "reviewedAccounts": reviewed, "liveReviewedRunId": run_id}
            if run_id and any(r.get("automationTaskId") in known_runs for r in record.get("runs", []) if isinstance(r, dict)):
                # This merge and the save share the same lock as runtime updates.
                # A late renderer 'running' snapshot must not erase completion.
                record = {**record, "liveReviewedRunId": run_id}
                if previous.get("endedAt") or reviewed:
                    for key in ("status", "stats", "runs", "endedAt", "endReason", "lastMessage"):
                        if key in previous:
                            record[key] = previous[key]
        records[task_id] = record
        _kv_set(con, _RECORD_CATEGORY, _RECORD_KEY, json.dumps(records, ensure_ascii=False))
    return {"success": True, "total": len(records)}


def update_task_record(db_path, task_id: str, patch: dict) -> dict:
    """按 patch 合并更新任务记录（状态、结束时间、结束原因等）。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"error": "缺少任务 id"}
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS system_settings (
                       category TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
                       updated_at TEXT, PRIMARY KEY (category, key))"""
            )
        except sqlite3.OperationalError:
            pass
        records = _load_records(con)
        rec = records.get(task_id)
        if rec is None:
            rec = {"id": task_id}
        for k, v in (patch or {}).items():
            if k in ("runs", "configSnapshot", "stats") and isinstance(v, (list, dict)):
                rec[k] = v
            else:
                rec[k] = v
        records[task_id] = rec
        _kv_set(con, _RECORD_CATEGORY, _RECORD_KEY, json.dumps(records, ensure_ascii=False))
    return {"success": True}


def delete_task_record(db_path, task_id: str) -> int:
    """删除一条任务记录。返回 1 表示确实删掉了，0 表示本来就没有。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        return 0
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS system_settings (
                       category TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
                       updated_at TEXT, PRIMARY KEY (category, key))"""
            )
        except sqlite3.OperationalError:
            pass
        records = _load_records(con)
        if task_id not in records:
            return 0
        records.pop(task_id, None)
        _kv_set(con, _RECORD_CATEGORY, _RECORD_KEY, json.dumps(records, ensure_ascii=False))
    return 1


def reviewed_run_update(db_path, task_id, account_id, run_task_id, *, initial=None, result=None):
    """Atomic account-level snapshots prevent a fast run erasing another account."""
    with _LOCK, _connect(db_path) as con:
        con.execute("CREATE TABLE IF NOT EXISTS system_settings (category TEXT,key TEXT,value TEXT,updated_at TEXT,PRIMARY KEY(category,key))")
        records = _load_records(con)
        record = records.get(task_id, {"id": task_id})
        accounts = record.get("reviewedAccounts", {})
        if initial is not None:
            for key in ("name", "configSnapshot", "startedAt"):
                record[key] = initial[key]
            record["liveReviewedRunId"] = run_task_id
            accounts[account_id] = {"runTaskId": run_task_id, "status": "prepared", "preparedAt": initial["startedAt"], "stats": {}}
        if result is not None:
            previous = accounts.get(account_id, {})
            if previous.get("runTaskId") not in {None, run_task_id}:
                return
            accounts[account_id] = {"runTaskId": run_task_id, **result}
            runs = [r for r in record.get("runs", []) if r.get("automationTaskId") != result["run"]["automationTaskId"]]
            record["runs"] = (runs + [result["run"]])[-100:]
        record["reviewedAccounts"] = accounts
        totals = {}
        for account in accounts.values():
            for field, value in account.get("stats", {}).items():
                if type(value) in (int, float):
                    totals[field] = totals.get(field, 0) + value
        record["stats"] = totals
        unfinished = any(a.get("status") in {"prepared", "running"} for a in accounts.values())
        record["status"] = "running" if unfinished else (
            "failed" if any(a.get("status") == "failed" for a in accounts.values()) else
            "stopped" if any(a.get("status") == "stopped" for a in accounts.values()) else "completed")
        record["endedAt"] = None if unfinished else max(a.get("endedAt", 0) for a in accounts.values())
        if result:
            record.update(endReason=result["endReason"], lastMessage=result["message"])
        records[task_id] = record
        _kv_set(con, _RECORD_CATEGORY, _RECORD_KEY, json.dumps(records, ensure_ascii=False))


def reviewed_readback_update(db_path, task_id, account_id, run_task_id):
    """Reconcile one receipt without replacing another account's counters."""
    def confirmed(stats):
        stats = dict(stats or {})
        for field in ("published", "videoComments", "readbackConfirmed"):
            stats[field] = stats.get(field, 0) + 1
        stats["uncertain"] = max(0, stats.get("uncertain", 0) - 1)
        return stats

    with _LOCK, _connect(db_path) as con:
        records = _load_records(con)
        record = records.get(task_id)
        if not record:
            return
        accounts = record.get("reviewedAccounts", {})
        account = accounts.get(account_id, {})
        current = account.get("runTaskId") == run_task_id
        if current:
            account["stats"] = confirmed(account.get("stats"))
            if account.get("run"):
                account["run"]["stats"] = dict(account["stats"])
            totals = {}
            for state in accounts.values():
                for field, value in state.get("stats", {}).items():
                    if type(value) in (int, float):
                        totals[field] = totals.get(field, 0) + value
            record["stats"] = totals
        elif not accounts:
            record["stats"] = confirmed(record.get("stats"))
        for run in record.get("runs", []):
            if (run.get("accountId") == account_id and
                    (run.get("runTaskId") == run_task_id or run.get("automationTaskId") == run_task_id or
                     (current and run.get("automationTaskId") == account.get("run", {}).get("automationTaskId")))):
                run["stats"] = confirmed(run.get("stats"))
        record["reviewStatus"] = "readback_confirmed"
        record["lastMessage"] = "已通过只读回查核实一条主评留存；统计已补记，原运行停止状态保留，未重复提交"
        _kv_set(con, _RECORD_CATEGORY, _RECORD_KEY, json.dumps(records, ensure_ascii=False))


def recover_reviewed_record(db_path, task_id, live_ids, now_ms):
    """Recover abandoned preparation only; never revive or resend a task."""
    with _LOCK, _connect(db_path) as con:
        records = _load_records(con)
        record = records.get(task_id, {})
        accounts = record.get("reviewedAccounts", {})
        changed = False
        for account in accounts.values():
            if account.get("status") not in {"prepared", "running"}:
                continue
            if account.get("runTaskId") in live_ids:
                continue
            if account.get("status") == "prepared" and now_ms - account.get("preparedAt", record.get("startedAt", 0)) < 120000:
                continue  # The desktop helper has not yet returned start-task.
            account.update(status="stopped", endedAt=now_ms, endReason="interrupted",
                           message="准备或运行已中断，未自动恢复发送；请重新核对剩余目标")
            changed = True
        if changed:
            active = any(a.get("status") in {"prepared", "running"} for a in accounts.values())
            record.update(status="running" if active else "stopped", endedAt=None if active else now_ms,
                          endReason="interrupted", lastMessage="部分账号运行已中断，请核对账号记录和剩余目标")
            _kv_set(con, _RECORD_CATEGORY, _RECORD_KEY, json.dumps(records, ensure_ascii=False))
        return record


def load_records(db_path) -> dict[str, dict]:
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        return _load_records(con)
