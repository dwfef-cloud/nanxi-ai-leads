"""监控主播直播（author_live）的接通测试。

2026-11-17 前的状态：前端选「监控主播直播」时把主播主页链接存进
liveAuthorUrls，但后端 target_types / run_monitor 只读 liveUrls ——
选了等于空目标，0.1 秒判「没有目标」退出。

接通约定：
* monitor_engine.author_live_targets() 规范化 liveAuthorUrls（兼容 dict/纯链接/分享文案）；
* runners.run_monitor 把 author_live_urls 传给 _live_pass，随 read-live-events
  下发 authorUrls，壳负责「打开主页 → 检测直播中 → 解析直播间 → 采集」；
* 壳对未开播的主播回 status:"not_live"，_live_pass 记日志后跳过本轮。
"""
from __future__ import annotations

import pytest

from app.runtime import monitor_engine as eng


def test_author_live_targets_accepts_dict_string_and_share_text():
    cfg = {"liveAuthorUrls": [
        {"url": "https://www.douyin.com/user/MS4wLjABAAAA111"},
        "https://v.douyin.com/abc123/ 或长按复制此条消息，打开抖音",
        {"url": "https://www.douyin.com/user/MS4wLjABAAAA111"},   # 重复要合并
        {"url": ""},
    ]}
    rows = eng.author_live_targets(cfg)
    assert [r["url"] for r in rows] == [
        "https://www.douyin.com/user/MS4wLjABAAAA111",
        "https://v.douyin.com/abc123/",
    ]


def test_author_live_targets_empty_when_not_configured():
    assert eng.author_live_targets({}) == []
    assert eng.author_live_targets({"liveAuthorUrls": None}) == []
    assert eng.author_live_targets({"liveAuthorUrls": "https://x"}) == []  # 只认数组


def test_run_monitor_passes_author_urls_to_live_pass(tmp_path, monkeypatch):
    """run_monitor 应把 liveAuthorUrls 与 liveUrls 一起下发（单轮视频侧为空也可走通）。"""
    from app.runtime import runners

    seen_cmds: list[dict] = []

    def fake_execute(cmd, **_kw):
        seen_cmds.append(cmd)
        if cmd.get("type") == "read-live-events":
            return {"ok": True, "rooms": [
                {"url": "https://live.douyin.com/9", "finalUrl": "https://live.douyin.com/9",
                 "status": "not_live", "events": []}]}
        return {"ok": True, "items": []}

    monkeypatch.setattr(runners.browser, "execute", fake_execute)
    monkeypatch.setattr(runners, "_match_comments",
                        lambda *a, **kw: {"new": 0, "matched": 0, "filtered": 0,
                                          "aiFailed": 0, "actions": 0, "actionFailed": 0})
    logs: list[str] = []
    monkeypatch.setattr(runners, "log",
                        lambda ctx, msg, level="normal": logs.append(str(msg)))

    rec = {"id": "monitor_test", "task_type": "monitor", "config": {
        "monitorTargetTypes": ["author_live"],
        "liveAuthorUrls": [{"url": "https://www.douyin.com/user/MS4wLjABAAAA222"}],
        "monitorInteractionVersion": 2,
        "maxCycles": 1,
        "intervalSecondsMin": 15, "intervalSecondsMax": 16,
    }}
    runners.run_monitor(tmp_path / "t.db", rec, runners.Ctx(kind="monitor", db_path=tmp_path / "t.db",
                    task_id="monitor_test", name="主播直播监控", account_id="acc", run_id="r1"))

    live_cmds = [c for c in seen_cmds if c.get("type") == "read-live-events"]
    assert live_cmds, "read-live-events 未被调用"
    assert live_cmds[0].get("authorUrls") == ["https://www.douyin.com/user/MS4wLjABAAAA222"]


def test_live_pass_skips_not_live_author(monkeypatch, tmp_path):
    """壳回 status:"not_live" 时记「主播未开播」并跳过，不当成错误。"""
    from app.runtime import runners

    monkeypatch.setattr(runners.browser, "execute", lambda cmd, **_kw: {"ok": True, "rooms": [
        {"url": "https://www.douyin.com/user/MS4wLjABAAAA333",
         "finalUrl": "https://www.douyin.com/user/MS4wLjABAAAA333",
         "status": "not_live", "events": []}]})
    logs: list[str] = []
    monkeypatch.setattr(runners, "log",
                        lambda ctx, msg, level="normal": logs.append(str(msg)))
    monkeypatch.setattr(runners, "_match_comments",
                        lambda *a, **kw: {"new": 0, "matched": 0, "filtered": 0,
                                          "aiFailed": 0, "actions": 0, "actionFailed": 0})
    ctx = runners.Ctx(kind="monitor", db_path=tmp_path / "t.db", task_id="t1",
                      name="x", account_id="acc", run_id="r1")
    out, failure = runners._live_pass(
        ctx, {"liveActionEventTypes": ["comment"]}, "acc",
        ["https://www.douyin.com/user/MS4wLjABAAAA333"], [], {}, 1,
        author_live_urls=["https://www.douyin.com/user/MS4wLjABAAAA333"])

    assert failure == ""
    assert out["read"] == 0
    assert any("主播未开播" in msg for msg in logs)
