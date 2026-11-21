"""M0 前后端桥接契约测试（纯离线，不触发任何平台请求）。

契约基线直接取自 demo-bridge.js 的 handler 键名 —— 前端 bundle 零改动，
因此「demo 里有的通道，真桥接必须全部能答」是最可靠的回归判据。
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app.api import ipc_bridge as bridge

DEMO_BRIDGE = bridge.RENDERER_DIR / "demo-bridge.js"


def _demo_channels() -> list[str]:
    text = DEMO_BRIDGE.read_text(encoding="utf-8")
    return sorted(set(re.findall(r'^\s*"([a-z0-9][a-z0-9\-]{2,60})"\s*:', text, re.M)))


CHANNELS = _demo_channels() if DEMO_BRIDGE.is_file() else []


@pytest.fixture(scope="module")
def client():
    with TestClient(bridge_server.app) as c:
        yield c


def _post(client, channel: str, args: list | None = None) -> dict:
    resp = client.post(f"/ipc/{channel}", json={"args": args or []})
    assert resp.status_code == 200, (channel, resp.status_code, resp.text[:200])
    return resp.json()


def test_contract_baseline_is_complete():
    """契约基线必须真的取到 demo 的通道表，否则后续测试会假绿。"""
    assert len(CHANNELS) >= 90, f"demo 通道解析异常: {len(CHANNELS)}"
    assert "get-leads-page" in CHANNELS
    assert "get-account-pool" in CHANNELS


@pytest.mark.parametrize("channel", CHANNELS)
def test_every_demo_channel_answers(client, channel):
    packet = _post(client, channel)
    assert packet.get("success") is True, (channel, packet)
    assert packet.get("mode") == "live"
    assert "data" in packet


def test_leads_page_shape_and_no_platform_uid(client):
    packet = _post(client, "get-leads-page", [{"page": 1, "pageSize": 5}])
    data = packet["data"]
    assert data["runtime"] == "sqlite"
    assert isinstance(data["items"], list) and data["total"] > 0
    sample = data["items"][0]

    def keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield str(k)
                yield from keys(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from keys(item)

    leaked = sorted({k for k in keys(data) if k in bridge.PLATFORM_UID_FIELDS})
    assert not leaked, f"平台 uid 字段泄漏: {leaked}"
    # 页面渲染必需的关键字段不能因为脱敏而丢失
    assert {"leadId", "nickname", "comment", "capturedAt", "intention"} <= set(sample)
    assert "douyinId" not in sample  # demo 里的虚构抖音号不应被带出


def test_leads_page_pagination_and_keyword_filter(client):
    p1 = _post(client, "get-leads-page", [{"page": 1, "pageSize": 2}])["data"]
    p2 = _post(client, "get-leads-page", [{"page": 2, "pageSize": 2}])["data"]
    assert len(p1["items"]) == len(p2["items"]) == 2
    assert p1["items"][0]["leadId"] != p2["items"][0]["leadId"]
    assert p1["total"] == p2["total"]

    filtered = _post(client, "get-leads-page", [{"keyword": "__一定不存在__", "pageSize": 3}])["data"]
    assert filtered["items"] == [] and filtered["total"] == 0


def test_real_data_is_used(client):
    data = _post(client, "get-leads-pool-stats", [])["data"]
    stats = data["stats"]
    # demo 只有 48 条虚构线索；真库规模远大于此，用来判定确实换了数据源而非写死快照
    assert stats["total"] > 500, "未读到南溪真实 leads 数据"
    assert stats["replies"] > 0
    # 存量 dm_send_results 全部是自动化测试记录（lead_id='__test__'、内容「模拟发送成功」），
    # 必须被账本剔除；真实私信发送为 0。接真实发送后此断言应改为 > 0。
    assert stats["messages"] == 0, "模拟发送记录不得计入触达统计"
    assert "剔除" in data["note"]
    accounts = _post(client, "get-account-pool", [])["data"]
    # 账号池是运营数据，数量随实际增删变化（批量清理会减少），
    # 这里只验证「读到了真实账号」而不是断言固定规模
    assert len(accounts) >= 1
    assert all("profile_dir" not in a for a in accounts), "CDP 用户目录路径不应外泄"
    tasks = _post(client, "get-leadgen-tasks", [])["data"]
    # 历史任务（crawl_tasks）+ 壳保存的任务记录会合并展示，数量随实际建任务/删任务变化
    # （2026-11-20 用户把 17 条历史任务全部删除后此断言失效——那是正常运营结果）。
    # 关键契约：接口返回列表且无占位名，合并逻辑由单元用例覆盖。
    assert isinstance(tasks, list), "get-leadgen-tasks 应返回列表"
    assert all(t["name"] != "未命名任务" for t in tasks)


def test_dashboard_shape_matches_demo(client):
    data = _post(client, "get-account-data-dashboard", [])["data"]
    for key in ("success", "scheduleTime", "lastCollectAt", "collecting", "days",
                "errors", "baselines", "latest", "accounts"):
        assert key in data, key
    assert isinstance(data["days"], list) and data["days"]


@pytest.mark.parametrize("channel", sorted(bridge.BLOCKED_CHANNELS))
def test_dangerous_channels_are_disabled(client, channel):
    packet = _post(client, channel)
    assert packet["success"] is True, "外层必须成功，否则前端会误判为桥接故障"
    assert packet["data"]["success"] is False
    assert packet["data"]["msg"] == "功能未开通"


def test_unknown_channel_returns_not_available(client):
    """未知通道必须明确返回「功能未开通」。

    旧实现返回 {"success": True}，会让 delete-*/clear-*/export-* 这类写操作变成
    假成功（前端提示成功、实际无任何变化）——属数据安全级缺陷，已修正。
    """
    # delete-leads-from-history 已注册（2026-11-17），换一个仍未注册的删除类通道
    data = _post(client, "delete-unknown-thing")["data"]
    assert data["success"] is False
    assert data["channel"] == "delete-unknown-thing"
    assert data["msg"] == "功能未开通"


def test_public_safe_guard():
    with pytest.raises(bridge.BridgeComplianceError):
        bridge.assert_public_safe({"items": [{"comment_user_id": "x"}]})
    with pytest.raises(bridge.BridgeComplianceError):
        bridge.assert_public_safe({"videoId": "x"})
    bridge.assert_public_safe({"leadId": "内部主键", "nickname": "张三"})


def test_existing_api_and_ui_are_untouched(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/ipc/health").json()["rendererExists"] is True
    assert client.get("/bridge-info").json()["mediaCrawlerAutostart"] is False
    page = client.get("/ui/")
    assert page.status_code == 200
    assert b"live-bridge.js" in page.content
    assert b"demo-bridge.js" in page.content, "demo 兜底文件必须保留"
    assert client.get("/ui/live-bridge.js").status_code == 200


def test_live_bridge_js_contract():
    text = (bridge.RENDERER_DIR / "live-bridge.js").read_text(encoding="utf-8")
    assert '"/ipc/"' in text or "'/ipc/'" in text or "/ipc/" in text
    assert "packet.data" in text, "必须拆包后再交给页面"
    assert 'demo=1' in text or "demoMode" in text
    assert "safeFallback" in text, "桥接故障不得回退假数据"


def test_bridge_db_connection_is_readonly():
    with closing(bridge._connect()) as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute("CREATE TABLE _bridge_write_probe (x INTEGER)")
        conn.rollback()
