"""视频短链解析 + 直播事件参数（liveEventTypes / liveConcurrency）+ 明细持久化测试。

2026-11-17 补齐的三块：
1. 视频分享短链（v.douyin.com）跟随重定向归一成 /video/<id>，失败原样保留；
2. liveEventTypes 限定监听的事件类型；liveConcurrency 限制每轮同时监听的房间数
   （超出排队轮转），并把排队状态写进明细；
3. _live_pass 每轮把目标状态（listening/ended/queued/retry）与事件明细写入
   monitor_live_targets / monitor_live_events，供 get-monitor-live-state 展示。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.runtime import monitor_engine as eng
from app.runtime import store


# --------------------------------------------------------------------------
# 视频短链解析
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, final_url: str):
        self._final = final_url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self._final


def test_resolve_video_url_normalizes_short_link(monkeypatch):
    eng._video_resolve_cache.clear()
    monkeypatch.setattr(eng.urllib.request, "urlopen",
                        lambda req, timeout=None, **_kw: _FakeResp(
                            "https://www.douyin.com/video/7301234567890123456?from=share&x=1"))
    assert eng.resolve_video_url("https://v.douyin.com/iAbc12/") == \
        "https://www.douyin.com/video/7301234567890123456"


def test_resolve_video_url_note_link_normalized(monkeypatch):
    eng._video_resolve_cache.clear()
    monkeypatch.setattr(eng.urllib.request, "urlopen",
                        lambda req, timeout=None, **_kw: _FakeResp(
                            "https://www.douyin.com/note/7409876543210987654"))
    assert eng.resolve_video_url("https://v.douyin.com/xYz99/") == \
        "https://www.douyin.com/note/7409876543210987654"


def test_resolve_video_url_passthrough_real_url(monkeypatch):
    """已是真实视频地址的不再请求网络。"""
    eng._video_resolve_cache.clear()

    def boom(*_a, **_k):
        raise AssertionError("真实地址不应发起网络请求")

    monkeypatch.setattr(eng.urllib.request, "urlopen", boom)
    url = "https://www.douyin.com/video/7301234567890123456"
    assert eng.resolve_video_url(url) == url


def test_resolve_video_url_keeps_unresolvable(monkeypatch):
    """解析失败原样返回，交给壳自己跟随跳转（与直播短链同策略）。"""
    eng._video_resolve_cache.clear()

    def boom(*_a, **_k):
        raise OSError("no network")

    monkeypatch.setattr(eng.urllib.request, "urlopen", boom)
    url = "https://v.douyin.com/unknown1/"
    assert eng.resolve_video_url(url) == url


# --------------------------------------------------------------------------
# liveEventTypes / liveConcurrency / 明细持久化
# --------------------------------------------------------------------------
def _patch_rooms(monkeypatch, rooms):
    from app.runtime import runners
    seen: list[dict] = []

    def fake_execute(cmd, **_kw):
        seen.append(cmd)
        # 与真实壳一致：只回本轮下发的房间（liveUrls + authorUrls）
        allowed = set(cmd.get("liveUrls") or []) | set(cmd.get("authorUrls") or [])
        return {"ok": True,
                "rooms": [r for r in rooms if r.get("url") in allowed] if allowed else rooms}

    monkeypatch.setattr(runners.browser, "execute", fake_execute)
    return seen


def _make_ctx(tmp_path):
    from app.runtime import runners
    return runners.Ctx(kind="monitor", db_path=tmp_path / "t.db", task_id="t1",
                       name="x", account_id="acc", run_id="r1")


LEADS_DDL = """CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY, nickname TEXT DEFAULT '', comment TEXT DEFAULT '',
    source_keyword TEXT DEFAULT '', source TEXT DEFAULT '', source_task_id TEXT DEFAULT '',
    source_url TEXT DEFAULT '', video_id TEXT DEFAULT '', comment_id TEXT DEFAULT '',
    comment_user_id TEXT DEFAULT '', comment_time TEXT DEFAULT '', account TEXT DEFAULT '',
    status TEXT DEFAULT 'collected', score INTEGER DEFAULT 0, intent_level TEXT DEFAULT 'C',
    platform TEXT DEFAULT 'douyin', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')"""


def _ensure_leads(db):
    con = sqlite3.connect(db)
    con.execute(LEADS_DDL)
    con.commit()
    con.close()


