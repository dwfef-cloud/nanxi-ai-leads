"""
数据备份与恢复 API 路由
=========================
POST   /api/backup/create          手动创建备份
GET    /api/backup/list            备份列表
POST   /api/backup/{filename}/restore  从备份恢复
DELETE /api/backup/{filename}       删除备份
GET    /api/backup/settings         自动备份设置
PUT    /api/backup/settings         更新自动备份设置
POST   /api/backup/export-json      全量导出 JSON
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_backup_service
from app.schemas.backup import (
    BackupInfo,
    BackupListResponse,
    BackupSettings,
    BackupSettingsUpdate,
    DeleteResponse,
    ExportResponse,
    RestoreResponse,
)
from app.services.backup_service import BackupService

router = APIRouter(prefix="/backup", tags=["backup"])


# ═══════════════════════════════════════════════════════════
# 创建备份
# ═══════════════════════════════════════════════════════════

@router.post("/create", response_model=BackupInfo)
def create_backup(service: BackupService = Depends(get_backup_service)) -> BackupInfo:
    """手动创建数据库备份"""
    try:
        result = service.create_backup(backup_type="manual")
        return BackupInfo(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建备份失败: {e}")


# ═══════════════════════════════════════════════════════════
# 备份列表
# ═══════════════════════════════════════════════════════════

@router.get("/list", response_model=BackupListResponse)
def list_backups(service: BackupService = Depends(get_backup_service)) -> BackupListResponse:
    """获取所有备份列表（按时间倒序）"""
    backups = service.list_backups()
    return BackupListResponse(
        backups=[BackupInfo(**b) for b in backups],
        total=len(backups),
    )


# ═══════════════════════════════════════════════════════════
# 自动备份设置
# ═══════════════════════════════════════════════════════════

@router.get("/settings", response_model=BackupSettings)
def get_backup_settings(service: BackupService = Depends(get_backup_service)) -> BackupSettings:
    """获取自动备份设置"""
    settings = service.get_settings()
    return BackupSettings(**settings)


@router.put("/settings", response_model=BackupSettings)
def update_backup_settings(
    payload: BackupSettingsUpdate,
    service: BackupService = Depends(get_backup_service),
) -> BackupSettings:
    """更新自动备份设置"""
    result = service.update_settings(enabled=payload.enabled, retain_count=payload.retain_count)
    return BackupSettings(**result)


# ═══════════════════════════════════════════════════════════
# 全量导出 JSON
# ═══════════════════════════════════════════════════════════

@router.post("/export-json", response_model=ExportResponse)
def export_all_json(service: BackupService = Depends(get_backup_service)) -> ExportResponse:
    """全量数据导出为 JSON 文件"""
    try:
        file_path = service.export_all_json()
        p = Path(file_path)
        return ExportResponse(
            success=True,
            message=f"导出成功: {p.name}",
            file_path=str(file_path),
            file_url=f"/exports/{p.name}",
            filename=p.name,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出失败: {e}")


# ═══════════════════════════════════════════════════════════
# 恢复备份（路径参数，放在 /settings 之后避免冲突）
# ═══════════════════════════════════════════════════════════

@router.post("/{filename}/restore", response_model=RestoreResponse)
def restore_backup(
    filename: str,
    service: BackupService = Depends(get_backup_service),
) -> RestoreResponse:
    """从指定备份恢复数据库（恢复前自动创建临时备份）"""
    try:
        result = service.restore_backup(filename)
        return RestoreResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"恢复失败: {e}")


# ═══════════════════════════════════════════════════════════
# 删除备份
# ═══════════════════════════════════════════════════════════

@router.delete("/{filename}", response_model=DeleteResponse)
def delete_backup(
    filename: str,
    service: BackupService = Depends(get_backup_service),
) -> DeleteResponse:
    """删除指定备份文件"""
    try:
        result = service.delete_backup(filename)
        return DeleteResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")
