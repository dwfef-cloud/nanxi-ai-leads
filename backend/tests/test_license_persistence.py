"""激活态持久性：激活后必须跨刷新 / 跨重启保持（DESIGN-DOC §3 授权展示）。

回归的三个真实故障：
1. 同一枚码二次激活时旧实现不写盘，本地记录留着 revoked → 刷新即变回未激活；
2. 复核把「后台库查不到 / 打不开」当成吊销，永久写死 revoked；
3. 复核一旦判定失败就再没有恢复路径。
"""
from __future__ import annotations

import json

import pytest

from app.admin import keys
from app.admin.store import AdminStore


@pytest.fixture()
def ll(monkeypatch, tmp_path):
    from app.api import license_local

    monkeypatch.setattr(license_local, "STATE_PATH", tmp_path / "bridge_license.json")
    monkeypatch.setattr(license_local, "_LAST_RECHECK", {"t": 0.0, "verdict": "", "plan": ""})
    return license_local


def _issue(plan: str = "pro", **extra) -> str:
    code = keys.make_code(plan, None)
    AdminStore().insert_license({"code": code, "plan": plan, **extra})
    return code


def test_activation_is_written_to_disk_and_survives_reload(ll, tmp_path):
    """激活后磁盘状态文件里必须是 used + active_code，重读仍是已激活。"""
    code = _issue("standard")
    assert ll.activate(code)["success"] is True

    raw = json.loads((tmp_path / "bridge_license.json").read_text(encoding="utf-8"))
    assert raw["active_code"] == code
    rec = next(r for r in raw["codes"] if r["code"] == code)
    assert rec["status"] == "used"
    assert rec["plan"] == "standard"

    # 模拟重启：丢掉内存里的节流缓存，只靠状态文件重建授权态
    ll._LAST_RECHECK = {"t": 0.0, "verdict": "", "plan": ""}
    ov = ll.auth_overlay()
    assert ov["entitlementVerified"] is True
    assert ov["savedCode"] == code
    assert "标准版" in ov["activationLabel"]


def test_reactivating_same_code_repairs_sticky_revoked(ll, monkeypatch):
    """历史版本留下的 revoked 记录，再次激活同一枚码必须恢复为已激活。"""
    code = _issue("pro")
    assert ll.activate(code)["success"] is True

    # 复现旧 bug：本地记录被误判写死 revoked，且复核拿不到结论（无法自愈）
    real_recheck = ll._admin_recheck
    monkeypatch.setattr(ll, "_admin_recheck", lambda *_a, **_k: ("unknown", "后台暂不可达", ""))
    state = ll.load_state()
    for rec in state["codes"]:
        if rec["code"] == code:
            rec["status"] = "revoked"
    ll.save_state(state)
    assert ll.auth_overlay()["entitlementVerified"] is False

    # 用户重新点激活：必须把记录改写回 used，而不是只回一句"激活成功"
    monkeypatch.setattr(ll, "_admin_recheck", real_recheck)
    ll._LAST_RECHECK = {"t": 0.0, "verdict": "", "plan": ""}
    again = ll.activate(code)
    assert again["success"] is True
    assert ll.auth_overlay()["entitlementVerified"] is True

    rec = next(r for r in ll.load_state()["codes"] if r["code"] == code)
    assert rec["status"] == "used"
    assert rec["activate_count"] >= 2


def test_sticky_revoked_self_heals_on_plain_check(ll, tmp_path):
    """即使不再点激活，只要发行方明确有效，check-auth 也应自愈回已激活。"""
    code = _issue("basic")
    assert ll.activate(code)["success"] is True

    state = ll.load_state()
    for rec in state["codes"]:
        if rec["code"] == code:
            rec["status"] = "revoked"
    ll.save_state(state)
    ll._LAST_RECHECK = {"t": 0.0, "verdict": "", "plan": ""}

    assert ll.auth_overlay()["entitlementVerified"] is True


def test_unknown_recheck_does_not_drop_activation(ll, monkeypatch):
    """后台库打不开 / 网络不通属于「无法判定」，激活态必须原样保持。"""
    code = _issue("pro")
    assert ll.activate(code)["success"] is True

    def boom(*_a, **_k):
        raise RuntimeError("admin db locked")

    monkeypatch.setattr(AdminStore, "get_license", boom)
    ll._LAST_RECHECK = {"t": 0.0, "verdict": "", "plan": ""}

    ov = ll.auth_overlay()
    assert ov["entitlementVerified"] is True
    assert ov["savedCode"] == code


def test_deleted_license_row_keeps_activation(ll, monkeypatch):
    """库里查不到码（后台库重建 / 误删）→ 无法判定，不能当成吊销。"""
    code = _issue("pro")
    assert ll.activate(code)["success"] is True

    monkeypatch.setattr(AdminStore, "get_license", lambda self, c: None)
    ll._LAST_RECHECK = {"t": 0.0, "verdict": "", "plan": ""}
    assert ll.auth_overlay()["entitlementVerified"] is True


def test_definitive_revocation_still_demotes(ll):
    """发行方明确吊销时必须降级——「保持激活」不等于取消失效能力。"""
    code = _issue("standard")
    assert ll.activate(code)["success"] is True

    AdminStore().update_license(code, {"status": "revoked"})
    ll._LAST_RECHECK = {"t": 0.0, "verdict": "", "plan": ""}
    assert ll.auth_overlay()["entitlementVerified"] is False


def test_activation_marks_admin_license_active(ll):
    """壳激活后后台密钥与实例状态同步，避免后台仍显示 unused。"""
    code = _issue("pro")
    assert ll.activate(code)["success"] is True

    lic = AdminStore().get_license(code)
    assert lic["status"] == "active"
    assert lic["activated_at"]
    assert lic["bound_machine"].startswith("SHELL-")
