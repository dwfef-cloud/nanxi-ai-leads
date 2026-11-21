from __future__ import annotations

from app.api import ipc_bridge, self_warmup, self_warmup_engine, task_logs, task_store


def _db(tmp_path):
    return tmp_path / "self-warmup.sqlite3"


def _record(db_path, *, task_id="reply-1", events=None):
    return task_store.save_task(db_path, "warmup", {
        "id": task_id,
        "name": "自动回复测试",
        "status": "ready",
        "events": events or [],
    })


def test_events_filter_paginate_and_aggregate_real_persisted_payload(tmp_path):
    db_path = _db(tmp_path)
    _record(db_path, events=[
        {"id": "e1", "nickname": "甲", "eventType": "comment", "status": "replied",
         "content": "工作流怎么搭", "accountId": "a1", "replyCount": 2},
        {"id": "e2", "nickname": "甲", "eventType": "comment", "status": "failed",
         "content": "不相关", "accountId": "a1"},
        {"id": "e3", "nickname": "乙", "eventType": "follow", "status": "success",
         "content": "关注", "accountId": "a2"},
    ])

    result = self_warmup.list_events(db_path, {
        "taskId": "reply-1", "keyword": "工作流", "offset": 0, "limit": 1,
    })
    assert result["total"] == 1
    assert result["items"][0]["id"] == "e1"
    assert self_warmup.user_stats(db_path, {"taskId": "reply-1"})["items"] == [
        {"nickname": "甲", "total": 2, "success": 1, "failed": 1, "replies": 2},
        {"nickname": "乙", "total": 1, "success": 1, "failed": 0, "replies": 0},
    ]
    assert self_warmup.account_stats(db_path, {"taskId": "reply-1"})["total"] == 2


def test_delete_and_clear_category_only_change_selected_tasks_events(tmp_path):
    db_path = _db(tmp_path)
    _record(db_path, events=[
        {"id": "c1", "eventType": "comment"},
        {"id": "f1", "eventType": "follow"},
    ])
    _record(db_path, task_id="reply-2", events=[{"id": "other", "eventType": "comment"}])

    deleted = self_warmup.delete_events(db_path, {"taskId": "reply-1", "eventIds": ["c1"]})
    assert deleted["success"] is True and deleted["deleted"] == 1
    cleared = self_warmup.clear_category(db_path, {"taskId": "reply-1", "category": "follow"})
    assert cleared["ok"] is True and cleared["deleted"] == 1
    assert self_warmup.list_events(db_path, {"taskId": "reply-1"})["total"] == 0
    assert self_warmup.list_events(db_path, {"taskId": "reply-2"})["total"] == 1


