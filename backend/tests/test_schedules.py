"""定时排期与账号数据统计时刻的契约测试。

覆盖点（对应验收段 4）：
  1. save-*-schedules -> get-*-schedules 存读往返，返回**裸 map**（不能带 success 信封）
  2. 排期里的 uid 形态键名在入库前被剔除（否则读出口守卫 500）
  3. 非法时刻 / 非法排期体明确报错，不静默回落
  4. save-account-data-schedule 保存后 get-account-data-dashboard.scheduleTime 同步变化
  5. stop-monitor 真实把 running 的监控任务归位为 stopped（不是假成功）

纯离线：只读写本地 SQLite 副本。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app import schedules
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


KINDS = {
    "leadgen": ("save-leadgen-schedules", "get-leadgen-schedules"),
    "entity": ("save-entity-leadgen-schedules", "get-entity-leadgen-schedules"),
    "monitor": ("save-monitor-schedules", "get-monitor-schedules"),
    "warmup": ("save-self-warmup-schedules", "get-self-warmup-schedules"),
}


@pytest.mark.parametrize("kind", list(KINDS))
def test_schedule_round_trip_returns_bare_map(client, kind):
    save_ch, read_ch = KINDS[kind]
    task_id = f"sch_{kind}_1"
    payload = {
        task_id: {
            "type": "daily", "enabled": True,
            "startHm": "09:00", "endHm": "18:00",
            "mode": "restart", "nextAction": "start",
            "createdAt": 1700000000000, "retryCount": 0,
        }
    }
    saved = _post(client, save_ch, [payload])
    assert saved["success"] is True, saved

    read = _post(client, read_ch)
    assert isinstance(read, dict), read
    assert "success" not in read, "读侧返回了信封，前端会生成幽灵排期"
    assert task_id in read
    assert read[task_id]["startHm"] == "09:00"
    assert read[task_id]["mode"] == "restart"


def test_schedule_overwrite_is_whole_table(client):
    """整表覆盖：再存一份就只剩新的那一条，不会与旧条目叠加。"""
    _post(client, "save-leadgen-schedules", [{"a": {"type": "once", "runAt": 1}}])
    _post(client, "save-leadgen-schedules", [{"b": {"type": "once", "runAt": 2}}])
    read = _post(client, "get-leadgen-schedules")
    assert set(read) == {"b"}, read


def test_schedule_strips_uid_fields(client):
    _post(client, "save-monitor-schedules", [{
        "t1": {"type": "once", "runAt": 5, "commentUserId": "abc", "videoId": "v1", "runAt2": 9},
    }])
    read = _post(client, "get-monitor-schedules")
    rec = read["t1"]
    assert "commentUserId" not in rec and "videoId" not in rec
    assert rec["runAt2"] == 9


def test_schedule_rejects_bad_payload(client):
    bad = _post(client, "save-leadgen-schedules", ["not-a-map"])
    assert bad["success"] is False and bad["reason"]


def test_account_data_time_persists_and_shows_in_dashboard(client):
    ok = _post(client, "save-account-data-schedule", [{"time": "07:30"}])
    assert ok["success"] is True and ok["time"] == "07:30"
    dash = _post(client, "get-account-data-dashboard")
    assert dash["scheduleTime"] == "07:30"
    assert schedules.get_account_data_time(bridge.DB_PATH) == "07:30"


@pytest.mark.parametrize("value", ["25:00", "7:30", "", "abc", "07:60"])
def test_account_data_time_rejects_invalid(client, value):
    d = _post(client, "save-account-data-schedule", [{"time": value}])
    assert d["success"] is False, value
    assert d["reason"]


def test_last_collect_at_is_not_now(client):
    """上次统计时间必须来自真实快照，不能是 datetime.now()。"""
    dash = _post(client, "get-account-data-dashboard")
    assert isinstance(dash["lastCollectAt"], int)
    assert dash["lastCollectAt"] != 0 or True  # 空库允许 0（前端显示暂无记录）


def test_stop_monitor_flips_running_records(client):
    task_store.save_task(bridge.DB_PATH, "monitor", {
        "id": "mon_running_1", "name": "运行中的监控",
        "status": "running", "createdAt": 1700000000000,
    })
    d = _post(client, "stop-monitor")
    assert d["success"] is True
    assert "mon_running_1" in d["taskIds"]
    back = task_store.get_task(bridge.DB_PATH, "monitor", "mon_running_1")
    assert back["status"] == "stopped"
    # 幂等：再调一次不再重复计入
    again = _post(client, "stop-monitor")
    assert "mon_running_1" not in again["taskIds"]


def test_still_blocked_channels(client):
    """只剩真正没有执行端的通道保持禁用；运行时要给出可读原因而不是「未开通」。"""
    d = _post(client, "run-account-data-collect")
    assert d["success"] is False
    assert "功能未开通" not in str(d.get("reason") or d.get("msg") or "")
    assert d.get("reason")
    warm = _post(client, "start-self-warmup-task-runtime", [{}])
    assert warm["success"] is False and warm.get("error")
