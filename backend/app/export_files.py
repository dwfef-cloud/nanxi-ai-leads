"""把壳生成的导出内容落盘（线索池「导出 CSV」的真实落点）。

前端不把数据回传后端重算：它已经按当前筛选 / 勾选把行拼成 CSV 文本，
只要求后端把内容写成文件（一屏 5000 条也不必再查一次库，口径天然一致）。
通道是 `save-lead-export-csvs`，入参：

    {"files": [{"name": "leads_export_1712345.csv", "content": "\ufeff昵称,平台\\n..."}]}

落盘目录：EXPORT_DIR/leads（默认 <软件根>/data/exports/leads），
同时返回 `/exports/leads/<文件名>` 的静态地址，壳可以直接打开。

安全边界：文件名只取 basename、限定扩展名、拒绝 . / .. / 路径分隔符；
单文件 ≤ 20MB、单次 ≤ 99 个，避免误传大文件把磁盘写满。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from app.paths import EXPORT_DIR

MAX_FILES = 99
MAX_BYTES = 20 * 1024 * 1024
_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fa5._-]+")
_ALLOWED_EXT = {".csv", ".txt"}


def _target_dir() -> Path:
    path = EXPORT_DIR / "leads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(raw: Any, fallback: str = "leads_export.csv") -> str:
    name = Path(str(raw or "")).name.strip()  # 去掉任何目录部分
    name = _SAFE.sub("_", name).lstrip(".") or fallback
    if Path(name).suffix.lower() not in _ALLOWED_EXT:
        name = f"{name}.csv"
    return name[:120]


def save_csvs(payload: Any) -> dict:
    """写入一组 CSV 文件。payload 既可以是 {"files": [...]}，也可以直接是数组。"""
    data = payload
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], Mapping):
        data = data[0]
    files = data.get("files") if isinstance(data, Mapping) else data
    if not isinstance(files, list) or not files:
        return {"success": False, "error": "没有可导出的文件内容"}

    folder = _target_dir()
    written: list[dict] = []
    for index, item in enumerate(files[:MAX_FILES]):
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content:
            continue
        blob = content.encode("utf-8")
        if len(blob) > MAX_BYTES:
            return {"success": False,
                    "error": f"第 {index + 1} 个文件超过 20MB，请减少条数或分批导出"}
        name = _safe_name(item.get("name"), f"leads_export_{index + 1}.csv")
        target = folder / name
        target.write_bytes(blob)
        written.append({"name": name, "bytes": len(blob), "path": str(target),
                        "url": f"/exports/leads/{name}"})

    if not written:
        return {"success": False, "error": "文件内容为空"}
    return {"success": True, "count": len(written), "dir": str(folder), "files": written}
