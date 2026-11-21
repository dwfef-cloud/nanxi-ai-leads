from __future__ import annotations

import json
import time

import pytest

from app.execution import live_scope, live_leadgen
from app.runtime import browser

VIDEO = "https://www.douyin.com/video/7400000000000000001"


@pytest.fixture()
def scope(tmp_path, monkeypatch):
    config = {"scopeId": "acceptance-one", "secret": "x" * 40,
              "accountId": "account-one", "leadgenTaskId": "task-one", "nickname": "测试账号",
              "expiresAt": time.time() + 600, "keywords": ["智能体开发"],
              "videoUrls": [VIDEO], "actions": {"post-comment": 1}}
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("LEADS_LIVE_ACCEPTANCE_SCOPE", str(path))
    return config, path


def test_default_closed_and_payload_flags_never_enable(monkeypatch):
    monkeypatch.delenv("LEADS_LIVE_ACCEPTANCE_SCOPE", raising=False)
    assert live_scope.load_scope() == {}
    assert not live_scope.command_allowed({"type": "post-comment", "allowWrite": True, "live": True})


def test_signed_commands_bind_account_task_video_text(scope):
    cmd = live_scope.signed_command("post-comment", account_id="account-one", task_id="task-one",
                                    video_url=VIDEO, text="智能体开发要先明确业务需求")
    assert live_scope.command_allowed(cmd, "account-one")
    assert not live_scope.command_allowed(cmd, "another-account")
    for key, value in (("text", "另一条评论"), ("leadgenTaskId", "other"),
                       ("videoUrl", VIDEO + "?wrong=1"), ("type", "send-dm"), ("scopeId", "other")):
        assert not live_scope.command_allowed({**cmd, key: value})


def test_expired_or_unapproved_actions_rejected(scope):
    config, path = scope
    with pytest.raises(ValueError):
        live_scope.signed_command("send-dm", account_id="account-one", task_id="task-one", video_url=VIDEO)
    config["expiresAt"] = time.time() - 1
    path.write_text(json.dumps(config), encoding="utf-8")
    assert live_scope.load_scope() == {}


@pytest.mark.parametrize("reviewed", [None, {}, [], [{"videoUrl": VIDEO, "text": "另一条评论"}]])
def test_reviewed_comment_scope_rejects_other_text(scope, reviewed):
    config, path = scope
    config["reviewedComments"] = reviewed
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="已确认草稿"):
        live_scope.signed_command("post-comment", account_id="account-one", task_id="task-one",
                                  video_url=VIDEO, text="智能体开发要先明确业务需求")


@pytest.mark.parametrize("keyword,expected", [("智能体开发", 1), ("工作流搭建", 0)])
def test_reviewed_draft_is_reused_only_after_matching_keyword_search(scope, tmp_path, monkeypatch, keyword, expected):
    from app.api import task_runner as runner
    from app import leadgen_pools
    from app.runtime import store
    config, path = scope
    text = "定制开发时，固定工作流和自主规划应该如何选择？"
    config["reviewedComments"] = [{"videoUrl": VIDEO, "keyword": keyword, "text": text}]
    path.write_text(json.dumps(config), encoding="utf-8")
    db = tmp_path / "reviewed.db"
    leadgen_pools.save_task_record(db, {"id": "task-one", "name": "已确认草稿验收"})
    ctx = runner.RunCtx(task_id="automation-reviewed", leadgen_id="task-one", account_id="account-one",
                        payload={"keywords": "智能体开发"}, db_path=db)
    ctx.run_id = store.run_begin(db, "leadgen", ctx.task_id, "已确认草稿验收", ctx.account_id)
    monkeypatch.setattr(runner.event_bus, "publish", lambda *a: None)
    monkeypatch.setattr(browser, "execute", lambda *a, **k:
                        {"ok": True, "items": [{"url": VIDEO, "title": "智能体定制开发"}]})
    monkeypatch.setattr(live_leadgen, "generate", lambda *a: pytest.fail("不能重新生成已确认的评论"))
    attempts = []
    monkeypatch.setattr(live_leadgen, "perform", lambda db, action, **kwargs:
                        attempts.append((action, kwargs)) or {"ok": True, "status": "sent_ok"})
    live_leadgen.run(ctx)
    assert len(attempts) == expected
    record = leadgen_pools.load_records(db)["task-one"]
    assert record["status"] == ("completed" if expected else "failed")
    if attempts:
        assert attempts[0][1]["text"] == text
        assert attempts[0][1]["video_url"] == VIDEO
        assert record["stats"]["published"] == 1


