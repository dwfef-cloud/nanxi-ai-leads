"""后台控制台回归测试。

注意：admin 模块在 import 时读取路径环境变量，因此本文件在导入前设置临时路径，
不影响其他测试与真实数据。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="admin-test-"))
os.environ["ADMIN_DB_PATH"] = str(_TMP / "admin.db")
os.environ["ADMIN_CRED_FILE"] = str(_TMP / "cred.json")
os.environ["ADMIN_SECRET_FILE"] = str(_TMP / "secret.key")
os.environ["ADMIN_INITIAL_PW_FILE"] = str(_TMP / "pw.txt")
os.environ.pop("ADMIN_PASSWORD", None)
os.environ.pop("ADMIN_LICENSE_SECRET", None)

# admin 子包在 import 时解析路径常量。若别的测试模块（如桥接测试）先导入了它，
# 这里的临时路径会失效并误连真实后台库。因此先清缓存再导入，保证测试隔离。
for _name in [n for n in list(sys.modules) if n == "app.admin" or n.startswith("app.admin.")]:
    del sys.modules[_name]

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.admin import bootstrap, keys  # noqa: E402
from app.admin.api import router  # noqa: E402

app = FastAPI()
app.include_router(router)
client = TestClient(app)

PW = bootstrap()
HEADERS = {"X-Admin-Token": client.post("/admin/api/login", json={"password": PW}).json()["token"]}


# ---------------- 密钥引擎 ----------------
def test_key_roundtrip_and_tamper():
    code = keys.make_code("pro", keys.expiry_from_days(365))
    v = keys.verify_code(code)
    assert v["valid"] and v["plan"] == "pro" and v["expiresAt"]
    bad = code[:-1] + ("A" if code[-1] != "A" else "B")
    assert not keys.verify_code(bad)["valid"]
    assert "签名" in keys.verify_code(bad)["reason"]


def test_key_perpetual_and_garbage():
    code = keys.make_code("standard", None)
    v = keys.verify_code(code)
    assert v["valid"] and v["expiresAt"] is None
    assert not keys.verify_code("LOCAL-TEST-AAAA-BBBB")["valid"]
    assert not keys.verify_code("")["valid"]


def test_key_uniqueness():
    codes = {keys.make_code("basic", keys.expiry_from_days(30)) for _ in range(50)}
    assert len(codes) == 50


# ---------------- 鉴权 ----------------
def test_login_required_and_wrong_password():
    assert client.get("/admin/api/overview").status_code == 401
    r = client.post("/admin/api/login", json={"password": "wrong-password"})
    assert r.status_code == 401


def test_password_change_flow():
    r = client.post("/admin/api/password", headers=HEADERS,
                    json={"oldPassword": "surely-wrong", "newPassword": "new-pass-12345"})
    assert r.status_code == 400
    r = client.post("/admin/api/password", headers=HEADERS,
                    json={"oldPassword": PW, "newPassword": "brand-new-pw-9"})
    assert r.status_code == 200
    # 旧会话应已失效
    assert client.get("/admin/api/overview", headers=HEADERS).status_code == 401
    # 用新口令重新登录，更新共享令牌
    globals()["PW"] = "brand-new-pw-9"
    HEADERS["X-Admin-Token"] = client.post(
        "/admin/api/login", json={"password": "brand-new-pw-9"}
    ).json()["token"]


# ---------------- 密钥生命周期 ----------------
def test_generate_and_list():
    r = client.post("/admin/api/licenses", headers=HEADERS,
                    json={"plan": "pro", "count": 3, "validDays": 365,
                          "dailyQuota": 500, "customerName": "测试客户A"})
    assert r.status_code == 200
    codes = r.json()["codes"]
    assert len(codes) == 3 and len(set(codes)) == 3
    r = client.get("/admin/api/licenses", headers=HEADERS,
                   params={"keyword": "测试客户A"})
    assert r.json()["total"] == 3
    globals()["CODES"] = codes


def test_activate_binds_machine():
    code = CODES[0]
    r = client.post("/admin/api/activate", json={
        "licenseCode": code, "machineId": "MACH-1", "hostname": "PC-1",
        "appVersion": "1.0.0"})
    body = r.json()
    assert body["ok"] and body["status"] == "active" and body["plan"] == "pro"
    r = client.post("/admin/api/activate", json={"licenseCode": code, "machineId": "MACH-2"})
    assert r.json()["status"] == "bound_elsewhere"


def test_heartbeat_accumulates_and_reports():
    code = CODES[0]
    r = client.post("/admin/api/heartbeat", json={
        "licenseCode": code, "machineId": "MACH-1",
        "usage": {"leads": 10, "comments": 5, "dms": 2, "logins": 1, "actions": 18}})
    body = r.json()
    assert body["ok"] and body["usedToday"] == 18
    r = client.post("/admin/api/heartbeat", json={
        "licenseCode": code, "machineId": "MACH-1",
        "usage": {"actions": 7}})
    assert r.json()["usedToday"] == 25
    ov = client.get("/admin/api/overview", headers=HEADERS).json()
    assert ov["today"]["actions"] == 25
    assert ov["instances"]["total"] >= 1
    assert ov["licenses"]["active"] >= 1


def test_revoke_stops_client():
    code = CODES[1]
    client.patch(f"/admin/api/licenses/{code}", headers=HEADERS, json={"status": "revoked"})
    r = client.post("/admin/api/heartbeat", json={"licenseCode": code, "machineId": "MACH-9"})
    assert r.json()["stop"] is True
    # 解绑 + 恢复
    client.patch(f"/admin/api/licenses/{code}", headers=HEADERS,
                 json={"status": "active", "unbind": True})
    r = client.post("/admin/api/activate",
                    json={"licenseCode": code, "machineId": "MACH-9"})
    assert r.json()["ok"]


def test_extend_and_expire_marking():
    code = CODES[2]
    r = client.patch(f"/admin/api/licenses/{code}", headers=HEADERS,
                     json={"extendDays": 30})
    assert r.status_code == 200 and r.json()["license"]["expiresAt"]
    # 直接把到期时间改成过去 → overview 应标记 expired
    client.patch(f"/admin/api/licenses/{code}", headers=HEADERS,
                 json={"status": "active"})
    from app.admin.store import AdminStore
    store = AdminStore()
    store.update_license(code, {"expires_at": "2026-01-02T00:00:00+00:00"})
    client.get("/admin/api/overview", headers=HEADERS)
    lic = store.get_license(code)
    assert lic["status"] == "expired"


def test_delete_and_audit():
    code = CODES[2]
    assert client.delete(f"/admin/api/licenses/{code}", headers=HEADERS).json()["ok"]
    assert client.get("/admin/api/licenses", headers=HEADERS,
                      params={"keyword": code}).json()["total"] == 0
    audit = client.get("/admin/api/audit", headers=HEADERS).json()["items"]
    actions = {a["action"] for a in audit}
    assert {"生成密钥", "激活绑定", "修改密钥", "删除密钥"} <= actions


def test_export_csv():
    r = client.get("/admin/api/export/licenses.csv", headers=HEADERS)
    assert r.status_code == 200
    text = r.content.decode("utf-8")
    assert text.startswith("\ufeff") and "密钥" in text


# ---------------- 壳激活通道 × 发行方密钥（ALS 桥接） ----------------
def _bridge_env(monkeypatch, tmp_path):
    """隔离桥接状态文件并清空复核节流缓存。"""
    from app.api import license_local as ll
    monkeypatch.setattr(ll, "STATE_PATH", tmp_path / "bridge_license.json")
    monkeypatch.setattr(ll, "_LAST_RECHECK", {"t": 0.0, "ok": True, "plan": ""})
    return ll


def test_shell_accepts_admin_issued_code(monkeypatch, tmp_path):
    ll = _bridge_env(monkeypatch, tmp_path)
    from app.admin.store import AdminStore
    store = AdminStore()
    code = keys.make_code("pro", None)
    store.insert_license({"code": code, "plan": "pro", "customer_name": "壳桥接测试"})

    r = ll.activate(code)
    assert r["success"] is True, r.get("msg")
    assert "激活成功" in r["msg"]
    ov = ll.auth_overlay()
    assert ov["entitlementVerified"] is True
    assert ov["savedCode"] == code
    assert "已激活" in ov["activationLabel"]


def test_shell_rejects_forged_admin_code(monkeypatch, tmp_path):
    ll = _bridge_env(monkeypatch, tmp_path)
    from app.admin.store import AdminStore
    store = AdminStore()
    code = keys.make_code("basic", None)
    store.insert_license({"code": code, "plan": "basic"})
    bad = code[:-1] + ("A" if code[-1] != "A" else "B")

    r = ll.activate(bad)
    assert r["success"] is False
    assert "签名" in r["msg"]


def test_shell_reflects_revocation_immediately(monkeypatch, tmp_path):
    ll = _bridge_env(monkeypatch, tmp_path)
    from app.admin.store import AdminStore
    store = AdminStore()
    code = keys.make_code("standard", None)
    store.insert_license({"code": code, "plan": "standard"})

    assert ll.activate(code)["success"] is True
    assert ll.auth_overlay()["entitlementVerified"] is True

    store.update_license(code, {"status": "revoked"})
    ll._LAST_RECHECK.update({"t": 0.0})  # 绕过节流，强制真实复核
    assert ll.auth_overlay()["entitlementVerified"] is False
    r = ll.activate(code)
    assert r["success"] is False and "吊销" in r["msg"]


def test_local_demo_code_still_works(monkeypatch, tmp_path):
    """LOCAL-TEST 演示码链路不受桥接影响。"""
    ll = _bridge_env(monkeypatch, tmp_path)
    demo = ll.issue_demo_code(ttl_hours=1)["code"]
    assert ll.activate(demo)["success"] is True
    assert ll.auth_overlay()["entitlementVerified"] is True


# ---------------- 壳激活通道 × 发行方密钥（ALS 桥接） ----------------
def _bridge_env(monkeypatch, tmp_path):
    """隔离桥接状态文件并清空复核节流缓存。"""
    from app.api import license_local as ll
    monkeypatch.setattr(ll, "STATE_PATH", tmp_path / "bridge_license.json")
    monkeypatch.setattr(ll, "_LAST_RECHECK", {"t": 0.0, "ok": True, "plan": ""})
    return ll


def test_shell_accepts_admin_issued_code(monkeypatch, tmp_path):
    ll = _bridge_env(monkeypatch, tmp_path)
    from app.admin.store import AdminStore
    store = AdminStore()
    code = keys.make_code("pro", None)
    store.insert_license({"code": code, "plan": "pro", "customer_name": "壳桥接测试"})

    r = ll.activate(code)
    assert r["success"] is True, r.get("msg")
    assert "激活成功" in r["msg"]
    ov = ll.auth_overlay()
    assert ov["entitlementVerified"] is True
    assert ov["savedCode"] == code
    assert "已激活" in ov["activationLabel"]


def test_shell_rejects_forged_admin_code(monkeypatch, tmp_path):
    ll = _bridge_env(monkeypatch, tmp_path)
    from app.admin.store import AdminStore
    store = AdminStore()
    code = keys.make_code("basic", None)
    store.insert_license({"code": code, "plan": "basic"})
    bad = code[:-1] + ("A" if code[-1] != "A" else "B")

    r = ll.activate(bad)
    assert r["success"] is False
    assert "签名" in r["msg"]


def test_shell_reflects_revocation_immediately(monkeypatch, tmp_path):
    ll = _bridge_env(monkeypatch, tmp_path)
    from app.admin.store import AdminStore
    store = AdminStore()
    code = keys.make_code("standard", None)
    store.insert_license({"code": code, "plan": "standard"})

    assert ll.activate(code)["success"] is True
    assert ll.auth_overlay()["entitlementVerified"] is True

    store.update_license(code, {"status": "revoked"})
    ll._LAST_RECHECK.update({"t": 0.0})  # 绕过节流，强制真实复核
    assert ll.auth_overlay()["entitlementVerified"] is False
    r = ll.activate(code)
    assert r["success"] is False and "吊销" in r["msg"]


def test_local_demo_code_still_works(monkeypatch, tmp_path):
    """LOCAL-TEST 演示码链路不受桥接影响。"""
    ll = _bridge_env(monkeypatch, tmp_path)
    demo = ll.issue_demo_code(ttl_hours=1)["code"]
    assert ll.activate(demo)["success"] is True
    assert ll.auth_overlay()["entitlementVerified"] is True
