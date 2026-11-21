"""线索库筛选与批量取数通道的契约测试（离线，用临时库）。

覆盖本次修复的三类缺陷：
1. 前端 get-leads-page 传的是 {page, pageSize, filters:{...}}，旧实现只读顶层 keyword，
   导致搜索/意向/来源/账号/地区/时间筛选全部失效（永远返回全量）；
2. keyword 未命中昵称与入口关键词，搜索框对昵称搜不到人；
3. get-leads-count / get-lead-ids / get-leads-by-ids / query-leads-for-batch
   四个通道未注册，返回 {"success": true} 而非页面期望的形状。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.api import ipc_bridge as bridge

DDL = """
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
)
"""

ROWS = [
    # id, nickname, source_keyword, intent_level, source, region, comment
    ("L1", "阿杰", "AI智能体", "A", "own_comment", "深圳", "这个智能体怎么搭"),
    ("L2", "李姐", "AI副业", "high", "competitor", "广州", "想做副业"),
    ("L3", "阿强", "装修", "C", "own_comment", "深圳", "多少钱一平"),
]


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "leads.db"
    conn = sqlite3.connect(db)
    conn.execute(DDL)
    for lid, nick, kw, level, src, region, comment in ROWS:
        conn.execute(
            "INSERT INTO leads (id, nickname, source_keyword, intent_level, source, region, comment,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'collected',?,?)",
            (lid, nick, kw, level, src, region, comment, "2026-11-01T00:00:00", "2026-11-01T00:00:00"),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(bridge, "DB_PATH", db)
    return db


def _page(filters: dict, page: int = 1, size: int = 20) -> dict:
    return bridge.dispatch("get-leads-page", [{"page": page, "pageSize": size, "filters": filters}])


def test_keyword_hits_nickname_and_source_keyword(tmp_db):
    assert [i["nickname"] for i in _page({"keyword": "阿杰"})["items"]] == ["阿杰"]
    assert _page({"keyword": "AI智能体"})["total"] == 1
    assert _page({"keyword": "智能体"})["total"] == 1  # 命中 comment 里的子串
    assert _page({"keyword": "查无此人"})["total"] == 0


def test_keyword_accepts_legacy_top_level_arg(tmp_db):
    """旧调用把筛选平铺在顶层参数上，仍需可用。"""
    out = bridge.dispatch("get-leads-page", [{"page": 1, "pageSize": 10, "keyword": "李姐"}])
    assert out["total"] == 1


def test_intention_filter_maps_cn_and_en(tmp_db):
    assert _page({"intention": "高意向"})["total"] == 2  # A + high
    assert _page({"intention": "high"})["total"] == 2
    assert _page({"intention": "低意向"})["total"] == 1
    assert _page({"intention": "low"})["total"] == 1


def test_source_and_location_filters(tmp_db):
    assert _page({"entrySource": ["competitor"]})["total"] == 1
    assert _page({"locations": ["深圳"]})["total"] == 2
    assert _page({"locations": ["深圳"], "entrySource": ["own_comment"]})["total"] == 2


def test_no_record_dimension_returns_empty_with_note(tmp_db):
    """触达类筛选本地无采集源：必须返回空集并说明，不能静默返回全量。"""
    out = _page({"batchFollowStatus": "已关注"})
    assert out["total"] == 0
    assert "无采集记录" in out.get("note", "")


def test_empty_filters_returns_all(tmp_db):
    assert _page({})["total"] == 3


def test_leads_count_matches_page_total(tmp_db):
    out = bridge.dispatch("get-leads-count", [{"filters": {"intention": "高意向"}}])
    assert out == {"total": 2, "runtime": "sqlite"}
    assert out["total"] == _page({"intention": "高意向"})["total"]


def test_leads_ids_returns_all_matching_ids(tmp_db):
    out = bridge.dispatch("get-lead-ids", [{"filters": {"locations": ["深圳"]}}])
    assert sorted(out["ids"]) == ["L1", "L3"]


def test_leads_by_ids_reports_missing(tmp_db):
    out = bridge.dispatch("get-leads-by-ids", [{"ids": ["L1", "L3"]}])
    assert sorted(i["leadId"] for i in out["items"]) == ["L1", "L3"]
    assert out["requested"] == 2 and out["missing"] == 0

    out2 = bridge.dispatch("get-leads-by-ids", [{"ids": ["L1", "NOPE"]}])
    assert out2["requested"] == 2 and out2["missing"] == 1


def test_query_leads_for_batch_shares_filter_scope(tmp_db):
    out = bridge.dispatch("query-leads-for-batch", [{"filters": {"intention": "低意向"}, "limit": 10}])
    assert [i["nickname"] for i in out["items"]] == ["阿强"]


def test_unregistered_write_channel_is_not_fake_success():
    # delete-leads-from-history / export-leads 已注册（2026-11-17），不再属于死通道
    for ch in ("clear-all-history", "toggle-intention"):
        data = bridge.dispatch(ch, [{}])
        assert data["success"] is False, ch
        assert data["msg"] == "功能未开通", ch
