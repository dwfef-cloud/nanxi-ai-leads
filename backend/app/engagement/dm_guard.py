"""D17 私信频控闸门 + 双通道路由（M5 私信模块的安全层）。

规格（DESIGN-DOC §M5 / §5.3，官方值**写死，不可配置放开**）：
- 非互关主动私信：3 条/人；
- 1 小时 40 人、1 天 100 人（按收件人去重）；
- 48h 会话窗口内最多 6 条（互关会话）；
- 双通道：主路径 = 企业号开放 API（默认开）；兜底 = 协议直连
  （默认关，需显式配置 + TEST-CHECKLIST A 组验证通过标记）；
- 闸门独立于编排器（双保险）：任何发送方都必须先过 guard.check。

状态码语义与写入安全三态对齐：check 拒绝 → 不发送、不落 pending。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# ── §5.3 官方频控（写死）─────────────────────────────────────
PER_RECIPIENT_LIMIT = 3          # 非互关：条/人
SESSION_WINDOW_H = 48            # 会话窗口
SESSION_WINDOW_LIMIT = 6         # 窗口内条数（互关会话）
HOURLY_RECIPIENTS = 40           # 1 小时 40 人
DAILY_RECIPIENTS = 100           # 1 天 100 人


class GuardBlocked(RuntimeError):
    """频控拒绝（reason 说明命中哪条限制）。"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None


