"""
合规引擎路由 · 安全模式 / 一键暂停恢复
==========================================
POST /api/compliance/safe-mode/enter   进入安全模式（全量暂停）
POST /api/compliance/safe-mode/exit    退出安全模式（人工确认）
POST /api/compliance/pause              一键全量暂停
POST /api/compliance/resume             恢复运行
"""
from fastapi import APIRouter, Depends

from app.core.dependencies import get_compliance_service
from app.schemas.compliance import SafeModeEnterRequest, SafeModeExitRequest
from app.services.compliance_service import ComplianceService

router = APIRouter(tags=["compliance"])


@router.post("/compliance/safe-mode/enter")
def enter_safe_mode(
    payload: SafeModeEnterRequest,
    service: ComplianceService = Depends(get_compliance_service),
) -> dict:
    """进入安全模式（全量暂停所有任务），写审计日志"""
    return service.enter_safe_mode(source=payload.source, reason=payload.reason)


@router.post("/compliance/safe-mode/exit")
def exit_safe_mode(
    payload: SafeModeExitRequest | None = None,
    service: ComplianceService = Depends(get_compliance_service),
) -> dict:
    """退出安全模式（必须人工确认），写审计日志"""
    note = payload.note if payload else ""
    return service.exit_safe_mode(note=note)


@router.post("/compliance/pause")
def pause_all(
    service: ComplianceService = Depends(get_compliance_service),
) -> dict:
    """一键全量暂停所有发送和采集任务，写审计日志"""
    return service.pause_all()


@router.post("/compliance/resume")
def resume_all(
    service: ComplianceService = Depends(get_compliance_service),
) -> dict:
    """恢复所有任务运行（安全模式激活时拒绝），写审计日志"""
    return service.resume_all()
