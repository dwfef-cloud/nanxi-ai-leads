from __future__ import annotations

import sqlite3

import pytest

from app.api import aggregate, ipc_bridge, task_store
from app.runtime import manual_retry, registry, store


@pytest.fixture
def monitor_db(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE leads (
            id TEXT PRIMARY KEY, nickname TEXT, comment TEXT, source_keyword TEXT,
            source TEXT, source_task_id TEXT, source_url TEXT, video TEXT, account TEXT,
            status TEXT, score INTEGER, intent_level TEXT, platform TEXT,
            created_at TEXT, updated_at TEXT
        )""")
    store.init(path)
    task_store.save_task(path, "monitor", {
        "id": "mon_test", "name": "监控测试", "status": "ready",
        "configSnapshot": {"accountId": "acct-test"},
    })
    store.insert_lead(
        path, lead_id="lead_local_1", nickname="测试用户", comment="想了解工作流如何搭建",
        keyword="工作流", source="monitor_comment", task_id="mon_test",
        video_url="https://www.douyin.com/video/7400000000000000001",
        video_id="7400000000000000001", comment_id="comment_platform_1",
        comment_user_id="user_platform_1", account="acct-test",
    )
    monkeypatch.setattr(aggregate, "DB_PATH", path)
    return path


def _scope():
    return {
        "accountId": "acct-test", "leadgenTaskId": "mon_test",
        "reviewedActions": [{
            "type": "reply-comment", "videoUrl": "https://www.douyin.com/video/7400000000000000001",
            "text": "可以先从固定工作流开始，再按复杂度逐步增加智能体规划。",
            "sourceText": "想了解工作流如何搭建", "commentId": "comment_platform_1",
            "commentAuthor": "测试用户", "recipientId": "user_platform_1",
        }],
    }


def test_monitor_match_view_has_local_selectable_id_but_no_platform_ids(monitor_db):
    result = aggregate.monitor_task_matches({"taskId": "mon_test"})
    assert result["total"] == 1
    assert result["items"][0]["id"] == "lead_local_1"
    assert result["items"][0]["commentText"] == "想了解工作流如何搭建"
    ipc_bridge.assert_public_safe(result)
    serialized = str(result)
    assert "comment_platform_1" not in serialized
    assert "user_platform_1" not in serialized


def test_monitor_match_view_honors_frontend_pagination_and_search(monitor_db):
    store.insert_lead(
        monitor_db, lead_id="lead_local_2", nickname="另一用户", comment="想咨询智能体报价",
        keyword="智能体", source="monitor_comment", task_id="mon_test",
        video_url="https://www.douyin.com/video/7400000000000000002",
        video_id="7400000000000000002", comment_id="comment_platform_2",
        comment_user_id="user_platform_2", account="acct-test",
    )
    first_page = aggregate.monitor_task_matches({"taskId": "mon_test", "offset": 0, "limit": 1})
    page = aggregate.monitor_task_matches({"taskId": "mon_test", "offset": 1, "limit": 1})
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] != first_page["items"][0]["id"]
    filtered = aggregate.monitor_task_matches({
        "taskId": "mon_test", "search": "另一用户", "offset": 0, "limit": 20,
    })
    assert filtered["total"] == 1
    assert filtered["items"][0]["id"] == "lead_local_2"


def test_monitor_match_view_maps_persisted_interaction_status(monitor_db):
    with sqlite3.connect(monitor_db) as con:
        con.execute("""CREATE TABLE live_action_results (
            id INTEGER PRIMARY KEY, task_id TEXT, action TEXT, status TEXT,
            code TEXT, url TEXT, created_ms INTEGER
        )""")
        con.execute("CREATE TABLE live_action_details (result_id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        con.execute("INSERT INTO live_action_results VALUES(1,'mon_test','reply-comment','sent_ok','',?,1)",
                    ("https://www.douyin.com/video/7400000000000000001",))
        con.execute("INSERT INTO live_action_details VALUES(1,?)", ('{"commentId":"comment_platform_1",'
                    '"commentAuthor":"测试用户","sourceText":"想了解工作流如何搭建"}',))
    row = aggregate.monitor_task_matches({"taskId": "mon_test"})["items"][0]
    assert row["replyStatus"] == "success"


def test_manual_retry_runs_only_exact_reviewed_target(monitor_db, monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "claim_account", lambda *args: True)
    monkeypatch.setattr(registry, "release_account", lambda *args: None)
    monkeypatch.setattr(manual_retry, "load_scope", lambda **_: _scope())
    monkeypatch.setattr(manual_retry, "_publish", lambda *args: None)
    monkeypatch.setattr("app.execution.live_leadgen.perform", lambda *args, **kwargs:
                        calls.append(kwargs) or {"ok": True, "status": "sent_ok"})

    result = manual_retry.retry(monitor_db, {
        "taskId": "mon_test", "recordIds": ["lead_local_1"], "confirmedUnsent": False,
    })

    assert result == {"success": True, "processed": 1, "completed": 1,
                      "failed": 0, "skipped": 0, "stopped": False}
    assert calls[0]["comment_id"] == "comment_platform_1"
    assert calls[0]["comment_author"] == "测试用户"
    assert calls[0]["text"] == _scope()["reviewedActions"][0]["text"]


def test_manual_retry_rejects_foreign_or_unreviewed_records_as_whole_batch(monitor_db, monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "claim_account", lambda *args: True)
    monkeypatch.setattr(registry, "release_account", lambda *args: None)
    monkeypatch.setattr(manual_retry, "load_scope", lambda **_: _scope())
    monkeypatch.setattr(manual_retry, "_publish", lambda *args: None)
    monkeypatch.setattr("app.execution.live_leadgen.perform", lambda *args, **kwargs:
                        calls.append(kwargs) or {"ok": True, "status": "sent_ok"})

    result = manual_retry.retry(monitor_db, {
        "taskId": "mon_test", "recordIds": ["lead_local_1", "foreign"],
        "confirmedUnsent": False,
    })
    assert result["success"] is False
    assert calls == []

    scope = _scope()
    scope["reviewedActions"][0]["sourceText"] = "另一个评论"
    monkeypatch.setattr(manual_retry, "load_scope", lambda **_: scope)
    result = manual_retry.retry(monitor_db, {
        "taskId": "mon_test", "recordIds": ["lead_local_1"],
        "confirmedUnsent": False,
    })
    assert result["success"] is False
    assert calls == []


def test_monitor_retry_channels_are_registered(monitor_db):
    assert ipc_bridge.dispatch("get-monitor-manual-retry-task-ids", [{}]) == []
    assert ipc_bridge.dispatch("stop-monitor-manual-retry", [{"taskId": "mon_test"}])["stopped"] is False
    result = ipc_bridge.dispatch("retry-monitor-interactions", [{
        "taskId": "missing", "recordIds": ["lead_local_1"], "confirmedUnsent": False,
    }])
    assert result["success"] is False
