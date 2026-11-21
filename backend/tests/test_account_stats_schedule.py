"""账号数据每日自动统计的到点触发（清单 #25）。

时刻由 save-account-data-schedule 落库（HH:MM），此前只有「能存能显示」，
到点没有任何东西去触发统计——本组用例锁定 scheduler.run_once 的钩子行为：
- 未到点 / 未配置（00:00）不触发；
- 到点触发一轮 collect（独立线程，schedule-runner 的 tick 不被拖住）；
- 成功后当天不再触发，第二天到点再次触发；
- 失败（壳不在线等）不占当天名额，但 10 分钟内限频重试。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import scheduler
from app import schedules as sched_store


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "lead_system.db"


@pytest.fixture(autouse=True)
def reset_stats_state():
    scheduler._STATS_DAILY["day"] = ""
    scheduler._STATS_DAILY["attempt"] = 0.0
    yield
    scheduler._STATS_DAILY["day"] = ""
    scheduler._STATS_DAILY["attempt"] = 0.0


def _wait_stats_thread():
    import time
    for _ in range(60):
        if not any(t.name == "account-stats-daily" and t.is_alive()
                   for t in __import__("threading").enumerate()):
            return
        time.sleep(0.05)


def test_not_triggered_before_configured_time(db, monkeypatch):
    sched_store.set_account_data_time(db, "09:00")
    fired = []
    monkeypatch.setattr("app.api.account_data.collect", lambda *a, **k: fired.append(1) or {"success": True})
    now = datetime(2026, 11, 18, 8, 59)
    out = scheduler.run_once(db, _ms(now))
    _wait_stats_thread()
    assert out.get("accountStatsTriggered") is None
    assert fired == []


def test_not_triggered_when_time_unconfigured(db, monkeypatch):
    monkeypatch.setattr("app.api.account_data.collect", lambda *a, **k: {"success": True})
    now = datetime(2026, 11, 18, 10, 0)
    out = scheduler.run_once(db, _ms(now))
    _wait_stats_thread()
    assert out.get("accountStatsTriggered") is None


def test_triggers_at_configured_time_once_per_day(db, monkeypatch):
    sched_store.set_account_data_time(db, "09:00")
    calls = []
    monkeypatch.setattr("app.api.account_data.collect",
                        lambda d, p=None: calls.append(str(d)) or {"success": True})
    now = datetime(2026, 11, 18, 9, 0)
    out = scheduler.run_once(db, _ms(now))
    _wait_stats_thread()
    assert out.get("accountStatsTriggered") is True
    assert len(calls) == 1
    # 同一天后续 tick 不再触发
    out2 = scheduler.run_once(db, _ms(now + timedelta(minutes=5)))
    _wait_stats_thread()
    assert out2.get("accountStatsTriggered") is None
    assert len(calls) == 1
    # 第二天到点再次触发
    out3 = scheduler.run_once(db, _ms(now + timedelta(days=1)))
    _wait_stats_thread()
    assert out3.get("accountStatsTriggered") is True
    assert len(calls) == 2


def test_failure_retries_after_gap_not_immediately(db, monkeypatch):
    sched_store.set_account_data_time(db, "09:00")
    calls = []

    def fake_collect(d, p=None):
        calls.append(1)
        return {"success": False, "reason": "桌面壳未运行：账号数据要通过本机登录窗口读取，请启动桌面端后重试"}

    monkeypatch.setattr("app.api.account_data.collect", fake_collect)
    now = datetime(2026, 11, 18, 9, 0)
    scheduler.run_once(db, _ms(now))
    _wait_stats_thread()
    assert len(calls) == 1
    # 1 分钟后重试会被限频挡住
    out = scheduler.run_once(db, _ms(now + timedelta(minutes=1)))
    _wait_stats_thread()
    assert out.get("accountStatsTriggered") is None
    assert len(calls) == 1
    # 超过 10 分钟后重试放行
    out = scheduler.run_once(db, _ms(now + timedelta(minutes=11)))
    _wait_stats_thread()
    assert out.get("accountStatsTriggered") is True
    assert len(calls) == 2
