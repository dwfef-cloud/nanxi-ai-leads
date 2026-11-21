"""D26 管理面收尾 · 执行侧仪表盘（漏斗 / 合规护栏）测试

覆盖：
1. 数据源缺失时的容错：200 + 全 0 + ready=False，不伪造
2. 有数据时的口径：候选→入池→评论→私信 逐级计数、成本只认 ad_spend
3. 合规护栏：配额账本、DM 官方阈值、账号池状态机、编排器阻断、养号
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.execution_dashboard import execution_dashboard_router


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(execution_dashboard_router)
    return TestClient(app)


def _init_execution_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE lead_candidates (
          id INTEGER PRIMARY KEY AUTOINCREMENT, video_url TEXT NOT NULL, nickname TEXT,
          comment_text TEXT, comment_text_hash TEXT NOT NULL, intent_score INTEGER,
          intent_tags TEXT, source_keyword TEXT, created_at INTEGER NOT NULL, expire_at INTEGER NOT NULL);
        CREATE TABLE pipeline_runs (
          id TEXT PRIMARY KEY, stage TEXT NOT NULL, account_id INTEGER, stats TEXT,
          started_at INTEGER NOT NULL, finished_at INTEGER);
        CREATE TABLE pipeline_progress (
          run_id TEXT NOT NULL, stage TEXT NOT NULL, item_key TEXT NOT NULL,
          result TEXT NOT NULL DEFAULT '', done_at INTEGER NOT NULL,
          PRIMARY KEY (run_id, stage, item_key));
        CREATE TABLE comment_send_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, candidate_id INTEGER,
          content TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE account_quotas (
          account_id INTEGER NOT NULL, day TEXT NOT NULL, metric TEXT NOT NULL,
          baseline INTEGER NOT NULL DEFAULT 0, snapshot INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(account_id, day, metric));
        CREATE TABLE nurture_sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER,
          videos_watched INTEGER NOT NULL DEFAULT 0, likes INTEGER NOT NULL DEFAULT 0,
          favorites INTEGER NOT NULL DEFAULT 0, shares INTEGER NOT NULL DEFAULT 0,
          comments_browsed INTEGER NOT NULL DEFAULT 0,
          started_at INTEGER NOT NULL, ended_at INTEGER);
        """
    )
    conn.commit()
    conn.close()


def _init_pool_db(path, states: dict[str, int], risk_events: int = 0) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE account_pool_states (
          account_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'healthy',
          risk_points INTEGER NOT NULL DEFAULT 0, cooling_until INTEGER,
          suspend_reason TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL);
        CREATE TABLE account_risk_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
          kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', happened_at INTEGER NOT NULL);
        """
    )
    now = int(time.time())
    for state, count in states.items():
        for i in range(count):
            conn.execute(
                "INSERT INTO account_pool_states(account_id, state, updated_at) VALUES (?,?,?)",
                (f"{state}-{i}", state, now),
            )
    for i in range(risk_events):
        conn.execute(
            "INSERT INTO account_risk_events(account_id, kind, happened_at) VALUES (?,?,?)",
            ("a1", "captcha", now - 60),
        )
    conn.commit()
    conn.close()


def _init_main_db(path, *, wechat: int, deals: int, replies: int, spend: float) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE leads (
          id TEXT PRIMARY KEY, status TEXT, created_at TEXT, deal_amount REAL,
          wechat_added_at TEXT);
        CREATE TABLE messages (
          id TEXT PRIMARY KEY, direction TEXT, created_at TEXT);
        CREATE TABLE dm_send_results (
          id TEXT PRIMARY KEY, status TEXT, created_at TEXT);
        CREATE TABLE compliance_events (
          id TEXT PRIMARY KEY, type TEXT, level TEXT, text TEXT, time TEXT, created_at TEXT);
        CREATE TABLE ad_spend (
          id TEXT PRIMARY KEY, date TEXT, platform TEXT, amount REAL, note TEXT, created_at TEXT);
        """
    )
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    for i in range(wechat):
        conn.execute("INSERT INTO leads VALUES (?,?,?,?,?)",
                     (f"w{i}", "wechat_added", now_iso, 0, now_iso))
    for i in range(deals):
        conn.execute("INSERT INTO leads VALUES (?,?,?,?,?)",
                     (f"d{i}", "deal_won", now_iso, 5000, now_iso))
    for i in range(replies):
        conn.execute("INSERT INTO messages VALUES (?,?,?)", (f"m{i}", "in", now_iso))
    conn.execute("INSERT INTO dm_send_results VALUES (?,?,?)", ("dm1", "success", now_iso))
    conn.execute("INSERT INTO dm_send_results VALUES (?,?,?)", ("dm2", "failed", now_iso))
    conn.execute(
        "INSERT INTO compliance_events VALUES (?,?,?,?,?,?)",
        ("e1", "config_change", "info", "配置变更", "11-13 10:00", now_iso),
    )
    conn.execute("INSERT INTO ad_spend VALUES (?,?,?,?,?,?)",
                 ("s1", time.strftime("%Y-%m-%d"), "douyin", spend, "", now_iso))
    conn.commit()
    conn.close()


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    exec_db = tmp_path / "execution.db"
    pool_db = tmp_path / "account_pool.db"
    dm_db = tmp_path / "dm_guard.db"
    main_db = tmp_path / "lead_system.db"
    monkeypatch.setenv("EXECUTION_DB_PATH", str(exec_db))
    monkeypatch.setenv("ACCOUNT_POOL_DB", str(pool_db))
    monkeypatch.setenv("DM_GUARD_DB", str(dm_db))
    monkeypatch.setenv("DB_PATH", str(main_db))
    return {"exec": exec_db, "pool": pool_db, "dm": dm_db, "main": main_db}