class DMFrequencyGuard:
    """独立频控闸门。线程安全；DB 记录全部发送（幂等审计）。

    用法（编排器/worker 内）：
        d = guard.check(acct, recipient, mutual=False)
        if not d.allowed: return  # 不发送
        ... 发送 ...
        guard.record(acct, recipient, mutual=False)  # 发送成功后记账
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS dm_send_log (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       account_id  TEXT NOT NULL,
                       recipient   TEXT NOT NULL,
                       mutual      INTEGER NOT NULL DEFAULT 0,
                       channel     TEXT NOT NULL DEFAULT 'official',
                       sent_at     INTEGER NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_dm_acct_rcpt "
                      "ON dm_send_log(account_id, recipient, sent_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_dm_acct_time "
                      "ON dm_send_log(account_id, sent_at)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ── 查询 ─────────────────────────────────────────────────
    def check(self, account_id: str, recipient: str, *, mutual: bool,
              now: int | None = None) -> Decision:
        ts = int(time.time()) if now is None else int(now)
        day_start = ts - (ts % 86400)
        with self._lock, self._conn() as c:
            # 1) 非互关：3 条/人
            if not mutual:
                n = c.execute(
                    "SELECT COUNT(*) FROM dm_send_log WHERE account_id=? "
                    "AND recipient=? AND mutual=0",
                    (account_id, recipient)).fetchone()[0]
                if n >= PER_RECIPIENT_LIMIT:
                    return Decision(False, f"非互关私信已达 {PER_RECIPIENT_LIMIT} 条/人")
            # 2) 48h 会话窗口 6 条（互关会话按窗口滚动计数）
            n = c.execute(
                "SELECT COUNT(*) FROM dm_send_log WHERE account_id=? "
                "AND recipient=? AND sent_at>?",
                (account_id, recipient, ts - SESSION_WINDOW_H * 3600)).fetchone()[0]
            if n >= SESSION_WINDOW_LIMIT:
                return Decision(False, f"48h 会话窗口已达 {SESSION_WINDOW_LIMIT} 条")
            # 3) 1 小时 40 人（收件人去重）
            n = c.execute(
                "SELECT COUNT(DISTINCT recipient) FROM dm_send_log "
                "WHERE account_id=? AND sent_at>?",
                (account_id, ts - 3600)).fetchone()[0]
            if n >= HOURLY_RECIPIENTS:
                return Decision(False, f"1 小时收件人已达 {HOURLY_RECIPIENTS} 人上限")
            # 4) 1 天 100 人（收件人去重，自然日）
            n = c.execute(
                "SELECT COUNT(DISTINCT recipient) FROM dm_send_log "
                "WHERE account_id=? AND sent_at>=?",
                (account_id, day_start)).fetchone()[0]
            if n >= DAILY_RECIPIENTS:
                return Decision(False, f"今日收件人已达 {DAILY_RECIPIENTS} 人上限")
        return Decision(True)

    def record(self, account_id: str, recipient: str, *, mutual: bool,
               channel: str = "official", now: int | None = None) -> None:
        ts = int(time.time()) if now is None else int(now)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO dm_send_log(account_id,recipient,mutual,channel,sent_at) "
                "VALUES(?,?,?,?,?)",
                (account_id, recipient, int(mutual), channel, ts))

    def usage(self, account_id: str, *, now: int | None = None) -> dict:
        ts = int(time.time()) if now is None else int(now)
        day_start = ts - (ts % 86400)
        with self._lock, self._conn() as c:
            return {
                "hour_recipients": c.execute(
                    "SELECT COUNT(DISTINCT recipient) FROM dm_send_log "
                    "WHERE account_id=? AND sent_at>?",
                    (account_id, ts - 3600)).fetchone()[0],
                "day_recipients": c.execute(
                    "SELECT COUNT(DISTINCT recipient) FROM dm_send_log "
                    "WHERE account_id=? AND sent_at>=?",
                    (account_id, day_start)).fetchone()[0],
                "limits": {"hour": HOURLY_RECIPIENTS, "day": DAILY_RECIPIENTS,
                           "per_recipient_non_mutual": PER_RECIPIENT_LIMIT,
                           "session_48h": SESSION_WINDOW_LIMIT},
            }


# ── 双通道路由 ───────────────────────────────────────────────
@dataclass
class DMChannelConfig:
    """兜底通道开关（默认关；开启需 TEST-CHECKLIST A 组通过标记）。"""
    fallback_enabled: bool = False
    a_group_verified: bool = False   # A2/A3 验证通过标记


class DMChannelRouter:
    """主路径（官方 API）→ 兜底（协议直连，默认关）。

    transport 注入：main_send / fallback_send 为 callable
    (account_id, recipient, text) -> bool（True=发送成功）。
    主路径失败时：兜底未启用 → 抛出（调用方按 sent_fail 处理）；
    兜底启用 → 切换并标记 channel='fallback'。
    """

    def __init__(self, guard: DMFrequencyGuard, cfg: DMChannelConfig | None = None,
                 *, main_send=None, fallback_send=None,
                 compliance_check=None) -> None:
        self.guard = guard
        self.cfg = cfg or DMChannelConfig()
        self.main_send = main_send
        self.fallback_send = fallback_send
        self.compliance_check = compliance_check  # (text) -> (ok, reason) 可注入南溪 compliance_service

    def send(self, account_id: str, recipient: str, text: str, *,
             mutual: bool, now: int | None = None) -> tuple[bool, str]:
        if self.compliance_check:
            ok, reason = self.compliance_check(text)
            if not ok:
                return False, f"compliance:{reason}"
        d = self.guard.check(account_id, recipient, mutual=mutual, now=now)
        if not d.allowed:
            raise GuardBlocked(d.reason or "frequency-guard")
        # 兜底启用前置校验（双保险：配置 + 验证标记都要有）
        if self.cfg.fallback_enabled and not self.cfg.a_group_verified:
            raise GuardBlocked("兜底通道配置开启但 TEST-CHECKLIST A 组未验证通过")
        if self.main_send and self.main_send(account_id, recipient, text):
            self.guard.record(account_id, recipient, mutual=mutual,
                              channel="official", now=now)
            return True, "official"
        if self.cfg.fallback_enabled and self.fallback_send \
                and self.fallback_send(account_id, recipient, text):
            self.guard.record(account_id, recipient, mutual=mutual,
                              channel="fallback", now=now)
            return True, "fallback"
        return False, "send-failed"