@pytest.mark.parametrize("field,value", [("expiresAt", float("nan")), ("expiresAt", True),
    ("expiresAt", "later"), ("actions", []), ("videoUrls", VIDEO)])
def test_malformed_scope_fails_closed(scope, field, value):
    config, path = scope
    config[field] = value
    path.write_text(json.dumps(config), encoding="utf-8")
    assert live_scope.load_scope() == {}


def test_comment_like_signature_binds_author_and_exact_comment(scope):
    config, path = scope
    config["actions"] = {"like-comment": 1}
    config["commentTargets"] = [{"videoUrl": VIDEO, "commentId": "123456789",
                                "authorName": "作者", "content": "需要工作流"}]
    path.write_text(json.dumps(config), encoding="utf-8")
    cmd = live_scope.signed_command("like-comment", account_id="account-one", task_id="task-one",
        video_url=VIDEO, comment_id="123456789", comment_author="作者", text="需要工作流")
    assert live_scope.command_allowed(cmd, "account-one")
    for field in ("commentAuthor", "commentId", "text", "accountId"):
        assert not live_scope.command_allowed({**cmd, field: "changed"})


def test_native_comment_scope_is_separate_and_missing_scope_cannot_fall_back(scope, tmp_path, monkeypatch):
    config, path = scope
    native = {**config, "scopeId":"native-only", "actions":{"like-comment":1},
              "commentTargets":[{"videoUrl":VIDEO,"commentId":"123456789","authorName":"作者","content":"工作流"}]}
    selected = tmp_path / "native.json"
    monkeypatch.setenv("LEADS_COMMENT_LIKE_SCOPE", str(selected))
    assert live_scope.load_scope("like-comment") == {}
    assert live_scope.load_scope()["scopeId"] == config["scopeId"]
    selected.write_text(json.dumps(native), encoding="utf-8")
    cmd = live_scope.signed_command("like-comment", account_id="account-one",task_id="task-one",
        video_url=VIDEO,comment_id="123456789",comment_author="作者",text="工作流")
    assert cmd["scopeId"] == "native-only"
    assert live_scope.command_allowed(cmd)
    assert live_scope.signed_command("post-comment",account_id="account-one",task_id="task-one",
        video_url=VIDEO,text="原评论授权")['scopeId'] == config["scopeId"]


