"""监控动作引擎与监控作用域的契约测试。

覆盖页面承诺但此前后端零消费的行为：多账号、失效链接、优先关键词、
仅评论研判、每日配额、监控作用域（含私信/关注）、Webhook 目标解析。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.execution import live_scope
from app.runtime import monitor_engine as eng


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "monitor_engine.db"
    sqlite3.connect(str(path)).close()
    return path


def test_accounts_accepts_ids_objects_and_single():
    cfg = {"selectedAccounts": ["a1", {"accountId": "a2"}, {"id": "a3"}],
           "accountId": "a0"}
    assert eng.accounts(cfg) == ["a0", "a1", "a2", "a3"]
    assert eng.accounts({}) == []


def test_invalid_links_are_skipped_at_runtime():
    cfg = {"videoUrls": [{"url": "https://a", "invalid": False},
                         {"url": "https://b", "invalid": True},
                         "https://c"]}
    assert eng.valid_urls(cfg, "videoUrls") == ["https://a", "https://c"]
    assert eng.invalid_count(cfg, "videoUrls") == 1


def test_priority_keyword_hit_is_case_insensitive_and_partial():
    cfg = {"aiPriorityKeywords": "报价\n合作"}
    assert eng.priority_hit("请问怎么合作？", cfg) == "合作"
    assert eng.priority_hit("多少Q", cfg) == ""


def test_limits_only_apply_when_enabled():
    assert eng.limit_of({"autoReplyLimitEnabled": False}, "reply") == 0
    assert eng.limit_of({"autoReplyLimitEnabled": True, "autoReplyLimitMin": 7}, "reply") == 7


def test_scopes_cover_only_switched_on_actions():
    scopes = eng.scopes_for({"enableAutoLike": True, "enableAutoDM": True,
                             "enableAutoFollow": False})
    assert set(scopes) == {"like-comment", "send-dm"}


def test_quota_blocks_after_limit_and_resets_next_logical_day(db):
    cfg = {"actionCountAutoResetTime": "00:00"}
    assert eng.quota_take(db, "a1", "reply-comment", 2, cfg) is True
    assert eng.quota_take(db, "a1", "reply-comment", 2, cfg) is True
    assert eng.quota_take(db, "a1", "reply-comment", 2, cfg) is False
    assert eng.quota_used(db, "a1", "reply-comment", cfg) == 2
    # 换到明天：配额重新可用
    today = eng._logical_day(cfg)
    with sqlite3.connect(str(db)) as con:
        con.execute("UPDATE monitor_action_quota SET day='1999-01-01' WHERE day=?", (today,))
    assert eng.quota_take(db, "a1", "reply-comment", 2, cfg) is True


def test_plan_drops_actions_over_quota(db):
    cfg = {"enableAutoLike": True, "enableAutoReply": True,
           "autoReplyLimitEnabled": True, "autoReplyLimitMin": 1}
    intents = eng.plan(cfg, "a1", {"nickname": "n", "text": "t"}, db_path=db,
                       limits={"reply": 1})
    assert {i.type for i in intents} == {"like-comment", "reply-comment"}
    eng.quota_take(db, "a1", "reply-comment", 1, cfg)
    intents = eng.plan(cfg, "a1", {"nickname": "n", "text": "t"}, db_path=db,
                       limits={"reply": 1})
    assert {i.type for i in intents} == {"like-comment"}


def test_dm_texts_respects_count_and_skips_blank():
    cfg = {"dmTemplate": "你好", "dmExtraTemplates": ["第二句", "", "第三句"],
           "dmMessageCount": 2}
    assert eng.dm_texts(cfg) == ["你好", "第二句"]


def test_webhook_target_switches_by_type():
    assert eng.webhook_url({"webhookType": "dingtalk",
                            "webhookUrlDingtalk": "https://d"}) == "https://d"
    assert eng.webhook_url({"webhookType": "feishu",
                            "webhookUrlFeishu": "https://f"}) == "https://f"
    body = eng.webhook_payload({"webhookType": "feishu"},
                               {"nickname": "小明", "comment": "多少钱"}, "监控A")
    assert "小明" in body["content"]["text"] and "监控A" in body["content"]["text"]


def test_monitor_scope_allows_dm_and_wildcard_video(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_MONITOR_SCOPE_DIR", str(tmp_path))
    live_scope.write_monitor_scope(task_id="mon_1", account_id="a1",
                                   actions={"send-dm": 5, "post-comment": 2})
    cmd = live_scope.signed_command("send-dm", account_id="a1", task_id="mon_1",
                                    video_url="https://v/1", text="你好",
                                    recipient_id="u1")
    assert cmd["scopeId"] and cmd["acceptanceSignature"]
    assert live_scope.command_allowed(cmd, "a1") is True
    # 未开启的动作仍然被拒
    with pytest.raises(ValueError):
        live_scope.signed_command("follow-user", account_id="a1", task_id="mon_1",
                                  video_url="https://v/1", recipient_id="u1")


def test_monitor_scope_is_task_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_MONITOR_SCOPE_DIR", str(tmp_path))
    live_scope.write_monitor_scope(task_id="mon_1", account_id="a1",
                                   actions={"reply-comment": 3})
    with pytest.raises(ValueError):
        live_scope.signed_command("reply-comment", account_id="a1", task_id="mon_2",
                                  video_url="https://v/1", text="hi")
    live_scope.clear_monitor_scope("mon_1", "a1")
    with pytest.raises(ValueError):
        live_scope.signed_command("reply-comment", account_id="a1", task_id="mon_1",
                                  video_url="https://v/1", text="hi")


def test_live_acceptance_scope_still_rejects_unreviewed_dm(tmp_path, monkeypatch):
    """非监控作用域（preview 验收）不能被这次改动放宽。"""
    import json
    import time

    path = tmp_path / "accept.json"
    path.write_text(json.dumps({
        "scopeId": "s1", "secret": "x" * 40, "accountId": "a1",
        "leadgenTaskId": "t1", "expiresAt": time.time() + 600,
        "actions": {"send-dm": 1}, "videoUrls": ["https://v/1"]}), encoding="utf-8")
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE", str(path))
    with pytest.raises(ValueError):
        live_scope.signed_command("send-dm", account_id="a1", task_id="t1",
                                  video_url="https://v/1", text="hi", recipient_id="u1")


def test_run_actions_uses_template_and_counts_quota(db, monkeypatch):
    """命中评论 → 真的走 perform；模板模式不再调用模型。"""
    from app.runtime import runners

    calls = []

    def fake_perform(db_path, action, **kw):
        calls.append((action, kw.get("text"), kw.get("recipient_id")))
        return {"ok": True, "status": "sent_ok"}

    monkeypatch.setattr("app.execution.live_leadgen.perform", fake_perform, raising=True)
    ctx = runners.Ctx(kind="monitor", db_path=db, task_id="m1", name="监控A",
                      account_id="a1", run_id="r1")
    cfg = {"enableAutoLike": True, "enableAutoReply": True,
           "replyMode": "template", "replyTemplate": "谢谢 {nickname} 的关注",
           "actionDelaySecondsMin": 0, "actionDelaySecondsMax": 0}
    out = runners._run_actions(ctx, cfg, "a1", {
        "nickname": "小明", "text": "怎么卖", "videoUrl": "https://v/1",
        "commentId": "c1", "userId": "u1"}, {"like": 0, "reply": 0})
    assert out == {"ok": 2, "failed": 0}
    assert [c[0] for c in calls] == ["like-comment", "reply-comment"]
    assert calls[1][1] == "谢谢 小明 的关注"
    assert eng.quota_used(db, "a1", "reply-comment", cfg) == 1


def test_run_actions_reports_failure_without_raising(db, monkeypatch):
    from app.runtime import runners

    monkeypatch.setattr("app.execution.live_leadgen.perform",
                        lambda *a, **k: {"ok": False, "status": "blocked",
                                         "code": "live_writes_disabled"}, raising=True)
    ctx = runners.Ctx(kind="monitor", db_path=db, task_id="m1", name="监控A",
                      account_id="a1", run_id="r1")
    out = runners._run_actions(ctx, {"enableAutoFollow": True,
                                     "actionDelaySecondsMin": 0,
                                     "actionDelaySecondsMax": 0}, "a1", {
        "nickname": "小明", "text": "x", "videoUrl": "https://v/1",
        "commentId": "c1", "userId": "u1"}, {"follow": 0})
    assert out == {"ok": 0, "failed": 1}
