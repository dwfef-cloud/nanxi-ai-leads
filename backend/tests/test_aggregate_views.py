"""聚合视图修复的契约测试：账号数据 / 创作聚合 / 消息聚合 / 离线视图适配。

纯离线：只读本地 SQLite，不触发任何平台请求、不启动 MediaCrawler。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app.api import ipc_bridge as bridge

DEMO_BRIDGE = bridge.RENDERER_DIR / "demo-bridge.js"


@pytest.fixture(scope="module")
def client():
    with TestClient(bridge_server.app) as c:
        yield c


def _post(client, channel: str, args: list | None = None) -> dict:
    resp = client.post(f"/ipc/{channel}", json={"args": args or []})
    assert resp.status_code == 200, (channel, resp.status_code, resp.text[:200])
    packet = resp.json()
    assert packet["success"] is True, (channel, packet)
    return packet["data"]


def _demo_channels() -> list[str]:
    if not DEMO_BRIDGE.is_file():
        return []
    return sorted(set(re.findall(
        r'^\s*"([a-z0-9][a-z0-9\-]{2,60})"\s*:',
        DEMO_BRIDGE.read_text(encoding="utf-8"), re.M,
    )))


def test_account_dashboard_uses_real_daily_events(client):
    d = _post(client, "get-account-data-dashboard")
    assert d["success"] is True
    assert 5 <= len(d["days"]) <= 7
    assert d["days"] == sorted(d["days"])
    assert d["accounts"], "账号池为空，热力表无法渲染"
    first = next(iter(d["accounts"]))
    assert set(first["days"]) == set(d["days"])
    entry = first["days"][d["days"][-1]]
    for key in ("followers", "following", "works", "likes", "collections",
                "videoShares", "replies", "profileComments", "videoComments",
                "follows", "messages", "snapshotAt"):
        assert key in entry, key
    totals = d["totals"]
    assert totals["comments"] > 0, "真实评论事件没有聚合进来"
    assert totals["outbound"] > 0, "真实消息事件没有聚合进来"
    assert "每日快照" in d["note"] and "本地库真实事件" in d["note"]


def test_creator_aggregate_reads_local_content_records(client):
    d = _post(client, "get-creator-aggregate", [{"accountName": "AI学长小马"}])
    assert d["success"] is True
    assert d["accountName"] == "AI学长小马"
    assert d["totals"]["leads"] > 0
    assert d["totals"]["videos"] > 0
    assert d["totals"]["tasks"] > 0
    assert d["videos"] and d["tasks"] and d["timeline"]
    assert all("title" in v and "leads" in v for v in d["videos"])
    assert d["accounts"] and "replies" in d["accounts"][0]
    assert "不内嵌真实创作者页面" in d["note"]


def test_message_aggregate_reads_local_conversations(client):
    d = _post(client, "get-message-aggregate", [{"limit": 12}])
    assert d["success"] is True
    stats = d["stats"]
    for key in ("threads", "messages", "inbound", "outbound", "unread", "today"):
        assert key in stats
    assert stats["threads"] > 0 and stats["messages"] > 0
    assert stats["messages"] == stats["inbound"] + stats["outbound"]
    assert d["threads"], "会话列表为空"
    assert d["messages"], "消息流为空"
    assert len(d["messages"]) <= 12
    thread = d["threads"][0]
    assert len(thread["threadKey"]) == 12, "会话键必须是不可逆短哈希"
    assert thread["threadKey"] != thread.get("accountName")
    msg = d["messages"][0]
    assert msg["direction"] in ("in", "out")
    assert "不是平台实时私信" in d["note"]


def test_message_thread_filter(client):
    all_data = _post(client, "get-message-aggregate", [{"limit": 20}])
    key = all_data["messages"][0]["threadKey"]
    one = _post(client, "get-message-aggregate", [{"limit": 20, "threadKey": key}])
    assert one["messages"], "单会话筛选后不应为空"
    assert all(m["threadKey"] == key for m in one["messages"])


def test_unread_state_matches_aggregate_stats(client):
    unread = _post(client, "get-chat-unread-state")
    stats = _post(client, "get-message-aggregate")["stats"]
    assert unread["total"] == stats["unread"], "两个通道未读口径不一致"
    assert unread["totalUnread"] == unread["total"]
    assert unread["total"] == sum(unread["unread"].values())


def test_view_adapter_channels_return_offline_panel(client):
    for ch in sorted(bridge.VIEW_ADAPTER_CHANNELS):
        d = _post(client, ch, [{"accountId": "x", "viewKey": "douyin_x"}])
        assert d["success"] is True, ch
        assert d["mode"] == "iframe-embed", ch
        assert "不做代操作" in d["message"], ch


def test_dangerous_channels_remain_disabled(client):
    """仍然没有执行引擎的通道必须维持「功能未开通」，不能变成假成功。"""
    for ch in ("generate",):
        d = _post(client, ch, [{}])
        assert d.get("success") is False and d.get("msg") == "功能未开通", ch
    # 账号数据统计已接真机采样：没有桌面壳时也要明确失败，且原因是「壳未运行」，
    # 不是「功能未开通」——两者含义完全不同，前者用户能自己解决。
    d = _post(client, "run-account-data-collect", [{}])
    assert d.get("success") is False
    assert "桌面壳未运行" in str(d.get("reason") or d.get("error") or ""), d


def test_runtime_start_without_task_is_rejected(client):
    """三类运行时已接线：但缺任务 id / 没有引擎时必须是明确失败，不是假成功。"""
    for ch, kind in (("start-entity-leadgen-task-runtime", "entity"),
                     ("start-monitor-task-runtime", "monitor"),
                     ("start-self-warmup-task-runtime", "warmup")):
        d = _post(client, ch, [{}])
        assert d.get("success") is False, ch
        assert d.get("error"), ch
        assert "任务" in d["error"] or "id" in d["error"], ch


def test_stop_monitor_is_local_state_reset(client):
    """stop-monitor 只做本地状态归位（无执行引擎），返回真实停止条数。"""
    d = _post(client, "stop-monitor", [{}])
    assert d.get("success") is True, d
    assert isinstance(d.get("stopped"), int) and d["stopped"] >= 0
    assert isinstance(d.get("taskIds"), list)


def test_local_only_writes_are_wired(client):
    """save-account-pool / stop-task 已接线：只动本地库与本地线程，不触达平台。"""
    pool = _post(client, "save-account-pool", [[]])
    assert pool.get("success") is True and "platforms" in pool, pool
    stop = _post(client, "stop-task", [{"taskId": "not-running"}])
    assert stop.get("success") is True


def test_former_empty_channels_now_carry_local_data(client):
    for ch, key in (
        ("get-monitor-live-views", "views"),
        ("get-leadgen-task-video-comments", "items"),
        ("get-self-warmup-events", "events"),
    ):
        d = _post(client, ch)
        assert key in d, ch
        assert isinstance(d[key], list), ch
    # 定时排期：壳直接 store.replace*(map)，必须是**裸 map**。
    # 包 success 信封会让前端把 "success" 当成一条排期（幽灵任务）。
    for ch in ("get-leadgen-schedules", "get-entity-leadgen-schedules",
               "get-monitor-schedules", "get-self-warmup-schedules"):
        d = _post(client, ch)
        assert isinstance(d, dict), ch
        assert "success" not in d, f"{ch} 返回了信封，前端会生成幽灵排期"
        assert all(isinstance(v, dict) for v in d.values()), ch
    # 运行日志抽屉直接把返回值 .map，必须是裸数组（不能包 success/logs 对象）
    logs = _post(client, "get-entity-leadgen-task-runtime-logs")
    assert isinstance(logs, list), "运行日志通道必须返回数组"
    # get-work-publish-templates 已接真实存储（2026-11-17），返回模板数组
    assert isinstance(_post(client, "get-work-publish-templates"), list)
    assert _post(client, "get-comment-like-results")["note"]


def test_new_channels_have_no_platform_uid_fields(client):
    forbidden = ("comment_user_id", "video_id", "comment_id", "external_id",
                 "douyinId", "conversation_id")
    for ch in ("get-account-data-dashboard", "get-creator-aggregate",
               "get-message-aggregate", "get-chat-unread-state",
               "get-monitor-live-views", "get-monitor-task-matches"):
        blob = json.dumps(_post(client, ch), ensure_ascii=False)
        for field in forbidden:
            assert f'"{field}"' not in blob, (ch, field)
        bridge.assert_public_safe(_post(client, ch))


def test_all_demo_channels_still_answered(client):
    channels = _demo_channels()
    assert len(channels) >= 90
    for ch in channels:
        packet = client.post(f"/ipc/{ch}", json={"args": [{}]})
        assert packet.status_code == 200, ch
        assert packet.json()["success"] is True, ch


def test_renderer_offline_view_assets(client):
    index = (bridge.RENDERER_DIR / "index.html").read_text(encoding="utf-8")
    assert "live-bridge.js" in index and "bridge-views.js" in index
    js = (bridge.RENDERER_DIR / "bridge-views.js").read_text(encoding="utf-8")
    assert "creator-host" in js and "chat-host" in js
    # v2 契约：宿主内嵌真实抖音页面 iframe（不是自造数据面板）
    assert "creator.douyin.com/creator-micro/home" in js
    assert "https://www.douyin.com/" in js
    assert "<iframe" in js or "createElement(\"iframe\")" in js
    # 明确不做代操作
    assert "不做任何代操作" in js
    live = (bridge.RENDERER_DIR / "live-bridge.js").read_text(encoding="utf-8")
    assert "creator-view-status" in live and "chat-view-status" in live
    resp = client.get("/ui/bridge-views.js")
    assert resp.status_code == 200 and b"bridge-views" in resp.content


def test_health_reports_view_adapters(client):
    h = client.get("/ipc/health").json()
    assert h["viewAdapterChannels"] == len(bridge.VIEW_ADAPTER_CHANNELS)
    assert h["blockedChannels"] == len(bridge.BLOCKED_CHANNELS)
