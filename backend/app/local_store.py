"""system_settings 通用键值存储 + 出口字段清洗。

页面配置（养号配置、各类排期、统计时刻）都落在同一张 system_settings：
category 分区、key 定位、value 存 JSON。不新增表结构，避免升级时迁移。

两件事必须在这里统一，否则各模块各写一份会走偏：

1. **uid 形态字段清洗**。写进库的东西最终要原样返回给页面，而 ipc 出口守卫
   （assert_public_safe）会拦 videoId / commentUserId 这类平台 uid 级键名。
   存的时候不剔，读的时候整页 500；这里在写入前统一剔掉。
2. **空写入保护**。带防抖的保存通道（页面 300ms 防抖）在初始化时可能先发一个
   空包，如果照写，用户已有配置会被空对象覆盖——属数据丢失级副作用。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

_LOCK = threading.Lock()

# 与 ipc_bridge / task_store 的出口守卫保持同一套形态
_UID_KEY = re.compile(
    r"(?:^|[_-])(?:uid|secuid|webcastuid|userid|openid|unionid|commentuserid|"
    r"commentid|videoid|externalid|profileurl|rawpage|rawhtml)(?:$|[_-])",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.execute(
        "CREATE TABLE IF NOT EXISTS system_settings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "category TEXT NOT NULL, key TEXT NOT NULL, value TEXT, "
        "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(category, key))"
    )
    return con


def clean(value: Any, depth: int = 0) -> Any:
    """递归剔除 uid 形态字段，保证「存得进去 = 读得出来」。"""
    if depth > 6:
        return None
    if isinstance(value, Mapping):
        out = {}
        for key, val in value.items():
            if _UID_KEY.search(str(key)):
                continue
            out[str(key)] = clean(val, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [clean(item, depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def read_raw(db_path, key: str, category: str) -> str | None:
    try:
        with _LOCK, connect(db_path) as con:
            row = con.execute(
                "SELECT value FROM system_settings WHERE category=? AND key=?",
                (category, key),
            ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def write_raw(db_path, key: str, value: str, category: str) -> None:
    with _LOCK, connect(db_path) as con:
        con.execute(
            "INSERT INTO system_settings(category,key,value,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (category, key, value, _now()),
        )


def read_json(db_path, key: str, category: str) -> Any:
    raw = read_raw(db_path, key, category)
    if not raw:
        return None
    import json

    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def write_json(db_path, key: str, payload: Any, category: str) -> None:
    import json

    write_raw(db_path, key, json.dumps(clean(payload), ensure_ascii=False), category)
