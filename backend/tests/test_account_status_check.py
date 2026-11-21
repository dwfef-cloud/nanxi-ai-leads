"""账号池登录状态检测（check-account-pool-status）契约。

背景：登录发生在壳的浏览器分区里，界面「已登录/未登录」读的是 accounts.status。
检测通道缺位时，用户在登录窗口里登录成功，账号池永远显示「未登录」。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.api import ipc_bridge
from app.runtime import browser


@pytest.fixture()
def test_account(tmp_path):
    """往（测试副本）账号库里塞一个专用账号，结束清理。"""
    con = sqlite3.connect(str(ipc_bridge.DB_PATH))
    con.execute(
        "INSERT OR REPLACE INTO accounts(id, name, nickname, platform, status, notes, "
        "created_at, updated_at) VALUES('acct-status-test', '状态检测专用', '', 'douyin', "
        "'idle', '__test__', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    con.commit()
    con.close()
    yield "acct-status-test"
    con = sqlite3.connect(str(ipc_bridge.DB_PATH))
    con.execute("DELETE FROM accounts WHERE id='acct-status-test'")
    con.commit()
    con.close()


def _patch(monkeypatch, result):
    monkeypatch.setattr(browser, "execute",
                        lambda cmd, **kw: dict(result))


def test_online_result_marks_account_active(monkeypatch, test_account):
    _patch(monkeypatch, {"ok": True, "loggedIn": True})
    out = ipc_bridge._check_account_pool_status({"accountIds": [test_account]})
    assert out["success"] and out["results"][0]["status"] == "online"
    assert out["summary"]["online"] == 1
    con = sqlite3.connect(str(ipc_bridge.DB_PATH))
    db = con.execute("SELECT status FROM accounts WHERE id=?", (test_account,)).fetchone()[0]
    con.close()
    assert db == "active"


def test_offline_result_marks_account_idle(monkeypatch, test_account):
    # 先置成 active，检测为未登录后必须回落
    from app.api import account_pool
    account_pool.set_login_status(ipc_bridge.DB_PATH, test_account, "online")
    _patch(monkeypatch, {"ok": True, "loggedIn": False, "note": "session cookie: 无"})
    out = ipc_bridge._check_account_pool_status({"accountIds": [test_account]})
    assert out["results"][0]["status"] == "offline"
    con = sqlite3.connect(str(ipc_bridge.DB_PATH))
    db = con.execute("SELECT status FROM accounts WHERE id=?", (test_account,)).fetchone()[0]
    con.close()
    assert db == "idle"


def test_shell_failure_reports_error_without_db_write(monkeypatch, test_account):
    from app.api import account_pool
    account_pool.set_login_status(ipc_bridge.DB_PATH, test_account, "online")
    _patch(monkeypatch, {"ok": False, "error": "桌面壳未运行"})
    out = ipc_bridge._check_account_pool_status({"accountIds": [test_account]})
    assert out["results"][0]["status"] == "error"
    assert "桌面壳" in out["results"][0]["message"]
    con = sqlite3.connect(str(ipc_bridge.DB_PATH))
    db = con.execute("SELECT status FROM accounts WHERE id=?", (test_account,)).fetchone()[0]
    con.close()
    assert db == "active"  # 检测失败不改库，避免把在线账号误标成离线


def test_unknown_account_reported_not_crash(monkeypatch):
    _patch(monkeypatch, {"ok": True, "loggedIn": True})
    out = ipc_bridge._check_account_pool_status({"accountIds": ["no-such-id"]})
    assert out["success"] and out["results"][0]["status"] == "error"


def test_missing_ids_rejected(monkeypatch):
    out = ipc_bridge._check_account_pool_status({})
    assert out["success"] is False and "账号" in out["error"]
