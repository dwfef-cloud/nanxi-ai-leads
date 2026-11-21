"""任务运行日志持久化 —— 让「运行日志」抽屉在刷新/重启后仍可查看。

为什么要落库
------------
壳里的任务日志有两份来源：
1. 实时：`entity-leadgen-task-event`（type=log）→ store.addTaskLog → 抽屉立刻可见；
2. 回看：打开抽屉时部分页面调 `get-*-runtime-logs` 主动拉取。

只靠实时事件，页面刷新后（事件已被 /ipc/events 取走）就没历史了；
只靠 localStorage，换机/清缓存即丢。所以后端单独记一份：
    task_runtime_logs(task_id, leadgen_task_id, account_id, ts, level, message)

键口径：`taskId` 用**任务记录 id（leadgenTaskId）**，与壳的
`getTaskLogs(task.id)` / `entity-leadgen-task-event.taskId` 保持一致；
运行实例 id（start-task 的 taskId）也一并记录，便于按运行批次过滤。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

MAX_PER_TASK = 500          # 单个任务保留条数，超出裁掉最老
MAX_MESSAGE_LEN = 500


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=10)
    con.execute(
        "CREATE TABLE IF NOT EXISTS task_runtime_logs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL DEFAULT '', "
        "leadgen_task_id TEXT NOT NULL DEFAULT '', "
        "account_id TEXT NOT NULL DEFAULT '', "
        "ts INTEGER NOT NULL, "
        "level TEXT NOT NULL DEFAULT 'normal', "
        "message TEXT NOT NULL)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_trl_task "
        "ON task_runtime_logs(task_id, leadgen_task_id, id)"
    )
    return con


def append(db_path: Path, task_id: str, leadgen_id: str, account_id: str,
           message: str, level: str = "normal",
           ts: int | None = None) -> None:
    """追加一条日志。失败绝不影响任务本身（日志可见性优先让位于执行）。"""
    text = str(message or "").strip()
    if not text:
        return
    if len(text) > MAX_MESSAGE_LEN:
        text = text[:MAX_MESSAGE_LEN] + "…"
    stamp = int(ts) if isinstance(ts, (int, float)) else int(time.time() * 1000)
    key = str(leadgen_id or task_id or "").strip()
    if not key:
        return
    try:
        con = _connect(db_path)
        try:
            con.execute(
                "INSERT INTO task_runtime_logs"
                "(task_id, leadgen_task_id, account_id, ts, level, message) "
                "VALUES(?,?,?,?,?,?)",
                (str(task_id or key), key, str(account_id or ""),
                 stamp, str(level or "normal"), text))
            # 只裁同一个任务键的旧日志
            con.execute(
                "DELETE FROM task_runtime_logs WHERE leadgen_task_id=? AND id <= "
                "(SELECT MAX(id)-? FROM task_runtime_logs WHERE leadgen_task_id=?)",
                (key, MAX_PER_TASK, key))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        return


def list_for(db_path: Path, task_id: str, limit: int = MAX_PER_TASK) -> list[dict[str, Any]]:
    """按任务键取日志（老的在前，与抽屉展示顺序一致）。"""
    key = str(task_id or "").strip()
    if not key:
        return []
    try:
        con = _connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT ts, level, message, account_id FROM task_runtime_logs "
                "WHERE leadgen_task_id=? OR task_id=? ORDER BY id DESC LIMIT ?",
                (key, key, int(limit))).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = [{
        "ts": int(r["ts"]),
        "level": str(r["level"] or "normal"),
        "message": str(r["message"] or ""),
        "accountId": str(r["account_id"] or ""),
    } for r in rows]
    out.reverse()
    return out


def clear(db_path: Path, task_id: str) -> int:
    key = str(task_id or "").strip()
    if not key:
        return 0
    try:
        con = _connect(db_path)
        try:
            cur = con.execute(
                "DELETE FROM task_runtime_logs WHERE leadgen_task_id=? OR task_id=?",
                (key, key))
            con.commit()
            return int(cur.rowcount or 0)
        finally:
            con.close()
    except sqlite3.Error:
        return 0
