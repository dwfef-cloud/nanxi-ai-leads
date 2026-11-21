"""监控任务表「部分实现」批次收口契约测试（2026-11-18，清单 #6/#15/#31/#33/#46/#49/#50）。

- #49 群成员明细：_monitor_group_history 读真实 leads（source=monitor_group），
  替换掉「采集器尚未接通」的硬编码空态 —— 采集器 _group_pass 已通。
- #33 主页筛选：关注/私信动作前按 profileActionGenderFilter/profileActionAgeFilter*
  开主页核对（inspect-profile，未知性别不通过）；未配置零开销。
- #31 首作互动：评论命中后对作者主页首个公开作品点赞/收藏/评论
  （commentOnProfileFirstWork 总开关 + 概率 + 过滤置顶 + 随机浏览）。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.api import ipc_bridge
from app.execution import live_scope
from app.runtime import browser, monitor_engine as eng, runners
from test_runtime import _monitor_task, db  # noqa: F401 复用运行时套件夹具


def _insert_group_lead(con, lid: str, tid: str, group: str, nick: str,
                       account: str = "acct-real", keyword: str = "AI") -> None:
    con.execute(
        "INSERT INTO leads(id, nickname, comment, source_keyword, source, source_task_id, "
        "video_id, comment_id, account, status, score, intent_level, platform, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lid, nick, f"[群成员] {nick}", keyword, "monitor_group", tid,
         f"{group}:{nick}", f"group:{group}:{nick}", account, "collected", 50, "B",
         "douyin", "2026-11-18T16:00:00"))


# ---------------------------------------------------------------- #49
def test_group_history_reads_real_members(db, monkeypatch):
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    tid = _monitor_task(db, "mon_grp")
    with sqlite3.connect(str(db)) as con:
        _insert_group_lead(con, "g1", tid, "AI交流群", "小美")
        _insert_group_lead(con, "g2", tid, "AI交流群", "阿强")
        _insert_group_lead(con, "g3", tid, "获客分享群", "老李")
        con.commit()
    got = ipc_bridge.dispatch("get-monitor-group-history",
                              [{"taskId": tid, "limit": 10}])
    assert got["success"] is True and got["total"] == 3
    assert [i["nickname"] for i in got["items"]] == ["老李", "阿强", "小美"]  # 时间倒序
    assert got["items"][0]["groupName"] == "获客分享群"
    assert got["items"][0]["role"] == "member"
    assert set(got["groups"]) == {"AI交流群", "获客分享群"}
    only = ipc_bridge.dispatch("get-monitor-group-history",
                               [{"taskId": tid, "groupName": "AI交流群"}])
    assert only["total"] == 2 and set(only["groups"]) == {"AI交流群"}


def test_group_history_filters_by_keyword(db, monkeypatch):
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    tid = _monitor_task(db, "mon_grp_kw")
    with sqlite3.connect(str(db)) as con:
        _insert_group_lead(con, "k1", tid, "群A", "张三")
        _insert_group_lead(con, "k2", tid, "群A", "李四")
        con.commit()
    got = ipc_bridge.dispatch("get-monitor-group-history",
                              [{"taskId": tid, "keyword": "张三"}])
    assert got["total"] == 1 and got["items"][0]["nickname"] == "张三"


def test_group_history_empty_task_stays_explicit(db, monkeypatch):
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    got = ipc_bridge.dispatch("get-monitor-group-history", [{"taskId": "no-such"}])
    assert got["success"] is True and got["items"] == [] and got["groups"] == []
    assert "还没有群成员记录" in got["note"]


# ---------------------------------------------------------------- #33
def test_profile_action_gate_off_by_default(monkeypatch, tmp_path):
    """没配主页筛选：直接放行，不开主页（inspect-profile 一次都不发）。"""
    ctx = _ctx(tmp_path)
    calls: list[str] = []

    def fake_execute(cmd, **kw):
        calls.append(cmd["type"])
        return {"ok": True, "item": {"gender": "female", "age": 30}}

    monkeypatch.setattr(browser, "execute", fake_execute)
    ok, why = runners._profile_action_gate(None, {"taskMode": "monitor"}, "acct",
                                           "uid-1", "小美", {})
    assert ok is True and why == "" and calls == []


def test_profile_action_gate_gender_rules(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: {
        "ok": True, "item": {"gender": None, "age": 25}})
    # male：未知性别不通过（页面口径）
    ok, why = runners._profile_action_gate(
        ctx, {"profileActionGenderFilter": "male"}, "acct", "uid-1", "小美", {})
    assert ok is False and "性别不符合" in why
    # male_unknown：未知放行
    ok, _ = runners._profile_action_gate(
        ctx, {"profileActionGenderFilter": "male_unknown"}, "acct", "uid-1", "小美", {})
    assert ok is True
    # female：男不通过（换一个缓存 uid 避免命中缓存）
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: {
        "ok": True, "item": {"gender": "male", "age": 25}})
    ok, why = runners._profile_action_gate(
        ctx, {"profileActionGenderFilter": "female"}, "acct", "uid-2", "小美", {})
    assert ok is False and "性别不符合" in why


def test_profile_action_gate_age_rules(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: {
        "ok": True, "item": {"gender": "female", "age": 31}})
    cfg = {"profileActionAgeFilterEnabled": True,
           "profileActionAgeMin": 18, "profileActionAgeMax": 30}
    ok, why = runners._profile_action_gate(ctx, cfg, "acct", "uid-3", "小美", {})
    assert ok is False and "18-30" in why
    cfg_allow = {**cfg, "profileActionAgeAllowUnknown": True}
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: {
        "ok": True, "item": {"gender": "female"}})
    ok, _ = runners._profile_action_gate(ctx, cfg_allow, "acct", "uid-4", "小美", {})
    assert ok is True
    ok, why = runners._profile_action_gate(ctx, cfg, "acct", "uid-5", "小美", {})
    assert ok is False and "年龄未知" in why


def test_profile_action_gate_caches_profile(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: (
        calls.append(cmd["type"]) or {"ok": True, "item": {"gender": "male", "age": 20}}))
    ctx = _ctx(tmp_path)
    cfg = {"profileActionGenderFilter": "male"}
    cache: dict = {}
    assert runners._profile_action_gate(ctx, cfg, "acct", "uid-x", "小美", cache)[0]
    assert runners._profile_action_gate(ctx, cfg, "acct", "uid-x", "小美", cache)[0]
    assert calls.count("inspect-profile") == 1, "同一 uid 只开一次主页"


# ---------------------------------------------------------------- #31
def test_scopes_for_profile_first_actions():
    out = eng.scopes_for({"commentOnProfileFirstWork": True})
    assert out["like-video"] == 100 and out["favorite-video"] == 100
    assert "post-comment" not in out  # 没开首作评论时不动 post-comment
    out2 = eng.scopes_for({"commentOnProfileFirstWork": True,
                           "enableProfileFirstComment": True})
    assert out2["post-comment"] == 100
    # 主评也开时取较高次数，不互相覆盖
    out3 = eng.scopes_for({"enableVideoComment": True,
                           "commentOnProfileFirstWork": True,
                           "enableProfileFirstComment": True})
    assert out3["post-comment"] == 100
    assert eng.scopes_for({}).get("like-video") is None


def test_live_actions_include_video_actions():
    assert "like-video" in live_scope.LIVE_ACTIONS
    assert "favorite-video" in live_scope.LIVE_ACTIONS


def _first_fixture(monkeypatch, works, calls):
    def fake_execute(cmd, **kw):
        calls.append(cmd["type"])
        if cmd["type"] == "read-author-works":
            return {"ok": True, "items": works, "author": "小美"}
        if cmd["type"] == "watch-video":
            return {"ok": True}
        return {"ok": True}

    monkeypatch.setattr(browser, "execute", fake_execute)
    performed: list[tuple] = []

    def fake_perform(db_path, action, **kw):
        calls.append("perform:" + action)  # 与 browser 调用同一时间线，便于断言先后
        performed.append((action, kw.get("video_url"), kw.get("text")))
        return {"ok": True, "code": "sent_ok"}

    import app.execution.live_leadgen as ll
    monkeypatch.setattr(ll, "perform", fake_perform)
    return performed


def _ctx(tmp_path):
    from app.runtime import runners as r
    return r.Ctx(kind="monitor", db_path=tmp_path / "x.db", task_id="mon_first",
                 name="首作测试", account_id="acct", run_id="run-1",
                 trigger="test", engine="shell")


def test_monitor_profile_first_likes_collects_comments(tmp_path, monkeypatch):
    calls: list[str] = []
    works = [{"url": "https://www.douyin.com/video/111", "pinned": True, "title": "置顶"},
             {"url": "https://www.douyin.com/video/222", "pinned": False, "title": "首作"}]
    performed = _first_fixture(monkeypatch, works, calls)
    ctx = _ctx(tmp_path)
    cfg = {"commentOnProfileFirstWork": True, "enableProfileFirstComment": True,
           "profileFirstWorkLikePercent": 100, "profileFirstWorkCollectPercent": 100,
           "profileFirstFilterPinned": True,
           "videoCommentMode": "template", "videoCommentContent": "内容不错{nickname}"}
    runners.monitor_profile_first(ctx, cfg, "acct",
                                  {"userId": "uid-9", "nickname": "小美"})
    actions = [a for a, _, _ in performed]
    assert actions == ["like-video", "favorite-video", "post-comment"]
    assert performed[0][1] == "https://www.douyin.com/video/222", "开了过滤置顶：跳过置顶作品"
    assert performed[2][2] == "内容不错{nickname}"  # 主评模板直出（公开评论无昵称上下文）
    assert "read-author-works" in calls


def test_monitor_profile_first_random_browse_before_comment(tmp_path, monkeypatch):
    calls: list[str] = []
    works = [{"url": "https://www.douyin.com/video/111", "pinned": False},
             {"url": "https://www.douyin.com/video/333", "pinned": False}]
    performed = _first_fixture(monkeypatch, works, calls)
    ctx = _ctx(tmp_path)
    cfg = {"commentOnProfileFirstWork": True, "enableProfileFirstComment": True,
           "profileFirstWorkLikePercent": 0, "profileFirstWorkCollectPercent": 0,
           "profileFirstRandomBrowseBeforeComment": True,
           "videoCommentMode": "template", "videoCommentContent": "不错"}
    runners.monitor_profile_first(ctx, cfg, "acct",
                                  {"userId": "uid-8", "nickname": "小美"})
    assert "watch-video" in calls, "勾了「先随机浏览」：评论前打开同主页另一作品停留"
    assert calls.index("watch-video") < calls.index("perform:post-comment"), \
        "随机浏览必须在评论之前"
    actions = [a for a, _, _ in performed]
    assert actions == ["post-comment"]  # 概率 0 → 不点赞不收藏


def test_monitor_profile_first_dedup_per_uid(tmp_path, monkeypatch):
    calls: list[str] = []
    performed = _first_fixture(
        monkeypatch, [{"url": "https://www.douyin.com/video/111", "pinned": False}], calls)
    ctx = _ctx(tmp_path)
    cfg = {"commentOnProfileFirstWork": True, "enableProfileFirstComment": True,
           "profileFirstWorkLikePercent": 100, "profileFirstWorkCollectPercent": 0,
           "videoCommentMode": "template", "videoCommentContent": "hi"}
    runners.monitor_profile_first(ctx, cfg, "acct", {"userId": "uid-7", "nickname": "小美"})
    runners.monitor_profile_first(ctx, cfg, "acct", {"userId": "uid-7", "nickname": "小美"})
    assert [a for a, _, _ in performed].count("like-video") == 1, "同一作者只互动一次"


def test_monitor_profile_first_skips_without_switch(tmp_path, monkeypatch):
    calls: list[str] = []
    performed = _first_fixture(
        monkeypatch, [{"url": "https://www.douyin.com/video/111", "pinned": False}], calls)
    ctx = _ctx(tmp_path)
    runners.monitor_profile_first(ctx, {}, "acct", {"userId": "uid-6", "nickname": "小美"})
    assert performed == [] and calls == []


def test_monitor_profile_first_no_works(tmp_path, monkeypatch):
    calls: list[str] = []
    performed = _first_fixture(monkeypatch, [], calls)
    ctx = _ctx(tmp_path)
    cfg = {"commentOnProfileFirstWork": True, "enableProfileFirstComment": True,
           "profileFirstWorkLikePercent": 100}
    runners.monitor_profile_first(ctx, cfg, "acct", {"userId": "uid-5", "nickname": "小美"})
    assert performed == []
