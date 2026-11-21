"""采集关键词（联想词扩词）：状态机 + 真机执行。

背景
----
页面「采集关键词」弹窗是前端早就做好的功能（Settings 里的 keyword-collector），
它按「关键词 + a」到「关键词 + z」逐次查询，收集抖音搜索框下拉联想词并去重。
但后端 6 个通道此前**一个都没注册**，点「开始采集」必失败 —— 前端在等一个不
存在的后端。本模块把这条链路补齐。

契约（从 ui/assets/Settings-*.js 取证，不是猜的）
------------------------------------------------
state 形状：
    {runId, status, accountId, seeds[], completed, total, currentQuery,
     keywords[], warnings[{query,message}], error}
    status ∈ idle|running|stopping|completed|stopped|failed|verification
通道：
    keyword-collector-start   {accountId, keywords}  → {success, state}
    keyword-collector-stop    {runId}                → {success, state}
    keyword-collector-state   {}                     → state
    keyword-collector-show    {runId}                → {success}（把采集画面调出来）
    keyword-collector-resume  {runId}                → {success, state}（人工过验证后继续）
    keyword-collector-update  事件通道，前端订阅后实时刷新进度

设计取舍
--------
- 逐条查询串行 + 1.2s 左右间隔：联想词本身是搜索接口的旁路，打太密容易被风控拦，
  拦了就要人工过验证，得不偿失。
- 撞到安全验证 → status=verification 并停下，不自动重试。自动重试在验证页上只会
  一直撞墙，还让用户以为程序没反应；界面上「继续采集」按钮就是给这一刻用的。
- 单实例：同时只允许一个采集任务（前端也只支持一个 run 的进度条）。
"""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
import uuid
from typing import Any, Mapping

LETTERS = [chr(ord("a") + i) for i in range(26)]
QUERY_GAP = (900, 1800)          # 两次查询之间的随机间隔（毫秒）
QUERY_TIMEOUT = 95.0             # 单次联想词查询的壳侧超时

_DDL = """
CREATE TABLE IF NOT EXISTS keyword_collector_runs (
    run_id TEXT PRIMARY KEY,
    account_id TEXT,
    status TEXT,
    seeds TEXT,
    total INTEGER,
    completed INTEGER,
    current_query TEXT,
    keywords TEXT,
    warnings TEXT,
    error TEXT,
    created_ms INTEGER,
    updated_ms INTEGER
);
"""

_ACTIVE = {"running", "stopping", "verification"}

_LOCK = threading.Lock()
_STOP = threading.Event()
_WORKER: threading.Thread | None = None


# ---------------------------------------------------------------- 存储
def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(_DDL)
    return con


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load(con: sqlite3.Connection, run_id: str) -> dict | None:
    row = con.execute("SELECT * FROM keyword_collector_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    return {
        "runId": row["run_id"],
        "status": row["status"] or "idle",
        "accountId": row["account_id"] or "",
        "seeds": json.loads(row["seeds"] or "[]"),
        "total": int(row["total"] or 0),
        "completed": int(row["completed"] or 0),
        "currentQuery": row["current_query"] or "",
        "keywords": json.loads(row["keywords"] or "[]"),
        "warnings": json.loads(row["warnings"] or "[]"),
        "error": row["error"] or "",
    }


def _save(con: sqlite3.Connection, state: dict) -> None:
    con.execute(
        "UPDATE keyword_collector_runs SET status=?, total=?, completed=?, current_query=?, "
        "keywords=?, warnings=?, error=?, updated_ms=? WHERE run_id=?",
        (state["status"], int(state["total"]), int(state["completed"]),
         state["currentQuery"], json.dumps(state["keywords"], ensure_ascii=False),
         json.dumps(state["warnings"], ensure_ascii=False), state["error"],
         _now_ms(), state["runId"]))


def _publish(state: dict) -> None:
    """推给页面。事件写库失败只影响进度条刷新，不影响采集本身。"""
    try:
        from app.api import event_bus
        event_bus.publish("keyword-collector-update", state)
    except Exception:  # noqa: BLE001 事件通道不是业务链路
        pass


