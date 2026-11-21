"""监控对象 · 群聊列表：真机读取 + 本机缓存。

为什么群列表必须缓存：页面写明「群列表保存在本机，未勾选的也会保留」——
用户下次打开弹窗仍能看到上次读到的全部群聊，只勾选要监控的那几个。
所以缓存是「账号 + 群名」的累加表：重新读取只做并集，不覆盖、不删除；
用户手动删掉的群也要靠这张表保留下来。

读取走壳的只读探针 read-group-list（打开会话列表逐行判定群聊），
本模块只负责：按账号串行执行、把结果写缓存、把进度按账号推给页面。
"""
from __future__ import annotations

import sqlite3
import time

TABLE = "monitor_group_cache"
MAX_ACCOUNTS = 20


def _connect(db_path):
    return sqlite3.connect(str(db_path), timeout=15)


def _ensure(con) -> None:
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE}("
        "account_id TEXT NOT NULL, name TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY(account_id, name))")


def save(db_path, account_id: str, names: list[str]) -> int:
    """把本次读到的群名并入缓存（同账号同名只刷新时间）。"""
    cleaned = [str(n).strip()[:120] for n in names if str(n).strip()]
    if not cleaned:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con = _connect(db_path)
    try:
        _ensure(con)
        con.executemany(
            f"INSERT INTO {TABLE}(account_id, name, updated_at) VALUES(?,?,?) "
            f"ON CONFLICT(account_id, name) DO UPDATE SET updated_at=excluded.updated_at",
            [(str(account_id), n, now) for n in dict.fromkeys(cleaned)])
        con.commit()
        return len(cleaned)
    finally:
        con.close()


def cached(db_path) -> list[dict]:
    con = _connect(db_path)
    try:
        _ensure(con)
        rows = con.execute(
            f"SELECT account_id, name FROM {TABLE} ORDER BY account_id, name").fetchall()
        return [{"accountId": r[0], "name": r[1]} for r in rows]
    finally:
        con.close()


def read(db_path, account_ids: list[str], *, emit=None, cancelled=lambda: False) -> dict:
    """按账号串行读群聊。返回结构直接就是页面 read-monitor-group-list 的约定：
    {success, results:[{accountId, status: done|error, names, message}], cancelled?, cacheError?}
    """
    from app.runtime import browser

    ids = [str(a) for a in account_ids][:MAX_ACCOUNTS]
    results: list[dict] = []
    cache_error = ""
    seen_by_account: dict[str, list[str]] = {}
    for account_id in ids:
        if cancelled():
            out = {"success": True, "cancelled": True, "results": results}
            if cache_error:
                out["cacheError"] = cache_error
            return out
        if emit:
            emit({"accountId": account_id, "names": []})
        res = browser.execute(
            {"type": "read-group-list", "scrollScreens": 6, "accountId": account_id},
            db_path=db_path, account_id=account_id, engine="shell", timeout=180)
        if not res.get("ok"):
            results.append({"accountId": account_id, "status": "error",
                            "message": str(res.get("error") or "读取群聊失败")})
            continue
        names = [str(g.get("name") or "").strip() for g in (res.get("groups") or [])
                 if isinstance(g, dict)]
        names = [n for n in dict.fromkeys(names) if n]
        seen_by_account[account_id] = names
        try:
            save(db_path, account_id, names)
        except sqlite3.Error as exc:
            cache_error = cache_error or str(exc)
        results.append({"accountId": account_id, "status": "done", "names": names,
                        "message": str(res.get("note") or "")})
        if emit and names:
            # 逐账号推一次，页面上「正在读取，已列出 N 项」才有真实数字
            emit({"accountId": account_id, "names": names})
    out = {"success": True, "results": results}
    if cache_error:
        out["cacheError"] = "群聊已读取，但未能保存到本机：" + cache_error
    return out
