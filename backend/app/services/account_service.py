"""抖音账号 Service · v1.0 契约对齐

健康分五维因子计算 + 限流状态机 + 日频配置。
依赖 Repository 接口（不直接依赖 MemoryRepository）。
"""
from datetime import datetime, timezone

from app.models.domain import Account
from app.repositories.base import Repository
from app.schemas.account import AccountCreate, AccountRead, AccountUpdate

# 五维因子键
_FACTOR_KEYS = ("dailyFreq", "banHistory", "login", "unsubscribe", "complaint")

# 因子扣分权重
_FACTOR_SCORE = {"good": 0, "warn": -10, "bad": -25}

# 限流状态机：不可自动降级的状态（需人工恢复）
_LOCKED_STATUSES = frozenset({"throttled40", "banned"})


class AccountService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._notification_service = None  # P4-A 注入

    def set_notification_service(self, svc) -> None:
        """注入通知服务（P4-A），用于账号预警自动提醒"""
        self._notification_service = svc

    # ═══════════════════════════════════════════════════════
    # CRUD
    # ═══════════════════════════════════════════════════════

    def list_accounts(self) -> list[Account]:
        return self._repo.list_accounts()

    def get_account(self, account_id: str) -> Account:
        return self._repo.get_account(account_id)

    def create_account(self, payload: AccountCreate) -> Account:
        account = Account(
            nickname=payload.nickname,
            name=payload.nickname,  # v0.1 兼容字段
            platform=payload.platform,
            daily_limit=payload.daily_limit,
            notes=payload.notes,
            health_score=100,  # 新账号满分
            factors={k: "good" for k in _FACTOR_KEYS},
            limit_status="healthy",
        )
        return self._repo.save_account(account)

    def update_account(self, account_id: str, payload: AccountUpdate) -> Account:
        """更新账号。

        - factors 更新 → 自动重算 health_score → 自动推导 limit_status（非锁定状态）
        - limit_status 显式传入 → 手动切换状态机
        - R1 触发（daily_outreach >= daily_limit 且原 limit=80）→ throttled40 + daily_limit=40
        """
        account = self._repo.get_account(account_id)
        old_limit_status = account.limit_status  # P4-A 预警对比基线
        old_health = account.health_score

        # ── 健康分因子更新（部分更新） ──
        if payload.factors is not None:
            for key, value in payload.factors.items():
                if key in _FACTOR_KEYS and value in ("good", "warn", "bad"):
                    account.factors[key] = value  # type: ignore[assignment]
            # 自动重算健康分
            account.health_score = self._calc_health_score(account.factors)
            # 非锁定状态下自动推导限流状态
            if account.limit_status not in _LOCKED_STATUSES:
                account.limit_status = self._derive_limit_status(account.health_score)

        # ── 健康分手动覆盖 ──
        if payload.health_score is not None:
            account.health_score = max(0, min(100, payload.health_score))
            if account.limit_status not in _LOCKED_STATUSES:
                account.limit_status = self._derive_limit_status(account.health_score)

        # ── 限流状态手动切换 ──
        if payload.limit_status is not None:
            account.limit_status = payload.limit_status  # type: ignore[assignment]
            # 进入 throttled40 时自动降日频
            if payload.limit_status == "throttled40" and account.daily_limit == 80:
                account.daily_limit = 40
                account.r1_note = "R1 命中：日频达上限，降速至 40/天"
            # 恢复 healthy 时重置日频
            if payload.limit_status == "healthy" and account.daily_limit == 40:
                account.daily_limit = 80
                account.r1_note = None

        # ── 日频配置 ──
        if payload.daily_limit is not None:
            account.daily_limit = payload.daily_limit
        if payload.daily_outreach is not None:
            account.daily_outreach = payload.daily_outreach
            # R1 自动触发：日频达上限且当前状态为 healthy/warn
            if (
                account.daily_outreach >= account.daily_limit
                and account.limit_status in ("healthy", "warn")
                and account.daily_limit == 80
            ):
                account.limit_status = "throttled40"  # type: ignore[assignment]
                account.daily_limit = 40
                account.r1_note = "R1 自动触发：日频达 80，降速至 40/天"

        # ── 备注与风控 ──
        if payload.notes is not None:
            account.notes = payload.notes
        if payload.last_ban_reason is not None:
            account.last_ban_reason = payload.last_ban_reason
        if payload.r1_note is not None:
            account.r1_note = payload.r1_note
        if payload.r3_note is not None:
            account.r3_note = payload.r3_note

        account.updated_at = datetime.now(timezone.utc)

        # P4-A：账号预警通知（限流状态变差 或 健康分跌破 60）
        if self._notification_service:
            worsened = account.limit_status != old_limit_status and account.limit_status not in ("healthy",)
            health_dropped = account.health_score < 60 and old_health >= 60
            if worsened:
                self._notification_service.create(
                    type="account_warning",
                    title=f"账号限流预警 · {account.nickname or account.name}",
                    content=f"限流状态 {old_limit_status} → {account.limit_status}，请及时检查账号。",
                    level="error" if account.limit_status in ("safe_mode", "banned", "throttled40") else "warning",
                    related_type="account",
                    related_id=account.id,
                )
            elif health_dropped:
                self._notification_service.create(
                    type="account_warning",
                    title=f"账号健康分偏低 · {account.nickname or account.name}",
                    content=f"健康分降至 {account.health_score}（<60），关注风控与日频。",
                    level="warning",
                    related_type="account",
                    related_id=account.id,
                )

        return self._repo.save_account(account)

    # ═══════════════════════════════════════════════════════
    # 健康分 & 状态机
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _calc_health_score(factors: dict[str, str]) -> int:
        """五维因子计算健康分：满分 100，warn -10，bad -25，clamp 0-100"""
        score = 100
        for key in _FACTOR_KEYS:
            score += _FACTOR_SCORE.get(factors.get(key, "good"), 0)
        return max(0, min(100, score))

    @staticmethod
    def _derive_limit_status(health_score: int) -> str:
        """根据健康分推导限流状态（throttled40/banned 需单独触发，不自动推导）"""
        if health_score < 40:
            return "safe_mode"
        if health_score < 60:
            return "warn"
        return "healthy"