def _idle_state() -> dict:
    return {"runId": "", "status": "idle", "accountId": "", "seeds": [], "total": 0,
            "completed": 0, "currentQuery": "", "keywords": [], "warnings": [], "error": ""}


def latest(db_path) -> dict:
    with _connect(db_path) as con:
        row = con.execute("SELECT run_id FROM keyword_collector_runs "
                          "ORDER BY created_ms DESC LIMIT 1").fetchone()
        if not row:
            return _idle_state()
        return _load(con, row["run_id"]) or _idle_state()


# ---------------------------------------------------------------- 执行
def _queries(seeds: list[str]) -> list[str]:
    """展开成「种子 + a..z」。顺序固定，completed 才能当断点用。"""
    return [f"{seed}{letter}" for seed in seeds for letter in LETTERS]


def _looks_blocked(res: dict) -> bool:
    if res.get("needVerify") or res.get("verifyHint"):
        return True
    text = str(res.get("error") or "")
    return any(k in text for k in ("验证", "captcha", "安全验证", "登录墙", "未登录"))


def _run(db_path, run_id: str) -> None:
    """采集线程：逐条查询，写进度，推事件。任何一条失败都不中断整体。"""
    global _WORKER
    from app.runtime import browser

    with _connect(db_path) as con:
        state = _load(con, run_id)
    if not state:
        return
    queue = _queries(state["seeds"])
    account_id = state["accountId"]

    while True:
        with _connect(db_path) as con:
            state = _load(con, run_id)
        if not state or state["status"] not in {"running"}:
            break
        if _STOP.is_set():
            state["status"] = "stopped"
            state["currentQuery"] = ""
            with _connect(db_path) as con:
                _save(con, state)
            _publish(state)
            break
        idx = state["completed"]
        if idx >= len(queue):
            state["status"] = "completed"
            state["currentQuery"] = ""
            with _connect(db_path) as con:
                _save(con, state)
            _publish(state)
            break

        query = queue[idx]
        state["currentQuery"] = query
        with _connect(db_path) as con:
            _save(con, state)
        _publish(state)

        res = browser.execute({"type": "suggest-keywords", "keyword": query, "reuse": True},
                              db_path=db_path, timeout=QUERY_TIMEOUT,
                              account_id=account_id)
        if not res.get("ok"):
            if _looks_blocked(res):
                # 撞验证：停下来等人工处理，不自动重试
                state["status"] = "verification"
                state["error"] = str(res.get("error") or "页面被安全验证拦住")[:300]
                with _connect(db_path) as con:
                    _save(con, state)
                _publish(state)
                break
            state["warnings"] = (state["warnings"] + [{
                "query": query,
                "message": str(res.get("error") or "未取得联想词")[:160]}])[:200]
            state["completed"] = idx + 1          # 失败也推进，否则会永远卡在同一条
            with _connect(db_path) as con:
                _save(con, state)
            _publish(state)
        else:
            items = [str(t).strip() for t in (res.get("items") or []) if str(t).strip()]
            seen = set(state["keywords"])
            for text in items:
                if text not in seen:
                    seen.add(text)
                    state["keywords"].append(text)
            state["completed"] = idx + 1
            state["error"] = ""
            with _connect(db_path) as con:
                _save(con, state)
            _publish(state)

        time.sleep(random.uniform(*QUERY_GAP) / 1000.0)

    with _LOCK:
        _WORKER = None


def _spawn(db_path, run_id: str) -> None:
    global _WORKER
    with _LOCK:
        _STOP.clear()
        thread = threading.Thread(target=_run, args=(db_path, run_id),
                                  name=f"kw-collect-{run_id[-6:]}", daemon=True)
        _WORKER = thread
        thread.start()


