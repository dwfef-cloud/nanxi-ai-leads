"""常见问题 · 问题反馈 / 粘贴截图 通道契约测试（离线）。

覆盖 2026-11-17 接通的两条通道：
- submit-faq-question     描述 + 最多 3 张截图（≤5MB）+ 联系方式 → 编号落盘
- read-faq-clipboard-image 剪贴板截图（壳侧任务回执）→ PNG data URL

设计约束：编号必须真实可查（索引 + 图片文件都在盘上），剪贴板读不到时必须
如实失败（前端会提示用户改走 Ctrl+V / 本地选图），不许返回假 base64。
"""
from __future__ import annotations

import base64
import json

import pytest

from app.api import desktop_commands
from app.api import faq_feedback
from app.api import ipc_bridge as bridge

PNG_1PX = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture()
def faq_dir(tmp_path, monkeypatch):
    folder = tmp_path / "faq_feedback"
    monkeypatch.setattr(faq_feedback, "FAQ_DIR", folder)
    monkeypatch.setattr(faq_feedback, "INDEX_PATH", folder / "faq_feedback.json")
    desktop_commands.reset_for_tests()
    yield folder
    desktop_commands.reset_for_tests()


def _post(channel, payload=None):
    return bridge.dispatch(channel, [payload] if payload is not None else [])


# ------------------------------------------------------------------ 提交反馈
def test_submit_returns_persisted_id(faq_dir):
    out = _post("submit-faq-question", {
        "requestId": "req-1",
        "content": "监控任务点开始后没有反应",
        "contact": "wx: test",
        "images": [{"base64": PNG_1PX}],
    })
    assert out["success"] is True
    assert out["id"].startswith("FAQ-")
    assert out["id"].endswith("-001")

    index = json.loads((faq_dir / "faq_feedback.json").read_text("utf-8"))
    record = index["records"][0]
    assert record["id"] == out["id"]
    assert record["content"] == "监控任务点开始后没有反应"
    assert record["contact"] == "wx: test"
    assert record["status"] == "pending"
    assert record["clientRequestId"] == "req-1"
    assert record["images"][0]["file"] == f"{out['id']}/1.png"
    saved = faq_dir / out["id"] / "1.png"
    assert saved.exists() and saved.stat().st_size == record["images"][0]["bytes"]


def test_serial_number_increments_within_day(faq_dir):
    first = _post("submit-faq-question", {"content": "第一个问题"})
    second = _post("submit-faq-question", {"content": "第二个问题"})
    assert first["success"] and second["success"]
    assert first["id"].endswith("-001")
    assert second["id"].endswith("-002")
    assert len(faq_feedback.list_records()) == 2
    assert faq_feedback.list_records()[0]["id"] == second["id"]  # 倒序


def test_submit_accepts_no_images_and_no_contact(faq_dir):
    out = _post("submit-faq-question", {"content": "只写文字"})
    assert out["success"] is True
    record = json.loads((faq_dir / "faq_feedback.json").read_text("utf-8"))["records"][0]
    assert record["images"] == [] and record["contact"] == ""


def test_blank_content_is_rejected(faq_dir):
    for payload in ({}, {"content": "   "}, "not-a-dict", None):
        out = _post("submit-faq-question", payload)
        assert out["success"] is False
        assert "请描述" in out["msg"]
    assert not (faq_dir / "faq_feedback.json").exists()  # 失败不落盘


def test_more_than_three_images_rejected(faq_dir):
    out = _post("submit-faq-question", {
        "content": "四张图",
        "images": [{"base64": PNG_1PX}] * 4,
    })
    assert out["success"] is False
    assert "最多 3 张" in out["msg"]


def test_oversized_image_rejected(faq_dir):
    big = base64.b64encode(b"\0" * (faq_feedback.MAX_IMAGE_BYTES + 1)).decode()
    out = _post("submit-faq-question", {
        "content": "大图",
        "images": [{"base64": "data:image/png;base64," + big}],
    })
    assert out["success"] is False
    assert "5MB" in out["msg"]
    assert not (faq_dir / "faq_feedback.json").exists()


def test_unsupported_image_type_rejected(faq_dir):
    out = _post("submit-faq-question", {
        "content": "pdf 当图片",
        "images": [{"base64": "data:application/pdf;base64,AAAA"}],
    })
    assert out["success"] is False
    assert "格式不支持" in out["msg"]


# -------------------------------------------------------------- 粘贴截图
def test_clipboard_image_proxies_shell_task(faq_dir, monkeypatch):
    calls = {}

    def fake_shell(action, timeout=8.0, **extra):
        calls["action"] = action
        calls["timeout"] = timeout
        return {"ok": True, "base64": PNG_1PX, "bytes": 68}

    monkeypatch.setattr(bridge, "_shell_task", fake_shell)
    out = _post("read-faq-clipboard-image")
    assert out["success"] is True
    assert out["base64"] == PNG_1PX
    assert out["bytes"] == 68
    assert calls["action"] == "read-clipboard-image"


def test_clipboard_timeout_reports_shell_busy(faq_dir, monkeypatch):
    monkeypatch.setattr(bridge, "_shell_task", lambda *a, **k: None)
    monkeypatch.setattr(bridge.desktop_commands, "agent_alive", lambda: True)
    out = _post("read-faq-clipboard-image")
    assert out["success"] is False
    assert "正忙" in out["msg"]


def test_clipboard_without_shell_tells_user_to_pick_file(faq_dir, monkeypatch):
    monkeypatch.setattr(bridge, "_shell_task", lambda *a, **k: None)
    monkeypatch.setattr(bridge.desktop_commands, "agent_alive", lambda: False)
    out = _post("read-faq-clipboard-image")
    assert out["success"] is False
    assert "桌面壳未运行" in out["msg"]


def test_clipboard_empty_reports_shell_error(faq_dir, monkeypatch):
    monkeypatch.setattr(bridge, "_shell_task",
                        lambda *a, **k: {"ok": False, "error": "剪贴板里没有图片"})
    out = _post("read-faq-clipboard-image")
    assert out["success"] is False
    assert "剪贴板里没有图片" in out["msg"]


def test_clipboard_rejects_non_image_payload(faq_dir, monkeypatch):
    monkeypatch.setattr(bridge, "_shell_task", lambda *a, **k: {"ok": True, "base64": "abc"})
    out = _post("read-faq-clipboard-image")
    assert out["success"] is False


# --------------------------------------------------------------- 壳侧契约
def test_shell_clipboard_branch_exists():
    """壳里必须真的挂了 read-clipboard-image 分支（否则后端永远读不到）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "desktop" / "system-settings.js").read_text("utf-8")
    assert 'cmd.action === "read-clipboard-image"' in src
    assert "clipboard.readImage()" in src
    assert "toPNG()" in src
