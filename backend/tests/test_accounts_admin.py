"""账号池管理：壳内保存同步、批量删除、重名/测试账号识别、状态复位。"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app.api import account_pool, ipc_bridge as bridge


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "lead_system.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, nickname TEXT,
            platform TEXT, avatar_hue INTEGER, health_score INTEGER, factors TEXT,
            limit_status TEXT, status TEXT, daily_outreach INTEGER, daily_limit INTEGER,
            daily_send_count INTEGER, risk_level TEXT, last_ban_reason TEXT,
            r1_note TEXT, r3_note TEXT, notes TEXT, created_at TEXT, updated_at TEXT,
            weight REAL, today_sent INTEGER, today_success INTEGER, profile_dir TEXT);
        CREATE TABLE leads (id TEXT PRIMARY KEY, nickname TEXT, account TEXT,
            comment TEXT, status TEXT, source_keyword TEXT, matched_keywords TEXT,
            score INTEGER, intent_level TEXT, tags TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE account_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, action TEXT, created_at TEXT);
        CREATE TABLE account_stats_daily (id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, day TEXT, total INTEGER);
        CREATE TABLE dm_send_results (id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, lead_id TEXT, created_at TEXT);
    """)
    con.executemany(
        "INSERT INTO accounts(id,name,nickname,platform,status,limit_status,"
        "health_score,daily_outreach,daily_limit,profile_dir,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [("a1", "sales-1", "sales-1", "douyin", "idle", "healthy", 100, 0, 80, "", "2026-11-13", "2026-11-13"),
         ("a2", "sales-1", "sales-1", "douyin", "idle", "warn", 100, 3, 80, "", "2026-11-13", "2026-11-13"),
         ("a3", "南溪", "南溪", "douyin", "active", "healthy", 100, 5, 80, "", "2026-09-18", "2026-09-18"),
         ("x1", "小号", "小号", "xiaohongshu", "idle", "healthy", 100, 0, 80, "", "2026-09-01", "2026-09-01")])
    con.executemany(
        "INSERT INTO leads(id,nickname,account,comment,status) VALUES(?,?,?,?,?)",
        [("l1", "张三", "南溪", "多少钱", "collected"),
         ("l2", "李四", "sales-1", "想了解", "collected")])
    con.execute("INSERT INTO account_events(account_id,action) VALUES('a2','test')")
    con.commit()
    con.close()
    return path


def test_list_marks_duplicates_and_tests(db):
    data = account_pool.list_pool(db)
    items = {it["id"]: it for it in data["items"]}
    assert items["a1"]["duplicate"] is True and items["a2"]["duplicate"] is True
    assert items["a3"]["duplicate"] is False
    assert items["a1"]["isTest"] is True          # sales-1 属测试账号
    assert items["a3"]["isTest"] is False
    assert data["summary"]["duplicateAccounts"] == 2


def test_list_filters(db):
    only_test = account_pool.list_pool(db, only="test")
    assert {it["id"] for it in only_test["items"]} == {"a1", "a2"}
    only_dup = account_pool.list_pool(db, only="duplicate")
    assert {it["id"] for it in only_dup["items"]} == {"a1", "a2"}
    by_kw = account_pool.list_pool(db, keyword="南溪")
    assert [it["id"] for it in by_kw["items"]] == ["a3"]


def test_delete_cascades_but_keeps_leads(db):
    res = account_pool.delete_accounts(db, ["a2"])
    assert res["success"] is True and res["removed"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM accounts WHERE id='a2'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM account_events WHERE account_id='a2'").fetchone()[0] == 0
    # 线索记录保留，不因删账号丢失
    assert con.execute("SELECT COUNT(*) FROM leads WHERE account='sales-1'").fetchone()[0] == 1
    con.close()


def test_delete_rejects_empty_selection(db):
    assert account_pool.delete_accounts(db, [])["success"] is False


def test_reset_status(db):
    assert account_pool.reset_status(db, ["a2"])["updated"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT limit_status FROM accounts WHERE id='a2'").fetchone()[0] == "healthy"
    con.close()


# ── 壳内 save-account-pool 同步 ─────────────────────────────
def test_sync_pool_adds_updates_and_removes(db):
    payload = [
        {"id": "a1", "name": "sales-1", "nickname": "sales-1", "platform": "douyin", "status": "online"},
        {"id": "a3", "name": "南溪", "nickname": "南溪", "platform": "douyin", "status": "online"},
        {"id": "new1", "name": "新号", "nickname": "新号", "platform": "douyin", "status": "offline"},
    ]
    res = account_pool.sync_pool(db, payload)
    assert res["success"] is True and res["added"] == 1 and res["removed"] == 1  # a2 被删
    con = sqlite3.connect(db)
    ids = {r[0] for r in con.execute("SELECT id FROM accounts")}
    assert ids == {"a1", "a3", "new1", "x1"}       # 其它平台账号不受影响
    status = con.execute("SELECT status FROM accounts WHERE id='a1'").fetchone()[0]
    assert status == "active"                       # online 映射回 active
    con.close()


def test_sync_pool_refuses_bad_payload(db):
    assert account_pool.sync_pool(db, {"accounts": []})["success"] is False


# ── HTTP 接口 ──────────────────────────────────────────────
def test_http_list_delete(db, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", db)
    with TestClient(bridge_server.app) as client:
        data = client.get("/accounts/api/list", params={"only": "test"}).json()
        assert data["success"] is True and len(data["items"]) == 2
        res = client.post("/accounts/api/delete", json={"ids": ["a2"]}).json()
        assert res["success"] is True and res["removed"] == 1
        left = client.get("/accounts/api/list").json()["items"]
        assert "a2" not in {it["id"] for it in left}


def test_accounts_page_is_served(db, monkeypatch):
    monkeypatch.setattr(bridge, "DB_PATH", db)
    with TestClient(bridge_server.app) as client:
        page = client.get("/accounts/")
        assert page.status_code == 200
        assert "账号池管理" in page.text
        assert client.get("/accounts/accounts.js").status_code == 200
