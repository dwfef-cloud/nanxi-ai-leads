"""后台控制台数据层（admin_console.db）。

与业务三库完全隔离：这里只存「谁在用、用了多少、密钥发给了谁」，
不存任何客户线索/账号/内容数据。整库可随包迁移。

表：
  licenses      密钥台账（签发、套餐、配额、到期、绑定机器、状态）
  instances     客户机实例（机器码、心跳、版本、在线状态）
  usage_daily   按天/实例聚合的使用量（采集、评论、私信、登录）
  audit_log     后台操作审计（谁在什么时候改了哪把密钥）

约定：连接随用随关（sqlite3 单文件，后台并发很低），时间统一 UTC ISO8601。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.paths import DATA_DIR

DEFAULT_DB = DATA_DIR / "admin_console.db"
DB_PATH = Path(os.getenv("ADMIN_DB_PATH", str(DEFAULT_DB)))

LICENSE_STATUSES = ("unused", "active", "suspended", "revoked", "expired")
PLANS = ("basic", "standard", "pro", "ultimate")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    plan            TEXT NOT NULL DEFAULT 'standard',
    customer_name   TEXT DEFAULT '',
    customer_contact TEXT DEFAULT '',
    seats           INTEGER NOT NULL DEFAULT 1,
    daily_quota     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'unused',
    issued_at       TEXT NOT NULL,
    activated_at    TEXT,
    expires_at      TEXT,
    bound_machine   TEXT DEFAULT '',
    note            TEXT DEFAULT '',
    created_by      TEXT DEFAULT 'admin'
);
CREATE INDEX IF NOT EXISTS idx_lic_status ON licenses(status);
CREATE INDEX IF NOT EXISTS idx_lic_expires ON licenses(expires_at);

CREATE TABLE IF NOT EXISTS instances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   TEXT NOT NULL UNIQUE,
    license_code TEXT NOT NULL,
    hostname     TEXT DEFAULT '',
    app_version  TEXT DEFAULT '',
    ip           TEXT DEFAULT '',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    actions_total INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inst_lic ON instances(license_code);
CREATE INDEX IF NOT EXISTS idx_inst_seen ON instances(last_seen);

CREATE TABLE IF NOT EXISTS usage_daily (
    day          TEXT NOT NULL,
    instance_id  TEXT NOT NULL,
    license_code TEXT DEFAULT '',
    leads        INTEGER NOT NULL DEFAULT 0,
    comments     INTEGER NOT NULL DEFAULT 0,
    dms          INTEGER NOT NULL DEFAULT 0,
    logins       INTEGER NOT NULL DEFAULT 0,
    actions      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (day, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_lic ON usage_daily(license_code, day);

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    actor  TEXT NOT NULL DEFAULT 'admin',
    action TEXT NOT NULL,
    target TEXT DEFAULT '',
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_str(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


class AdminStore:
    """后台控制台存储。所有 SQL 一律参数绑定。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.path = Path(db_path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---------- 基础设施 ----------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur.rowcount

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def _scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with closing(self._connect()) as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    def audit(self, action: str, target: str = "", detail: str = "", actor: str = "admin") -> None:
        self._exec(
            "INSERT INTO audit_log(at, actor, action, target, detail) VALUES (?,?,?,?,?)",
            (now_iso(), actor, action, target, detail),
        )

    # ---------- 密钥 ----------
    def insert_license(self, rec: Mapping[str, Any]) -> None:
        self._exec(
            """INSERT INTO licenses
               (code, plan, customer_name, customer_contact, seats, daily_quota,
                status, issued_at, expires_at, note, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec["code"], rec.get("plan", "standard"),
                rec.get("customer_name", ""), rec.get("customer_contact", ""),
                int(rec.get("seats") or 1), int(rec.get("daily_quota") or 0),
                rec.get("status", "unused"), rec.get("issued_at") or now_iso(),
                rec.get("expires_at"), rec.get("note", ""),
                rec.get("created_by", "admin"),
            ),
        )

    def code_exists(self, code: str) -> bool:
        return self._scalar("SELECT 1 FROM licenses WHERE code=?", (code,)) is not None

    def get_license(self, code: str) -> dict | None:
        return self._one("SELECT * FROM licenses WHERE code=?", (code,))

    def list_licenses(
        self, status: str = "", keyword: str = "", plan: str = "",
        page: int = 1, size: int = 20,
    ) -> tuple[list[dict], int]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status=?")
            params.append(status)
        if plan:
            where.append("plan=?")
            params.append(plan)
        if keyword:
            like = f"%{keyword}%"
            where.append(
                "(code LIKE ? OR customer_name LIKE ? OR customer_contact LIKE ?"
                " OR note LIKE ? OR bound_machine LIKE ?)"
            )
            params.extend([like] * 5)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(self._scalar(f"SELECT COUNT(*) FROM licenses{clause}", params) or 0)
        rows = self._all(
            f"SELECT * FROM licenses{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size],
        )
        return rows, total

    def update_license(self, code: str, fields: Mapping[str, Any]) -> int:
        allowed = {
            "plan", "customer_name", "customer_contact", "seats", "daily_quota",
            "status", "expires_at", "bound_machine", "note", "activated_at",
        }
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                params.append(value)
        if not sets:
            return 0
        params.append(code)
        return self._exec(f"UPDATE licenses SET {', '.join(sets)} WHERE code=?", params)

    def delete_license(self, code: str) -> int:
        return self._exec("DELETE FROM licenses WHERE code=?", (code,))

    def all_license_codes(self) -> set[str]:
        return {r["code"] for r in self._all("SELECT code FROM licenses")}

    def expire_overdue(self) -> int:
        """把已过到期时间但仍为 active/unused 的密钥标记为 expired。返回影响行数。"""
        now = now_iso()
        return self._exec(
            "UPDATE licenses SET status='expired' "
            "WHERE expires_at IS NOT NULL AND expires_at<>'' AND expires_at < ? "
            "AND status IN ('active','unused')",
            (now,),
        )

    # ---------- 实例 ----------
    def upsert_instance(
        self, machine_id: str, license_code: str, hostname: str = "",
        app_version: str = "", ip: str = "", actions_delta: int = 0,
        seen_at: str | None = None,
    ) -> str:
        ts = seen_at or now_iso()
        existing = self._one("SELECT id, first_seen FROM instances WHERE machine_id=?", (machine_id,))
        if existing:
            self._exec(
                "UPDATE instances SET license_code=?, hostname=?, app_version=?, ip=?,"
                " last_seen=?, actions_total=actions_total+? WHERE machine_id=?",
                (license_code, hostname, app_version, ip, ts, int(actions_delta), machine_id),
            )
            return existing["first_seen"]
        self._exec(
            """INSERT INTO instances
               (machine_id, license_code, hostname, app_version, ip, first_seen, last_seen, actions_total)
               VALUES (?,?,?,?,?,?,?,?)""",
            (machine_id, license_code, hostname, app_version, ip, ts, ts, int(actions_delta)),
        )
        return ts

    def list_instances(self, keyword: str = "", online_minutes: int = 10) -> list[dict]:
        """
        online 判定：last_seen 在 online_minutes 内。
        用 SQLite 的 julianday 计算，避免依赖本机时区。
        """
        where, params = "", []
        if keyword:
            like = f"%{keyword}%"
            where = " WHERE (i.machine_id LIKE ? OR i.hostname LIKE ? OR i.license_code LIKE ?" \
                    " OR l.customer_name LIKE ?)"
            params = [like] * 4
        rows = self._all(
            f"""SELECT i.*, l.customer_name, l.plan, l.status AS license_status, l.expires_at,
                       CASE WHEN (julianday('now') - julianday(i.last_seen)) * 1440 <= ?
                            THEN 1 ELSE 0 END AS online
                FROM instances i LEFT JOIN licenses l ON l.code = i.license_code
                {where}
                ORDER BY i.last_seen DESC""",
            [online_minutes] + params,
        )
        return rows

    def instance_count(self, online_minutes: int = 10) -> dict:
        total = int(self._scalar("SELECT COUNT(*) FROM instances") or 0)
        online = int(
            self._scalar(
                "SELECT COUNT(*) FROM instances "
                "WHERE (julianday('now') - julianday(last_seen)) * 1440 <= ?",
                (online_minutes,),
            ) or 0
        )
        return {"total": total, "online": online, "offline": total - online}

    # ---------- 用量 ----------
    def add_usage(
        self, day: str, instance_id: str, license_code: str,
        leads: int = 0, comments: int = 0, dms: int = 0,
        logins: int = 0, actions: int = 0,
    ) -> None:
        self._exec(
            """INSERT INTO usage_daily(day, instance_id, license_code, leads, comments,
                                        dms, logins, actions, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day, instance_id) DO UPDATE SET
                 license_code=excluded.license_code,
                 leads=usage_daily.leads + excluded.leads,
                 comments=usage_daily.comments + excluded.comments,
                 dms=usage_daily.dms + excluded.dms,
                 logins=usage_daily.logins + excluded.logins,
                 actions=usage_daily.actions + excluded.actions,
                 updated_at=excluded.updated_at""",
            (day, instance_id, license_code, int(leads), int(comments), int(dms),
             int(logins), int(actions), now_iso()),
        )

    def usage_series(self, days: int = 14, license_code: str = "") -> list[dict]:
        """近 N 天按天汇总，缺失日期补 0（前端画图不缺格）。"""
        start = today_str(-(days - 1))
        where = "WHERE day >= ?"
        params: list[Any] = [start]
        if license_code:
            where += " AND license_code=?"
            params.append(license_code)
        rows = self._all(
            f"""SELECT day, SUM(leads) leads, SUM(comments) comments, SUM(dms) dms,
                       SUM(logins) logins, SUM(actions) actions
                FROM usage_daily {where} GROUP BY day ORDER BY day""",
            params,
        )
        by_day = {r["day"]: r for r in rows}
        series = []
        for i in range(days):
            d = today_str(-(days - 1 - i))
            r = by_day.get(d)
            series.append({
                "day": d,
                "leads": int(r["leads"] or 0) if r else 0,
                "comments": int(r["comments"] or 0) if r else 0,
                "dms": int(r["dms"] or 0) if r else 0,
                "logins": int(r["logins"] or 0) if r else 0,
                "actions": int(r["actions"] or 0) if r else 0,
            })
        return series

    def usage_by_license(self, days: int = 14) -> list[dict]:
        start = today_str(-(days - 1))
        return self._all(
            """SELECT u.license_code, COALESCE(l.customer_name,'') customer_name,
                      SUM(u.leads) leads, SUM(u.comments) comments, SUM(u.dms) dms,
                      SUM(u.logins) logins, SUM(u.actions) actions
               FROM usage_daily u LEFT JOIN licenses l ON l.code = u.license_code
               WHERE u.day >= ?
               GROUP BY u.license_code ORDER BY actions DESC""",
            (start,),
        )

    def used_on(self, day: str, instance_id: str) -> int:
        """某实例某天已累计的动作数（配额判断用）。"""
        n = self._scalar(
            "SELECT COALESCE(actions,0) FROM usage_daily WHERE day=? AND instance_id=?",
            (day, instance_id),
        )
        return int(n or 0)

    def usage_today(self) -> dict:
        row = self._one(
            "SELECT COALESCE(SUM(leads),0) leads, COALESCE(SUM(comments),0) comments,"
            " COALESCE(SUM(dms),0) dms, COALESCE(SUM(logins),0) logins,"
            " COALESCE(SUM(actions),0) actions, COUNT(DISTINCT instance_id) active_instances"
            " FROM usage_daily WHERE day=?",
            (today_str(),),
        )
        return row or {}

    def license_stats(self) -> dict:
        rows = self._all("SELECT status, COUNT(*) n FROM licenses GROUP BY status")
        stats = {s: 0 for s in LICENSE_STATUSES}
        for r in rows:
            stats[r["status"]] = int(r["n"])
        stats["total"] = sum(stats.values())
        return stats

    # ---------- 审计 ----------
    def list_audit(self, limit: int = 100, keyword: str = "") -> list[dict]:
        if keyword:
            like = f"%{keyword}%"
            return self._all(
                "SELECT * FROM audit_log WHERE action LIKE ? OR target LIKE ? OR detail LIKE ?"
                " ORDER BY id DESC LIMIT ?",
                (like, like, like, limit),
            )
        return self._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
