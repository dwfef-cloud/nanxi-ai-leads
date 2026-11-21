"""线索库 · 批量跟进：任务、运行记录、领取规则。

为什么单独一层
--------------
批量跟进的页面（配置弹窗、任务列表、运行历史、日志、结果、用户跟进历史）
全都已经画好了，缺的是后面这套数据。这里只做**本地真实数据**：

- 任务：保存 / 编辑 / 删除 / 停用，按 schedule 计算 `nextRunAt`
- 运行记录：每次执行的 run（当前/总数/成功/失败/跳过）
- 明细：每条线索每个动作的结果与失败原因
- 日志：按时间线与账号的行日志
- 领取规则：8 类跳过原因，命中即跳过，并把原因记进 run 的统计

**执行边界（不假成功）**：真正点「开始执行」要往抖音发关注 / 私信 / 分享，
需要桌面端逐动作授权（`app/execution/live_leadgen.perform` 的 scope 签名）与
壳侧扩展桥。没有授权时 `start_batch` 直接拒绝启动并说明原因，绝不创建一个
「运行中」但什么都没发的假 run。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Iterable, Mapping

# 领取规则的跳过原因（页面「领取规则」文案对应的 8 类）
SKIP_REASONS: dict[str, str] = {
    "touched": "已触达（跟进历史里已有动作记录）",
    "duplicate": "本轮重复（同一次执行里已领取过）",
    "processing": "正在处理（另一任务正在跟进该用户）",
    "recent_failed": "最近已尝试且失败（未到重试窗口）",
    "missing_identity": "缺少用户标识（无抖音号/主页链接）",
    "low_intent": "低意向（按筛选条件排除）",
    "removed": "线索已删除或隐藏",
    "unavailable": "账号不可用（已登录态失效）",
}

_RUN_STATUS = {"queued", "preparing", "running", "draining", "retrying",
               "blocked", "disabled", "stopped", "completed", "empty", "interrupted"}

_DDL = """
CREATE TABLE IF NOT EXISTS batch_follow_tasks (
    id TEXT PRIMARY KEY,
    name TEXT,
    schedule TEXT,
    filters TEXT,
    max_leads INTEGER,
    quantity_mode TEXT,
    account_success_limits TEXT,
    config TEXT,
    enabled INTEGER DEFAULT 1,
    created_ms INTEGER,
    updated_ms INTEGER,
    last_ended_ms INTEGER,
    next_run_ms INTEGER,
    last_status TEXT,
    last_run_id TEXT,
    last_selection TEXT,
    last_progress TEXT
);
CREATE TABLE IF NOT EXISTS batch_follow_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    task_name TEXT,
    kind TEXT,
    status TEXT,
    started_ms INTEGER,
    ended_ms INTEGER,
    total INTEGER DEFAULT 0,
    current INTEGER DEFAULT 0,
    success INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    account_ids TEXT,
    selection TEXT,
    progress TEXT,
    logs_cleared INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS batch_follow_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    lead_id TEXT,
    nickname TEXT,
    account_id TEXT,
    action TEXT,
    status TEXT,
    reason TEXT,
    created_ms INTEGER
);
CREATE TABLE IF NOT EXISTS batch_follow_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    ts_ms INTEGER,
    level TEXT,
    account TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS batch_follow_claims (
    lead_id TEXT,
    run_id TEXT,
    account_id TEXT,
    created_ms INTEGER,
    PRIMARY KEY (lead_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_bf_runs_task ON batch_follow_runs(task_id, started_ms);
CREATE INDEX IF NOT EXISTS idx_bf_items_run ON batch_follow_items(run_id, created_ms);
CREATE INDEX IF NOT EXISTS idx_bf_items_lead ON batch_follow_items(lead_id, created_ms);
CREATE INDEX IF NOT EXISTS idx_bf_logs_run ON batch_follow_logs(run_id, ts_ms);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def ensure_tables(db_path) -> None:
    with _connect(db_path) as con:
        con.executescript(_DDL)


def _json(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(raw or "")
        return value if isinstance(value, type(default)) else default
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------- 任务
def _task_public(row: Mapping, last_run: Mapping | None, logs: list[dict]) -> dict:
    schedule = _json(row["schedule"], {})
    config = _json(row["config"], {})
    progress = _json(row["last_progress"], {}) if "last_progress" in row.keys() else {}
    item = {
        "id": row["id"],
        "name": row["name"] or "",
        "schedule": {
            "type": schedule.get("type") or "once",
            "startHm": schedule.get("startHm") or "",
            "endHm": schedule.get("endHm") or "",
            "intervalHours": schedule.get("intervalHours") or 0,
            "intervalMinutes": schedule.get("intervalMinutes") or 0,
            "runAt": schedule.get("runAt") or "",
            "firstRunAt": schedule.get("firstRunAt") or "",
            "firstRunMode": schedule.get("firstRunMode") or "immediate",
            "enabled": bool(schedule.get("enabled", row["enabled"])),
        },
        "filters": _json(row["filters"], {}),
        "maxLeads": int(row["max_leads"] or 0),
        "quantityMode": row["quantity_mode"] or "total",
        "accountSuccessLimits": _json(row["account_success_limits"], {}),
        "config": config,
        "enabled": bool(row["enabled"]),
        "status": row["last_status"] or ("scheduled" if row["enabled"] else "disabled"),
        "nextRunAt": row["next_run_ms"] or 0,
        "lastEndedAt": row["last_ended_ms"] or 0,
        "lastSelection": _json(row["last_selection"], {}) if "last_selection" in row.keys() else {},
        "lastProgress": progress,
        "activeOccurrence": None,
        "logs": logs,
        "createdAt": row["created_ms"] or 0,
        "updatedAt": row["updated_ms"] or 0,
    }
    if last_run:
        item["lastRun"] = {
            "id": last_run["id"], "status": last_run["status"],
            "startedAt": last_run["started_ms"] or 0,
            "endedAt": last_run["ended_ms"] or 0,
            "total": last_run["total"] or 0, "current": last_run["current"] or 0,
            "success": last_run["success"] or 0,
            "failed": last_run["failed"] or 0, "skipped": last_run["skipped"] or 0,
        }
        if not item["lastProgress"]:
            item["lastProgress"] = {
                "total": last_run["total"] or 0, "current": last_run["current"] or 0,
                "success": last_run["success"] or 0, "failed": last_run["failed"] or 0,
            }
    return item


def tasks(db_path) -> dict:
    ensure_tables(db_path)
    with _connect(db_path) as con:
        rows = con.execute("SELECT * FROM batch_follow_tasks ORDER BY updated_ms DESC").fetchall()
        items = []
        for row in rows:
            last = con.execute(
                "SELECT * FROM batch_follow_runs WHERE task_id=? ORDER BY started_ms DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            logs = [
                {"id": r["id"], "ts": r["ts_ms"], "level": r["level"] or "normal",
                 "account": r["account"] or "", "message": r["message"] or ""}
                for r in con.execute(
                    "SELECT * FROM batch_follow_logs WHERE run_id IN "
                    "(SELECT id FROM batch_follow_runs WHERE task_id=?) "
                    "ORDER BY ts_ms DESC LIMIT 50", (row["id"],)).fetchall()
            ]
            items.append(_task_public(row, last, logs))
    return {"success": True, "items": items, "total": len(items)}


def _next_run_ms(schedule: Mapping) -> int:
    """按 schedule 推下一次执行时间（0 = 无定时）。"""
    kind = str(schedule.get("type") or "once")
    if kind == "once":
        raw = str(schedule.get("runAt") or "")
        return _parse_ms(raw)
    if kind == "daily":
        # 每天固定时段：下一次起点取今天的 startHm，已过则明天
        start_hm = str(schedule.get("startHm") or "")
        if not start_hm:
            return 0
        now = time.localtime()
        hh, _, mm = start_hm.partition(":")
        target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                              int(hh or 0), int(mm or 0), 0, 0, 0, -1))
        if target * 1000 <= _now_ms():
            target += 86400
        return int(target * 1000)
    if kind == "after_finish":
        hours = int(schedule.get("intervalHours") or 0)
        minutes = int(schedule.get("intervalMinutes") or 0)
        delta = max(0, hours * 60 + minutes) * 60_000
        base = _parse_ms(str(schedule.get("firstRunAt") or "")) or _now_ms()
        return base + delta
    return 0


def _parse_ms(raw: str) -> int:
    raw = str(raw or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(time.mktime(time.strptime(raw, fmt)) * 1000)
        except ValueError:
            continue
    return 0


def save_task(db_path, payload: Any) -> dict:
    """保存定时任务。id 存在即更新，否则新建。"""
    data = payload if isinstance(payload, Mapping) else {}
    name = str(data.get("name") or "").strip()
    if not name:
        return {"success": False, "error": "请填写任务名称"}
    schedule = data.get("schedule") if isinstance(data.get("schedule"), Mapping) else {}
    config = data.get("config") if isinstance(data.get("config"), Mapping) else {}
    accounts = config.get("batchAccountIds") or []
    if not isinstance(accounts, list) or not accounts:
        return {"success": False, "error": "请至少选择一个执行账号"}

    ensure_tables(db_path)
    task_id = str(data.get("id") or "").strip() or f"bf_{uuid.uuid4().hex[:12]}"
    now = _now_ms()
    enabled = bool(schedule.get("enabled", 1))
    next_ms = _next_run_ms(schedule) if enabled else 0
    with _connect(db_path) as con:
        exists = con.execute("SELECT 1 FROM batch_follow_tasks WHERE id=?", (task_id,)).fetchone()
        common = (
            name, json.dumps(schedule, ensure_ascii=False),
            json.dumps(data.get("filters") or {}, ensure_ascii=False),
            int(data.get("maxLeads") or 0), str(data.get("quantityMode") or "total"),
            json.dumps(data.get("accountSuccessLimits") or {}, ensure_ascii=False),
            json.dumps(config, ensure_ascii=False), 1 if enabled else 0,
        )
        if exists:
            con.execute(
                "UPDATE batch_follow_tasks SET name=?,schedule=?,filters=?,max_leads=?,"
                "quantity_mode=?,account_success_limits=?,config=?,enabled=?,updated_ms=?,"
                "next_run_ms=? WHERE id=?",
                common + (now, next_ms, task_id),
            )
        else:
            con.execute(
                "INSERT INTO batch_follow_tasks(id,name,schedule,filters,max_leads,quantity_mode,"
                "account_success_limits,config,enabled,created_ms,updated_ms,next_run_ms,last_status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id,) + common + (now, now, next_ms,
                                       "scheduled" if enabled else "disabled"),
            )
    return {"success": True, "id": task_id, "nextRunAt": next_ms}


def control_task(db_path, payload: Any) -> dict:
    """任务操作：stop / pause / resume / delete / clear_logs / run_now。"""
    data = payload if isinstance(payload, Mapping) else {}
    task_id = str(data.get("id") or "").strip()
    action = str(data.get("action") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少任务 id"}
    ensure_tables(db_path)
    with _connect(db_path) as con:
        row = con.execute("SELECT * FROM batch_follow_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return {"success": False, "error": "任务不存在"}
        if action == "delete":
            con.execute("DELETE FROM batch_follow_tasks WHERE id=?", (task_id,))
            run_ids = [r["id"] for r in con.execute(
                "SELECT id FROM batch_follow_runs WHERE task_id=?", (task_id,)).fetchall()]
            for rid in run_ids:
                con.execute("DELETE FROM batch_follow_items WHERE run_id=?", (rid,))
                con.execute("DELETE FROM batch_follow_logs WHERE run_id=?", (rid,))
                con.execute("DELETE FROM batch_follow_claims WHERE run_id=?", (rid,))
            con.execute("DELETE FROM batch_follow_runs WHERE task_id=?", (task_id,))
            return {"success": True}
        if action == "clear_logs":
            con.execute("UPDATE batch_follow_runs SET logs_cleared=1 WHERE task_id=?", (task_id,))
            con.execute(
                "DELETE FROM batch_follow_logs WHERE run_id IN "
                "(SELECT id FROM batch_follow_runs WHERE task_id=?)", (task_id,))
            return {"success": True}
        if action == "pause":
            schedule = _json(row["schedule"], {})
            schedule["enabled"] = False
            con.execute("UPDATE batch_follow_tasks SET enabled=0,schedule=?,next_run_ms=0,"
                        "last_status=?,updated_ms=? WHERE id=?",
                        (json.dumps(schedule, ensure_ascii=False), "disabled", _now_ms(), task_id))
            return {"success": True, "enabled": False}
        if action == "resume":
            schedule = _json(row["schedule"], {})
            schedule["enabled"] = True
            next_ms = _next_run_ms(schedule)
            con.execute("UPDATE batch_follow_tasks SET enabled=1,schedule=?,next_run_ms=?,"
                        "last_status=?,updated_ms=? WHERE id=?",
                        (json.dumps(schedule, ensure_ascii=False), next_ms, "scheduled",
                         _now_ms(), task_id))
            return {"success": True, "enabled": True, "nextRunAt": next_ms}
        if action in {"stop", "stop_run", "cancel"}:
            con.execute("UPDATE batch_follow_runs SET status='stopped',ended_ms=? "
                        "WHERE task_id=? AND status IN ('running','queued','preparing','draining','retrying')",
                        (_now_ms(), task_id))
            con.execute("UPDATE batch_follow_tasks SET last_status=?,updated_ms=? WHERE id=?",
                        ("stopped", _now_ms(), task_id))
            return {"success": True}
        if action == "run_now":
            blocked = _start_block_reason(db_path)
            if blocked:
                return {"success": False, "error": blocked}
            return {"success": False, "error": "立即执行需桌面端逐动作授权后由任务入口启动"}
        return {"success": False, "error": f"不支持的任务操作：{action or '空'}"}


# ----------------------------------------------------------------- 运行记录
def runs_page(db_path, payload: Any) -> dict:
    data = payload if isinstance(payload, Mapping) else {}
    task_id = str(data.get("taskId") or "").strip()
    page = max(1, int(data.get("page") or 1))
    size = max(1, min(200, int(data.get("pageSize") or 20)))
    ensure_tables(db_path)
    where, params = "", ()
    if task_id:
        where, params = " WHERE task_id=?", (task_id,)
    with _connect(db_path) as con:
        total = con.execute(f"SELECT COUNT(*) FROM batch_follow_runs{where}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT * FROM batch_follow_runs{where} ORDER BY started_ms DESC LIMIT ? OFFSET ?",
            params + (size, (page - 1) * size),
        ).fetchall()
    items = [{
        "id": r["id"], "runId": r["id"], "taskId": r["task_id"] or "",
        "taskName": r["task_name"] or "", "type": r["kind"] or "immediate",
        "status": r["status"] or "completed",
        "startedAt": r["started_ms"] or 0, "endedAt": r["ended_ms"] or 0,
        "total": r["total"] or 0, "current": r["current"] or 0,
        "success": r["success"] or 0, "failed": r["failed"] or 0,
        "skipped": r["skipped"] or 0,
        "accounts": _json(r["account_ids"], []),
        "logCount": 0 if r["logs_cleared"] else _log_count(db_path, r["id"]),
    } for r in rows]
    return {"items": items, "total": total, "page": page, "pageSize": size}


def _log_count(db_path, run_id: str) -> int:
    with _connect(db_path) as con:
        return con.execute("SELECT COUNT(*) FROM batch_follow_logs WHERE run_id=?",
                           (run_id,)).fetchone()[0]


def run_details(db_path, payload: Any) -> dict:
    """运行详情：kind=logs 返回日志，kind=results 返回每条线索的结果。"""
    data = payload if isinstance(payload, Mapping) else {}
    run_id = str(data.get("id") or data.get("runId") or "").strip()
    kind = str(data.get("kind") or "logs")
    page = max(1, int(data.get("page") or 1))
    size = max(1, min(500, int(data.get("pageSize") or 50)))
    search = str(data.get("search") or "").strip()
    status = str(data.get("status") or "").strip()
    ensure_tables(db_path)
    with _connect(db_path) as con:
        run = con.execute("SELECT * FROM batch_follow_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return {"run": None, "items": [], "total": 0, "page": page}
        run_public = {
            "id": run["id"], "runId": run["id"], "taskId": run["task_id"] or "",
            "taskName": run["task_name"] or "", "type": run["kind"] or "immediate",
            "status": run["status"] or "completed",
            "startedAt": run["started_ms"] or 0, "endedAt": run["ended_ms"] or 0,
            "total": run["total"] or 0, "current": run["current"] or 0,
            "success": run["success"] or 0, "failed": run["failed"] or 0,
            "skipped": run["skipped"] or 0,
            "accounts": _json(run["account_ids"], []),
            "selection": _json(run["selection"], {}),
            "progress": _json(run["progress"], {}),
            "of": 0,
        }
        if kind == "results":
            clauses, params = ["run_id=?"], [run_id]
            if search:
                clauses.append("(nickname LIKE ? OR reason LIKE ?)")
                params += [f"%{search}%", f"%{search}%"]
            if status and status not in {"all", "全部"}:
                clauses.append("status=?")
                params.append(_status_key(status))
            where = " WHERE " + " AND ".join(clauses)
            total = con.execute(f"SELECT COUNT(*) FROM batch_follow_items{where}", params).fetchone()[0]
            rows = con.execute(
                f"SELECT * FROM batch_follow_items{where} ORDER BY created_ms DESC LIMIT ? OFFSET ?",
                tuple(params) + (size, (page - 1) * size)).fetchall()
            items = [{
                "id": r["id"], "leadId": r["lead_id"] or "", "nickname": r["nickname"] or "",
                "accountId": r["account_id"] or "", "action": r["action"] or "",
                "status": r["status"] or "", "reason": r["reason"] or "",
                "time": _fmt_ms(r["created_ms"]), "ts": r["created_ms"] or 0,
            } for r in rows]
            return {"run": run_public, "items": items, "total": total, "page": page,
                    "pageSize": size, "through": _now_ms()}
        logs_cleared = bool(run["logs_cleared"])
        if logs_cleared:
            return {"run": run_public, "items": [], "total": 0, "page": page,
                    "pageSize": size, "through": _now_ms()}
        clauses, params = ["run_id=?"], [run_id]
        if search:
            clauses.append("(message LIKE ? OR account LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        where = " WHERE " + " AND ".join(clauses)
        total = con.execute(f"SELECT COUNT(*) FROM batch_follow_logs{where}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT * FROM batch_follow_logs{where} ORDER BY ts_ms ASC, id ASC LIMIT ? OFFSET ?",
            tuple(params) + (size, (page - 1) * size)).fetchall()
        items = [{
            "id": r["id"], "time": _fmt_ms(r["ts_ms"]), "ts": r["ts_ms"] or 0,
            "level": r["level"] or "normal", "account": r["account"] or "",
            "accountName": r["account"] or "", "message": r["message"] or "",
        } for r in rows]
        return {"run": run_public, "items": items, "total": total, "page": page,
                "pageSize": size, "through": _now_ms()}


def _status_key(label: str) -> str:
    return {"成功": "success", "失败": "failed", "跳过": "skipped"}.get(label, label)


def _fmt_ms(ms: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms) / 1000))
    except (TypeError, ValueError, OSError):
        return ""


def user_history(db_path, payload: Any) -> dict:
    """某个用户（线索）在各次执行里的动作历史，按昵称 / leadId 查。"""
    data = payload if isinstance(payload, Mapping) else {}
    limit = max(1, min(200, int(data.get("limit") or 30)))
    offset = int(data.get("cursor") or 0) if str(data.get("cursor") or "").isdigit() else 0
    lead_id = str(data.get("leadId") or data.get("userKey") or "").strip()
    nickname = str(data.get("nickname") or "").strip()
    ensure_tables(db_path)
    clauses, params = [], []
    if lead_id:
        clauses.append("lead_id=?")
        params.append(lead_id)
    if nickname:
        clauses.append("nickname LIKE ?")
        params.append(f"%{nickname}%")
    if not clauses:
        return {"success": False, "error": "请先选择要查看的用户", "items": [],
                "total": 0, "exact": False, "hasMore": False}
    where = " WHERE " + " AND ".join(clauses)
    with _connect(db_path) as con:
        total = con.execute(f"SELECT COUNT(*) FROM batch_follow_items{where}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT * FROM batch_follow_items{where} ORDER BY created_ms DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset)).fetchall()
    items = []
    for r in rows:
        run = None
        with _connect(db_path) as con:
            run = con.execute("SELECT task_name,kind FROM batch_follow_runs WHERE id=?",
                              (r["run_id"],)).fetchone()
        items.append({
            "id": r["id"], "runId": r["run_id"], "leadId": r["lead_id"] or "",
            "nickname": r["nickname"] or "", "accountId": r["account_id"] or "",
            "action": r["action"] or "", "status": r["status"] or "",
            "outcome": "success" if r["status"] == "success" else
                       "failed" if r["status"] == "failed" else "skipped",
            "reason": r["reason"] or "", "time": _fmt_ms(r["created_ms"]),
            "ts": r["created_ms"] or 0,
            "taskName": (run["task_name"] if run else "") or "",
        })
    return {"success": True, "items": items, "total": total, "exact": bool(lead_id),
            "claim": {"leadId": lead_id, "nickname": nickname},
            "hasMore": offset + len(items) < total, "nextCursor": offset + len(items)}


# ------------------------------------------------------------------ 清理类
def clear_runs(db_path) -> dict:
    ensure_tables(db_path)
    with _connect(db_path) as con:
        con.execute("DELETE FROM batch_follow_items")
        con.execute("DELETE FROM batch_follow_logs")
        con.execute("DELETE FROM batch_follow_claims")
        con.execute("DELETE FROM batch_follow_runs")
        con.execute("UPDATE batch_follow_tasks SET last_run_id=NULL,last_status=NULL,"
                    "last_progress=NULL,last_selection=NULL")
    return {"success": True}


def delete_runs(db_path, ids: Any) -> dict:
    """删除运行记录。入参可能是 id 数组、单个 id，或（RAW 通道）外层包一层的数组。"""
    raw = ids if isinstance(ids, list) else [ids]
    flat: list[Any] = []
    for entry in raw:
        if isinstance(entry, (list, tuple, set)):
            flat.extend(entry)
        else:
            flat.append(entry)
    targets = [str(x).strip() for x in flat if str(x or "").strip()]
    if not targets:
        return {"success": False, "error": "未选择要删除的记录"}
    ensure_tables(db_path)
    marks = ",".join("?" * len(targets))
    with _connect(db_path) as con:
        con.execute(f"DELETE FROM batch_follow_items WHERE run_id IN ({marks})", tuple(targets))
        con.execute(f"DELETE FROM batch_follow_logs WHERE run_id IN ({marks})", tuple(targets))
        con.execute(f"DELETE FROM batch_follow_claims WHERE run_id IN ({marks})", tuple(targets))
        con.execute(f"DELETE FROM batch_follow_runs WHERE id IN ({marks})", tuple(targets))
    return {"success": True, "deleted": len(targets)}


def clear_run_logs(db_path, payload: Any) -> dict:
    data = payload if isinstance(payload, Mapping) else {}
    run_id = str(data.get("id") or data.get("runId") or "").strip()
    if not run_id:
        return {"success": False, "error": "缺少记录 id"}
    ensure_tables(db_path)
    with _connect(db_path) as con:
        con.execute("DELETE FROM batch_follow_logs WHERE run_id=?", (run_id,))
        con.execute("UPDATE batch_follow_runs SET logs_cleared=1 WHERE id=?", (run_id,))
    return {"success": True}


def cancel_user_history(payload: Any) -> dict:
    """用户历史是本地分页查询，没有长任务可取消：如实返回已取消。"""
    data = payload if isinstance(payload, Mapping) else {}
    return {"success": True, "requestKey": str(data.get("requestKey") or "")}


def delete_failed_leads(db_path) -> dict:
    """删除跟进失败且被标记的线索（config.deleteFailedLeads 打开时才标记）。"""
    ensure_tables(db_path)
    with _connect(db_path) as con:
        has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads'").fetchone()
        if not has:
            return {"success": True, "deleted": 0}
        cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
        if "status" not in cols:
            return {"success": True, "deleted": 0}
        cur = con.execute("DELETE FROM leads WHERE status='batch_follow_failed'")
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return {"success": True, "deleted": deleted}


def test_proxy(payload: Any) -> dict:
    """代理连通性：与账号池「测试连接」同一实现（真实请求目标站）。"""
    from app.api import ipc_bridge

    return ipc_bridge._test_account_proxy(payload)


# ------------------------------------------------------------------ 领取规则
def claim(
    db_path,
    filters: Mapping,
    *,
    limit: int,
    skip_touched: bool = True,
    run_id: str = "",
    exclude_lead_ids: Iterable[str] = (),
) -> dict:
    """按筛选条件领取线索，并把跳过原因分类统计。

    8 类原因见 SKIP_REASONS；这里只对**有数据依据**的原因做判断，
    不编造「性别 / 年龄」这类本地没有的维度（那类线索照常领取）。
    """
    from app.api import ipc_bridge

    ensure_tables(db_path)
    limit = max(1, min(50000, int(limit or 1000)))
    where, params, _notes = ipc_bridge._leads_where(dict(filters or {}))
    clause = " WHERE " + " AND ".join(where) if where else ""
    with _connect(db_path) as con:
        rows = con.execute(
            f"SELECT * FROM leads{clause} ORDER BY created_at DESC LIMIT ?",
            tuple(params) + (limit * 3,)).fetchall()
        claimed = {r[0] for r in con.execute(
            "SELECT lead_id FROM batch_follow_claims WHERE run_id<>?", (run_id,)).fetchall()}
        this_run = {r[0] for r in con.execute(
            "SELECT lead_id FROM batch_follow_claims WHERE run_id=?", (run_id,)).fetchall()}
        recent_failed = {r[0] for r in con.execute(
            "SELECT lead_id FROM batch_follow_items WHERE status='failed' "
            "AND created_ms > ?", (_now_ms() - 24 * 3600 * 1000,)).fetchall()}
        touched = ipc_bridge._touch_ledger().touched_ids() if skip_touched else set()

    excluded = {str(x) for x in exclude_lead_ids}
    picked: list[dict] = []
    skipped: dict[str, int] = {key: 0 for key in SKIP_REASONS}
    for row in rows:
        lead_id = str(row["id"])
        if len(picked) >= limit:
            break
        if lead_id in excluded or lead_id in this_run:
            skipped["duplicate"] += 1
            continue
        if lead_id in claimed:
            skipped["processing"] += 1
            continue
        if lead_id in touched:
            skipped["touched"] += 1
            continue
        if lead_id in recent_failed:
            skipped["recent_failed"] += 1
            continue
        if not (str(row["external_id"] or "").strip() or str(row["comment_user_id"] or "").strip()
                or str(row["source_url"] or "").strip()):
            skipped["missing_identity"] += 1
            continue
        picked.append({
            "leadId": lead_id, "nickname": row["nickname"] or "",
            "userUrl": row["source_url"] or "", "comment": row["comment"] or "",
            "intentLevel": row["intent_level"] or "",
        })
    return {
        "items": picked,
        "requested": limit,
        "skipped": {k: v for k, v in skipped.items() if v},
        "skippedReasons": {k: SKIP_REASONS[k] for k in skipped if skipped[k]},
    }


def claim_mark(db_path, run_id: str, lead_ids: Iterable[str], account_id: str = "") -> int:
    """把本轮领取的线索写入 claims（去重 / 处理中判断的依据）。"""
    ensure_tables(db_path)
    now = _now_ms()
    count = 0
    with _connect(db_path) as con:
        for lead_id in lead_ids:
            try:
                con.execute("INSERT OR IGNORE INTO batch_follow_claims(lead_id,run_id,account_id,created_ms) "
                            "VALUES(?,?,?,?)", (str(lead_id), run_id, account_id, now))
                count += 1
            except sqlite3.Error:
                continue
    return count


def claim_release(db_path, run_id: str) -> None:
    ensure_tables(db_path)
    with _connect(db_path) as con:
        con.execute("DELETE FROM batch_follow_claims WHERE run_id=?", (run_id,))


# ------------------------------------------------------------------- 启动
def _start_block_reason(db_path) -> str:
    """批量执行的真实前置条件：桌面壳在线。

    授权范围不再靠「有没有授权文件」判断 —— 用户点「开始执行」时选定的
    账号 + 跟进方式 + 线索清单本身就是授权，执行器会把它固化成一次性作用域。
    """
    from app.runtime import browser

    if not browser.shell_available():
        return "桌面壳未运行：批量跟进的动作需要在本机账号窗口里执行"
    return ""


def start_batch(db_path, payload: Any) -> dict:
    """立即执行入口：条件不满足时如实拒绝，不创建「运行中」的假记录。"""
    data = payload if isinstance(payload, Mapping) else {}
    leads = data.get("leads")
    config = data.get("config") if isinstance(data.get("config"), Mapping) else {}
    if not isinstance(leads, list) or not leads:
        return {"ok": False, "reason": "没有可跟进的线索"}
    if not (config.get("batchAccountIds") or []):
        return {"ok": False, "reason": "请至少选择一个执行账号"}
    if config.get("share") and not str(config.get("profileShareFriends") or "").strip():
        return {"ok": False, "reason": "请填写接收主页的好友昵称、备注名或群聊名称"}
    blocked = _start_block_reason(db_path)
    if blocked:
        return {"ok": False, "reason": blocked}
    ensure_tables(db_path)
    from app.runtime import batch_runner

    return batch_runner.start(db_path, data)


def stop_batch(payload: Any) -> dict:
    """停止一次正在跑的批量执行（已开始的单条会走完，未领取的不再领）。"""
    from app.runtime import batch_runner

    run_id = str((payload or {}).get("runId") or "").strip()
    if not run_id:
        return {"ok": False, "reason": "缺少运行编号"}
    return {"ok": batch_runner.stop(run_id), "runId": run_id}


def runtime_state(db_path) -> dict:
    """get-batch-runtime-state：大屏「批量跟进进度」卡片的真实运行态。

    前端契约（TaskStore.applyBatchRuntimeState）：
      {active, starting, runningViewKeys:[], runs:[{runId,...}],
       progress:{total,current,success,failed,skipped}}
    以前这里恒返回 {state:"disabled"}，前端拿不到 runningViewKeys 直接 no-op，
    卡片永远显示「正在等待授权连接恢复」。现在按真实 runs 表统计；没有运行时
    就是 0 —— 0 是事实，不是占位。
    """
    ensure_tables(db_path)
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, task_id, task_name, status, total, current, success, failed, skipped, "
            "account_ids FROM batch_follow_runs WHERE status IN ('running','paused','pending') "
            "ORDER BY started_ms DESC").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()

    runs = []
    progress = {"total": 0, "current": 0, "success": 0, "failed": 0, "skipped": 0}
    keys: set[str] = set()
    for r in rows:
        account_ids = _json(r["account_ids"], []) or []
        for aid in account_ids:
            keys.add(f"batch_{aid}")
        for field in ("total", "current", "success", "failed", "skipped"):
            progress[field] += int(r[field] or 0)
        runs.append({
            "runId": str(r["id"]), "taskId": str(r["task_id"]),
            "taskName": str(r["task_name"] or ""), "status": str(r["status"]),
            "accounts": [str(a) for a in account_ids],
            "total": int(r["total"] or 0), "current": int(r["current"] or 0),
            "success": int(r["success"] or 0), "failed": int(r["failed"] or 0),
            "skipped": int(r["skipped"] or 0),
        })

    return {
        "success": True,
        "active": bool(runs),
        "starting": False,
        "runningViewKeys": sorted(keys),
        "runs": runs,
        "progress": progress,
        "state": "executing" if runs else "idle",
        "note": "" if runs else "当前没有正在执行的批量跟进",
    }
