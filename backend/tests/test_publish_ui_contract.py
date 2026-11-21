"""发布页 ↔ 后端契约：#69 里能静态验证的那部分。

真机验证（上传框、封面弹窗、定时开关的实际选择器）只能在已登录的创作者中心跑，
本地无法替代。但**通道名和字段名**可以先锁死 —— 这两处漂移的代价是「界面点得动，
后端收不到」，而且不报错，是最难发现的一类问题。

- UI 里所有以 requset 方式调用的 work-publish 通道，后端必须注册；
  以事件监听（`on("...")`/`L("...")`）方式用的名字是壳→UI 推送，不该有路由。
- 后端读取的发布配置键，必须都能在打包资源里找到同名发送方。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "ui" / "assets"
BRIDGE = ROOT / "backend" / "app" / "api" / "ipc_bridge.py"

# UI 的请求通道（来自 WorkPublish 打包资源里的 $("...") 调用）
REQUEST_CHANNELS = [
    "delete-work-publish-task", "delete-work-publish-tasks", "delete-work-publish-template",
    "get-work-publish-quota", "get-work-publish-task-runtime-logs", "get-work-publish-tasks",
    "get-work-publish-templates", "pick-work-publish-cover", "pick-work-publish-media",
    "save-work-publish-task", "save-work-publish-template", "show-work-publish-monitor",
    "work-publish-generate",
]

# 壳 → UI 的推送事件，不是路由
EVENT_CHANNELS = ["work-publish-task-event", "work-publish-view"]

# 后端 publish.py 读取的发布配置键
CONFIG_KEYS = [
    "publishMode", "staggerMinSec", "staggerMaxSec",
    "coverEnabled", "coverMode", "coverLandscapePath", "coverPortraitPath",
    "runAt", "schedule", "items", "filePath", "filePaths", "media",
]

# 前端有、后端没接的键 —— 每条都必须对应清单里的一条「部分实现」，不能悄悄沉底。
# imageCoverIndex：图文作品「第 N 张做封面」，平台侧要进编辑页选图，壳侧没有稳定原语（#62）。
UI_ONLY_KEYS = {"imageCoverIndex": "#62 图文第 N 张封面"}


@pytest.fixture(scope="module")
def ui_source() -> str:
    if not ASSETS.is_dir():
        pytest.skip("打包资源不在本地（只有前端产物时跳过契约检查）")
    return "".join(p.read_text(encoding="utf-8", errors="ignore")
                   for p in sorted(ASSETS.glob("*.js")))


def test_every_request_channel_is_registered(ui_source):
    bridge = BRIDGE.read_text(encoding="utf-8")
    for name in REQUEST_CHANNELS:
        assert f'"{name}"' in ui_source, f"UI 里找不到通道 {name}，契约表需要更新"
        assert f'"{name}"' in bridge, f"通道 {name} 未在后端注册"


def test_event_channels_are_not_routes(ui_source):
    """事件监听名不该被当成路由注册 —— 注册了也不会有人调用。"""
    bridge = BRIDGE.read_text(encoding="utf-8")
    for name in EVENT_CHANNELS:
        assert f'"{name}"' in ui_source
        assert f'"{name}": ' not in bridge, f"{name} 是推送事件，不应出现在路由表里"


def test_publish_config_keys_have_a_sender(ui_source):
    publish = (ROOT / "backend" / "app" / "runtime" / "publish.py").read_text(encoding="utf-8")
    for key in CONFIG_KEYS:
        # 后端有的键是循环读取（item.get(key)），所以只要求出现这个名字
        assert f'"{key}"' in publish, f"后端已不再读取 {key}，契约表需要更新"
        assert key in ui_source, f"后端读取 {key}，但前端没有发送方"


def test_ui_only_keys_are_still_a_known_gap(ui_source):
    """前端还在发、后端仍没接的字段：确认它确实还没被接上（接上就该挪进 CONFIG_KEYS）。"""
    publish = (ROOT / "backend" / "app" / "runtime" / "publish.py").read_text(encoding="utf-8")
    for key, gap in UI_ONLY_KEYS.items():
        assert key in ui_source, f"{key} 已从界面移除，{gap} 的备注要更新"
        assert f'"{key}"' not in publish, f"{key} 已经接上了，请移到 CONFIG_KEYS 并更新 {gap}"


def test_schedule_time_lives_under_item_schedule(ui_source):
    """定时时间在 item.schedule.runAt —— 后端按这条路径解析，改结构必须同步改这里。"""
    publish = (ROOT / "backend" / "app" / "runtime" / "publish.py").read_text(encoding="utf-8")
    assert 'item.get("schedule")' in publish
    assert re.search(r'runAt:\s*null', ui_source)
