"""D9 配额账本 —— 快照对账法 + 每日限额闸门（§0.3，从阿里山观测继承）。

数据表 account_quotas 已由 execution/store.py 建好（account_id, day, metric,
baseline, snapshot）。本模块在其上封装语义：

- baseline  ：当日开工时记录的平台侧累计值（快照对账的锚点）
- snapshot  ：当前已知累计消耗（本地记账 +1；平台观测值可覆盖校准）
- delta     = max(0, snapshot - baseline) —— 当日已消耗
- remaining = 日限额 - delta，<0 截断为 0

平台观测值覆盖本地记账（record_platform_observation）是对账修正入口：
本地丢计数不会导致超额（观测值只会让 delta 更大）。

限额说明：
- dm 日限额 = §5.3 官方锁定值（100 人/天），不可配置放开；
- like/comment/read 的日限额 §5 未锁定，默认值可在管理面调整（非红线参数）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.execution.params import DM_LIMITS
from app.execution.store import ExecutionStore


@dataclass(frozen=True)
class QuotaPolicy:
    """各指标每日限额。dm 为官方锁定值；其余为默认配置（可调，需评审）。"""
    read_daily_limit: int = 600
    like_daily_limit: int = 150
    comment_daily_limit: int = 50
    dm_daily_limit: int = DM_LIMITS.per_day_people  # 官方口径，写死

    def limit_of(self, metric: str) -> int:
        try:
            return getattr(self, f"{metric}_daily_limit")
        except AttributeError:
            raise ValueError(f"非法配额指标: {metric}") from None


DEFAULT_POLICY = QuotaPolicy()


@dataclass
class QuotaState:
    metric: str
    baseline: int
    snapshot: int
    used: int
    limit: int
    remaining: int


def day_key(now: int | None = None) -> str:
    ts = int(time.time()) if now is None else int(now)
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class QuotaLedger:
    """每账号每指标一本账（三动作分账本，§1.3 约束 2）。"""

    def __init__(self, store: ExecutionStore, policy: QuotaPolicy = DEFAULT_POLICY) -> None:
        self.store = store
        self.policy = policy

    # ── 记账 ────────────────────────────────────────────────
    def set_platform_baseline(self, account_id: int, metric: str,
                              platform_total: int, *, now: int | None = None) -> None:
        """当日开工：以平台侧累计值为基线锚点。"""
        day = day_key(now)
        prev = self.store.quota_delta(account_id, day, metric)  # 保留已记账增量
        self.store.upsert_quota(
            account_id, day, metric,
            baseline=int(platform_total),
            snapshot=int(platform_total) + prev,
            )

    def record_platform_observation(self, account_id: int, metric: str,
                                    platform_total: int, *, now: int | None = None) -> int:
        """平台侧观测到最新累计值 → 覆盖校准 snapshot。返回当日 delta。"""
        day = day_key(now)
        row = self._row(account_id, day, metric)
        baseline = int(row["baseline"]) if row else int(platform_total)
        self.store.upsert_quota(account_id, day, metric, baseline, int(platform_total))
        return max(0, int(platform_total) - baseline)

    def consume(self, account_id: int, metric: str, n: int = 1, *, now: int | None = None) -> None:
        """本地完成一次动作后记账。"""
        day = day_key(now)
        row = self._row(account_id, day, metric)
        baseline = int(row["baseline"]) if row else 0
        snapshot = (int(row["snapshot"]) if row else 0) + int(n)
        self.store.upsert_quota(account_id, day, metric, baseline, snapshot)

    # ── 查询 / 闸门 ─────────────────────────────────────────
    def used(self, account_id: int, metric: str, *, now: int | None = None) -> int:
        return self.store.quota_delta(account_id, day_key(now), metric)

    def remaining(self, account_id: int, metric: str, *, now: int | None = None) -> int:
        return max(0, self.policy.limit_of(metric) - self.used(account_id, metric, now=now))

    def can_do(self, account_id: int, metric: str, n: int = 1, *, now: int | None = None) -> tuple[bool, str]:
        """闸门判定。返回 (是否允许, 原因)。"""
        used = self.used(account_id, metric, now=now)
        limit = self.policy.limit_of(metric)
        if used + n > limit:
            return False, f"quota_exhausted({metric}:{used}/{limit})"
        return True, "allowed"

    def state(self, account_id: int, metric: str, *, now: int | None = None) -> QuotaState:
        day = day_key(now)
        row = self._row(account_id, day, metric)
        baseline = int(row["baseline"]) if row else 0
        snapshot = int(row["snapshot"]) if row else 0
        used = max(0, snapshot - baseline)
        limit = self.policy.limit_of(metric)
        return QuotaState(metric=metric, baseline=baseline, snapshot=snapshot,
                          used=used, limit=limit, remaining=max(0, limit - used))

    def snapshot_all(self, account_id: int, *, now: int | None = None) -> list[QuotaState]:
        return [self.state(account_id, m, now=now)
                for m in ("read", "like", "comment", "dm")]

    # ── 内部 ────────────────────────────────────────────────
    def _row(self, account_id: int, day: str, metric: str):
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT baseline,snapshot FROM account_quotas "
                "WHERE account_id=? AND day=? AND metric=?",
                (account_id, day, metric),
            ).fetchone()
