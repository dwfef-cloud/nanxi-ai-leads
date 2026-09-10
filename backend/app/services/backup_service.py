"""
BackupService · 数据备份与恢复服务
====================================
SQLite 数据库文件级备份、恢复、自动备份调度与全量 JSON 导出。

存储约定：
  - 备份目录：D:\\.backups\\（文件名 backup_{type}_{YYYYMMDD_HHMMSS}.db）
  - 导出目录：D:\\.exports\\（全量 JSON）
  - 自动备份配置：system_settings 表，category="backup"
  - 恢复前自动创建 _pre_restore_{timestamp}.db 临时备份，失败可回滚
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.repositories.base import Repository

_CST = timezone(timedelta(hours=8))

# 安全校验：文件名只允许字母数字下划线点横线
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")

# system_settings 分类
CAT_BACKUP = "backup"


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _now_iso() -> str:
    return _now_cst().isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _fmt_size(size_bytes: int) -> str:
    """字节数 → 人类可读"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


class BackupService:
    """数据备份与恢复服务"""

    def __init__(
        self,
        repo: Repository,
        db_path: str = r"D:\.data\lead_system.db",
        backup_dir: str = r"D:\.backups",
        export_dir: str = r"D:\.exports",
    ) -> None:
        self._repo = repo
        self._db_path = Path(db_path)
        self._backup_dir = Path(backup_dir)
        self._export_dir = Path(export_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._export_dir.mkdir(parents=True, exist_ok=True)

    # ═══════════════════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _validate_filename(filename: str) -> None:
        """校验备份文件名安全：禁止路径穿越"""
        if not filename or not _SAFE_NAME_RE.match(filename):
            raise ValueError(f"非法文件名: {filename!r}（只允许字母数字下划线点横线）")
        if ".." in filename:
            raise ValueError(f"非法文件名: {filename!r}（禁止路径穿越）")

    def _resolve_backup_path(self, filename: str) -> Path:
        """解析备份文件完整路径，并确保在备份目录内"""
        self._validate_filename(filename)
        full = (self._backup_dir / filename).resolve()
        # 确保解析后的路径仍在备份目录内
        if not str(full).startswith(str(self._backup_dir.resolve())):
            raise ValueError(f"路径越界: {filename!r}")
        return full

    @staticmethod
    def _parse_type_from_filename(filename: str) -> str:
        """从文件名解析备份类型"""
        # backup_manual_20260910_120000.db → manual
        # backup_auto_20260910_120000.db → auto
        # _pre_restore_20260910_120000.db → pre_restore
        parts = filename.split("_")
        if len(parts) >= 3 and parts[0] == "backup":
            return parts[1]
        if parts[0] == "_pre":
            return "pre_restore"
        return "unknown"

    # ═══════════════════════════════════════════════════════
    # 创建备份
    # ═══════════════════════════════════════════════════════

    def create_backup(self, backup_type: str = "manual") -> dict:
        """复制 SQLite 文件到备份目录"""
        timestamp = _now_cst().strftime("%Y%m%d_%H%M%S")
        filename = f"backup_{backup_type}_{timestamp}.db"
        dest = self._backup_dir / filename

        # 确保源数据库存在
        if not self._db_path.exists():
            raise FileNotFoundError(f"数据库文件不存在: {self._db_path}")

        # 使用 shutil.copy2 保留元数据
        shutil.copy2(str(self._db_path), str(dest))
        # WAL 模式下也复制 -wal 和 -shm 文件（如果存在）
        for suffix in ("-wal", "-shm"):
            sidecar = self._db_path.with_suffix(self._db_path.suffix + suffix)
            if sidecar.exists():
                shutil.copy2(str(sidecar), str(dest) + suffix)

        size = dest.stat().st_size
        result = {
            "filename": filename,
            "size": size,
            "size_display": _fmt_size(size),
            "created_at": _now_iso(),
            "type": backup_type,
        }
        print(f"[backup] 备份创建成功: {filename} ({_fmt_size(size)})")
        return result

    # ═══════════════════════════════════════════════════════
    # 备份列表
    # ═══════════════════════════════════════════════════════

    def list_backups(self) -> list[dict]:
        """扫描备份目录，返回按时间倒序的备份列表"""
        backups: list[dict] = []
        for f in self._backup_dir.iterdir():
            if not f.is_file() or not f.name.endswith(".db"):
                continue
            stat = f.stat()
            backups.append({
                "filename": f.name,
                "size": stat.st_size,
                "size_display": _fmt_size(stat.st_size),
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=_CST).isoformat(),
                "type": self._parse_type_from_filename(f.name),
            })
        # 按修改时间倒序（最新在前）
        backups.sort(key=lambda x: x["created_at"], reverse=True)
        return backups

    # ═══════════════════════════════════════════════════════
    # 恢复备份
    # ═══════════════════════════════════════════════════════

    def restore_backup(self, filename: str) -> dict:
        """从备份文件恢复数据库

        恢复前先创建当前数据库的临时备份（_pre_restore_*），
        然后复制备份文件覆盖当前数据库。失败时回滚。
        """
        backup_path = self._resolve_backup_path(filename)
        if not backup_path.exists():
            raise FileNotFoundError(f"备份文件不存在: {filename}")

        print(f"[backup] 开始恢复: {filename} → {self._db_path}")

        # 步骤1：创建恢复前临时备份
        timestamp = _now_cst().strftime("%Y%m%d_%H%M%S")
        pre_restore_name = f"_pre_restore_{timestamp}.db"
        pre_restore_path = self._backup_dir / pre_restore_name

        try:
            if self._db_path.exists():
                shutil.copy2(str(self._db_path), str(pre_restore_path))
                print(f"[backup] 恢复前临时备份已创建: {pre_restore_name}")
        except Exception as e:
            print(f"[backup] 警告：创建恢复前备份失败: {e}")
            pre_restore_name = None

        # 步骤2：复制备份文件覆盖当前数据库
        try:
            shutil.copy2(str(backup_path), str(self._db_path))
            print(f"[backup] 恢复成功: {filename}")
            return {
                "success": True,
                "message": f"已从备份 {filename} 恢复，恢复前备份: {pre_restore_name or '无'}",
                "restored_from": filename,
                "pre_restore_backup": pre_restore_name,
            }
        except Exception as e:
            # 回滚：用恢复前备份恢复当前数据库
            print(f"[backup] 恢复失败，尝试回滚: {e}")
            if pre_restore_name and pre_restore_path.exists():
                try:
                    shutil.copy2(str(pre_restore_path), str(self._db_path))
                    print("[backup] 回滚成功")
                except Exception as rollback_err:
                    print(f"[backup] 回滚也失败了: {rollback_err}")
            return {
                "success": False,
                "message": f"恢复失败: {e}",
                "restored_from": filename,
            }

    # ═══════════════════════════════════════════════════════
    # 删除备份
    # ═══════════════════════════════════════════════════════

    def delete_backup(self, filename: str) -> dict:
        """删除指定备份文件"""
        backup_path = self._resolve_backup_path(filename)
        if not backup_path.exists():
            raise FileNotFoundError(f"备份文件不存在: {filename}")
        backup_path.unlink()
        # 清理 sidecar 文件
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(backup_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        print(f"[backup] 备份已删除: {filename}")
        return {"success": True, "message": f"备份 {filename} 已删除", "filename": filename}

    # ═══════════════════════════════════════════════════════
    # 自动备份设置
    # ═══════════════════════════════════════════════════════

    def get_settings(self) -> dict:
        """从 system_settings 读取自动备份配置"""
        raw = self._repo.get_system_settings(CAT_BACKUP)
        return {
            "enabled": self._deserialize_bool(raw.get("enabled"), False),
            "retain_count": self._deserialize_int(raw.get("retain_count"), 7),
            "last_auto_backup_at": raw.get("last_auto_backup_at"),
        }

    def update_settings(self, enabled: bool, retain_count: int) -> dict:
        """更新自动备份设置"""
        self._repo.set_system_setting(CAT_BACKUP, "enabled", json.dumps(enabled))
        self._repo.set_system_setting(CAT_BACKUP, "retain_count", json.dumps(retain_count))
        print(f"[backup] 自动备份设置已更新: enabled={enabled}, retain_count={retain_count}")
        return self.get_settings()

    @staticmethod
    def _deserialize_bool(raw: str | None, default: bool) -> bool:
        if raw is None:
            return default
        try:
            return bool(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return raw == "1" or raw.lower() == "true"

    @staticmethod
    def _deserialize_int(raw: str | None, default: int) -> int:
        if raw is None:
            return default
        try:
            return int(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

    # ═══════════════════════════════════════════════════════
    # 自动备份检查（启动钩子）
    # ═══════════════════════════════════════════════════════

    def auto_backup_check(self) -> dict:
        """启动时检查：如果启用且距上次自动备份>24小时，自动创建备份"""
        settings = self.get_settings()
        if not settings["enabled"]:
            print("[backup] 自动备份未启用，跳过")
            return {"checked": True, "skipped": True, "reason": "disabled"}

        last_str = settings.get("last_auto_backup_at")
        last_dt = _parse_iso(last_str)
        now = _now_cst()

        if last_dt and (now - last_dt) < timedelta(hours=24):
            hours_left = 24 - (now - last_dt).total_seconds() / 3600
            print(f"[backup] 距上次自动备份不足24小时（还差 {hours_left:.1f}h），跳过")
            return {"checked": True, "skipped": True, "reason": "too_recent"}

        # 执行自动备份
        try:
            result = self.create_backup(backup_type="auto")
            # 更新 last_auto_backup_at
            self._repo.set_system_setting(
                CAT_BACKUP, "last_auto_backup_at", json.dumps(result["created_at"])
            )
            # 清理旧备份
            self._cleanup_old_backups(settings["retain_count"])
            print(f"[backup] 自动备份完成: {result['filename']}")
            return {"checked": True, "skipped": False, "backup": result}
        except Exception as e:
            print(f"[backup] 自动备份失败: {e}")
            return {"checked": True, "skipped": True, "error": str(e)}

    # ═══════════════════════════════════════════════════════
    # 清理旧备份
    # ═══════════════════════════════════════════════════════

    def _cleanup_old_backups(self, retain_count: int) -> None:
        """保留最近N份备份，删除最旧的。只清理 manual 和 auto 类型，不碰 pre_restore。"""
        backups = self.list_backups()
        # 只统计 manual 和 auto 类型
        managed = [b for b in backups if b["type"] in ("manual", "auto")]
        if len(managed) <= retain_count:
            return
        # managed 已按时间倒序，保留前 retain_count 个，删除后面的
        to_delete = managed[retain_count:]
        for b in to_delete:
            try:
                self.delete_backup(b["filename"])
                print(f"[backup] 自动清理旧备份: {b['filename']}")
            except Exception as e:
                print(f"[backup] 清理旧备份失败 {b['filename']}: {e}")

    # ═══════════════════════════════════════════════════════
    # 全量 JSON 导出
    # ═══════════════════════════════════════════════════════

    def export_all_json(self) -> str:
        """全量数据导出为 JSON 文件，返回文件路径"""
        timestamp = _now_cst().strftime("%Y%m%d_%H%M%S")
        filename = f"export_all_{timestamp}.json"
        filepath = self._export_dir / filename

        # 从 Repository 收集所有数据
        data: dict[str, Any] = {
            "exported_at": _now_iso(),
            "version": "1.0",
            "leads": self._serialize_list(self._repo.list_leads()),
            "customers": self._serialize_list(self._repo.list_customers()),
            "followups": self._serialize_list(self._repo.list_followups()),
            "accounts": self._serialize_list(self._repo.list_accounts()),
            "scripts": self._serialize_list(self._repo.list_scripts()),
            "tasks": self._serialize_list(self._repo.list_tasks()),
            "conversations": self._serialize_list(self._repo.list_conversations()),
            "compliance_events": self._serialize_list(self._repo.list_compliance_events()),
            "crawl_tasks": self._serialize_list(self._repo.list_crawl_tasks()),
            "system_settings": self._repo.get_all_system_settings(),
            "summary": self._repo.summary(),
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

        size = filepath.stat().st_size
        print(f"[backup] 全量导出完成: {filename} ({_fmt_size(size)})")
        return str(filepath)

    @staticmethod
    def _serialize_list(items: list) -> list[dict]:
        """将 Pydantic/dataclass 对象列表转为 dict 列表"""
        result = []
        for item in items:
            if hasattr(item, "model_dump"):
                result.append(item.model_dump(mode="json"))
            elif hasattr(item, "dict"):
                result.append(item.dict())
            else:
                result.append(str(item))
        return result
