"""M6 账号池 v2（D23）—— 账号状态机 + 封号风险事件 + 租约联动。

状态机（DESIGN-DOC §M6）：

    healthy → cooling        触发验证码/风控冷却（driver 写 cooldown.json 时上报）
    cooling → healthy        冷却到期（读取时自动判定）
    healthy → risk_warning   风险积分累计到阈值（uncertain 写入 +1/次）
    risk_warning → suspended 警告期再次出现风险事件，或人工暂停
    suspended → healthy      仅人工恢复（confirm 模式，永不自动解除）

事件 → 状态映射（本地策略，代码内写死、全部可测）：
    captcha          → cooling（2h，与 engine 冷却一致）
    send_uncertain   → 风险积分 +1；积分 ≥3 → risk_warning
    risk_warning 期间任何风险事件 → suspended
    send_fail_403    → cooling（4h）

租约联动：pick_available() 只返回 healthy 且未被租约持有的账号；
崩溃租约回收复用 EngineManager（心跳 30s，D8 已测），本模块只做状态过滤。

存储：独立 sqlite（data/account_pool.db），模式同 dm_guard；不改南溪现有文件。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

STATE_HEALTHY = "healthy"
STATE_COOLING = "cooling"
STATE_RISK_WARNING = "risk_warning"
STATE_SUSPENDED = "suspended"
VALID_STATES = frozenset({STATE_HEALTHY, STATE_COOLING, STATE_RISK_WARNING, STATE_SUSPENDED})

RISK_WARNING_THRESHOLD = 3       # 风险积分阈值：达到即 risk_warning
COOLING_HOURS_CAPTCHA = 2.0      # 触发验证码冷却（与 engine.py 一致）
COOLING_HOURS_403 = 4.0          # 403 风控拦截冷却


class AccountPool:
    """账号池 v2。线程安全；状态全部持久化。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    # ── 基础设施 ─────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_pool_states (
                    account_id  TEXT PRIMARY KEY,
                    state       TEXT NOT NULL DEFAULT 'healthy',
                    risk_points INTEGER NOT NULL DEFAULT 0,
                    cooling_until  INTEGER,
                    suspend_reason TEXT NOT NULL DEFAULT '',
                    updated_at  INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_risk_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    kind       TEXT NOT NULL,
                    detail     TEXT NOT NULL DEFAULT '',
                    happened_at INTEGER NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_risk_events_acct"
                " ON account_risk_events(account_id, happened_at)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _row(self, conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
        conn.execute(
            "INSERT OR IGNORE INTO account_pool_states (account_id, state, updated_at)"
            " VALUES (?,?,?)", (account_id, STATE_HEALTHY, int(time.time())))
        return conn.execute(
            "SELECT * FROM account_pool_states WHERE account_id=?",
            (account_id,)).fetchone()

    def _resolve_state(self, conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> str:
        """读取时自动判定：cooling 到期 → healthy（同连接内落库，避免锁冲突）。"""
        state = row["state"]
        if state == STATE_COOLING and row["cooling_until"] is not None and now >= row["cooling_until"]:
            conn.execute(
                "UPDATE account_pool_states SET state='healthy', cooling_until=NULL,"
                " updated_at=? WHERE account_id=?",
                (now, row["account_id"]))
            return STATE_HEALTHY
        return state

    # ── 状态查询 ─────────────────────────────────────────────

    def state_of(self, account_id: str, *, now: int | None = None) -> str:
        ts = int(time.time()) if now is None else int(now)
        with self._conn() as conn:
            return self._resolve_state(conn, self._row(conn, account_id), ts)

    def states(self, *, now: int | None = None) -> list[dict]:
        ts = int(time.time()) if now is None else int(now)
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM account_pool_states").fetchall()
            out = []
            for r in rows:
                state = self._resolve_state(conn, r, ts)
                out.append({
                    "account_id": r["account_id"],
                    "state": state,
                    "risk_points": r["risk_points"],
                    "cooling_until": r["cooling_until"],
                    "suspend_reason": r["suspend_reason"],
                })
            return out

    def pick_available(self, candidates: list[str], *, now: int | None = None) -> list[str]:
        """按 healthy 过滤候选账号（租约占用由 EngineManager 另行判断）。"""
        ts = int(time.time()) if now is None else int(now)
        ok: dict[str, str] = {a["account_id"]: a["state"] for a in self.states(now=ts)}
        return [a for a in candidates if ok.get(a, STATE_HEALTHY) == STATE_HEALTHY]

    # ── 事件上报（状态机驱动）───────────────────────────────

    def report_event(self, account_id: str, kind: str, *, detail: str = "",
                     now: int | None = None) -> dict:
        """上报风控/运行事件，返回迁移后的状态。

        kind：captcha / send_uncertain / send_fail_403 / manual_suspend / manual_recover
        """
        ts = int(time.time()) if now is None else int(now)
        with self._lock:
            with self._conn() as conn:
                row = self._row(conn, account_id)
                state = self._resolve_state(conn, row, ts)
                points = int(row["risk_points"])
                new_state, cooling_until, reason = state, None, ""

                if kind == "captcha":
                    new_state = STATE_COOLING
                    cooling_until = ts + int(COOLING_HOURS_CAPTCHA * 3600)
                elif kind == "send_fail_403":
                    new_state = STATE_COOLING
                    cooling_until = ts + int(COOLING_HOURS_403 * 3600)
                elif kind == "send_uncertain":
                    points += 1
                    if state == STATE_RISK_WARNING:
                        new_state = STATE_SUSPENDED
                        reason = "risk_warning 期间再次出现不确定写入"
                    elif points >= RISK_WARNING_THRESHOLD:
                        new_state = STATE_RISK_WARNING
                elif kind == "manual_suspend":
                    new_state = STATE_SUSPENDED
                    reason = detail or "人工暂停"
                elif kind == "manual_recover":
                    # 唯一入口：suspended 只能人工恢复
                    new_state, points = STATE_HEALTHY, 0
                else:
                    raise ValueError(f"未知事件类型: {kind}")

                conn.execute(
                    "INSERT INTO account_risk_events (account_id,kind,detail,happened_at)"
                    " VALUES (?,?,?,?)", (account_id, kind, detail, ts))
                conn.execute(
                    "UPDATE account_pool_states SET state=?, risk_points=?, cooling_until=?,"
                    " suspend_reason=?, updated_at=? WHERE account_id=?",
                    (new_state, points,
                     cooling_until if new_state == STATE_COOLING else None,
                     reason, ts, account_id))
                return {
                    "account_id": account_id,
                    "state": new_state,
                    "risk_points": points,
                    "cooling_until": cooling_until,
                    "suspend_reason": reason,
                }

    def events_of(self, account_id: str, *, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM account_risk_events WHERE account_id=?"
                " ORDER BY id DESC LIMIT ?", (account_id, limit)).fetchall()
            return [dict(r) for r in rows]