# ═══════════════════════════════════════════════════════════
# 1. 容错
# ═══════════════════════════════════════════════════════════

def test_funnel_tolerates_missing_databases(client: TestClient, isolated) -> None:
    res = client.get("/api/dashboard/execution-funnel")
    assert res.status_code == 200
    data = res.json()
    assert [s["value"] for s in data["stages"]] == [0, 0, 0, 0, 0, 0]
    assert data["sources"]["execution_db"]["ready"] is False
    assert data["sources"]["main_db"]["ready"] is False
    assert data["cost"]["totalSpend"] == 0
    assert data["cost"]["basis"].startswith("未录入")


def test_compliance_tolerates_missing_databases(client: TestClient, isolated) -> None:
    res = client.get("/api/dashboard/execution-compliance")
    assert res.status_code == 200
    data = res.json()
    assert data["quota"]["ready"] is False
    assert data["dm_guard"]["ready"] is False
    assert data["pool"]["ready"] is False
    # 官方阈值即使未接线也必须照实展示（写死值不可配置）
    assert data["dm_guard"]["limits"]["per_recipient"] == 3
    assert data["dm_guard"]["limits"]["hourly_recipients"] == 40
    assert data["dm_guard"]["limits"]["daily_recipients"] == 100
    assert data["dm_guard"]["limits"]["session_window_limit"] == 6


# ═══════════════════════════════════════════════════════════
# 2. 漏斗口径
# ═══════════════════════════════════════════════════════════

def test_funnel_counts_real_rows(client: TestClient, isolated) -> None:
    _init_execution_db(isolated["exec"])
    _init_main_db(isolated["main"], wechat=4, deals=2, replies=3, spend=900.0)

    now = int(time.time())
    conn = sqlite3.connect(isolated["exec"])
    # 3 条候选：2 条达 70 分阈值
    conn.execute("INSERT INTO lead_candidates(video_url, comment_text_hash, intent_score, created_at, expire_at)"
                 " VALUES (?,?,?,?,?)", ("v1", "h1", 88, now, now + 100))
    conn.execute("INSERT INTO lead_candidates(video_url, comment_text_hash, intent_score, created_at, expire_at)"
                 " VALUES (?,?,?,?,?)", ("v1", "h2", 72, now, now + 100))
    conn.execute("INSERT INTO lead_candidates(video_url, comment_text_hash, intent_score, created_at, expire_at)"
                 " VALUES (?,?,?,?,?)", ("v2", "h3", 30, now, now + 100))
    # 评论：2 成功 1 失败
    for status in ("sent_ok", "sent_ok", "sent_fail"):
        conn.execute("INSERT INTO comment_send_log(status, created_at) VALUES (?,?)", (status, now))
    conn.commit()
    conn.close()

    data = client.get("/api/dashboard/execution-funnel?days=30").json()
    exec_stages = {s["key"]: s["value"] for s in data["exec_stages"]}
    assert exec_stages["collect"] == 3
    assert exec_stages["filter"] == 2
    assert exec_stages["engage"] == 2
    assert exec_stages["dm"] == 1  # 只有 1 条 dm_send_results success
    assert exec_stages["reply"] == 3
    assert exec_stages["convert"] == 6  # 4 加微 + 2 成交

    stages = {s["key"]: s["value"] for s in data["stages"]}
    assert stages["exposure"] == 3
    assert stages["outreach"] == 1
    assert stages["wechat"] == 6
    assert stages["deal"] == 2

    # 成本只认 ad_spend：900 元 / 6 加微 = 150 元
    assert data["cost"]["totalSpend"] == 900.0
    assert data["cost"]["perWechatAdd"] == 150.0
    assert data["cost"]["avgDealAmount"] == 5000.0