# ---------------------------------------------------------------- 通道
def start(db_path, payload: Any) -> dict:
    data = payload if isinstance(payload, Mapping) else {}
    account_id = str(data.get("accountId") or "").strip()
    raw = str(data.get("keywords") or "")
    seeds: list[str] = []
    seen: set[str] = set()
    for line in raw.replace(",", "\n").replace("，", "\n").splitlines():
        text = line.strip()
        if text and text not in seen:
            seen.add(text)
            seeds.append(text)
    if not account_id:
        return {"success": False, "error": "请选择执行账号"}
    if not seeds:
        return {"success": False, "error": "请至少填写一个关键词"}
    if len(seeds) > 20:
        return {"success": False, "error": f"关键词最多 20 个（收到 {len(seeds)} 个）"}

    current = latest(db_path)
    if current["status"] in _ACTIVE:
        return {"success": False,
                "error": f"已有采集任务在运行（{current['status']}），请先停止或等待完成",
                "state": current}

    run_id = "kwc_" + uuid.uuid4().hex[:12]
    total = len(seeds) * len(LETTERS)
    state = {"runId": run_id, "status": "running", "accountId": account_id,
             "seeds": seeds, "total": total, "completed": 0, "currentQuery": "",
             "keywords": [], "warnings": [], "error": ""}
    with _connect(db_path) as con:
        con.execute("INSERT INTO keyword_collector_runs(run_id,account_id,status,seeds,total,"
                    "completed,current_query,keywords,warnings,error,created_ms,updated_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, account_id, "running", json.dumps(seeds, ensure_ascii=False),
                     total, 0, "", "[]", "[]", "", _now_ms(), _now_ms()))
        _save(con, state)
    _publish(state)
    _spawn(db_path, run_id)
    return {"success": True, "state": state}


def stop(db_path, payload: Any) -> dict:
    data = payload if isinstance(payload, Mapping) else {}
    run_id = str(data.get("runId") or "").strip()
    state = latest(db_path)
    if run_id and state["runId"] and run_id != state["runId"]:
        return {"success": False, "error": "runId 与当前采集任务不一致", "state": state}
    if not run_id:
        run_id = state["runId"]
    if not run_id:
        return {"success": True, "state": _idle_state(), "note": "没有正在运行的采集任务"}
    with _connect(db_path) as con:
        state = _load(con, run_id) or _idle_state()
        if state["status"] in {"running", "verification"}:
            _STOP.set()
            state["status"] = "stopping"
            _save(con, state)
        elif state["status"] == "stopping":
            pass
    _publish(state)
    return {"success": True, "state": state}


def state_of(db_path, payload: Any = None) -> dict:
    return {"success": True, **latest(db_path)}


def resume(db_path, payload: Any) -> dict:
    """人工过完验证后继续：从 completed 那一条重跑，不清已有结果。"""
    data = payload if isinstance(payload, Mapping) else {}
    run_id = str(data.get("runId") or "").strip()
    with _connect(db_path) as con:
        current = _load(con, run_id) if run_id else None
        if current is None:
            row = con.execute("SELECT run_id FROM keyword_collector_runs "
                              "ORDER BY created_ms DESC LIMIT 1").fetchone()
            current = _load(con, row["run_id"]) if row else None
        if current is None:
            return {"success": False, "error": "没有可继续的采集任务", "state": _idle_state()}
        if current["status"] != "verification":
            return {"success": True, "state": current,
                    "note": "当前不在「待验证」状态，无需继续"}
        current["status"] = "running"
        current["error"] = ""
        _save(con, current)
    _publish(current)
    _spawn(db_path, current["runId"])
    return {"success": True, "state": current}


def show(db_path, payload: Any) -> dict:
    """把采集用的页面窗口调出来（用户要看着它跑 / 手工过验证）。"""
    from app.runtime import browser

    data = payload if isinstance(payload, Mapping) else {}
    run_id = str(data.get("runId") or "").strip()
    state = latest(db_path)
    if run_id and state["runId"] and run_id != state["runId"]:
        return {"success": False, "error": "runId 与当前采集任务不一致"}
    account_id = (state["runId"] and state["accountId"]) or str(data.get("accountId") or "")
    res = browser.execute({"type": "show-worker", "accountId": account_id},
                          db_path=db_path, timeout=30, account_id=account_id)
    if not res.get("ok"):
        return {"success": False, "error": str(res.get("error") or "采集窗口暂时无法显示")[:200]}
    return {"success": True, "visible": bool(res.get("visible")), "account": res.get("account")}
