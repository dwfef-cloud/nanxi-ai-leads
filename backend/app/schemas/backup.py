"""
数据备份与恢复 · Pydantic Schema
==================================
备份信息、列表响应、恢复请求、自动备份设置、导出响应模型。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════
# 备份信息
# ═══════════════════════════════════════════════════════════

class BackupInfo(BaseModel):
    """单个备份文件信息"""
    filename: str = Field(description="备份文件名")
    size: int = Field(default=0, description="文件大小（字节）")
    size_display: str = Field(default="", description="人类可读大小")
    created_at: str = Field(default="", description="创建时间（ISO 8601）")
    type: str = Field(default="manual", description="备份类型：manual / auto / pre_restore")


class BackupListResponse(BaseModel):
    """备份列表响应"""
    backups: list[BackupInfo] = Field(default_factory=list)
    total: int = Field(default=0, description="备份总数")


# ═══════════════════════════════════════════════════════════
# 恢复
# ═══════════════════════════════════════════════════════════

class RestoreResponse(BaseModel):
    """恢复操作响应"""
    success: bool
    message: str = Field(default="")
    restored_from: str = Field(default="", description="从哪个备份恢复")
    pre_restore_backup: str | None = Field(default=None, description="恢复前临时备份文件名")


# ═══════════════════════════════════════════════════════════
# 删除
# ═══════════════════════════════════════════════════════════

class DeleteResponse(BaseModel):
    """删除备份响应"""
    success: bool
    message: str = Field(default="")
    filename: str = Field(default="")


# ═══════════════════════════════════════════════════════════
# 自动备份设置
# ═══════════════════════════════════════════════════════════

class BackupSettings(BaseModel):
    """自动备份设置"""
    enabled: bool = Field(default=False, description="是否启用自动备份")
    retain_count: int = Field(default=7, ge=1, le=30, description="保留最近份数（1-30）")
    last_auto_backup_at: str | None = Field(default=None, description="上次自动备份时间")


class BackupSettingsUpdate(BaseModel):
    """自动备份设置更新"""
    enabled: bool = Field(description="是否启用自动备份")
    retain_count: int = Field(default=7, ge=1, le=30, description="保留最近份数（1-30）")


# ═══════════════════════════════════════════════════════════
# 导出
# ═══════════════════════════════════════════════════════════

class ExportResponse(BaseModel):
    """全量导出响应"""
    success: bool
    message: str = Field(default="")
    file_path: str = Field(default="", description="导出文件绝对路径")
    file_url: str = Field(default="", description="导出文件下载 URL（/exports/...）")
    filename: str = Field(default="", description="导出文件名")
