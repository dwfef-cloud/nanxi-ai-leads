"""可搬路径集中管理。

交付包必须能整目录拷到任意位置直接运行，因此数据 / 导出 / 备份目录一律相对
软件根解析，需要换位置时用环境变量覆盖。业务代码里不要再出现盘符绝对路径。

数据目录放在**软件根目录**（与 backend 平级）而不是 backend 内部，理由：
代码目录可以整体替换 / 重新解包，数据目录独立存在，升级时不会被覆盖。
交付安装时由桌面壳注入 DATA_DIR 指向 %APPDATA%，避免装到 Program Files
时无写权限、以及覆盖安装带走数据。

环境变量（均可选）：
    DATA_DIR       数据根目录，默认 <软件根>/data
    LEADS_DB_PATH  主库路径，默认 DATA_DIR/lead_system.db
    EXPORT_DIR     导出目录，默认 DATA_DIR/exports
    BACKUP_DIR     备份目录，默认 DATA_DIR/backups

本模块是数据路径的**唯一来源**：其他模块不要再自行拼 "data/xxx.db"。
"""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT_DIR / "data")))
DB_PATH = Path(os.getenv("LEADS_DB_PATH", str(DATA_DIR / "lead_system.db")))
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", str(DATA_DIR / "exports")))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(DATA_DIR / "backups")))


def ensure(path: Path) -> Path:
    """建目录并返回，供 mount / 落盘前调用。"""
    path.mkdir(parents=True, exist_ok=True)
    return path
