"""养号配置与自热任务补丁的契约测试（对应验收段 9）。

这两条通道的共同点是「页面会校验返回值」，接错就变成假成功：

  get-nurture-config
      页面执行 Object.assign(config, 返回值)。返回 {success, config} 信封
      会把这两个键写进用户配置并原样存回库——配置里永久多两个假字段。
      所以断言：有配置时返回裸对象、且不含 success 键；无配置时返回 None。

  save-nurture-config
      断言存读往返、uid 形态键被剔除、空包不覆盖已有配置。

  update-self-warmup-task
      页面靠回执里的字段判断成功（s.autoStartOnLaunch !== 期望值 就报
      「设置未保存」）。断言回执=落库后的整条记录，而不是 {"success":true}。

纯离线：只读写本地 SQLite 副本。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app import local_store, nurture_config
from app.api import ipc_bridge as bridge
from app.api import task_store


@pytest.fixture(scope="module")
def client():
    with TestClient(bridge_server.app) as c:
        yield c


def _post(client, channel: str, args: list | None = None):
    resp = client.post(f"/ipc/{channel}", json={"args": args or []})
    assert resp.status_code == 200, (channel, resp.status_code, resp.text[:200])
    packet = resp.json()
    assert packet["success"] is True, (channel, packet)
    return packet["data"]


def test_nurture_config_round_trip_is_bare_object(client):
    payload = {
        "dailyLikeLimit": 30,
        "dailyCommentLimit": 10,
        "activeHours": ["09:00-12:00", "14:00-18:00"],
        "enabled": True,
    }
    saved = _post(client, "save-nurture-config", [payload])
    assert saved["success"] is True, saved

    read = _post(client, "get-nurture-config")
    assert isinstance(read, dict), read
    assert "success" not in read, "读侧返回了信封，页面会把 success 写进用户配置"
    assert read["dailyLikeLimit"] == 30
    assert read["activeHours"] == ["09:00-12:00", "14:00-18:00"]


def test_nurture_config_missing_returns_none(client):
    """没配置过时返回 None，页面回落 localStorage 并推上来（自然迁移）。"""
    con = local_store.connect(bridge.DB_PATH)
    with con:
        con.execute("DELETE FROM system_settings WHERE category=? AND key=?",
                    (nurture_config.CATEGORY, nurture_config.CONFIG_KEY))
    assert _post(client, "get-nurture-config") is None


def test_nurture_config_empty_payload_does_not_wipe(client):
    """防抖首帧发的空包不能把已有配置刷空。"""
    _post(client, "save-nurture-config", [{"dailyLikeLimit": 5}])
    skipped = _post(client, "save-nurture-config", [{}])
    assert skipped.get("skipped") == "empty"
    assert _post(client, "get-nurture-config")["dailyLikeLimit"] == 5


def test_nurture_config_strips_uid_fields(client):
    _post(client, "save-nurture-config", [{
        "dailyLikeLimit": 7, "videoId": "v9", "commentUserId": "u1",
        "nested": {"secUid": "s1", "keep": 1},
    }])
    read = _post(client, "get-nurture-config")
    assert "videoId" not in read and "commentUserId" not in read
    assert "secUid" not in read["nested"]
    assert read["nested"]["keep"] == 1


def test_update_warmup_task_echoes_saved_field(client):
    task_store.save_task(bridge.DB_PATH, "warmup", {
        "id": "wu_echo_1", "name": "养号-回执",
        "status": "ready", "createdAt": 1700000000000,
        "configSnapshot": {"accounts": []},
    })
    d = _post(client, "update-self-warmup-task",
              [{"id": "wu_echo_1", "patch": {"autoStartOnLaunch": True}}])
    assert isinstance(d, dict), d
    # 页面判定：回执里必须有这个字段且值一致
    assert d.get("autoStartOnLaunch") is True, d
    # 回执必须等于落库结果，不能是拼出来的中间值
    assert task_store.get_task(bridge.DB_PATH, "warmup", "wu_echo_1")["autoStartOnLaunch"] is True


def test_update_warmup_task_config_snapshot_persists(client):
    task_store.save_task(bridge.DB_PATH, "warmup", {
        "id": "wu_cfg_1", "name": "养号-自热配置",
        "status": "ready", "createdAt": 1700000000000,
        "configSnapshot": {"accounts": []},
    })
    snapshot = {"accounts": ["acc_a", "acc_b"], "perRound": 3, "rounds": 5}
    d = _post(client, "update-self-warmup-task",
              [{"id": "wu_cfg_1", "patch": {"configSnapshot": snapshot}}])
    assert d["configSnapshot"]["perRound"] == 3
    back = task_store.get_task(bridge.DB_PATH, "warmup", "wu_cfg_1")
    assert back["configSnapshot"]["accounts"] == ["acc_a", "acc_b"]


def test_update_warmup_task_stop_keeps_raw_status(client):
    """停止任务走 patch.status：落库后状态必须是 stopped，结束原因不丢。"""
    task_store.save_task(bridge.DB_PATH, "warmup", {
        "id": "wu_stop_1", "name": "养号-停止",
        "status": "running", "createdAt": 1700000000000,
    })
    d = _post(client, "update-self-warmup-task",
              [{"id": "wu_stop_1", "patch": {
                  "status": "stopped", "endedAt": 1700000009999, "endReason": "manual_stop"}}])
    assert task_store.get_task(bridge.DB_PATH, "warmup", "wu_stop_1")["status"] == "stopped"
    assert d.get("endReason") == "manual_stop"


def test_update_warmup_task_unknown_id_fails_loudly(client):
    """库中没有该任务时返回假值，页面才会提示「设置未保存」。"""
    d = _post(client, "update-self-warmup-task",
              [{"id": "no_such_task", "patch": {"autoStartOnLaunch": True}}])
    assert not d, d
    assert _post(client, "update-self-warmup-task", [{"id": "x"}]) in (False, None, {})
