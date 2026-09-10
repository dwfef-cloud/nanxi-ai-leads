"""
ComplianceService · 合规引擎业务逻辑
=======================================
安全模式进入/退出 / 一键暂停恢复 / 审计日志写入

所有高风险操作必须写 ComplianceEvent 审计日志。
依赖 Repository 接口。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.domain import ComplianceEvent, RuntimeState
from app.repositories.base import Repository

_CST = timezone(timedelta(hours=8))


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _fmt_display_time(dt: datetime) -> str:
    return dt.astimezone(_CST).strftime("%m-%d %H:%M")


class ComplianceService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    # ═══════════════════════════════════════════════════════
    # 审计日志辅助
    # ═══════════════════════════════════════════════════════

    def _write_audit(self, event_type: str, level: str, text: str) -> ComplianceEvent:
        event = ComplianceEvent(
            type=event_type,
            level=level,
            text=text,
            time=_fmt_display_time(_now_cst()),
        )
        return self._repo.save_compliance_event(event)

    # ═══════════════════════════════════════════════════════
    # 安全模式
    # ═══════════════════════════════════════════════════════

    def enter_safe_mode(self, source: str = "manual", reason: str = "") -> dict:
        """进入安全模式（全量暂停）

        - 设置 RuntimeState.safe_mode_active = true
        - 同时停止任务运行 task_running = false
        - 写 ComplianceEvent（type=safe_mode, level=danger）
        """
        state = self._repo.get_runtime()
        if state.safe_mode_active:
            return {"ok": True, "message": "安全模式已处于激活状态", "alreadyActive": True}

        now_str = _fmt_display_time(_now_cst())
        state.safe_mode_active = True
        state.safe_mode_reason = reason or f"触发源：{source}"
        state.safe_mode_triggered_at = now_str
        state.safe_mode_trigger_source = source
        state.task_running = False  # 安全模式下全量暂停
        state.updated_at = datetime.now(timezone.utc)
        self._repo.save_runtime(state)

        source_label = {"manual": "人工", "r3": "R3规则", "health_score": "健康分"}.get(source, source)
        self._write_audit(
            "safe_mode",
            "danger",
            f"进入安全模式（{source_label}）：{reason or '全量暂停所有任务'}",
        )
        return {"ok": True, "message": "已进入安全模式，所有任务已暂停", "safeMode": True}

    def exit_safe_mode(self, note: str = "") -> dict:
        """退出安全模式（必须人工确认）

        - 关闭 safe_mode_active
        - 恢复 task_running = true
        - 写审计日志
        """
        state = self._repo.get_runtime()
        if not state.safe_mode_active:
            return {"ok": True, "message": "当前未处于安全模式", "alreadyExited": True}

        state.safe_mode_active = False
        state.safe_mode_reason = ""
        state.safe_mode_triggered_at = None
        state.safe_mode_trigger_source = None
        state.task_running = True
        state.updated_at = datetime.now(timezone.utc)
        self._repo.save_runtime(state)

        self._write_audit(
            "safe_mode",
            "ok",
            f"人工确认退出安全模式，恢复任务运行{('：' + note) if note else ''}",
        )
        return {"ok": True, "message": "已退出安全模式，任务恢复运行", "safeMode": False}

    # ═══════════════════════════════════════════════════════
    # 一键暂停 / 恢复
    # ═══════════════════════════════════════════════════════

    def pause_all(self) -> dict:
        """一键全量暂停

        - 设置 RuntimeState.task_running = false
        - 写审计日志（type=pause, level=warn）
        """
        state = self._repo.get_runtime()
        if not state.task_running:
            return {"ok": True, "message": "任务已处于暂停状态", "alreadyPaused": True}

        state.task_running = False
        state.updated_at = datetime.now(timezone.utc)
        self._repo.save_runtime(state)

        self._write_audit("pause", "warn", "一键全量暂停：所有发送和采集任务已停止")
        return {"ok": True, "message": "已全量暂停所有任务", "taskRunning": False}

    def resume_all(self) -> dict:
        """恢复运行

        - 设置 RuntimeState.task_running = true
        - 安全模式激活时拒绝恢复
        - 写审计日志（type=resume, level=info）
        """
        state = self._repo.get_runtime()
        if state.safe_mode_active:
            return {"ok": False, "message": "安全模式激活中，请先退出安全模式再恢复任务", "safeMode": True}
        if state.task_running:
            return {"ok": True, "message": "任务已在运行中", "alreadyRunning": True}

        state.task_running = True
        state.updated_at = datetime.now(timezone.utc)
        self._repo.save_runtime(state)

        self._write_audit("resume", "info", "恢复运行：所有发送和采集任务已恢复")
        return {"ok": True, "message": "已恢复所有任务运行", "taskRunning": True}
