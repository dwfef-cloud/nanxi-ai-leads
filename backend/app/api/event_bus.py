"""桥接事件总线 —— 后端 → 壳页面的事件推送队列。

壳里页面通过 window.ipcRenderer.on(channel, cb) 订阅运行日志等事件，
live-bridge.js 每 5 秒轮询 GET /ipc/events，把新事件 emit 给订阅者。
真实 Electron 里这些事件由主进程推送；浏览器环境下没有主进程，因此由本模块
在后端建一个持久化队列，页面刷新/重连后仍能拿到未消费事件。

存储：data/bridge_events.db（与主库分开，避免污染业务库与只读约束）

事件形状（live-bridge 的消费约定）：
    {"channel": "automation-data", "payload": {...}}
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.paths import DATA_DIR as _DATA_DIR
BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB = _DATA_DIR / "bridge_events.db"
EVENTS_DB = Path(os.getenv("BRIDGE_EVENTS_DB", str(DEFAULT_DB)))

# 队列上限：日志类事件没有长期保留价值，超过就裁掉最老的
MAX_ROWS = 2000

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    EVENTS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(EVENTS_DB), timeout=10)
    con.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts REAL NOT NULL, channel TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    return con


def publish(channel: str, payload: Any) -> None:
    """投递一条事件。写库失败只影响日志可见性，绝不影响业务动作本身。"""
    try:
        with _LOCK:
            con = _connect()
            try:
                con.execute(
                    "INSERT INTO events(ts, channel, payload) VALUES(?,?,?)",
                    (time.time(), str(channel),
                     json.dumps(payload, ensure_ascii=False, default=str)))
                con.execute(
                    "DELETE FROM events WHERE id <= "
                    "(SELECT MAX(id) - ? FROM events)", (MAX_ROWS,))
                con.commit()
            finally:
                con.close()
    except sqlite3.Error:
        return


def drain(limit: int = 200) -> list[dict]:
    """取走未消费事件（先进先出）。同一时刻只有一个壳窗口在轮询。"""
    try:
        with _LOCK:
            con = _connect()
            try:
                rows = con.execute(
                    "SELECT id, channel, payload FROM events ORDER BY id LIMIT ?",
                    (int(limit),)).fetchall()
                if rows:
                    con.execute("DELETE FROM events WHERE id <= ?", (rows[-1][0],))
                    con.commit()
            finally:
                con.close()
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for _id, channel, payload in rows:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            continue
        out.append({"channel": channel, "payload": data})
    return out


def pending() -> int:
    try:
        with _LOCK:
            con = _connect()
            try:
                return int(con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            finally:
                con.close()
    except sqlite3.Error:
        return 0
