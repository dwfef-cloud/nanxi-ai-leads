"""账号池管理 —— 壳内保存落库 + 批量清理接口的数据层。

背景
----
壳里「账号池」的单条删除只是改了前端数组，保存走 `save-account-pool`，
而该通道此前未接线，所以删除刷新后会"复活"。这里补齐真实同步，
并提供多选 / 全选 / 批量删除所需的查询与批处理能力。

删除语义（写清楚，避免误删）：
- 删 `accounts` 行；同时清掉该账号的 `account_events` / `account_stats_daily` /
  `dm_send_results`（账号级运行痕迹）；
- **保留** `leads` / `comment_tasks`（历史线索与任务记录不因删账号而丢）；
- 若该账号有本地登录态目录且位于 data/ 下，一并删除（否则占用磁盘）。
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

# 前端 online/offline <-> 库里 active/idle
_STATUS_TO_DB = {"online": "active", "offline": "idle"}
_STATUS_FROM_DB = {"active": "online", "idle": "offline"}

# 测试账号识别：自动化用例留下的 sales-1 / acct-detail 之类
_TEST_NAME_RE = re.compile(r"^(sales-\d+|acct-detail|新抖音账号|__test__.*)$", re.I)


def _connect(db_path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _lead_counts(con: sqlite3.Connection) -> dict[str, int]:
    """按账号统计关联线索数，删号前让用户看到影响面。"""
    if not _table_exists(con, "leads"):
        return {}
    try:
        rows = con.execute(
            "SELECT account AS name, COUNT(*) AS n FROM leads "
            "WHERE account IS NOT NULL AND account<>'' GROUP BY account").fetchall()
    except sqlite3.Error:
        return {}
    return {str(r["name"]): int(r["n"]) for r in rows}


def list_pool(db_path: Path | str, keyword: str = "", only: str = "") -> dict:
    """账号列表。only: all | duplicate（重名）| test（测试账号）| nologin（无登录态）。"""
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, name, nickname, platform, status, limit_status, health_score, "
            "daily_outreach, daily_limit, notes, profile_dir, created_at, updated_at "
            "FROM accounts ORDER BY created_at DESC").fetchall()
        by_lead = _lead_counts(con)
        by_name_lead = {}
        if _table_exists(con, "leads"):
            try:
                for r in con.execute(
                        "SELECT account, COUNT(*) n FROM leads "
                        "WHERE account IS NOT NULL GROUP BY account"):
                    by_name_lead[str(r["account"])] = int(r["n"])
            except sqlite3.Error:
                pass

        items: list[dict] = []
        name_count: dict[str, int] = {}
        for r in rows:
            name = str(r["name"] or "")
            name_count[name] = name_count.get(name, 0) + 1
            items.append({
                "id": r["id"], "name": name,
                "nickname": r["nickname"] or "", "platform": r["platform"] or "douyin",
                "status": _STATUS_FROM_DB.get(str(r["status"]), "offline"),
                "limitStatus": r["limit_status"] or "healthy",
                "healthScore": r["health_score"],
                "dailyOutreach": r["daily_outreach"], "dailyLimit": r["daily_limit"],
                "notes": r["notes"] or "", "profileDir": r["profile_dir"] or "",
                "createdAt": r["created_at"],
                "leadCount": by_lead.get(r["id"], 0) + by_name_lead.get(name, 0),
            })
        for it in items:
            it["duplicate"] = name_count.get(it["name"], 0) > 1
            it["isTest"] = bool(_TEST_NAME_RE.match(it["name"]))
    finally:
        con.close()

    kw = (keyword or "").strip().lower()
    if kw:
        items = [it for it in items
                 if kw in f"{it['name']} {it['nickname']} {it['id']}".lower()]
    if only == "duplicate":
        items = [it for it in items if it["duplicate"]]
    elif only == "test":
        items = [it for it in items if it["isTest"]]
    elif only == "nologin":
        items = [it for it in items if not it["profileDir"]]

    dup_groups: dict[str, int] = {}
    for it in items:
        if it["duplicate"]:
            dup_groups[it["name"]] = dup_groups.get(it["name"], 0) + 1

    return {
        "success": True,
        "items": items,
        "total": len(items),
        "summary": {
            "all": len(rows),
            "duplicateGroups": len(dup_groups),
            "duplicateAccounts": sum(dup_groups.values()),
            "testAccounts": sum(1 for it in items if it["isTest"]),
        },
    }


def _remove_profile_dir(data_dir: Path, profile_dir: str) -> bool:
    """只删 data/ 下的账号态目录，避免误删用户自己的路径。"""
    text = (profile_dir or "").strip()
    if not text:
        return False
    try:
        target = Path(text)
        if not target.is_absolute():
            target = (data_dir / target).resolve()
        else:
            target = target.resolve()
        root = data_dir.resolve()
        if root not in target.parents and target != root:
            return False
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            return True
    except OSError:
        return False
    return False


def delete_accounts(db_path: Path | str, ids: list[str],
                    data_dir: Path | str | None = None) -> dict:
    """批量删除账号。返回删除明细，供页面二次确认后展示结果。"""
    wanted = [str(i) for i in (ids or []) if str(i).strip()]
    if not wanted:
        return {"success": False, "error": "未选择账号"}
    marks = ",".join("?" * len(wanted))
    db_path = Path(db_path)
    root = Path(data_dir) if data_dir else db_path.parent

    con = _connect(db_path)
    try:
        rows = con.execute(
            f"SELECT id, name, nickname, profile_dir FROM accounts WHERE id IN ({marks})",
            tuple(wanted)).fetchall()
        if not rows:
            return {"success": False, "error": "账号不存在或已被删除"}
        names = [str(r["name"] or r["id"]) for r in rows]
        found = [str(r["id"]) for r in rows]

        cascade = {"account_events": 0, "account_stats_daily": 0, "dm_send_results": 0}
        for table, column in (("account_events", "account_id"),
                              ("account_stats_daily", "account_id"),
                              ("dm_send_results", "account_id")):
            if _table_exists(con, table):
                try:
                    cascade[table] = con.execute(
                        f"DELETE FROM {table} WHERE {column} IN "
                        f"({','.join('?' * len(found))})", tuple(found)).rowcount
                except sqlite3.Error:
                    cascade[table] = 0

        removed = con.execute(
            f"DELETE FROM accounts WHERE id IN ({','.join('?' * len(found))})",
            tuple(found)).rowcount
        con.commit()
    finally:
        con.close()

    profiles = 0
    for r in rows:
        if _remove_profile_dir(root, str(r["profile_dir"] or "")):
            profiles += 1
    return {
        "success": True, "removed": removed, "names": names,
        "profilesRemoved": profiles, "cascade": cascade,
        "note": "历史线索与任务记录已保留（不随账号删除）",
    }


def reset_status(db_path: Path | str, ids: list[str]) -> dict:
    """把选中账号的限流状态复位为 healthy（用于清掉测试留下的 warn/限流标记）。"""
    wanted = [str(i) for i in (ids or []) if str(i).strip()]
    if not wanted:
        return {"success": False, "error": "未选择账号"}
    con = _connect(db_path)
    try:
        n = con.execute(
            f"UPDATE accounts SET limit_status='healthy', updated_at=CURRENT_TIMESTAMP "
            f"WHERE id IN ({','.join('?' * len(wanted))})", tuple(wanted)).rowcount
        con.commit()
    finally:
        con.close()
    return {"success": True, "updated": n}


def set_login_status(db_path: Path | str, account_id: str, status: str,
                     nickname: str = "") -> bool:
    """把真机检测结果写回账号库（online→active / offline→idle）。

    登录发生在壳的浏览器分区里，库里那份 status 只是影子；不回写的话
    「界面里登录成功」和「账号池显示未登录」会永远对不上。
    nickname 非空才更新（页面读不到昵称时不覆盖库里已有值）。
    """
    db_status = _STATUS_TO_DB.get(str(status or ""), None)
    if db_status is None:
        return False
    con = _connect(db_path)
    try:
        sets = ["status=?", "updated_at=CURRENT_TIMESTAMP"]
        params: list = [db_status]
        if str(nickname or "").strip():
            sets.append("nickname=?")
            params.append(str(nickname).strip())
        params.append(str(account_id))
        n = con.execute(
            f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", tuple(params)).rowcount
        con.commit()
        return n > 0
    except sqlite3.Error:
        return False
    finally:
        con.close()


# --------------------------------------------------------------------------
# 壳内 save-account-pool 同步
# --------------------------------------------------------------------------
def sync_pool(db_path: Path | str, items: Any) -> dict:
    """按壳里的账号数组做全量同步（增 / 改 / 删）。

    删除只发生在**报文里出现过的平台**内，且必须显式 missing：
    这样即使某个平台页面没加载全，也不会把其它平台的账号一起清掉。
    """
    if not isinstance(items, list):
        return {"success": False, "reason": "账号池数据格式不正确"}

    con = _connect(db_path)
    try:
        existing = {str(r["id"]): dict(r) for r in con.execute(
            "SELECT id, platform FROM accounts").fetchall()}
        platforms = set()
        added = updated = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = str(it.get("id") or "").strip()
            if not pid:
                continue
            platform = str(it.get("platform") or "douyin")
            platforms.add(platform)
            status = _STATUS_TO_DB.get(str(it.get("status") or ""), "idle")
            name = str(it.get("name") or pid)
            nickname = str(it.get("nickname") or "")
            notes = str(it.get("remark") or it.get("notes") or "")
            if pid in existing:
                con.execute(
                    "UPDATE accounts SET name=?, nickname=?, platform=?, status=?, "
                    "notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (name, nickname, platform, status, notes, pid))
                updated += 1
            else:
                con.execute(
                    "INSERT INTO accounts(id, name, nickname, platform, status, notes, "
                    "health_score, limit_status, daily_outreach, daily_limit, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?,100,'healthy',0,80,"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                    (pid, name, nickname, platform, status, notes))
                added += 1

        keep = {str(it.get("id")) for it in items if isinstance(it, dict) and it.get("id")}
        doomed = [pid for pid in existing
                  if pid not in keep and existing[pid].get("platform") in platforms]
        if doomed:
            marks = ",".join("?" * len(doomed))
            for table in ("account_events", "account_stats_daily", "dm_send_results"):
                if _table_exists(con, table):
                    con.execute(f"DELETE FROM {table} WHERE account_id IN ({marks})",
                                tuple(doomed))
            con.execute(f"DELETE FROM accounts WHERE id IN ({marks})", tuple(doomed))
        con.commit()
    except sqlite3.Error as exc:
        return {"success": False, "reason": f"账号池写入失败：{exc}"}
    finally:
        con.close()
    return {"success": True, "added": added, "updated": updated,
            "removed": len(doomed), "platforms": sorted(platforms)}
