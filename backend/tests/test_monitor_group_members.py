"""监控群成员（group_members）的接通测试。

2026-11-17 前的状态：前端选「监控群成员」存 groupNames/groupSelections，后端
run_monitor 只打一句「采集器尚未接通，本轮不采集」的 warning 就跳过。

接通约定：
* monitor_engine.group_targets() 合并手填群名 + 勾选群，去重；
* runners._group_pass 下发 read-group-members（壳打开消息页读完整名单），
  按 excludeGroupOwner / excludeGroupAdmins 过滤后交 _match_comments 入库/互动；
* 去重维度 = 群名:昵称；点赞评论/回复评论不适用（同直播）。
"""
from __future__ import annotations

import pytest

from app.runtime import monitor_engine as eng


def test_group_targets_merges_names_and_selections():
    cfg = {
        "groupNames": "AI智能体教师交流群\nAI获客系统分享",
        "groupSelections": [
            {"name": "AI智能体教师交流群"},   # 与手填重复 → 合并
            {"groupName": "手表"},
            "直接字符串群名",
            {"title": "空名跳过"},             # name 为空时用 title
        ],
    }
    assert eng.group_targets(cfg) == [
        "AI智能体教师交流群", "AI获客系统分享", "手表", "直接字符串群名", "空名跳过",
    ]


def test_group_targets_empty_when_not_configured():
    assert eng.group_targets({}) == []
    assert eng.group_targets({"groupNames": ""}) == []
    assert eng.group_targets({"groupSelections": None}) == []


def test_group_pass_filters_owner_and_admin(monkeypatch, tmp_path):
    """excludeGroupOwner/excludeGroupAdmins 分别过滤对应身份，去重维度=群:昵称。"""
    from app.runtime import runners

    monkeypatch.setattr(runners.browser, "execute", lambda cmd, **_kw: {"ok": True, "groups": [
        {"name": "AI智能体教师交流群", "found": True, "members": [
            {"nickname": "马老师", "role": "owner"},
            {"nickname": "管理员甲", "role": "admin"},
            {"nickname": "小暖", "role": "member"},
            {"nickname": "噜噜噜", "role": "member"},
            {"nickname": "小暖", "role": "member"},   # 重复成员 → 壳层已去重，防御一次
        ]},
        {"name": "不存在的群", "found": False, "members": []},
    ]})
    logs: list[str] = []
    monkeypatch.setattr(runners, "log",
                        lambda ctx, msg, level="normal": logs.append(str(msg)))
    captured: dict = {}

    def fake_match(ctx, comments, keywords, note, cfg=None, **kw):
        captured["comments"] = comments
        captured["kw"] = kw
        return {"new": len(comments), "matched": 0, "filtered": 0, "aiFailed": 0,
                "actions": 0, "actionFailed": 0}

    monkeypatch.setattr(runners, "_match_comments", fake_match)
    ctx = runners.Ctx(kind="monitor", db_path=tmp_path / "t.db", task_id="t1",
                      name="群成员监控", account_id="acc", run_id="r1")
    cfg = {"excludeGroupOwner": True, "excludeGroupAdmins": True}
    out, failure = runners._group_pass(
        ctx, cfg, "acc", ["AI智能体教师交流群", "不存在的群"], [], {})

    assert failure == ""
    assert out["read"] == 0
    nicks = [c["nickname"] for c in captured["comments"]]
    assert nicks == ["小暖", "噜噜噜"]            # 群主+管理员被排除，重复去重
    assert captured["comments"][0]["videoId"] == "AI智能体教师交流群:小暖"
    assert captured["kw"]["source"] == "monitor_group"
    assert captured["kw"]["allow_actions"] is True
    assert captured["kw"]["skip_action_types"] == {"like-comment", "reply-comment"}
    assert any("未找到" in msg for msg in logs)


def test_group_pass_keeps_owner_when_not_excluded(monkeypatch, tmp_path):
    """不勾排除项时群主/管理员也入库。"""
    from app.runtime import runners

    monkeypatch.setattr(runners.browser, "execute", lambda cmd, **_kw: {"ok": True, "groups": [
        {"name": "AI获客系统分享", "found": True, "members": [
            {"nickname": "马老师", "role": "owner"}]}]})
    monkeypatch.setattr(runners, "log", lambda ctx, msg, level="normal": None)
    seen: list = []
    monkeypatch.setattr(runners, "_match_comments",
                        lambda ctx, comments, *a, **kw: seen.extend(comments)
                        or {"new": len(comments), "matched": 0, "filtered": 0,
                            "aiFailed": 0, "actions": 0, "actionFailed": 0})
    ctx = runners.Ctx(kind="monitor", db_path=tmp_path / "t.db", task_id="t1",
                      name="x", account_id="acc", run_id="r1")
    out, failure = runners._group_pass(ctx, {}, "acc", ["AI获客系统分享"], [], {})

    assert failure == ""
    assert [c["nickname"] for c in seen] == ["马老师"]
    assert seen[0]["text"] == "[群成员] 马老师"


def test_group_pass_reports_login_required(monkeypatch, tmp_path):
    """壳报需要登录时任务记失败而不是静默 0 条。"""
    from app.runtime import runners

    monkeypatch.setattr(runners.browser, "execute",
                        lambda cmd, **_kw: {"ok": False, "error": "需要登录才能读群聊"})
    logs: list[str] = []
    monkeypatch.setattr(runners, "log",
                        lambda ctx, msg, level="normal": logs.append((level, str(msg))))
    ctx = runners.Ctx(kind="monitor", db_path=tmp_path / "t.db", task_id="t1",
                      name="x", account_id="acc", run_id="r1")
    out, failure = runners._group_pass(ctx, {}, "acc", ["某群"], [], {})

    assert failure == "需要登录才能读群聊"
    assert out["read"] == 0
    assert logs and logs[0][0] == "error"
