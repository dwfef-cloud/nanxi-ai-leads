"""定时排期（监控 / 评论获客 / 关键词获客 / 自动回复）的持久化。

壳里这四类排期的存读契约是**一个 map**：

    { "<taskId>": {type:"once"|"daily"|"after_finish", enabled, runAt,
                   startHm, endHm, mode, nextAction, nextActionAt, ...} }

保存走 save-*-schedules（入参就是整张 map 的 JSON），读取走 get-*-schedules
（返回值直接喂给 store.replaceScheduled*，前端不再解包）。

历史实现把这些读通道接到 aggregate.schedules_from_settings，返回的是
{"success": true, "schedules": [...]}——前端拿到的是一个**对象**，ro() 会按
map 逐键校验，于是 "success" 这个键被当成一个 runAt=1 的排期记录，
任务列表里凭空多出一条「立即继续」的幽灵排期，且刷新不消失。
所以这里必须返回**裸 map**，不能包 success 信封。

存储落在 system_settings（category='schedules'），一类一条 JSON，
与 personas / 通知配置同库同表，不新增表结构。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

_LOCK = threading.Lock()
_CATEGORY = "schedules"

# kind -> system_settings.key
KEYS = {
    "leadgen": "scheduled_leadgen",
    "entity": "scheduled_entity_leadgen",
    "monitor": "scheduled_monitor",
    "warmup": "scheduled_self_warmup",
}

# 前端排期记录里可能带账号/窗口标识，出口守卫会拦 uid 形态键名；
# 存之前在本地先剔掉，保证「存得进去 = 读得出来」。
_UID_KEY = re.compile(
    r"(?:^|[_-])(?:uid|secuid|webcastuid|userid|openid|unionid|commentuserid|"
    r"commentid|videoid|externalid|profileurl|rawpage|rawhtml)(?:$|[_-])",
    re.IGNORECASE,
)

_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

ACCOUNT_DATA_KEY = "account_data_time"


def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.execute(
        "CREATE TABLE IF NOT EXISTS system_settings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "category TEXT NOT NULL, key TEXT NOT NULL, value TEXT, "
        "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(category, key))"
    )
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(value, Mapping):
        out = {}
        for key, val in value.items():
            if _UID_KEY.search(str(key)):
                continue
            out[str(key)] = _clean(val, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_clean(item, depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _read_raw(db_path, key: str, category: str = _CATEGORY) -> str | None:
    try:
        with _LOCK, _connect(db_path) as con:
            row = con.execute(
                "SELECT value FROM system_settings WHERE category=? AND key=?",
                (category, key),
            ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _write_raw(db_path, key: str, value: str, category: str = _CATEGORY) -> None:
    with _LOCK, _connect(db_path) as con:
        con.execute(
            "INSERT INTO system_settings(category,key,value,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (category, key, value, _now()),
        )


def get_map(db_path, kind: str) -> dict:
    """读排期表：返回裸 map（键=任务 id），没配置过返回空 dict。"""
    key = KEYS.get(kind)
    if not key:
        return {}
    raw = _read_raw(db_path, key)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # 只保留「值仍是对象」的条目，避免坏行把前端 ro() 校验整表打空
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def save_map(db_path, kind: str, payload: Any) -> dict:
    """整表覆盖写入。payload 可以是 map 本身，也可以是 {schedules: map} 信封。"""
    key = KEYS.get(kind)
    if not key:
        return {"success": False, "reason": f"未知排期类型：{kind}"}
    data = payload
    if isinstance(data, Mapping) and isinstance(data.get("schedules"), Mapping):
        data = data["schedules"]
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], Mapping):
        data = data[0]
    if not isinstance(data, Mapping):
        return {"success": False, "reason": "排期数据格式不正确"}
    clean = {str(k): _clean(v) for k, v in data.items() if isinstance(v, Mapping)}
    try:
        _write_raw(db_path, key, json.dumps(clean, ensure_ascii=False))
    except sqlite3.Error as exc:
        return {"success": False, "reason": f"排期保存失败：{exc}"}
    # 已过期的历史条目（nextActionAt 早于 now 且不再重试）不主动删，
    # 由前端展示层判断；这里只保证「存了什么就读回什么」。
    return {"success": True, "count": len(clean), "kind": kind}


def get_account_data_time(db_path) -> str:
    """账号数据每日自动统计时刻（HH:MM），未设置时 00:00。"""
    raw = _read_raw(db_path, ACCOUNT_DATA_KEY)
    text = str(raw or "").strip()
    return text if _HM.match(text) else "00:00"


def set_account_data_time(db_path, value: Any) -> dict:
    """校验并保存统计时刻。非法格式明确报错，不静默回落到 00:00。"""
    text = str(value or "").strip()
    if not _HM.match(text):
        return {"success": False, "reason": "时刻格式应为 HH:MM（00:00 - 23:59）", "time": text}
    try:
        _write_raw(db_path, ACCOUNT_DATA_KEY, text)
    except sqlite3.Error as exc:
        return {"success": False, "reason": f"保存统计时刻失败：{exc}"}
    return {"success": True, "time": text}
