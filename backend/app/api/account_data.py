"""账号数据 · 「立即统计」的真机采样落库。

为什么不是直接放行通道
----------------------
`run-account-data-collect` 原来在 BLOCKED 里，注释写着「仍无执行引擎，放行即假成功」。
现在有了采样器（壳侧 `read-account-stats`：读该账号主页的关注/粉丝/获赞/作品），
才把它接到执行链路上。

两条纪律：
1. **只写读到的字段**。没读到粉丝数就保留昨天那条快照的粉丝数，绝不写 0 ——
   否则看板会把「没读到」画成「掉了 3 万粉」。
2. **一个账号失败不影响其它账号**，结果逐条回报 ok / skipped / error，页面据此
   显示「完成 X 个，跳过 Y 个，失败 Z 个」。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

_LOCK = threading.RLock()
_COLLECTING = {"on": False}

FIELDS = ("following", "followers", "works", "likes")


def _connect(db_path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _num(text: Any) -> int | None:
    """把「1.2万」「3亿」「1,234」换算成整数；读不出来返回 None（不是 0）。"""
    raw = str(text or "").strip().replace(",", "").replace("，", "")
    if not raw:
        return None
    unit = 1
    if raw.endswith("万") or raw.endswith("w") or raw.endswith("W"):
        unit, raw = 10000, raw[:-1]
    elif raw.endswith("亿"):
        unit, raw = 100_000_000, raw[:-1]
    try:
        return int(round(float(raw) * unit))
    except (TypeError, ValueError):
        return None


def _upsert(db_path: Path | str, account_id: str, stats: Mapping) -> int:
    """写入当天快照：只覆盖真正读到的字段，返回写入的字段数。"""
    day = time.strftime("%Y-%m-%d")
    values = {k: _num(stats.get(k)) for k in FIELDS}
    values = {k: v for k, v in values.items() if v is not None}
    if not values:
        return 0
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT following, followers, works, likes FROM account_stats_daily "
            "WHERE account_id=? AND day=?", (account_id, day)).fetchone()
        base = dict(row) if row else {}
        base.update(values)
        con.execute(
            "INSERT INTO account_stats_daily(account_id,day,nickname,douyin_id,"
            "following,followers,works,likes,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,day) DO UPDATE SET nickname=excluded.nickname, "
            "douyin_id=excluded.douyin_id, following=excluded.following, "
            "followers=excluded.followers, works=excluded.works, likes=excluded.likes, "
            "updated_at=excluded.updated_at",
            (account_id, day, str(stats.get("nickname") or ""),
             str(stats.get("douyinId") or ""),
             int(base.get("following") or 0), int(base.get("followers") or 0),
             int(base.get("works") or 0), int(base.get("likes") or 0),
             time.strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
    finally:
        con.close()
    return len(values)


def collect(db_path, payload: Any = None) -> dict:
    """run-account-data-collect：逐个账号读主页计数并落当天快照。"""
    from app.api import aggregate
    from app.runtime import browser

    data = payload if isinstance(payload, Mapping) else {}
    wanted = data.get("accountIds")
    if isinstance(wanted, str):
        wanted = [wanted]
    with _LOCK:
        if _COLLECTING["on"]:
            return {"success": False, "reason": "collecting"}
        _COLLECTING["on"] = True
    try:
        # 先判能力，再读账号：没有壳的话再怎么统计也读不到数据，
        # 这时候说「账号池为空」会误导用户去加账号。
        if not browser.shell_available():
            return {"success": False,
                    "reason": "桌面壳未运行：账号数据要通过本机登录窗口读取，请启动桌面端后重试"}
        try:
            con = _connect(db_path)
            try:
                rows = con.execute(
                    "SELECT id,name,nickname FROM accounts ORDER BY created_at").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            rows = []
        targets = [r for r in rows if not wanted or str(r["id"]) in {str(w) for w in wanted}]
        if not targets:
            return {"success": False, "reason": "账号池里没有可统计的账号"}

        results: list[dict] = []
        for row in targets[:30]:
            account_id = str(row["id"])
            label = str(row["name"] or row["nickname"] or account_id)
            try:
                res = browser.execute({"type": "read-account-stats", "accountId": account_id},
                                      db_path=db_path, timeout=120, account_id=account_id,
                                      engine="shell")
            except Exception as exc:  # noqa: BLE001 - 单账号异常不阻断整批
                results.append({"accountId": account_id, "name": label, "ok": False,
                                "error": f"{type(exc).__name__}: {exc}"})
                continue
            if res.get("ok"):
                written = _upsert(db_path, account_id, res)
                if written:
                    results.append({"accountId": account_id, "name": label, "ok": True,
                                    "fields": written,
                                    "nickname": str(res.get("nickname") or ""),
                                    "stats": {k: _num(res.get(k)) for k in FIELDS}})
                else:
                    results.append({"accountId": account_id, "name": label, "ok": False,
                                    "skipped": True, "error": "页面读到了，但数值都为空"})
                continue
            # 未登录的账号不算失败，算跳过（用户知道要先去登录）
            skipped = str(res.get("code") or "") == "login_required"
            results.append({"accountId": account_id, "name": label, "ok": False,
                            "skipped": skipped,
                            "error": str(res.get("error") or "读取失败")})

        return {
            "success": True,
            "results": results,
            "dashboard": aggregate.account_dashboard({}),
            "note": "账号数据来自各账号本机登录窗口的主页计数，未读取作品级数据",
        }
    finally:
        with _LOCK:
            _COLLECTING["on"] = False


def collecting() -> bool:
    return bool(_COLLECTING["on"])
