"""直播监控的写动作：开关→作用域→签名命令→运行时执行。"""
from __future__ import annotations

import pytest

from app.runtime import monitor_engine


def _patch_rooms(monkeypatch, rooms):
    from app.runtime import runners

    monkeypatch.setattr(runners.browser, "execute",
                        lambda cmd, **_kw: {"ok": True, "rooms": rooms})


def test_live_pass_hands_comments_to_matcher_with_actions_on(tmp_path, monkeypatch):
    """直播轮把评论交给线索匹配，并允许执行动作（弹幕点赞/回复单条仍排除）。"""
    from app.runtime import runners

    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(runners, "log", lambda ctx, msg, level="normal": logs.append((level, msg)))
    _patch_rooms(monkeypatch, [
        {"url": "https://live.douyin.com/1", "finalUrl": "https://live.douyin.com/1",
         "status": "live", "title": "AI学长小马的直播间",
         "events": [
             {"kind": "comment", "nickname": "小王", "content": "智能体怎么报价"},
             {"kind": "enter", "nickname": "路人", "content": "来了"},
             {"kind": "gift", "nickname": "土豪", "content": "送出 小心心"},
         ]},
        {"url": "https://live.douyin.com/2", "status": "ended", "events": []},
    ])
    captured: dict = {}

    def fake_match(ctx, comments, keywords, note, cfg=None, **kw):
        captured["comments"] = comments
        captured["kw"] = kw
        return {"new": len(comments), "matched": 1, "filtered": 0, "aiFailed": 0,
                "actions": 2, "actionFailed": 0}

    monkeypatch.setattr(runners, "_match_comments", fake_match)
    ctx = runners.Ctx(kind="monitor", db_path=tmp_path / "live.db", task_id="t1",
                      name="监控任务1", account_id="acc", run_id="r1")
    cfg = {"liveActionEventTypes": ["enter", "like", "follow", "comment"]}
    out, failure = runners._live_pass(
        ctx, cfg, "acc",
        ["https://live.douyin.com/1", "https://live.douyin.com/2"], ["报价"], {}, 1)

    assert failure == ""
    assert out["read"] == 1
    assert out["actions"] == 2          # 评论级动作要回传到轮次统计
    assert captured["kw"]["allow_actions"] is True
    assert captured["kw"]["skip_action_types"] == {"like-comment", "reply-comment"}
    assert captured["kw"]["source"] == "monitor_live"
    assert captured["comments"][0]["nickname"] == "小王"
    assert captured["comments"][0]["videoId"] == "https://live.douyin.com/1"
    assert any("已下播" in msg for _level, msg in logs)


def test_live_room_actions_only_run_what_is_enabled(tmp_path, monkeypatch):
    """开关没开的房间级动作一个都不该发；开了的按次数带过去。"""
    from app.runtime import runners
    from app.execution import live_leadgen

    calls: list[tuple[str, str, str]] = []

    def fake_perform(db_path, action, *, account_id, task_id, video_url, text="", **kw):
        calls.append((action, video_url, text))
        return {"ok": True, "status": "sent_ok", "code": "ok",
                "times": 3 if action == "like-live" else 0}

    monkeypatch.setattr(live_leadgen, "perform", fake_perform)
    monkeypatch.setattr(runners, "log", lambda *a, **k: None)
    ctx = runners.Ctx(kind="monitor", db_path=tmp_path / "live.db", task_id="t1",
                      name="监控任务1", account_id="acc", run_id="r1")
    cfg = {"enableLiveLike": True, "liveLikeTimesPerRoom": 7}

    out = runners._live_room_actions(ctx, cfg, "acc", "https://live.douyin.com/1", "直播间 1", {})
    assert out == {"ok": 1, "failed": 0}
    assert calls == [("like-live", "https://live.douyin.com/1", "7")]


def test_live_room_actions_stop_on_daily_quota(tmp_path, monkeypatch):
    from app.runtime import runners
    from app.execution import live_leadgen

    calls: list[str] = []
    monkeypatch.setattr(live_leadgen, "perform",
                        lambda *a, **kw: calls.append(kw.get("video_url", "")) or {"ok": True, "code": "ok"})
    monkeypatch.setattr(runners, "log", lambda *a, **k: None)
    ctx = runners.Ctx(kind="monitor", db_path=tmp_path / "live.db", task_id="t1",
                      name="监控任务1", account_id="acc", run_id="r1")
    cfg = {"enableLiveFollowAnchor": True}
    limits = {"follow": 1}

    first = runners._live_room_actions(ctx, cfg, "acc", "https://live.douyin.com/1", "直播间 1", limits)
    second = runners._live_room_actions(ctx, cfg, "acc", "https://live.douyin.com/1", "直播间 1", limits)
    assert first["ok"] == 1
    assert second["ok"] == 0          # 当日额度用完就不再发
    assert len(calls) == 1


def test_monitor_scope_carries_live_actions_and_file_path(tmp_path, monkeypatch):
    """监控作用域要含直播动作，并把文件路径随命令下发（壳按路径读作用域）。"""
    from app.execution import live_scope

    monkeypatch.setenv("LEADS_MONITOR_SCOPE_DIR", str(tmp_path))
    live_scope.write_monitor_scope(task_id="t1", account_id="acc",
                                   actions={"like-live": 5, "follow-live": 1, "lottery-live": 1},
                                   nickname="AI学长小马")
    cmd = live_scope.signed_command("like-live", account_id="acc", task_id="t1",
                                    video_url="https://live.douyin.com/1", text="5")
    assert cmd["scopeFile"].endswith(".json")
    assert cmd["type"] == "like-live"
    assert live_scope.command_allowed(cmd)

    with pytest.raises(ValueError):
        live_scope.signed_command("send-dm", account_id="acc", task_id="t1",
                                  video_url="https://live.douyin.com/1", text="hi",
                                  comment_author="小王")


def test_scopes_for_maps_live_switches():
    cfg = {"enableLiveLike": True, "liveLikeTimesPerRoom": 6,
           "enableLiveFollowAnchor": True, "enableLiveLottery": True}
    actions = monitor_engine.scopes_for(cfg)
    assert actions["like-live"] == 6
    assert actions["follow-live"] == 1
    assert actions["lottery-live"] == 1
    assert monitor_engine.scopes_for({}) == {}
