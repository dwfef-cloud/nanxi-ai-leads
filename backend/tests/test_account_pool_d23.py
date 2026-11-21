"""D23 账号池 v2 测试：状态机、风控事件、租约联动、REST。

验收标准（DESIGN-DOC D23）：模拟崩溃 30s 后租约自动回收（D8 已测，
此处验证与账号池过滤联动）+ 状态机全迁移。
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.engagement.account_pool import (
    STATE_COOLING,
    STATE_HEALTHY,
    STATE_RISK_WARNING,
    STATE_SUSPENDED,
    AccountPool,
)
from app.engagement.engine import EngineManager


@pytest.fixture()
def pool(tmp_path):
    return AccountPool(tmp_path / "pool.db")


class TestStateMachine:
    def test_新账号默认healthy(self, pool):
        assert pool.state_of("a") == STATE_HEALTHY

    def test_验证码事件触发2h冷却_到期自动恢复(self, pool):
        now = 1_000_000
        r = pool.report_event("a", "captcha", now=now)
        assert r["state"] == STATE_COOLING
        assert r["cooling_until"] == now + 2 * 3600
        # 未到期仍 cooling；到期读取即恢复 healthy
        assert pool.state_of("a", now=now + 3600) == STATE_COOLING
        assert pool.state_of("a", now=now + 2 * 3600) == STATE_HEALTHY

    def test_403事件触发4h冷却(self, pool):
        now = 1_000_000
        r = pool.report_event("a", "send_fail_403", now=now)
        assert r["state"] == STATE_COOLING
        assert r["cooling_until"] == now + 4 * 3600

    def test_uncertain积分累计_3次进risk_warning(self, pool):
        now = 1_000_000
        assert pool.report_event("a", "send_uncertain", now=now)["risk_points"] == 1
        assert pool.report_event("a", "send_uncertain", now=now + 1)["state"] == STATE_HEALTHY
        r3 = pool.report_event("a", "send_uncertain", now=now + 2)
        assert r3["state"] == STATE_RISK_WARNING and r3["risk_points"] == 3

    def test_警告期再次事件直接suspended(self, pool):
        now = 1_000_000
        for i in range(3):
            pool.report_event("a", "send_uncertain", now=now + i)
        r = pool.report_event("a", "send_uncertain", now=now + 3)
        assert r["state"] == STATE_SUSPENDED
        assert "再次" in r["suspend_reason"]
        # suspended 不会自动解除
        assert pool.state_of("a", now=now + 999_999) == STATE_SUSPENDED

    def test_suspended仅人工恢复_恢复后积分清零(self, pool):
        now = 1_000_000
        for i in range(4):
            pool.report_event("a", "send_uncertain", now=now + i)
        r = pool.report_event("a", "manual_recover", now=now + 10)
        assert r["state"] == STATE_HEALTHY and r["risk_points"] == 0

    def test_人工暂停与恢复(self, pool):
        now = 1_000_000
        r = pool.report_event("a", "manual_suspend", now=now, detail="违规操作")
        assert r["state"] == STATE_SUSPENDED and "违规" in r["suspend_reason"]
        assert pool.report_event("a", "manual_recover", now=now + 1)["state"] == STATE_HEALTHY

    def test_未知事件类型拒绝(self, pool):
        with pytest.raises(ValueError):
            pool.report_event("a", "boom")


class TestPoolFilter:
    def test_pick_available只返回healthy(self, pool):
        now = 1_000_000
        pool.report_event("b", "captcha", now=now)          # cooling
        for i in range(4):
            pool.report_event("c", "send_uncertain", now=now + i)  # suspended
        got = pool.pick_available(["a", "b", "c", "d"], now=now + 60)
        assert set(got) == {"a", "d"}

    def test_states视图与事件流水(self, pool):
        now = 1_000_000
        pool.report_event("a", "captcha", now=now, detail="评论时弹码")
        states = {s["account_id"]: s for s in pool.states(now=now)}
        assert states["a"]["state"] == STATE_COOLING
        evs = pool.events_of("a")
        assert evs[0]["kind"] == "captcha" and evs[0]["detail"] == "评论时弹码"


class TestLeaseLinkage:
    def test_崩溃30s租约回收后账号可重新调度(self, tmp_path):
        """D23 验收：模拟崩溃 30s 后租约自动回收 → pick 过滤 + 引擎重租。"""
        pool = AccountPool(tmp_path / "pool.db")
        engine = EngineManager(tmp_path / "profiles")
        now = int(time.time())
        engine.acquire(1, now=now)
        engine.heartbeat(1, now=now + 29)
        # 最后心跳 +30s 无续期（模拟崩溃）→ 租约回收
        assert engine.lease_of(1, now=now + 59) is not None
        assert engine.lease_of(1, now=now + 60) is None
        # 账号状态 healthy → 可被重新租约调度
        assert pool.state_of("1", now=now + 60) == STATE_HEALTHY
        assert pool.pick_available(["1"], now=now + 60) == ["1"]
        engine.acquire(1, now=now + 61)
        assert engine.lease_of(1, now=now + 62) is not None

    def test_cooling账号租约存在也不参与调度(self, tmp_path):
        pool = AccountPool(tmp_path / "pool.db")
        engine = EngineManager(tmp_path / "profiles")
        now = int(time.time())
        pool.report_event("2", "captcha", now=now)
        engine.acquire(2, now=now)
        assert pool.pick_available(["2"], now=now + 1) == []  # cooling 优先排除


class TestRestApi:
    def test_pool_endpoints(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ACCOUNT_POOL_DB", str(tmp_path / "api.db"))
        from app.engagement import pool_api
        app = FastAPI()
        app.include_router(pool_api.pool_router)
        client = TestClient(app)
        P = "/api/engagement/pool"
        # 上报事件
        r = client.post(f"{P}/acct-1/events",
                        json={"kind": "captcha", "detail": "t"}).json()
        assert r["success"] and r["state"] == STATE_COOLING
        # 列表视图
        r = client.get(P).json()
        assert r["accounts"][0]["account_id"] == "acct-1"
        # 单账号（存在）
        assert client.get(f"{P}/acct-1").json()["state"] == STATE_COOLING
        # 单账号（不存在）→ 404
        assert client.get(f"{P}/nope").status_code == 404
        # 非法事件类型 → 422
        assert client.post(f"{P}/acct-1/events", json={"kind": "boom"}).status_code == 422