def _patch_match(monkeypatch):
    from app.runtime import runners
    captured: dict = {}

    def fake_match(ctx, comments, keywords, note, cfg=None, **kw):
        captured["comments"] = comments
        captured["kw"] = kw
        return {"new": 0, "matched": 0, "filtered": 0, "aiFailed": 0,
                "actions": 0, "actionFailed": 0}

    monkeypatch.setattr(runners, "_match_comments", fake_match)
    monkeypatch.setattr(runners, "log", lambda *a, **k: None)
    return captured


ROOMS = [
    {"url": "https://live.douyin.com/1", "finalUrl": "https://live.douyin.com/1",
     "status": "live", "title": "小马的直播间", "nickname": "小马",
     "events": [
         {"kind": "comment", "nickname": "小王", "content": "智能体怎么报价"},
         {"kind": "enter", "nickname": "路人", "content": ""},
         {"kind": "gift", "nickname": "土豪", "content": "小心心"},
     ]},
    {"url": "https://live.douyin.com/2", "finalUrl": "https://live.douyin.com/2",
     "status": "ended", "events": []},
]


def test_live_pass_persists_targets_and_events(tmp_path, monkeypatch):
    from app.runtime import runners

    _patch_rooms(monkeypatch, ROOMS)
    captured = _patch_match(monkeypatch)
    db = tmp_path / "t.db"
    ctx = _make_ctx(tmp_path)
    cfg = {"liveActionEventTypes": ["enter", "like", "follow", "comment"]}
    out, failure = runners._live_pass(
        ctx, cfg, "acc", ["https://live.douyin.com/1", "https://live.douyin.com/2"],
        ["报价"], {}, 1)

    assert failure == ""
    # 目标状态：房间1监听中，房间2已下播
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = {r["target_key"]: dict(r)
            for r in con.execute("SELECT * FROM monitor_live_targets")}
    assert rows["https://live.douyin.com/1"]["state"] == "listening"
    assert rows["https://live.douyin.com/1"]["name"] == "小马"
    assert rows["https://live.douyin.com/1"]["events"] == 2   # gift 被 liveActionEventTypes 挡掉
    assert rows["https://live.douyin.com/2"]["state"] == "ended"
    # 事件明细：gift 被 liveActionEventTypes 挡掉，comment=pending，enter=signal
    evs = [dict(r) for r in con.execute("SELECT * FROM monitor_live_events")]
    con.close()
    kinds = {e["kind"]: e for e in evs}
    assert set(kinds) == {"comment", "enter"}
    assert kinds["comment"]["outcome"] == "pending"
    assert kinds["enter"]["outcome"] == "signal"
    # 弹幕带 _eventId 传给匹配器（研判后回写 outcome）
    assert captured["comments"][0]["_eventId"] == kinds["comment"]["id"]
    assert captured["kw"]["track_events"] is True


def test_live_pass_event_type_filter(tmp_path, monkeypatch):
    """liveEventTypes 只保留配置的事件类型；默认（未配置）不额外过滤。"""
    from app.runtime import runners

    _patch_rooms(monkeypatch, ROOMS)
    captured = _patch_match(monkeypatch)
    ctx = _make_ctx(tmp_path)
    cfg = {"liveActionEventTypes": ["enter", "like", "follow", "comment", "gift"],
           "liveEventTypes": ["comment"]}
    runners._live_pass(ctx, cfg, "acc",
                       ["https://live.douyin.com/1"], ["报价"], {}, 1)
    assert [c["text"] for c in captured["comments"]] == ["智能体怎么报价"]
    con = sqlite3.connect(tmp_path / "t.db")
    kinds = {r[0] for r in con.execute("SELECT kind FROM monitor_live_events")}
    con.close()
    assert kinds == {"comment"}


def test_live_pass_concurrency_rotates_rooms(tmp_path, monkeypatch):
    """liveConcurrency 限制每轮监听数，超出排队轮转，排队目标写进明细。"""
    from app.runtime import runners

    seen = _patch_rooms(monkeypatch, [
        {"url": f"https://live.douyin.com/{i}", "finalUrl": f"https://live.douyin.com/{i}",
         "status": "live", "title": f"房间{i}", "events": []} for i in (1, 2, 3)])
    _patch_match(monkeypatch)
    ctx = _make_ctx(tmp_path)
    cfg = {"liveConcurrency": 2}
    pool = [f"https://live.douyin.com/{i}" for i in (1, 2, 3)]

    runners._live_pass(ctx, cfg, "acc", pool, [], {}, 1)
    assert seen[0]["liveUrls"] == ["https://live.douyin.com/1", "https://live.douyin.com/2"]
    con = sqlite3.connect(tmp_path / "t.db")
    states = dict(con.execute("SELECT target_key, state FROM monitor_live_targets"))
    assert states["https://live.douyin.com/3"] == "queued"

    runners._live_pass(ctx, cfg, "acc", pool, [], {}, 2)
    assert seen[1]["liveUrls"] == ["https://live.douyin.com/3", "https://live.douyin.com/1"]
    states = dict(con.execute("SELECT target_key, state FROM monitor_live_targets"))
    con.close()
    assert states["https://live.douyin.com/2"] == "queued"
    assert states["https://live.douyin.com/1"] == "listening"


