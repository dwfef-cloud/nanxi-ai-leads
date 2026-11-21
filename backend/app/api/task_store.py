"""监控 / 养号 / 作品发布 三类任务配置的持久化。

壳保存任务时传的是完整 taskRecord（id / name / status / runs / stats /
configSnapshot / events / matches / cycles）。tasks 表只有 7 列，逐字段拆开
存会丢掉结构化配置，于是「保存成功、重启后配置全空」——用户看到任务回来了
但账号、关键词、配额都没了，改一次配置还得从头填。

所以这里把整条记录以 JSON 存进 tasks.payload，读取时原样还原：
id / task_type / status 仍落独立列，方便按类型查询与状态统计。

历史遗留行（media_crawler_live 等）不在此模块处理，仍由 ipc_bridge 按原逻辑
映射，避免改动影响已通过的列表行为。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()

# kind -> tasks.task_type（新增记录用它，与遗留 task_type 区分开）
KIND_TYPES = {
    "monitor": "monitor_live",
    "warmup": "self_warmup",
    "publish": "work_publish",
}

# 与 ipc_bridge 出口守卫保持一致的 uid 形态字段：存进来的东西必须能返回出去，
# 否则保存成功、列表一读就因合规校验整页 500。
_UID_KEY = re.compile(
    r"(?:^|[_-])(?:uid|secuid|webcastuid|userid|openid|unionid|"
    r"commentuserid|commentid|videoid|externalid|profileurl|rawpage|rawhtml)(?:$|[_-])",
    re.IGNORECASE,
)

_STATUS_MAP = {
    "running": "running", "pending": "running", "processing": "running",
    "starting": "running", "queued": "running",
    "completed": "completed", "done": "completed", "success": "completed",
    "finished": "completed",
    "failed": "failed", "error": "failed",
    "stopped": "stopped", "cancelled": "stopped", "canceled": "stopped",
    "idle": "stopped", "paused": "stopped",
}


def normalize_status(raw: Any) -> str:
    """把前端各种状态词归一到 running/completed/failed/stopped 四态。"""
    return _STATUS_MAP.get(str(raw or "").strip().lower(), "stopped")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Any) -> str:
    """毫秒时间戳 / 秒时间戳 / ISO 串 -> ISO 串，无法识别时返回当前时间。"""
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return _now()
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return _now()


def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _ensure(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'stopped',
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )


def clean(value: Any, depth: int = 0) -> Any:
    """递归剔除 uid 形态字段，保证存进去的记录能原样返回给页面。"""
    if depth > 6:
        return None
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if _UID_KEY.search(str(key)):
                continue
            out[key] = clean(val, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [clean(item, depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def save_task(db_path, kind: str, record: Any) -> dict:
    """保存/覆盖一条任务配置（整条 taskRecord 落 tasks.payload）。"""
    if kind not in KIND_TYPES:
        return {"success": False, "error": f"未知任务类型：{kind}"}
    rec = dict(record) if isinstance(record, dict) else {}
    task_id = str(rec.get("id") or "").strip()
    if not task_id:
        task_id = f"{KIND_TYPES[kind]}_{int(time.time() * 1000)}"
        rec["id"] = task_id
    name = str(rec.get("name") or "").strip()
    if not name:
        return {"success": False, "error": "任务缺少名称"}
    rec["name"] = name
    # payload 里保留前端原始状态词（ready / running / failed …），
    # status 列只存四态供统计查询；否则「待启动」会被改写成「已停止」，
    # 重启后任务卡片的状态徽标就和保存前不一致。
    raw_status = str(rec.get("status") or "stopped")
    status = normalize_status(raw_status)
    rec["status"] = raw_status
    created = _iso(rec.get("createdAt"))
    rec.setdefault("createdAt", created)
    payload = json.dumps(clean(rec), ensure_ascii=False)
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        cur = con.execute(
            "UPDATE tasks SET task_type=?, payload=?, status=?, error_message=?, "
            "updated_at=? WHERE id=?",
            (KIND_TYPES[kind], payload, status, str(rec.get("error") or ""), _now(), task_id),
        )
        if cur.rowcount == 0:
            con.execute(
                "INSERT INTO tasks(id, task_type, payload, status, error_message, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, KIND_TYPES[kind], payload, status,
                 str(rec.get("error") or ""), created, _now()),
            )
    return {"success": True, "id": task_id, "name": name, "status": raw_status}


def get_task(db_path, kind: str, task_id: str) -> dict | None:
    if kind not in KIND_TYPES or not str(task_id or "").strip():
        return None
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        row = con.execute(
            "SELECT * FROM tasks WHERE id=? AND task_type=?",
            (str(task_id), KIND_TYPES[kind]),
        ).fetchone()
    return _record(row) if row else None


def list_tasks(db_path, kind: str) -> list[dict]:
    """按类型读取任务配置（新的在前）。payload 损坏的行跳过，不让单行坏数据毁掉整页。"""
    if kind not in KIND_TYPES:
        return []
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        rows = con.execute(
            "SELECT * FROM tasks WHERE task_type=? ORDER BY created_at DESC",
            (KIND_TYPES[kind],),
        ).fetchall()
    items = [_record(r) for r in rows]
    items = [i for i in items if i]
    items.sort(key=lambda i: str(i.get("createdAt") or ""), reverse=True)
    return items


def delete_task(db_path, kind: str, task_id: str) -> int:
    if kind not in KIND_TYPES:
        return 0
    with _LOCK, _connect(db_path) as con:
        _ensure(con)
        cur = con.execute(
            "DELETE FROM tasks WHERE id=? AND task_type=?",
            (str(task_id), KIND_TYPES[kind]),
        )
        return int(cur.rowcount or 0)


def _record(row: sqlite3.Row) -> dict | None:
    try:
        rec = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    rec.setdefault("id", row["id"])
    rec.setdefault("name", row["id"])
    rec.setdefault("status", normalize_status(row["status"]))
    rec.setdefault("runs", [])
    rec.setdefault("stats", {})
    rec.setdefault("configSnapshot", {})
    return rec