def test_uncertain_receipt_blocks_second_command_across_gate_instances(scope, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(browser, "execute", lambda *a, **k: calls.append(a) or
                        {"ok": False, "status": "uncertain", "code": "receipt_missing"})
    kwargs = dict(account_id="account-one", task_id="task-one", video_url=VIDEO, text="需要先明确业务需求")
    first = live_leadgen.perform(tmp_path / "live.db", "post-comment", **kwargs)
    second = live_leadgen.perform(tmp_path / "live.db", "post-comment", **kwargs)
    assert first["status"] == "uncertain"
    assert second["code"] == "uncertain_pending_manual"
    assert len(calls) == 1


def test_readback_reconciliation_rejects_mismatch_and_keeps_dedup(scope, tmp_path, monkeypatch):
    from app.execution.persistent_gate import PersistentWriteSafetyGate
    db = tmp_path / "review.db"
    monkeypatch.setattr(browser, "execute", lambda *a, **k: {"ok": False, "status": "uncertain"})
    text = "工具调用与工作流需要明确权限边界"
    live_leadgen.perform(db, "post-comment", account_id="account-one", task_id="task-one", video_url=VIDEO, text=text)
    confirmation = {"http": 200, "platform_code": 0, "video_id": VIDEO.split("/")[-1],
                    "comment_id": "123456789", "text": text, "nickname": "测试账号"}
    assert not live_leadgen.reconcile_readback(db, 1, {**confirmation, "nickname": "别的账号"})
    assert not live_leadgen.reconcile_readback(db, 1, {**confirmation, "text": "别的正文"})
    assert live_leadgen.reconcile_readback(db, 1, confirmation)
    key = json.dumps(["account-one", "post-comment", VIDEO, ""], separators=(",", ":"))
    assert PersistentWriteSafetyGate(db, namespace="live").can_attempt(key) == (False, "already_sent")
    assert not live_leadgen.reconcile_readback(db, 1, confirmation)


def test_generated_text_uses_actual_title_and_no_template_fallback(monkeypatch):
    prompts = []
    monkeypatch.setattr(live_leadgen.ai_personas, "_llm_settings", lambda _: {})
    monkeypatch.setattr(live_leadgen.ai_personas, "_call_chat", lambda cfg, prompt:
                        prompts.append(prompt) or json.dumps({"should_reply": True,
                        "reply_content": "工具调用和记忆模块分开设计，排查工作流会清楚一些。", "thought": "回应架构内容"}))
    result = live_leadgen.generate("unused", {}, {"title": "智能体工具调用和记忆架构"})
    assert result["ok"]
    assert "智能体工具调用和记忆架构" in prompts[0]
    monkeypatch.setattr(live_leadgen.ai_personas, "_call_chat", lambda *a: '{"should_reply": false}')
    assert not live_leadgen.generate("unused", {}, {"title": "无关内容"})["ok"]


@pytest.mark.parametrize("response,code", [
    ({"should_reply": False, "thought": "内容与当前业务无关"}, "not_suitable"),
    ({"should_reply": "true", "reply_content": "这是长度合格的正文"}, "invalid_decision"),
    ({"should_reply": True, "reply_content": []}, "empty_content"),
    ({"should_reply": True, "reply_content": ""}, "empty_content"),
    ({"should_reply": True, "reply_content": "好"}, "invalid_length"),
    ({"should_reply": True, "reply_content": "文" * 201}, "invalid_length"),
])
def test_generation_explains_rejection_without_coercing_model_output(monkeypatch, response, code):
    monkeypatch.setattr(live_leadgen.ai_personas, "_llm_settings", lambda _: {})
    monkeypatch.setattr(live_leadgen.ai_personas, "_call_chat", lambda *a: json.dumps(response))
    result = live_leadgen.generate("unused", {}, {"title": "智能体教程"})
    assert not result["ok"] and result["code"] == code
    if code == "not_suitable":
        assert response["thought"] in result["reason"]


def test_main_comment_and_reply_use_distinct_model_scenes(monkeypatch):
    prompts = []
    monkeypatch.setattr(live_leadgen.ai_personas, "_llm_settings", lambda _: {})
    monkeypatch.setattr(live_leadgen.ai_personas, "_call_chat", lambda cfg, prompt:
                        prompts.append(prompt) or '{"should_reply":false}')
    live_leadgen.generate("unused", {"aiGoal": "寻找有需求的潜客"}, {"title": "智能体教程"})
    live_leadgen.generate("unused", {}, {"title": "智能体教程", "nickname": "用户甲", "comment": "节点报错怎么办"})
    assert "潜客需求在评论区另行识别" in prompts[0]
    assert "评论内容：节点报错怎么办" in prompts[1]
    assert "评论者昵称：用户甲" in prompts[1]
    assert "当前生成的是视频公开主评" not in prompts[1]


def test_model_auth_error_is_safe_and_task_leaves_running_state(scope, tmp_path, monkeypatch):
    from urllib.error import HTTPError
    from app.api import task_runner as runner
    from app import leadgen_pools
    from app.runtime import store
    db = tmp_path / "failed.db"
    leadgen_pools.save_task_record(db, {"id": "task-one", "name": "验收任务"})
    ctx = runner.RunCtx(task_id="automation-one", leadgen_id="task-one", account_id="account-one",
                        payload={"keywords": "智能体开发"}, db_path=db)
    ctx.run_id = store.run_begin(db, "leadgen", ctx.task_id, "验收任务", ctx.account_id)
    runner._RUNS[ctx.task_id] = ctx
    events = []
    monkeypatch.setattr(runner.event_bus, "publish", lambda channel, data: events.append((channel, data)))
    monkeypatch.setattr(browser, "execute", lambda *a, **k: {"ok": True, "items": [{"url": VIDEO, "title": "智能体开发"}]})
    monkeypatch.setattr(live_leadgen.ai_personas, "_llm_settings", lambda _: {})
    def failed(*args):
        try:
            raise HTTPError("https://model.invalid", 401, "secret response", {}, None)
        except HTTPError as exc:
            raise RuntimeError("secret response body") from exc
    monkeypatch.setattr(live_leadgen.ai_personas, "_call_chat", failed)
    live_leadgen.run(ctx)
    assert ctx.task_id not in runner.active_ids()
    record = leadgen_pools.load_records(db)["task-one"]
    assert record["status"] == "failed"
    assert record["endReason"] == "ai_critical_failure"
    assert "HTTP 401" in record["lastMessage"]
    assert "secret" not in json.dumps(events)
    finished = [data for ch, data in events if ch == "leadgen-task-finished"]
    assert len(finished) == 1
    assert finished[0]["reason"] == "ai_critical_failure"
    assert finished[0]["progress"]["interactions"]["done"] == 0
