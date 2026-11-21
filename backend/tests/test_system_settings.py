"""系统设置通道契约测试（离线）。

覆盖 2026-11-17 接通的「系统设置」整页：
- 缩放 / 系统代理 / 实况自动收起 / 链接打开方式 / 硬件加速 / 自动清理 / 静音
- 缓存统计与清理、闲置 Session 分区清理（壳侧任务回执）
- 持久化文件与壳侧读取口径一致（desktop/system-settings.js 的 DEFAULTS）

设计约束：这些开关必须**真实落地**（持久化 + 壳应用），所以测试同时校验
「值写进文件」和「壳能读到同一个文件」，而不是只看接口返回。
"""
from __future__ import annotations

import json
import os
import time

import pytest

from app.api import app_settings
from app.api import ipc_bridge as bridge
from app.api import desktop_commands


@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "app_settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", path)
    desktop_commands.reset_for_tests()
    yield path
    desktop_commands.reset_for_tests()


def _post(channel, payload=None):
    return bridge.dispatch(channel, [payload] if payload is not None else [])


# --------------------------------------------------------------- 读写与持久化
def test_zoom_roundtrip_persists_to_file(settings_file):
    assert _post("get-ui-zoom-factor") == 100
    assert _post("set-ui-zoom-factor", 125) == 125  # 标量入参（RAW）不能丢值
    assert _post("get-ui-zoom-factor") == 125
    saved = json.loads(settings_file.read_text("utf-8"))
    assert saved["uiZoomFactor"] == 125
    assert saved["revision"] >= 1


def test_zoom_is_clamped_to_supported_range(settings_file):
    assert _post("set-ui-zoom-factor", 10) == 50
    assert _post("set-ui-zoom-factor", 999) == 200
    assert _post("set-ui-zoom-factor", "125.4") == 125
    assert _post("set-ui-zoom-factor", "abc") == 125  # 非法值不改，保持上一次


def test_boolean_switches_roundtrip(settings_file):
    for channel, key, value in (
        ("use-system-proxy", "useSystemProxy", True),
        ("background-preview-auto-close", "backgroundPreviewAutoClose", False),
        ("hardware-acceleration", "hardwareAcceleration", False),
        ("auto-cache-cleanup", "autoCacheCleanup", False),
    ):
        assert _post(f"set-{channel}", value) is value, channel
        assert _post(f"get-{channel}") is value, channel
        assert json.loads(settings_file.read_text("utf-8"))[key] is value, key


def test_link_open_mode_only_accepts_two_values(settings_file):
    assert _post("set-manual-link-open-mode", "account") == "account"
    assert _post("get-manual-link-open-mode") == "account"
    assert _post("set-manual-link-open-mode", "whatever") == "browser"
    assert _post("set-manual-link-open-mode", "ACCOUNT") == "account"


def test_all_views_muted_returns_state(settings_file):
    assert _post("set-all-views-muted", True) == {"success": True, "muted": True}
    assert app_settings.get("allViewsMuted") is True
    assert _post("set-all-views-muted", False) == {"success": True, "muted": False}


def test_settings_file_is_valid_json_after_many_writes(settings_file):
    for value in (100, 110, 125, 150, 100):
        _post("set-ui-zoom-factor", value)
    saved = json.loads(settings_file.read_text("utf-8"))
    assert saved["uiZoomFactor"] == 100
    # 原子替换后不应残留 .tmp
    assert not (settings_file.parent / "app_settings.json.tmp").exists()


def test_shell_defaults_match_backend():
    """壳（desktop/system-settings.js）与后端 DEFAULTS 必须同口径。

    两边各写一份默认值是为了壳能在后端启动前就读到设置（硬件加速必须这么早），
    代价是可能漂移 —— 这条测试就是防漂移的锁。
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shell = os.path.join(os.path.dirname(here), "desktop", "system-settings.js")
    text = open(shell, encoding="utf-8").read()
    for key in app_settings.DEFAULTS:
        assert f"{key}:" in text, f"壳侧缺少设置项 {key}"
    assert "disableHardwareAcceleration" in text
    assert "setZoomFactor" in text and "setAudioMuted" in text


# --------------------------------------------------------------------- 缓存
def test_cache_size_counts_data_dir_files(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_settings, "shell_userdata", lambda: None)
    (tmp_path / "lead_system.db").write_bytes(b"x" * 100)  # 数据库不算缓存
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "leads.csv").write_bytes(b"y" * 40)
    assert app_settings.cache_size() == 40


def test_clear_cache_reports_shell_offline_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_DIR", tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "blob.bin").write_bytes(b"z" * 12)
    monkeypatch.setattr(desktop_commands, "wait_result", lambda *a, **k: None)
    monkeypatch.setattr(desktop_commands, "agent_alive", lambda: False)
    out = _post("clear-cache")
    assert out["success"] is True  # 后端侧缓存确实清了
    assert out["freed"] >= 12
    assert not cache.exists()
    assert "桌面壳未运行" in out["note"]


def test_clear_cache_merges_shell_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(desktop_commands, "wait_result",
                        lambda *a, **k: {"ok": True, "cleared": 3, "failed": 1,
                                         "skipped": 2, "freed": 2048})
    out = _post("clear-cache")
    assert out == {"success": True, "cleared": 4, "failed": 1, "skipped": 2, "freed": 2048}


def test_clear_cache_shell_error_is_not_fake_success(monkeypatch):
    monkeypatch.setattr(desktop_commands, "wait_result",
                        lambda *a, **k: {"ok": False, "error": "磁盘忙"})
    out = _post("clear-cache")
    assert out["success"] is False and "磁盘忙" in out["reason"]


def test_cleanup_idle_accounts_requires_shell(monkeypatch):
    monkeypatch.setattr(desktop_commands, "wait_result", lambda *a, **k: None)
    monkeypatch.setattr(desktop_commands, "agent_alive", lambda: True)
    out = _post("cleanup-idle-account-data")
    assert out["success"] is False
    assert "正忙" in out["reason"]


def test_cleanup_idle_accounts_returns_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(app_settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(desktop_commands, "wait_result",
                        lambda *a, **k: {"ok": True,
                                         "partitions": {"scanned": 6, "deleted": 2},
                                         "storePrune": {"changed": False}})
    out = _post("cleanup-idle-account-data")
    assert out["success"] is True
    assert out["partitions"] == {"scanned": 6, "deleted": 2}
    assert out["storePrune"]["changed"] is False


def test_prune_history_only_removes_old_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_DIR", tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    old = logs / "run.log"
    old.write_text("old", encoding="utf-8")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    fresh = logs / "today.log"
    fresh.write_text("new", encoding="utf-8")
    keep = tmp_path / "lead_system.db"
    keep.write_text("db", encoding="utf-8")
    os.utime(keep, (stale, stale))
    removed = app_settings.prune_history(30)
    assert removed == 1
    assert not old.exists() and fresh.exists()
    assert keep.exists()  # 数据库永远不动


def test_auto_cleanup_state_shape(monkeypatch):
    monkeypatch.setattr(app_settings, "_AUTO_STATE",
                        {"thread": None, "ticks": 0, "lastRun": 0, "freed": 0})
    state = app_settings.auto_cleanup_state()
    assert set(state) == {"enabled", "ticks", "lastRun", "freed", "running"}
    assert isinstance(state["enabled"], bool)


def test_get_app_settings_exposes_all_options(settings_file):
    data = _post("get-app-settings")
    for key in app_settings.DEFAULTS:
        assert key in data
    assert isinstance(data["cacheSize"], int)
