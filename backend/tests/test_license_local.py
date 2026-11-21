"""本地演示授权后端测试（离线、隔离状态文件、不碰生产库）。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app.api import ipc_bridge, license_local


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """每个用例一份独立状态文件，绝不落到 data/bridge_license.json。"""
    monkeypatch.setattr(license_local, "STATE_PATH", tmp_path / "bridge_license.json")
    yield tmp_path / "bridge_license.json"


@pytest.fixture(scope="module")
def client():
    with TestClient(bridge_server.app) as c:
        yield c


def _ipc(client, channel, args=None) -> dict:
    r = client.post(f"/ipc/{channel}", json={"args": args or []})
    assert r.status_code == 200
    packet = r.json()
    assert packet["success"] is True, packet
    return packet["data"]


def test_code_format_and_checksum(isolated_state):
    issued = license_local.issue_demo_code()
    code = issued["code"]
    assert re.fullmatch(r"LOCAL-TEST-[A-Z0-9]{10}-[A-F0-9]{4}", code)
    state = license_local.load_state()
    payload, check = code.split("-")[2], code.split("-")[3]
    assert license_local._checksum(payload, state["secret"]) == check
    assert issued["nonCommercial"] is True and issued["distributable"] is False
    assert issued["capabilitiesUnlocked"] == []


def test_normalize_and_extract_various_shapes():
    issued = license_local.issue_demo_code()
    code = issued["code"]
    assert license_local.normalize_code(code.lower()) == code
    assert license_local.normalize_code(f"  {code}  ") == code
    assert license_local.normalize_code(code.replace("-", "", 1)) == code
    assert license_local.extract_code({"licenseCode": code}) == code
    assert license_local.extract_code({"code": code, "deviceId": "x"}) == code
    assert license_local.extract_code([{"payload": {"savedCode": code}}]) == code
    assert license_local.extract_code(code) == code
    assert license_local.extract_code({}) == ""


def test_initial_state_is_unactivated(client):
    auth = _ipc(client, "check-auth")
    assert auth["entitlementVerified"] is False
    assert auth["savedCode"] == ""
    assert auth["isFree"] is True
    assert auth["licenseTierLabel"] == "本地自托管版"
    assert auth["nonCommercial"] is True
    assert auth["capabilitiesUnlocked"] == []
    assert auth["aiQuota"] == 0


def test_full_activate_flow_via_ipc(client):
    issued = license_local.issue_demo_code()
    code = issued["code"]

    result = _ipc(client, "activate", [{"code": code}])
    assert result["success"] is True
    assert result["entitlementVerified"] is True
    assert result["savedCode"] == code
    assert result["licenseTierLabel"] == "本地演示凭证（非商业授权）"
    assert result["nonCommercial"] is True and result["distributable"] is False
    assert result["capabilitiesUnlocked"] == []

    # 前端真正消费的是 check-auth；刷新页面后应读到已激活状态
    auth = _ipc(client, "check-auth")
    assert auth["entitlementVerified"] is True
    assert auth["savedCode"] == code
    assert auth["licenseTier"] == "local-demo"
    assert auth["isTrial"] is False
    assert auth["offline"] is True
    assert _ipc(client, "refresh-auth")["entitlementVerified"] is True

    status = _ipc(client, "license-status")
    assert status["active"] is True and status["activeCode"] == code
    assert status["codes"][-1]["activateCount"] == 1


def test_activate_accepts_bare_string_args(client):
    code = license_local.issue_demo_code()["code"]
    assert _ipc(client, "activate", [code])["success"] is True
    # 幂等：同一码重复激活不报错、不重复计数
    assert _ipc(client, "activate", [code])["success"] is True
    assert _ipc(client, "license-status")["codes"][-1]["activateCount"] == 1


def test_activate_via_rest(client):
    code = license_local.issue_demo_code()["code"]
    r = client.post("/license/activate", json={"code": code})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert client.get("/license/status").json()["active"] is True


def test_rest_demo_code_endpoint(client, isolated_state):
    r = client.post("/license/demo-code", json={"ttlHours": 2})
    assert r.status_code == 200
    code = r.json()["code"]
    assert license_local.CODE_RE.match(code)
    assert client.post("/license/activate", json={"code": code}).json()["success"] is True


@pytest.mark.parametrize("bad,reason", [
    ("", "未提供"),
    ("ABC-123", "格式无效"),
    ("LOCAL-TEST-ABCDEFGHIJ-0000", "校验位"),
])
def test_invalid_codes_are_rejected(client, bad, reason):
    result = license_local.activate(bad)
    assert result["success"] is False
    assert reason in result["msg"]


def test_unregistered_valid_looking_code_rejected():
    """校验位正确但未在本机登记，仍拒绝。"""
    state = license_local.load_state()
    payload = "ABCDEFGHIJ"
    forged = f"LOCAL-TEST-{payload}-{license_local._checksum(payload, state['secret'])}"
    assert license_local.activate(forged)["msg"].startswith("授权码未在本机登记")


def test_expired_code_rejected(isolated_state):
    issued = license_local.issue_demo_code(ttl_hours=1)
    state = license_local.load_state()
    record = next(r for r in state["codes"] if r["code"] == issued["code"])
    record["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    license_local.save_state(state)
    assert "过期" in license_local.activate(issued["code"])["msg"]


def test_revoke_and_reset(client):
    code = license_local.issue_demo_code()["code"]
    assert license_local.activate(code)["success"] is True
    revoked = _ipc(client, "license-revoke")
    assert revoked["success"] is True
    assert _ipc(client, "check-auth")["entitlementVerified"] is False
    assert "吊销" in license_local.activate(code)["msg"]

    assert _ipc(client, "license-reset")["success"] is True
    assert _ipc(client, "license-status")["codeCount"] == 0


def test_activation_never_unlocks_capabilities(client):
    code = license_local.issue_demo_code()["code"]
    license_local.activate(code)
    assert "activate" not in ipc_bridge.BLOCKED_CHANNELS
    # 采集 / 互动 / 自动化写入通道在激活前后都必须保持禁用
    # （save-account-pool / stop-task / stop-monitor 已接成纯本地动作，不属这一类）
    for channel in ("generate",):
        assert _ipc(client, channel, [{}])["success"] is False
        assert "功能未开通" in _ipc(client, channel, [{}])["msg"]
    # 账号统计已接线：授权不能替代桌面壳，失败原因是能力缺失而不是未开通
    collect = _ipc(client, "run-account-data-collect", [{}])
    assert collect["success"] is False
    assert "功能未开通" not in str(collect.get("reason") or collect.get("msg") or "")
    # 运行时通道已接线，但授权不能解锁它：没有任务/引擎时仍然失败，
    # 且失败原因是「缺任务 / 没引擎」，不是「功能未开通」。
    warm = _ipc(client, "start-self-warmup-task-runtime", [{}])
    assert warm["success"] is False and warm.get("error")
    assert "功能未开通" not in str(warm.get("error"))
    # open-chat-view 只做窗口状态管理：改为离线视图后放行，但不内嵌平台页面、
    # 不解锁任何能力（与「激活不解锁能力」并不矛盾）。
    view = _ipc(client, "open-chat-view", [{}])
    assert view["success"] is True and view["mode"] == "iframe-embed"
    assert "不做代操作" in view["message"]
    auth = _ipc(client, "check-auth")
    assert auth["capabilitiesUnlocked"] == []
    assert auth["aiQuota"] == 0


def test_state_file_isolated_from_production_db(isolated_state):
    license_local.issue_demo_code()
    license_local.activate(license_local.load_state()["codes"][-1]["code"])
    assert isolated_state.is_file()
    raw = json.loads(isolated_state.read_text(encoding="utf-8"))
    assert raw["schema"] == 1 and "非商业授权" in raw["notice"]
    # 生产库不应出现任何授权表
    db = ipc_bridge.DB_PATH
    import sqlite3
    with sqlite3.connect(str(db)) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("license" in n for n in names)


def test_all_demo_channels_still_answer(client):
    """新增授权通道不能打破原 94 通道契约。"""
    text = (ipc_bridge.RENDERER_DIR / "demo-bridge.js").read_text(encoding="utf-8")
    channels = sorted(set(re.findall(r'^\s*"([a-z0-9][a-z0-9\-]{2,60})"\s*:', text, re.M)))
    assert len(channels) >= 90
    for ch in channels:
        packet = client.post(f"/ipc/{ch}", json={"args": []}).json()
        assert packet["success"] is True, (ch, packet)
