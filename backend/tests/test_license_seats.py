"""授权席位折算契约（多账号登录被「每平台最多 1 个」卡死的回归）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.api import ipc_bridge


@pytest.fixture()
def license_state(tmp_path, monkeypatch):
    """把授权状态文件指到临时目录，返回可写状态对象。"""
    from app.api import license_local

    path = tmp_path / "bridge_license.json"
    monkeypatch.setenv("BRIDGE_LICENSE_STATE", str(path))
    monkeypatch.delenv("BRIDGE_LICENSE_SEATS", raising=False)
    state = license_local._new_state()
    license_local.save_state(state)
    return state


def _activate(state, code, *, plan="", status="used", expires_days=30):
    from app.api import license_local

    record = {
        "code": code, "status": status, "plan": plan,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat(),
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "activate_count": 1, "note": "test",
    }
    state["codes"] = [r for r in state["codes"] if r.get("code") != code]
    state["codes"].append(record)
    state["active_code"] = code
    license_local.save_state(state)  # 不落盘的话 _license_entitlement 读不到
    return record


def test_demo_license_one_active_code_one_seat(license_state):
    """演示码：激活 1 枚 = 1 席（保底 1，和旧口径兼容）。"""
    _activate(license_state, "LOCAL-TEST-AAAAAAAAAA-0000")
    out = ipc_bridge._license_entitlement({})
    assert out["success"] and out["seats"] >= 1
    assert out["maxConcurrentAccounts"] == max(out["seats"], 1)
    assert out["licenseTier"].startswith("local")


def test_demo_license_counts_activated_codes(license_state):
    """演示码：先后激活 2 枚（第一枚保持 used）→ 2 席。"""
    _activate(license_state, "LOCAL-TEST-AAAAAAAAAA-0000")
    _activate(license_state, "LOCAL-TEST-BBBBBBBBBB-0000")
    out = ipc_bridge._license_entitlement({})
    assert out["seats"] == 2


def test_demo_license_expired_or_revoked_do_not_count(license_state):
    """过期 / 吊销的码不折算席位。"""
    _activate(license_state, "LOCAL-TEST-AAAAAAAAAA-0000", status="revoked")
    _activate(license_state, "LOCAL-TEST-BBBBBBBBBB-0000", expires_days=-1)
    out = ipc_bridge._license_entitlement({})
    assert out["seats"] == 0 or out["maxConcurrentAccounts"] == 1  # 无有效激活 → 保底 1


def test_commercial_license_reads_admin_seats(license_state, monkeypatch, tmp_path):
    """商业码：席位按发行方后台登记的 seats 折算，不再写死 1。"""
    code = "ALS-S2PB-9LQ2S67C-H4X6"
    _activate(license_state, code, plan="standard")

    class FakeStore:
        def get_license(self, c):
            return {"code": c, "seats": 10, "status": "active"} if c == code else None

    import app.admin.store as admin_store
    monkeypatch.setattr(admin_store, "AdminStore", FakeStore)
    out = ipc_bridge._license_entitlement({})
    assert out["seats"] == 10
    assert out["maxConcurrentAccounts"] == 10
    assert out["licenseTier"] == "commercial"


def test_env_override_can_raise_seats(license_state, monkeypatch):
    """联调开关：BRIDGE_LICENSE_SEATS 临时抬高席位，优先级最高。"""
    monkeypatch.setenv("BRIDGE_LICENSE_SEATS", "5")
    _activate(license_state, "LOCAL-TEST-AAAAAAAAAA-0000")
    out = ipc_bridge._license_entitlement({})
    assert out["seats"] == 5


def test_missing_admin_store_falls_back_to_one_seat(license_state, monkeypatch):
    """后台库不可用（换机部署）时按 1 席兜底，不抛异常阻断账号页。"""
    code = "ALS-S2PB-9LQ2S67C-H4X6"
    _activate(license_state, code, plan="standard")

    class BrokenStore:
        def __init__(self, *a, **k):
            raise RuntimeError("no admin db")

    import app.admin.store as admin_store
    monkeypatch.setattr(admin_store, "AdminStore", BrokenStore)
    out = ipc_bridge._license_entitlement({})
    assert out["success"] and out["seats"] == 1
