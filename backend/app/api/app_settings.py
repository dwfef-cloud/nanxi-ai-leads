"""系统设置：桌面壳设置项的唯一持久化层。

为什么放在后端而不是壳里
------------------------
前端设置页的所有读写都走 `/ipc/{channel}`（后端分发）。设置写在后端一处，
升级重装壳也不会丢；壳在启动时**同步读同一个文件**来应用「重启才生效」的项
（硬件加速），运行期按文件 mtime 变化应用「立即生效」的项（缩放/静音/代理）。

文件位置与后端其它数据一致：`DATA_DIR/app_settings.json`（壳用同一套
`DATA_DIR` 环境变量规则解析，见 desktop/main.js 的 commentLikeDataDir）。

字段约定（与前端 SystemSettings 页逐一对应）
--------------------------------------------
- uiZoomFactor            50-200 的整数百分比，默认 100
- useSystemProxy          授权/AI/更新等默认会话是否走系统代理，默认否
- backgroundPreviewAutoClose  窗口在后台 30 秒后自动收起实况画面，默认开
- manualLinkOpenMode      "browser"（电脑默认浏览器）/ "account"（软件账号窗口）
- hardwareAcceleration    硬件加速，默认开；关闭需重启生效
- autoCacheCleanup        空闲时后台清理缓存，默认开
- allViewsMuted           自动化窗口静音，默认关

`revision` 每次写入 +1：壳只比对 mtime + revision，避免同值重复应用。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from app.paths import DATA_DIR

SETTINGS_PATH: Path = Path(os.getenv("APP_SETTINGS_PATH", str(DATA_DIR / "app_settings.json")))

DEFAULTS: dict[str, Any] = {
    "uiZoomFactor": 100,
    "useSystemProxy": False,
    "backgroundPreviewAutoClose": True,
    "manualLinkOpenMode": "browser",
    "hardwareAcceleration": True,
    "autoCacheCleanup": True,
    "allViewsMuted": False,
}

_LOCK = threading.RLock()


def _load_locked() -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        raw = json.loads(SETTINGS_PATH.read_text("utf-8"))
        if isinstance(raw, dict):
            data = raw
    except (OSError, ValueError):
        data = {}
    merged = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = data.get(key, default)
        # 类型对齐：布尔项绝不让字符串 "false" 混进来（前端会 typeof 校验）
        if isinstance(default, bool):
            merged[key] = bool(value) if isinstance(value, (bool, int)) else default
        elif isinstance(default, int):
            try:
                merged[key] = int(value)
            except (TypeError, ValueError):
                merged[key] = default
        else:
            merged[key] = str(value or default)
    merged["revision"] = int(data.get("revision") or 0)
    return merged


def _save_locked(data: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["revision"] = int(data.get("revision") or 0) + 1
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(SETTINGS_PATH)  # 原子替换：壳读到的永远是完整 JSON


def all_settings() -> dict[str, Any]:
    with _LOCK:
        return _load_locked()


def get(key: str) -> Any:
    return all_settings().get(key, DEFAULTS.get(key))


def update(**patch: Any) -> dict[str, Any]:
    """写入若干项并返回写后的全量设置（含 revision）。"""
    with _LOCK:
        data = _load_locked()
        for key, value in patch.items():
            if key not in DEFAULTS:
                continue
            default = DEFAULTS[key]
            if isinstance(default, bool):
                data[key] = bool(value)
            elif isinstance(default, int):
                try:
                    data[key] = int(value)
                except (TypeError, ValueError):
                    continue
            else:
                data[key] = str(value)
        _save_locked(data)
        return dict(data)


# --------------------------------------------------------------------------
# 缓存：统计与清理
# --------------------------------------------------------------------------
_CACHE_SKIP_SUFFIX = {".db", ".db-journal", ".db-wal", ".db-shm"}
# 后端自己产生的临时缓存目录（可安全清理；导出/备份/数据库不在其中）
_BACKEND_CACHE_DIRS = ("cache", "tmp", ".cache")
_SHELL_CACHE_DIRS = (
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "ShaderCache", "GrShaderCache", "VideoDecodeStats",
)


def shell_userdata() -> Path | None:
    """桌面壳的 Electron userData 目录（读 desktop/package.json 的 name 推导）。

    为什么要推导：缓存的大头在 Electron 侧（网络缓存 + 分区缓存），只统计
    DATA_DIR 会把 1.3G 报成 400M，用户点「清理缓存」后数字几乎不动。
    """
    appdata = os.getenv("APPDATA") or ""
    if not appdata:
        return None
    try:
        pkg = Path(__file__).resolve().parents[3] / "desktop" / "package.json"
        name = json.loads(pkg.read_text("utf-8")).get("name") or "nanxi-desktop-shell"
    except (OSError, ValueError):
        name = "nanxi-desktop-shell"
    candidate = Path(appdata) / str(name)
    return candidate if candidate.is_dir() else None


def _walk_size(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def cache_size() -> int:
    """应用缓存占用（字节）：数据目录非数据库文件 + Electron 缓存目录。"""
    total = 0
    if DATA_DIR.exists():
        for path in DATA_DIR.rglob("*"):
            try:
                if path.is_file() and path.suffix.lower() not in _CACHE_SKIP_SUFFIX:
                    total += path.stat().st_size
            except OSError:
                continue
    userdata = shell_userdata()
    if userdata:
        for name in _SHELL_CACHE_DIRS:
            target = userdata / name
            if target.is_dir():
                total += _walk_size(target)
        parts = userdata / "Partitions"
        if parts.is_dir():
            try:
                for part in parts.iterdir():
                    cache = part / "Cache"
                    if cache.is_dir():
                        total += _walk_size(cache)
            except OSError:
                pass
    return int(total)


def clear_backend_cache() -> tuple[int, int]:
    """清理后端自己的缓存目录，返回 (释放字节, 失败目录数)。"""
    freed = 0
    failed = 0
    for name in _BACKEND_CACHE_DIRS:
        target = DATA_DIR / name
        if not target.is_dir():
            continue
        size = _walk_size(target)
        try:
            shutil.rmtree(target, ignore_errors=False)
            freed += size
        except OSError:
            failed += 1
    return freed, failed


def prune_history(days: int = 30) -> int:
    """压缩本地配置历史：删掉 N 天前的运行日志 / 临时文件（不动库、导出、备份）。

    对应设置页「清理闲置账号数据」里的「压缩本地配置中的历史冗余」——
    以前这句只是文案，现在真的清：DATA_DIR/logs 与 DATA_DIR 顶层的旧日志/临时文件。
    """
    cutoff = time.time() - max(1, int(days)) * 86400
    removed = 0
    candidates: list[Path] = []
    for name in ("logs", "log"):
        folder = DATA_DIR / name
        if folder.is_dir():
            candidates.extend(p for p in folder.rglob("*") if p.is_file())
    if DATA_DIR.is_dir():
        candidates.extend(
            p for p in DATA_DIR.glob("*")
            if p.is_file() and p.suffix.lower() in {".log", ".tmp", ".old"}
        )
    for path in candidates:
        if path.suffix.lower() not in {".log", ".tmp", ".old", ".txt", ".jsonl"}:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------
# 空闲自动清理：低频检查，有任务在跑就跳过
# --------------------------------------------------------------------------
_AUTO_STATE: dict[str, Any] = {"thread": None, "ticks": 0, "lastRun": 0, "freed": 0}
AUTO_INTERVAL_S = float(os.getenv("AUTO_CACHE_CLEANUP_INTERVAL_S", "900"))
AUTO_TICK_EVERY = 3  # 每 3 次（约 45 分钟）真正清一次


def _auto_cleanup_loop(db_path) -> None:
    while True:
        try:
            time.sleep(max(30.0, AUTO_INTERVAL_S))
            _AUTO_STATE["ticks"] = int(_AUTO_STATE["ticks"]) + 1
            if not get("autoCacheCleanup"):
                continue
            if _AUTO_STATE["ticks"] % AUTO_TICK_EVERY:
                continue
            from app.runtime import registry  # 延迟导入，避免启动期循环依赖

            if registry.active():  # 有任务在跑：本轮跳过（"运行中的账号自动跳过"）
                continue
            freed, _failed = clear_backend_cache()
            _AUTO_STATE["lastRun"] = int(time.time() * 1000)
            _AUTO_STATE["freed"] = freed
        except Exception:  # noqa: BLE001 - 后台线程绝不能因单次异常退出
            continue


def start_auto_cleanup(db_path) -> bool:
    if os.getenv("AUTO_CACHE_CLEANUP_OFF") == "1":
        return False
    with _LOCK:
        if _AUTO_STATE["thread"] is not None:
            return False
        thread = threading.Thread(target=_auto_cleanup_loop, args=(db_path,),
                                  name="auto-cache-cleanup", daemon=True)
        _AUTO_STATE["thread"] = thread
        thread.start()
        return True


def auto_cleanup_state() -> dict[str, Any]:
    return {
        "enabled": bool(get("autoCacheCleanup")),
        "ticks": _AUTO_STATE["ticks"],
        "lastRun": _AUTO_STATE["lastRun"],
        "freed": _AUTO_STATE["freed"],
        "running": _AUTO_STATE["thread"] is not None,
    }
