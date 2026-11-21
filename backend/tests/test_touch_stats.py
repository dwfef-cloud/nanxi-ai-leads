"""线索触达账本测试（离线，临时库）。

覆盖本轮修复的假数据缺陷：
    池统计旧实现 `COUNT(*) FROM dm_send_results WHERE status='success'` 把
    自动化测试记录（lead_id='__test__'、内容「模拟发送成功」）算成真实私信，
    页面上因此出现「私信 19 条」，而真实发送为 0。

同时锁定：
1. 触达次数只能来自真实动作记录，不能拿 leads.status 臆造；
2. 点赞/关注/主页评论无来源时如实 0，并给出 note；
3. touchTypes / batchFollowStatus 筛选必须按真实记录命集，不能静默返回全量。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.api import ipc_bridge as bridge
from app.execution.touch_stats import TouchLedger, blank_counts, is_test_lead_id

REAL_ID = "a" * 32
REAL_ID_2 = "b" * 32

LEADS_DDL = """
CREATE TABLE leads (
    id TEXT PRIMARY KEY,
    nickname TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'own_comment',
    source_url TEXT NOT NULL DEFAULT '',
    source_keyword TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'douyin',
    external_id TEXT NOT NULL DEFAULT '',
    source_task_id TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    video TEXT NOT NULL DEFAULT '',
    referral_note TEXT,
    score INTEGER NOT NULL DEFAULT 0,
    intent_level TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    customer_need TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'collected',
    account TEXT,
    throttled_note TEXT,
    fail_note TEXT,
    reject_reason TEXT,
    wechat_added_at TEXT,
    manual INTEGER NOT NULL DEFAULT 0,
    deal_amount REAL,
    deal_at TEXT,
    region TEXT NOT NULL DEFAULT '',
    hue INTEGER NOT NULL DEFAULT 200,
    next_followup_at TEXT,
    lost_reason TEXT NOT NULL DEFAULT '',
    conversion_amount REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE comment_tasks (
    id TEXT PRIMARY KEY, lead_id TEXT, status TEXT, replied_at TEXT
);
CREATE TABLE dm_send_results (
    id TEXT PRIMARY KEY, lead_id TEXT, content TEXT, message TEXT, status TEXT, created_at TEXT
);
CREATE TABLE account_events (
    id TEXT PRIMARY KEY, account_id TEXT, action TEXT, detail TEXT, success INTEGER, created_at TEXT
);
"""


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "leads.db"
    conn = sqlite3.connect(db)
    conn.executescript(LEADS_DDL)

    for lid, nick, status in ((REAL_ID, "真线索甲", "collected"),
                              (REAL_ID_2, "真线索乙", "contacted"),
                              ("test-lead-001", "测试线索", "collected")):
        conn.execute(
            "INSERT INTO leads (id, nickname, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            (lid, nick, status, "2026-11-01T00:00:00", "2026-11-01T00:00:00"),
        )

    # 真实回复：2 条（同一线索 1 条 + 另一线索 1 条），1 条测试线索的回复应被剔除
    conn.executemany(
        "INSERT INTO comment_tasks (id, lead_id, status, replied_at) VALUES (?,?,?,?)",
        [
            ("t1", REAL_ID, "replied", "2026-11-02T00:00:00"),
            ("t2", REAL_ID_2, "user_replied", "2026-11-02T01:00:00"),
            ("t3", "test-lead-001", "replied", "2026-11-02T02:00:00"),
            ("t4", REAL_ID, "pending", None),
        ],
    )

    # 私信：1 条真实成功 + 3 条测试记录（状态 success，内容是「模拟发送成功」）
    conn.executemany(
        "INSERT INTO dm_send_results (id, lead_id, content, message, status, created_at)"
        " VALUES (?,?,?,?,?,?)",
        [
            ("d1", REAL_ID, "您好，看到您留言了", "sent", "success", "2026-11-03T00:00:00"),
            ("d2", "__test__", "你好呀～看到你对我们的服务感兴趣", "模拟发送成功", "success", "2026-11-03T00:00:00"),
            ("d3", "__test__", "你好呀～看到你对我们的服务感兴趣", "模拟发送成功", "success", "2026-11-03T00:00:00"),
            ("d4", "__test__", "你好呀～看到你对我们的服务感兴趣", "模拟需要验证码", "need_captcha", "2026-11-03T00:00:00"),
        ],
    )

    # 关注事件：detail 里编码 lead_id
    conn.execute(
        "INSERT INTO account_events (id, account_id, action, detail, success, created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("e1", "acc1", "follow_sent", f"lead_id={REAL_ID_2}", 1, "2026-11-04T00:00:00"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(bridge, "DB_PATH", db)
    bridge._TOUCH_LEDGERS.clear()
    yield db
    bridge._TOUCH_LEDGERS.clear()


def _page(filters: dict, size: int = 20) -> dict:
    return bridge.dispatch("get-leads-page", [{"page": 1, "pageSize": size, "filters": filters}])


# ── 测试 id 识别 ────────────────────────────────────────────
def test_test_lead_id_detection():
    assert is_test_lead_id("__test__")
    assert is_test_lead_id("test-lead-001")
    assert is_test_lead_id("")
    assert is_test_lead_id(None)
    assert not is_test_lead_id(REAL_ID)


# ── 账本本身 ────────────────────────────────────────────────
def test_ledger_drops_mock_dm_records(tmp_db):
    ledger = TouchLedger(tmp_db)
    totals = ledger.totals()
    # 唯一真实私信为 1 条；2 条「模拟发送成功」被剔除（need_captcha 本就不计成功）
    assert totals["message"] == 1
    assert ledger.report()["dropped_test_records"] == 2


def test_ledger_counts_reply_per_lead(tmp_db):
    ledger = TouchLedger(tmp_db)
    counts = ledger.per_lead([REAL_ID, REAL_ID_2, "test-lead-001", "unknown"])
    assert counts[REAL_ID]["reply"] == 1
    assert counts[REAL_ID_2]["reply"] == 1
    assert counts["test-lead-001"] == blank_counts()   # 测试线索的真实记录也要剔除
    assert counts["unknown"] == blank_counts()


def test_ledger_event_counts_follow(tmp_db):
    ledger = TouchLedger(tmp_db)
    assert ledger.ids_with(["follow"]) == {REAL_ID_2}
    assert ledger.per_lead([REAL_ID_2])[REAL_ID_2]["follow"] == 1


def test_ledger_report_marks_missing_sources(tmp_db):
    report = TouchLedger(tmp_db).report()
    assert "like" in report["missing"]
    assert "profileComment" in report["missing"]
    assert "reply" in report["available"]


# ── 池统计口径 ──────────────────────────────────────────────
def test_pool_stats_excludes_fake_messages(tmp_db):
    out = bridge.dispatch("get-leads-pool-stats", [{}])
    stats = out["stats"]
    assert stats["messages"] == 1          # 不是 3 —— 测试记录必须剔除
    assert stats["replies"] == 2
    assert stats["follows"] == 1
    assert stats["likes"] == 0
    assert "剔除 2 条测试" in out["note"]


def test_pool_stats_touched_uses_records_and_status(tmp_db):
    stats = bridge.dispatch("get-leads-pool-stats", [{}])["stats"]
    assert stats["touchedByRecord"] == 2       # 甲、乙都有真实动作记录
    assert stats["touchedByStatus"] == 1       # 只有乙 status=contacted
    assert stats["touched"] == 2
    assert stats["untouched"] == 1


# ── 列表触达列 ──────────────────────────────────────────────
def test_page_items_carry_real_touch_counts(tmp_db):
    items = {i["leadId"]: i for i in _page({})["items"]}
    assert items[REAL_ID]["touchCounts"]["reply"] == 1
    assert items[REAL_ID]["touchCounts"]["message"] == 1
    assert items[REAL_ID]["replied"] is True
    assert items[REAL_ID]["messaged"] is True
    assert items[REAL_ID]["touchTotal"] == 2
    assert items["test-lead-001"]["touchTotal"] == 0


def test_page_batch_follow_status_from_records(tmp_db):
    items = {i["leadId"]: i for i in _page({})["items"]}
    assert items[REAL_ID_2]["batchFollowStatus"] == "已关注"
    assert items[REAL_ID]["batchFollowStatus"] == "未关注"


# ── 筛选 ────────────────────────────────────────────────────
def test_touch_types_filter_hits_real_records(tmp_db):
    out = _page({"touchTypes": ["回复"]})
    assert {i["leadId"] for i in out["items"]} == {REAL_ID, REAL_ID_2}

    out = _page({"touchTypes": ["私信"]})
    assert {i["leadId"] for i in out["items"]} == {REAL_ID}   # 假私信不得命中


def test_touch_types_no_source_returns_empty_with_note(tmp_db):
    out = _page({"touchTypes": ["点赞"]})
    assert out["total"] == 0
    assert "无采集记录" in out.get("note", "")


def test_batch_follow_status_filter(tmp_db):
    assert {i["leadId"] for i in _page({"batchFollowStatus": "已关注"})["items"]} == {REAL_ID_2}
    # 「未关注」= 全量 3 条减去已关注的 1 条
    assert _page({"batchFollowStatus": "未关注"})["total"] == 2


def test_filters_combine_touch_and_keyword(tmp_db):
    out = _page({"touchTypes": ["回复"], "keyword": "真线索乙"})
    assert out["total"] == 1
    assert out["items"][0]["leadId"] == REAL_ID_2