def test_match_comments_writeback_updates_event_outcome(tmp_path, monkeypatch):
    """track_events=True：命中→matched+关键词+动作摘要；未命中→filtered。"""
    from app.runtime import runners

    monkeypatch.setattr(runners, "log", lambda *a, **k: None)
    monkeypatch.setattr(runners, "_run_actions",
                        lambda *a, **k: {"ok": 1, "failed": 0})
    db = tmp_path / "t.db"
    ctx = _make_ctx(tmp_path)
    store.live_target_upsert(db, task_id="t1", target_key="https://live.douyin.com/1",
                             state="listening")
    eid_hit = store.live_event_insert(db, task_id="t1", target_key="https://live.douyin.com/1",
                                      kind="comment", nickname="小王", content="智能体怎么报价")
    eid_miss = store.live_event_insert(db, task_id="t1", target_key="https://live.douyin.com/1",
                                       kind="comment", nickname="小赵", content="今天天气不错")
    comments = [
        {"text": "智能体怎么报价", "nickname": "小王", "videoId": "https://live.douyin.com/1",
         "videoUrl": "https://live.douyin.com/1", "commentId": "c1", "_eventId": eid_hit},
        {"text": "今天天气不错", "nickname": "小赵", "videoId": "https://live.douyin.com/1",
         "videoUrl": "https://live.douyin.com/1", "commentId": "c2", "_eventId": eid_miss},
    ]
    _ensure_leads(db)
    out = runners._match_comments(ctx, comments, ["报价"], None, {},
                                  account_id="acc", source="monitor_live",
                                  skip_action_types={"like-comment", "reply-comment"},
                                  track_events=True)
    assert out["matched"] == 1 and out["filtered"] == 1 and out["new"] == 2
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    outcomes = {r["id"]: dict(r) for r in con.execute("SELECT * FROM monitor_live_events")}
    targets = {r["target_key"]: dict(r) for r in con.execute("SELECT * FROM monitor_live_targets")}
    con.close()
    assert outcomes[eid_hit]["outcome"] == "matched"
    assert outcomes[eid_hit]["keyword"] == "报价"
    assert "1 成功" in outcomes[eid_hit]["actions"]
    assert outcomes[eid_miss]["outcome"] == "filtered"
    assert targets["https://live.douyin.com/1"]["matched"] == 1
    assert targets["https://live.douyin.com/1"]["added"] == 1   # 只有命中的那条落线索


def test_match_comments_dedup_marks_event(tmp_path, monkeypatch):
    """同一弹幕第二轮进来：标记 dedup，不重复落线索。"""
    from app.runtime import runners

    monkeypatch.setattr(runners, "log", lambda *a, **k: None)
    db = tmp_path / "t.db"
    ctx = _make_ctx(tmp_path)
    eid = store.live_event_insert(db, task_id="t1", target_key="https://live.douyin.com/1",
                                  kind="comment", nickname="小王", content="智能体怎么报价")
    _ensure_leads(db)
    comment = {"text": "智能体怎么报价", "nickname": "小王",
               "videoId": "https://live.douyin.com/1",
               "videoUrl": "https://live.douyin.com/1", "commentId": "c1", "_eventId": eid}
    runners._match_comments(ctx, [dict(comment)], ["报价"], None, {},
                            account_id="acc", source="monitor_live", track_events=True)
    runners._match_comments(ctx, [dict(comment)], ["报价"], None, {},
                            account_id="acc", source="monitor_live", track_events=True)
    con = sqlite3.connect(db)
    outcome, leads = con.execute(
        "SELECT (SELECT outcome FROM monitor_live_events WHERE id=?), "
        "(SELECT COUNT(*) FROM leads)", (eid,)).fetchone()
    con.close()
    assert outcome == "dedup" and leads == 1
