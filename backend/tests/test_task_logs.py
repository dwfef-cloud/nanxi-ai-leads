"""运行日志持久化与桥接契约测试。

用户反馈「启动任务后在页面上找不到运行日志」，根因是日志只走了
automation-data（账号级），任务页的「运行日志」抽屉读的是
entity-leadgen-task-event.taskId 对应的 taskLogs 与
get-entity-leadgen-task-runtime-logs。这里把两条都锁住。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.api import task_logs, task_runner


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "logs.db"


# ---------------------------------------------------------------------------
# 存储层
# ---------------------------------------------------------------------------
def test_append_and_list_in_order(db):
    task_logs.append(db, "run-1", "task-1", "acc-1", "第一条")
    task_logs.append(db, "run-1", "task-1", "acc-1", "第二条", "warning")
    logs = task_logs.list_for(db, "task-1")
    assert [x["message"] for x in logs] == ["第一条", "第二条"]
    assert logs[1]["level"] == "warning"
    assert logs[0]["accountId"] == "acc-1"


def test_list_accepts_run_id_too(db):
    """壳有的页面传运行 id，有的传任务 id，两种都要能查到。"""
    task_logs.append(db, "run-9", "task-9", "acc-1", "日志A")
    assert len(task_logs.list_for(db, "run-9")) == 1
    assert len(task_logs.list_for(db, "task-9")) == 1


def test_append_skips_empty(db):
    task_logs.append(db, "run-1", "task-1", "acc-1", "   ")
    assert task_logs.list_for(db, "task-1") == []


def test_long_message_is_clipped(db):
    task_logs.append(db, "run-1", "task-1", "", "长" * 800)
    text = task_logs.list_for(db, "task-1")[0]["message"]
    assert len(text) <= task_logs.MAX_MESSAGE_LEN + 1


def test_cap_per_task(db):
    for i in range(task_logs.MAX_PER_TASK + 20):
        task_logs.append(db, "run-1", "task-1", "", f"m{i}")
    logs = task_logs.list_for(db, "task-1")
    assert len(logs) == task_logs.MAX_PER_TASK
    assert logs[-1]["message"] == f"m{task_logs.MAX_PER_TASK + 19}"


def test_clear(db):
    task_logs.append(db, "run-1", "task-1", "", "x")
    assert task_logs.clear(db, "task-1") == 1
    assert task_logs.list_for(db, "task-1") == []


def test_broken_db_is_silent(tmp_path):
    """库打不开时不能让任务线程崩：日志失败只是看不见。"""
    bad = tmp_path / "no_dir" / "x.db"
    task_logs.append(bad, "run", "task", "", "msg")   # 目录会自动建，正常
    assert task_logs.list_for(tmp_path / "missing.db", "nope") == []


# ---------------------------------------------------------------------------
# 执行器：双通道 + 落库
# ---------------------------------------------------------------------------
def test_runner_publishes_both_channels(db, monkeypatch):
    """一条执行日志必须同时到达大屏通道与任务通道，并落库。

    这里直接驱动 _log（评论获客执行器未接入时不会产生线程），
    验证的是通道契约，不依赖执行器是否就绪。
    """
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(task_runner.event_bus, "publish",
                        lambda ch, p: events.append((ch, p)))

    ctx = task_runner.RunCtx(task_id="run-77", leadgen_id="task-77",
                             account_id="acc-1", payload={}, db_path=db)
    task_runner._log(ctx, "发表主评前检查账号状态")
    task_runner._emit_task(ctx, "finished", status="completed",
                           endReason="completed", stats={}, message="任务结束")

    channels = {ch for ch, _ in events}
    assert "automation-data" in channels
    assert "entity-legen-task-event" not in channels  # 拼写陷阱，防止改名回归
    assert "entity-leadgen-task-event" in channels

    task_payloads = [p for ch, p in events if ch == "entity-leadgen-task-event"]
    logs = [p for p in task_payloads if p.get("type") == "log"]
    assert logs, "任务页拿不到任何日志事件"
    # 键必须是任务记录 id，壳按它索引 taskLogs
    assert all(p["taskId"] == "task-77" for p in task_payloads)
    assert any(p.get("type") == "finished" for p in task_payloads)

    stored = task_logs.list_for(db, "task-77")
    assert stored and "发表主评" in stored[0]["message"]


def test_runner_log_survives_without_leadgen_id(db, monkeypatch):
    """报文没带 leadgenTaskId 时退回用 taskId 作键，日志仍可查。"""
    monkeypatch.setattr(task_runner.event_bus, "publish", lambda ch, p: None)
    ctx = task_runner.RunCtx(task_id="run-only", leadgen_id="", account_id="acc-1",
                             payload={}, db_path=db)
    task_runner._log(ctx, "任务结束：0 条已发表")
    assert any("任务结束" in x["message"] for x in task_logs.list_for(db, "run-only"))


# ---------------------------------------------------------------------------
# 桥接契约
# ---------------------------------------------------------------------------
def test_bridge_returns_bare_array(tmp_path, monkeypatch):
    from app.api import ipc_bridge as ib

    db = tmp_path / "bridge.db"
    monkeypatch.setattr(ib, "DB_PATH", db, raising=False)
    task_logs.append(db, "run-1", "task-1", "acc-1", "hello")

    out = ib._entity_task_logs({"taskId": "task-1"})
    assert isinstance(out, list), "壳会对返回值直接 .map，必须是数组"
    assert out[0]["message"] == "hello"
    # 也必须能序列化成 JSON 数组（桥接层会 jsonable_encoder）
    assert json.loads(json.dumps(out))[0]["level"] == "normal"


def test_bridge_clear(tmp_path, monkeypatch):
    from app.api import ipc_bridge as ib

    db = tmp_path / "bridge.db"
    monkeypatch.setattr(ib, "DB_PATH", db, raising=False)
    task_logs.append(db, "run-1", "task-1", "", "x")
    assert ib._clear_entity_task_logs({"taskId": "task-1"})["success"] is True
    assert ib._entity_task_logs({"taskId": "task-1"}) == []


def test_monitor_log_clear_channel_uses_persisted_runtime_logs(tmp_path, monkeypatch):
    from app.api import ipc_bridge as ib

    db = tmp_path / "monitor-logs.db"
    monkeypatch.setattr(ib, "DB_PATH", db, raising=False)
    task_logs.append(db, "run-1", "monitor-1", "acc-1", "monitor event")

    result = ib.dispatch("clear-monitor-task-runtime-logs", [{"taskId": "monitor-1"}])

    assert result == {"success": True, "removed": 1}
    assert ib._entity_task_logs({"taskId": "monitor-1"}) == []
