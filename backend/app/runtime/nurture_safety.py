"""养号安全暂停：状态存储 + 「处理后继续」入口。

背景（清单 #38/#39）
--------------------
页面有两个地方读 ``nurtureSafetyPaused``：养号页的「需处理」列表和总览的任务卡。
前端逻辑是 ``R.nurtureSafetyPaused !== void 0 && (te.nurtureSafetyPaused = ...)``，
字段缺失就静默不显示 —— 所以「检测端没实现」在界面上表现为「永远没有需要处理的」，
而「处理后继续」按钮一旦被点到，会打到一个**后端根本没注册**的通道，直接报错。

这里只做两件零风险的事：
1. 把暂停状态**存下来**（谁检测到就写谁），给后续真机风控检测留一个确定的口子；
2. 注册 ``resume-nurture-safety``：幂等、无副作用，只把标记清掉并记一条恢复时间。

刻意不做的事：不从页面猜风控。风控提示可能是 toast、弹窗、验证码页或直接跳登录，
文案还会变；猜错一次就是正常浏览被无故暂停，或者账号已被限流还在继续跑。
检测规则要等真机撞到一次限制、拿到页面结构之后再补。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Mapping

_DDL = """
CREATE TABLE IF NOT EXISTS nurture_safety (
    account_id TEXT,
    task_id TEXT,
    paused INTEGER DEFAULT 0,
    reason TEXT DEFAULT '',
    updated_ms INTEGER,
    resumed_ms INTEGER DEFAULT 0,
    PRIMARY KEY (account_id, task_id)
);
"""


def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(_DDL)
    return con


def _now_ms() -> int:
    return int(time.time() * 1000)


def mark_paused(db_path, *, account_id: str, task_id: str, reason: str = "") -> dict:
    """记录一次安全暂停。检测端接入后调它，界面的「需处理」才会出现。"""
    if not account_id:
        return {"ok": False, "reason": "缺少账号"}
    with _connect(db_path) as con:
        con.execute("INSERT INTO nurture_safety(account_id,task_id,paused,reason,updated_ms) "
                    "VALUES(?,?,1,?,?) ON CONFLICT(account_id,task_id) DO UPDATE SET "
                    "paused=1, reason=excluded.reason, updated_ms=excluded.updated_ms",
                    (account_id, str(task_id or ""), (reason or "")[:300], _now_ms()))
    return {"ok": True, "accountId": account_id, "taskId": str(task_id or ""), "paused": True}


def resume(db_path, payload: Any) -> dict:
    """「处理后继续」：清掉暂停标记。幂等 —— 没有暂停记录也返回成功。"""
    data = payload if isinstance(payload, Mapping) else {}
    account_id = str(data.get("accountId") or "").strip()
    task_id = str(data.get("taskId") or "").strip()
    if not account_id:
        return {"ok": False, "reason": "缺少账号，无法确认要恢复哪个账号的养号"}
    with _connect(db_path) as con:
        changed = con.execute(
            "UPDATE nurture_safety SET paused=0, resumed_ms=?, updated_ms=? "
            "WHERE account_id=? AND task_id=? AND paused=1",
            (_now_ms(), _now_ms(), account_id, task_id)).rowcount
    return {"ok": True, "accountId": account_id, "taskId": task_id,
            "resumed": changed > 0}


def state(db_path, payload: Any = None) -> dict:
    """查询暂停状态：不带账号时返回全部处于暂停的账号。"""
    data = payload if isinstance(payload, Mapping) else {}
    account_id = str(data.get("accountId") or "").strip()
    with _connect(db_path) as con:
        if account_id:
            rows = con.execute("SELECT * FROM nurture_safety WHERE account_id=? AND paused=1",
                               (account_id,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM nurture_safety WHERE paused=1").fetchall()
    return {"ok": True, "items": [
        {"accountId": r["account_id"], "taskId": r["task_id"],
         "paused": bool(r["paused"]), "reason": r["reason"] or "",
         "updatedMs": int(r["updated_ms"] or 0)} for r in rows]}


def paused_accounts(db_path) -> dict[str, str]:
    """{account_id: reason}，给账号列表做可选标注用。"""
    with _connect(db_path) as con:
        rows = con.execute("SELECT account_id, reason FROM nurture_safety WHERE paused=1").fetchall()
    return {str(r["account_id"]): str(r["reason"] or "") for r in rows}
