"""多账号调度 Service · 策略选择 / 健康分护栏 / 发送统计

核心逻辑：
1. 从 Repository 获取所有账号
2. 按策略（round_robin / weighted / health_based）选择账号
3. 健康分护栏：< 30 不分配，< 60 降速 50%
4. 日频护栏：today_sent >= daily_limit 不分配
5. 记录发送并统计
"""
from __future__ import annotations

import random
import threading
from datetime import datetime, timezone

from app.models.domain import Account
from app.repositories.base import Repository
from app.schemas.scheduler import (
    AccountScheduleStatus,
    AssignResponse,
    PerAccountStat,
    SchedulerConfig,
    SchedulerStats,
)

# 不可分配的限流状态
_BLOCKED_STATUSES = frozenset({"throttled40", "safe_mode", "banned"})

# 调度状态映射（limit_status → schedule status）
_STATUS_MAP = {
    "healthy": "active",
    "warn": "active",
    "throttled40": "throttled",
    "safe_mode": "safe_mode",
    "banned": "banned",
}


class SchedulerService:
    """多账号调度器"""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._rr_counter = 0  # round_robin 内存计数器
        self._lock = threading.Lock()

    # ═══════════════════════════════════════════════════════
    # 配置读写
    # ═══════════════════════════════════════════════════════

    def get_config(self) -> SchedulerConfig:
        """读取调度配置"""
        data = self._repo.get_scheduler_config()
        return SchedulerConfig(
            strategy=data.get("strategy", "round_robin"),
            health_threshold_warn=data.get("health_threshold_warn", 60),
            health_threshold_critical=data.get("health_threshold_critical", 30),
            max_concurrent=data.get("max_concurrent", 3),
        )

    def update_config(self, data: dict) -> SchedulerConfig:
        """更新调度配置（部分字段）"""
        # 过滤掉 None 值
        updates = {k: v for k, v in data.items() if v is not None}
        self._repo.save_scheduler_config(updates)
        return self.get_config()

    # ═══════════════════════════════════════════════════════
    # 账号调度状态
    # ═══════════════════════════════════════════════════════

    def get_accounts_schedule_status(self) -> list[AccountScheduleStatus]:
        """查询所有账号的调度状态"""
        accounts = self._repo.list_accounts()
        config = self.get_config()
        result = []
        for acc in accounts:
            remaining = max(0, acc.daily_limit - acc.today_sent)
            status = self._derive_schedule_status(acc, config)
            result.append(AccountScheduleStatus(
                account_id=acc.id,
                account_name=acc.nickname or acc.name,
                health_score=acc.health_score,
                today_sent=acc.today_sent,
                remaining_quota=remaining,
                status=status,
                weight=acc.weight,
            ))
        return result

    # ═══════════════════════════════════════════════════════
    # 核心：分配账号
    # ═══════════════════════════════════════════════════════

    def assign_account(self, task_type: str = "dm", priority: int = 0) -> AssignResponse:
        """为发送任务分配一个可用账号

        流程：
        1. 获取所有账号，过滤不可用状态
        2. 健康分 < critical 不分配
        3. 超过 daily_limit 不分配
        4. 按策略选择
        5. 健康分 < warn 的账号降速 50%
        """
        config = self.get_config()
        accounts = self._repo.list_accounts()

        # 1. 过滤不可用账号
        candidates = [
            acc for acc in accounts
            if acc.limit_status not in _BLOCKED_STATUSES
            and acc.health_score >= config.health_threshold_critical
            and acc.today_sent < acc.daily_limit
        ]

        if not candidates:
            # 尝试返回一个原因明确的响应（无可用账号）
            return AssignResponse(
                account_id="",
                account_name="",
                strategy_used=config.strategy,
                reason="无可用账号：所有账号均被限流/安全模式/封禁/健康分过低/已达日频上限",
            )

        # 2. 健康分降速：< warn 阈值的账号选中概率减半
        #    weighted/health_based 策略通过复制高健康分账号实现概率加权
        weighted_pool = []
        for acc in candidates:
            if acc.health_score >= config.health_threshold_warn:
                weighted_pool.append(acc)
                weighted_pool.append(acc)  # 健康账号双倍权重
            else:
                weighted_pool.append(acc)  # 低健康账号单倍权重（=降速50%）

        # 3. 按策略选择
        strategy = config.strategy
        if strategy == "weighted":
            chosen = self._select_weighted(weighted_pool)
            reason = f"按权重随机选择（weight={chosen.weight}）"
        elif strategy == "health_based":
            chosen = self._select_health_based(weighted_pool)
            reason = f"按健康分降序选择（health_score={chosen.health_score}）"
        else:  # round_robin（默认）
            # round_robin 使用原始候选列表，低健康分账号有50%概率被跳过
            chosen = self._select_round_robin(candidates, config.health_threshold_warn)
            reason = "轮询选择（round_robin 计数器）"

        # 4. 如果选中的是低健康分账号，附加降速说明
        if chosen.health_score < config.health_threshold_warn:
            reason += f"；健康分 {chosen.health_score} < {config.health_threshold_warn}，已降速50%"

        return AssignResponse(
            account_id=chosen.id,
            account_name=chosen.nickname or chosen.name,
            strategy_used=strategy,
            reason=reason,
        )

    # ═══════════════════════════════════════════════════════
    # 发送记录 & 统计
    # ═══════════════════════════════════════════════════════

    def record_send(self, account_id: str, success: bool) -> None:
        """记录一次发送结果"""
        self._repo.increment_account_send(account_id, success)

    def get_stats(self) -> SchedulerStats:
        """调度统计：总发送量、成功率、活跃账号数、各账号明细"""
        accounts = self._repo.list_accounts()
        config = self.get_config()

        total_sent = sum(acc.today_sent for acc in accounts)
        total_success = sum(acc.today_success for acc in accounts)
        success_rate = round(total_success / total_sent * 100, 1) if total_sent > 0 else 0.0

        active_count = sum(
            1 for acc in accounts
            if acc.limit_status not in _BLOCKED_STATUSES
            and acc.health_score >= config.health_threshold_critical
            and acc.today_sent < acc.daily_limit
        )

        per_account = []
        for acc in accounts:
            acc_rate = round(acc.today_success / acc.today_sent * 100, 1) if acc.today_sent > 0 else 0.0
            per_account.append(PerAccountStat(
                account_id=acc.id,
                account_name=acc.nickname or acc.name,
                sent=acc.today_sent,
                success_rate=acc_rate,
                health_score=acc.health_score,
            ))

        return SchedulerStats(
            total_sent=total_sent,
            success_rate=success_rate,
            active_accounts=active_count,
            per_account=per_account,
        )

    # ═══════════════════════════════════════════════════════
    # 内部：策略选择
    # ═══════════════════════════════════════════════════════

    def _select_round_robin(self, pool: list[Account], warn_threshold: int = 60) -> Account:
        """轮询选择：用内存计数器在候选池中轮转
        健康分 < warn_threshold 的账号有 50% 概率被跳过（降速）
        """
        with self._lock:
            n = len(pool)
            for _ in range(n):
                idx = self._rr_counter % n
                self._rr_counter += 1
                candidate = pool[idx]
                # 低健康分账号：50% 概率跳过
                if candidate.health_score < warn_threshold and random.random() < 0.5:
                    continue
                return candidate
            # 全部被跳过（极端情况），返回计数器指向的账号
            return pool[self._rr_counter % n]

    @staticmethod
    def _select_weighted(pool: list[Account]) -> Account:
        """按账号 weight 权重随机选择"""
        weights = [max(1, acc.weight) for acc in pool]
        return random.choices(pool, weights=weights, k=1)[0]

    @staticmethod
    def _select_health_based(pool: list[Account]) -> Account:
        """按健康分降序选择最高分账号"""
        return max(pool, key=lambda acc: acc.health_score)

    # ═══════════════════════════════════════════════════════
    # 内部：状态推导
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _derive_schedule_status(acc: Account, config: SchedulerConfig) -> str:
        """根据账号状态和健康分推导调度状态"""
        if acc.limit_status == "banned":
            return "banned"
        if acc.limit_status == "safe_mode":
            return "safe_mode"
        if acc.limit_status == "throttled40":
            return "throttled"
        if acc.health_score < config.health_threshold_critical:
            return "paused"
        if acc.today_sent >= acc.daily_limit:
            return "throttled"
        if acc.status == "paused":
            return "paused"
        return "active"