def test_delete_task_skips_running_task(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    _record(db_path)
    monkeypatch.setattr("app.runtime.registry.active_ids", lambda kind: ["reply-1"])
    result = self_warmup.delete_tasks(db_path, {"taskIds": ["reply-1"]})
    assert result["success"] is False
    assert result["skipped"] == ["reply-1"]
    assert task_store.get_task(db_path, "warmup", "reply-1") is not None


def test_logs_are_task_scoped_and_clearable(tmp_path):
    db_path = _db(tmp_path)
    task_logs.append(db_path, "r1", "reply-1", "a1", "第一条")
    task_logs.append(db_path, "r2", "reply-2", "a2", "另一任务")
    assert [row["message"] for row in self_warmup.logs(db_path, {"taskId": "reply-1"})["logs"]] == ["第一条"]
    assert self_warmup.clear_logs(db_path, {"taskId": "reply-1"})["deleted"] == 1
    assert self_warmup.logs(db_path, {"taskId": "reply-1"})["logs"] == []
    assert len(self_warmup.logs(db_path, {"taskId": "reply-2"})["logs"]) == 1


def test_live_reply_retry_and_round_reset_fail_closed_without_event_source():
    retry = self_warmup.retry_events({"taskId": "reply-1", "eventIds": ["e1"]})
    assert retry["success"] is False
    assert retry["code"] == "reply_executor_unavailable"
    assert "未执行、未发送" in retry["error"]
    assert self_warmup.clear_user_rounds({"taskId": "reply-1"})["ok"] is False


def test_auto_reply_task_never_falls_through_to_warmup_runner(tmp_path, monkeypatch):
    """自动回复任务走自己的运行时，绝不会被当成养号去刷推荐流。

    2026-11-17 之前这里断言的是「启动即拒绝」（当时没有平台通知读取器）。
    私信读取器接通后，正确的行为变成：可以启动，但执行的必须是 autoreply 而不是 warmup。
    """
    from app.runtime import registry, runners

    db_path = _db(tmp_path)
    task_store.save_task(db_path, "warmup", {
        "id": "auto-reply-1",
        "name": "自动回复任务",
        "status": "ready",
        "configSnapshot": {
            "enableAutoActions": True,
            "watchMessages": True,
            "selectedAccounts": ["account-1"],
            "nurtureDurationMin": 60,
        },
    })
    calls = []
    monkeypatch.setattr(registry.browser, "status", lambda _db: {"engine": "shell"})
    monkeypatch.setattr(runners, "resolve_account", lambda _db, _rec, _cfg: "account-1")
    monkeypatch.setattr(runners, "run_auto_reply",
                        lambda _db, _rec, ctx: calls.append(("autoreply", ctx.task_id)))
    monkeypatch.setattr(runners, "run_warmup",
                        lambda _db, _rec, ctx: calls.append(("warmup", ctx.task_id)))

    result = registry.start(db_path, "warmup", "auto-reply-1")

    assert result["success"] is True, result
    registry.wait_idle(db_path, 3)
    assert calls == [("autoreply", "auto-reply-1")]
    assert registry.active_ids("warmup") == []


def test_frontend_ipc_channels_are_mapped_to_local_handlers(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db_path)
    _record(db_path, events=[{"id": "e1", "nickname": "甲", "eventType": "comment"}])
    channels = {
        "get-self-warmup-events", "get-self-warmup-user-stats",
        "get-self-warmup-account-stats", "delete-self-warmup-events",
        "delete-self-warmup-tasks", "retry-self-warmup-events",
        "stop-self-warmup-manual-retry", "clear-self-warmup-user-rounds",
        "clear-self-warmup-event-category", "clear-self-warmup-task-logs",
        "get-self-warmup-task-logs",
    }
    assert channels <= set(ipc_bridge.EXPLICIT) | set(ipc_bridge.EMPTY_CHANNELS)
    events = ipc_bridge.dispatch("get-self-warmup-events", [{"taskId": "reply-1"}])
    assert events["total"] == 1 and events["items"][0]["id"] == "e1"
    retry = ipc_bridge.dispatch("retry-self-warmup-events", [{"taskId": "reply-1"}])
    assert retry["success"] is False and retry["code"] == "reply_executor_unavailable"


def test_offline_event_produces_persisted_proposal_without_platform_writes(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db_path)
    task_store.save_task(db_path, "warmup", {
        "id": "reply-sim",
        "name": "离线自动回复验收",
        "status": "ready",
        "configSnapshot": {
            "enableAutoActions": True,
            "watchComments": True,
            "watchMessages": True,
            "actionCommentReply": True,
            "actionMessageReply": True,
            "keywords": "工作流,智能体",
            "excludeUsers": "广告账号",
            "replyTemplateItems": [{"text": "你好 {nickname}，关于 {content} 收到啦"}],
            "selectedAccounts": ["account-1"],
        },
        "events": [],
        "stats": {},
    })

    result = ipc_bridge.dispatch("simulate-self-warmup-event", [{
        "simulation": True,
        "taskId": "reply-sim",
        "event": {
            "eventType": "comment", "nickname": "小林",
            "content": "想了解智能体工作流", "accountId": "account-1",
            "timestamp": "event-time-1", "commentId": "must-not-persist",
        },
    }])

    assert result["success"] is True and result["mode"] == "simulation"
    assert result["sourceAvailable"] is False
    assert result["event"]["status"] == "suggested"
    assert result["event"]["actions"] == [{
        "type": "reply", "status": "proposal_only",
        "text": "你好 小林，关于 想了解智能体工作流 收到啦",
    }]
    saved = task_store.get_task(db_path, "warmup", "reply-sim")
    assert len(saved["events"]) == 1
    assert "commentId" not in saved["events"][0]
    assert saved["stats"]["simulatedActions"] == 1
    assert saved["stats"]["actionsExecuted"] == 0

    duplicate = ipc_bridge.dispatch("simulate-self-warmup-event", [{
        "simulation": True, "taskId": "reply-sim",
        "event": {"eventType": "comment", "nickname": "小林",
                  "content": "想了解智能体工作流", "accountId": "account-1",
                  "timestamp": "event-time-1"},
    }])
    assert duplicate["success"] is True and duplicate["duplicate"] is True
    assert len(task_store.get_task(db_path, "warmup", "reply-sim")["events"]) == 1


def test_offline_event_requires_explicit_simulation_and_respects_rules(tmp_path):
    db_path = _db(tmp_path)
    task_store.save_task(db_path, "warmup", {
        "id": "reply-rules", "name": "规则验收", "status": "ready",
        "configSnapshot": {"enableAutoActions": True, "watchComments": True,
                           "actionCommentReply": True, "keywords": "工作流",
                           "excludeUsers": "屏蔽用户"},
        "events": [],
    })

    assert self_warmup_engine.simulate_event(db_path, {
        "taskId": "reply-rules", "event": {"eventType": "comment"},
    })["code"] == "simulation_required"
    excluded = self_warmup_engine.simulate_event(db_path, {
        "simulation": True, "taskId": "reply-rules",
        "event": {"eventType": "comment", "nickname": "屏蔽用户",
                  "content": "工作流怎么配置"},
    })
    assert excluded["event"]["reason"] == "excluded_user"
    mismatch = self_warmup_engine.simulate_event(db_path, {
        "simulation": True, "taskId": "reply-rules",
        "event": {"eventType": "comment", "nickname": "访客",
                  "content": "你好"},
    })
    assert mismatch["event"]["reason"] == "keyword_mismatch"


def test_offline_event_never_marks_proposed_actions_as_executed():
    result = self_warmup_engine.evaluate_event({
        "enableAutoActions": True, "watchMessages": True,
        "actionMessageReply": True, "dmTemplate": "收到啦，{nickname}",
    }, {"eventType": "message", "nickname": "阿明", "content": "你好"})
    assert result["actions"] == [{
        "type": "reply", "status": "proposal_only", "text": "收到啦，阿明",
    }]
    assert all(action["status"] != "sent" for action in result["actions"])

    group = self_warmup_engine.evaluate_event({
        "enableAutoActions": True, "watchMessages": True,
        "excludeGroupChats": True, "actionMessageReply": True,
    }, {"eventType": "message", "nickname": "群聊", "isGroupChat": True})
    assert group["reason"] == "group_chat_excluded"
    assert group["actions"] == []


def test_delete_tasks_accepts_bare_id_array(tmp_path):
    """壳的 invoke(ch, [ids]) 走 RAW_ARGS 后传裸数组：
    delete_tasks 必须接住数组入参，否则 ids 落空 → 假成功（弹「已删除」实际删 0 条）。"""
    db_path = _db(tmp_path)
    for i in ("ar-1", "ar-2"):
        task_store.save_task(db_path, "warmup", {"id": i, "name": i, "status": "stopped"})
    out = self_warmup.delete_tasks(db_path, ["ar-1", "ar-2"])
    assert out["success"] is True, out
    assert sorted(out["deleted"]) == ["ar-1", "ar-2"], out
    assert task_store.list_tasks(db_path, "warmup") == []

def test_delete_tasks_partial_skips_running_keeps_rest(tmp_path, monkeypatch):
    """混合选择（部分运行中、部分已停止）必须删掉能删的：
    2026-11-20 用户实测——只要有一条被跳过就整体 success:false，前端中止、
    列表不刷新，已删掉的部分看起来像「还是删除不了」。"""
    db_path = _db(tmp_path)
    for i in ("ar-run", "ar-ok"):
        task_store.save_task(db_path, "warmup", {"id": i, "name": i, "status": "stopped"})
    monkeypatch.setattr("app.runtime.registry.active_ids", lambda kind: ["ar-run"])
    out = self_warmup.delete_tasks(db_path, ["ar-run", "ar-ok"])
    assert out["success"] is True, out
    assert out["deleted"] == ["ar-ok"], out
    assert out["skipped"] == ["ar-run"], out
    assert out.get("partial") is True, out
    assert "运行中" in out.get("message", ""), out
    assert task_store.get_task(db_path, "warmup", "ar-run") is not None
    assert task_store.get_task(db_path, "warmup", "ar-ok") is None
    out2 = self_warmup.delete_tasks(db_path, ["ar-run"])
    assert out2["success"] is False
    assert "运行中" in (out2.get("error") or ""), out2