def test_funnel_window_excludes_old_rows(client: TestClient, isolated) -> None:
    _init_execution_db(isolated["exec"])
    _init_main_db(isolated["main"], wechat=0, deals=0, replies=0, spend=0.0)
    old = int(time.time()) - 60 * 86400
    conn = sqlite3.connect(isolated["exec"])
    conn.execute("INSERT INTO lead_candidates(video_url, comment_text_hash, intent_score, created_at, expire_at)"
                 " VALUES (?,?,?,?,?)", ("v0", "hold", 90, old, old + 100))
    conn.commit()
    conn.close()

    data = client.get("/api/dashboard/execution-funnel?days=30").json()
    assert data["exec_stages"][0]["value"] == 0
    # 收藏/采集总量口径仍能看到历史（趋势外），窗口内为 0
    assert data["today"]["collected"] == 0


# ═══════════════════════════════════════════════════════════
# 3. 合规护栏
# ═══════════════════════════════════════════════════════════

def test_compliance_aggregates_guardrails(client: TestClient, isolated) -> None:
    _init_execution_db(isolated["exec"])
    _init_pool_db(isolated["pool"], states={"healthy": 2, "cooling": 1}, risk_events=2)
    _init_main_db(isolated["main"], wechat=1, deals=0, replies=0, spend=0.0)

    now = int(time.time())
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    conn = sqlite3.connect(isolated["exec"])
    conn.execute("INSERT INTO account_quotas VALUES (?,?,?,?,?)", (1, today, "like", 0, 12))
    conn.execute("INSERT INTO account_quotas VALUES (?,?,?,?,?)", (1, today, "comment", 0, 5))
    conn.execute("INSERT INTO account_quotas VALUES (?,?,?,?,?)", (2, today, "like", 0, 8))
    conn.execute(
        "INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?)",
        ("run-1", "done", None,
         json.dumps({"engage_blocked": "no_handler(合规闸门:未过TEST-CHECKLIST验证)",
                     "dm_blocked": "no_handler(合规闸门:未过TEST-CHECKLIST验证)"}),
         now, now),
    )
    conn.execute("INSERT INTO pipeline_progress VALUES (?,?,?,?,?)", ("run-1", "collect", "v1", "ok", now))
    conn.execute("INSERT INTO nurture_sessions(videos_watched, likes, favorites, comments_browsed, started_at)"
                 " VALUES (?,?,?,?,?)", (10, 4, 2, 6, now))
    conn.commit()
    conn.close()

    data = client.get("/api/dashboard/execution-compliance?days=30").json()
    assert data["quota"]["like"] == 20
    assert data["quota"]["comment"] == 5
    assert data["quota"]["accounts"] == 2
    assert data["pool"]["total"] == 3
    assert data["pool"]["by_state"] == {"healthy": 2, "cooling": 1}
    assert data["pool"]["risk_events_day"] == 2
    assert data["pipeline"]["runs_in_window"] == 1
    assert data["pipeline"]["blocked_engage"] == 1
    assert data["pipeline"]["blocked_dm"] == 1
    assert data["pipeline"]["checkpoints"] == 1
    assert data["nurture"]["sessions"] == 1
    assert data["nurture"]["likes"] == 4
    assert data["audit"]["events"] == 1


def test_dm_guard_counts_when_wired(client: TestClient, isolated) -> None:
    _init_execution_db(isolated["exec"])
    _init_main_db(isolated["main"], wechat=0, deals=0, replies=0, spend=0.0)
    now = int(time.time())
    conn = sqlite3.connect(isolated["dm"])
    conn.execute(
        """CREATE TABLE dm_send_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
           account_id TEXT NOT NULL, recipient TEXT NOT NULL,
           mutual INTEGER NOT NULL DEFAULT 0, channel TEXT NOT NULL DEFAULT 'official',
           sent_at INTEGER NOT NULL)""")
    conn.execute("INSERT INTO dm_send_log(account_id, recipient, sent_at) VALUES ('a1','r1',?)", (now,))
    conn.execute("INSERT INTO dm_send_log(account_id, recipient, sent_at) VALUES ('a1','r2',?)", (now,))
    conn.commit()
    conn.close()

    data = client.get("/api/dashboard/execution-compliance").json()
    assert data["dm_guard"]["ready"] is True
    assert data["dm_guard"]["today_sent"] == 2
    assert data["dm_guard"]["today_recipients"] == 2
