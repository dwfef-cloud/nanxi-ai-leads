"""直播监控目标解析的契约测试。

真机复现（2026-11-17）：用户在监控任务里选「直播监控」，把抖音分享文案整段
粘进直播链接框：
  「4.t r@e.oq :5pm 05/31 【AI学长小马的直播间】 点击打开👉https://v.douyin.com/bUzExrIs2Cg/ …」
运行时既不解析 liveUrls，也没有直播采集器 —— 日志连着两句
「直播监控采集器尚未接通」「监控任务没有目标」，0.1 秒就结束。
"""
from __future__ import annotations

import pytest

from app.runtime import monitor_engine as eng

SHARE_TEXT = (
    "4.t r@e.oq :5pm 05/31 【AI学长小马的直播间】  "
    "点击打开👉https://v.douyin.com/bUzExrIs2Cg/  或长按复制此条消息，打开抖音，看TA直播"
)


def test_share_text_yields_bare_url():
    assert eng.extract_share_url(SHARE_TEXT) == "https://v.douyin.com/bUzExrIs2Cg/"


def test_plain_url_passes_through():
    url = "https://live.douyin.com/584947519700"
    assert eng.extract_share_url(url) == url
    assert eng.extract_share_url("") == ""
    assert eng.extract_share_url("没有链接的一段话") == ""


def test_share_text_with_chinese_punctuation_tail():
    assert eng.extract_share_url("看直播👉https://live.douyin.com/123456，快来") == \
        "https://live.douyin.com/123456"


def test_live_targets_accepts_dict_string_and_share_text():
    cfg = {"liveUrls": [
        {"url": SHARE_TEXT},
        "https://live.douyin.com/111",
        {"url": "https://live.douyin.com/111"},   # 重复要合并
        {"url": ""},
    ]}
    rows = eng.live_targets(cfg)
    assert [r["url"] for r in rows] == [
        "https://v.douyin.com/bUzExrIs2Cg/", "https://live.douyin.com/111"]


def test_live_targets_empty_when_not_configured():
    assert eng.live_targets({}) == []
    assert eng.live_targets({"liveUrls": None}) == []


def test_room_label_prefers_room_id():
    assert eng.room_label("https://live.douyin.com/584947519700") == "直播间 584947519700"
    assert eng.room_label(
        "https://webcast.amemv.com/douyin/webcast/reflow/7709004986222529318?roomId=1"
    ) == "直播间 7709004986222529318"


def test_is_live_url_covers_share_and_room_hosts():
    assert eng.is_live_url("https://v.douyin.com/bUzExrIs2Cg/")
    assert eng.is_live_url("https://live.douyin.com/584947519700")
    assert eng.is_live_url("https://webcast.amemv.com/douyin/webcast/reflow/1")
    assert not eng.is_live_url("https://www.douyin.com/video/123")


def test_resolve_keeps_unresolvable_url(monkeypatch):
    """解析失败必须原样返回，让壳自己跟随跳转，而不是把目标丢掉。"""
    eng._live_resolve_cache.clear()

    def boom(*_a, **_k):
        raise OSError("no network")

    monkeypatch.setattr(eng.urllib.request, "urlopen", boom)
    url = "https://v.douyin.com/unknown/"
    assert eng.resolve_live_url(url) == url


def test_resolve_is_cached(monkeypatch):
    eng._live_resolve_cache.clear()
    calls = []

    class Resp:
        def geturl(self):
            return "https://live.douyin.com/999"

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def fake(*_a, **_k):
        calls.append(1)
        return Resp()

    monkeypatch.setattr(eng.urllib.request, "urlopen", fake)
    url = "https://v.douyin.com/abc/"
    assert eng.resolve_live_url(url) == "https://live.douyin.com/999"
    assert eng.resolve_live_url(url) == "https://live.douyin.com/999"
    assert len(calls) == 1


def _patch_rooms(monkeypatch, rooms):
    from app.runtime import runners

    monkeypatch.setattr(runners.browser, "execute",
                        lambda cmd, **_kw: {"ok": True, "rooms": rooms})


def test_live_pass_hands_comments_to_matcher_with_actions_on(tmp_path, monkeypatch):
    """直播轮把评论交给线索匹配，并允许执行动作（直播场景剔除点赞/回复单条弹幕）。"""
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


def test_live_pass_reports_failure_without_raising(tmp_path, monkeypatch):
    from app.runtime import runners

    monkeypatch.setattr(runners, "log", lambda *a, **k: None)
    monkeypatch.setattr(runners.browser, "execute", lambda cmd, **_kw: {
        "ok": False, "error": "页面脚本执行超时（20s）"})
    ctx = runners.Ctx(kind="monitor", db_path=tmp_path / "live.db", task_id="t1",
                      name="监控任务1", account_id="acc", run_id="r1")
    out, failure = runners._live_pass(ctx, {}, "acc", ["https://live.douyin.com/1"], [], {}, 2)
    assert out["read"] == 0
    assert "超时" in failure


def test_live_room_actions_only_run_what_is_enabled(tmp_path, monkeypatch):
    """开关没开的房间级动作一个都不该发；开了的按次数带过去。"""
    from app.runtime import runners
    from app.execution import live_leadgen

    calls: list[tuple[str, str, str]] = []

    def fake_perform(db_path, action, *, account_id, task_id, video_url, text="", **kw):
        calls.append((action, video_url, text))
        return {"ok": True, "status": "sent_ok", "code": "ok", "times": 3 if action == "like-live" else 0}

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
    assert command_ok(cmd)

    # 未开启的动作必须发不出去
    try:
        live_scope.signed_command("send-dm", account_id="acc", task_id="t1",
                                  video_url="https://live.douyin.com/1", text="hi",
                                  comment_author="小王")
    except ValueError:
        pass
    else:
        raise AssertionError("未开启的动作不应通过签名")


def command_ok(cmd):
    from app.execution import live_scope

    return live_scope.command_allowed(cmd)
