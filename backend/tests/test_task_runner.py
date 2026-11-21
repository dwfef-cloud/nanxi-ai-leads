"""评论获客执行器：语义与门禁。

业务定义（2026-11-14 纠正）：评论获客 = 在关键词相关视频下**发表主评**并对评论者互动，
是写动作；「读评论 → 打分 → 待触达」属于线索采集，不属于这里。

没有有效的桌面确认时必须拒绝发送（live_review_required），不能用采集冒充发送，
并在 runtime_runs / 任务记录留下本次被拒绝的证据。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app.api import event_bus, ipc_bridge as bridge, task_runner


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "lead_system.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE leads (id TEXT PRIMARY KEY, nickname TEXT, comment TEXT, "
        "source_keyword TEXT, matched_keywords TEXT, status TEXT, score INTEGER, "
        "intent_level TEXT, tags TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    con.commit()
    con.close()

    monkeypatch.setattr(event_bus, "EVENTS_DB", tmp_path / "events.db")
    monkeypatch.setattr(bridge, "DB_PATH", db)
    task_runner.reset_for_tests()
    yield db
    task_runner.reset_for_tests()


def _seed_record(env, record: dict) -> None:
    bridge.leadgen_pools.save_task_record(env, record)


def _fake_running(task_id: str, account_id: str, db) -> None:
    """手工注入一个活跃线程（执行器未接入，但大屏/停止通道仍需按活跃态工作）。"""
    ctx = task_runner.RunCtx(task_id=task_id, leadgen_id="", account_id=account_id,
                             payload={}, db_path=db)
    with task_runner._LOCK:
        task_runner._RUNS[task_id] = ctx


# --------------------------------------------------------------------------
# 启动门禁
# --------------------------------------------------------------------------
def test_executor_not_ready_by_default():
    assert task_runner.executor_ready() is False


def test_comment_like_or_reader_lease_blocks_leadgen(env, monkeypatch):
    from app.runtime import registry, browser
    from app.execution import live_scope
    monkeypatch.setattr(task_runner, "executor_ready", lambda: True)
    monkeypatch.setattr(live_scope, "task_allowed", lambda _: True)
    monkeypatch.setattr(browser, "status", lambda _: {"engine": "shell"})
    assert registry.claim_account("account-one", "comment-like-run")
    try:
        result = task_runner.start(env, {"taskId": "t1", "accountId": "account-one"})
        assert result["success"] is False and "其他任务" in result["error"]
    finally:
        registry.release_account("account-one", "comment-like-run")


def test_start_requires_task_and_account(env):
    assert task_runner.start(env, {})["success"] is False
    res = task_runner.start(env, {"taskId": "t1"})
    assert res["success"] is False and "账号" in res["error"]


def test_start_rejected_with_human_readable_reason(env):
    _seed_record(env, {"id": "t1", "name": "评论获客-测试", "entityKind": "leadgen",
                       "createdAt": 1})
    res = task_runner.start(env, {"taskId": "t1", "accountId": "a1",
                                  "keywords": ["火锅"], "videoMax": 2})
    assert res["success"] is False
    assert res["code"] == "live_review_required"
    assert "未发送" in res["error"]
    assert "尚未接入" not in res["error"]
    assert task_runner.active_ids() == []      # 没有线程被启动


def test_rejected_start_never_calls_collector(env, monkeypatch):
    """核心回归：拒绝启动时绝不能下发采集指令（这正是此前的语义错误）。"""
    from app.api import desktop_commands

    calls: list[str] = []

    def spy(*args, **kwargs):
        calls.append(str(args or kwargs))
        raise AssertionError("拒绝启动时不允许下发任何平台指令")

    monkeypatch.setattr(desktop_commands, "publish", spy)
    monkeypatch.setattr(desktop_commands, "agent_alive", lambda *a, **k: True)

    _seed_record(env, {"id": "t2", "name": "评论获客-禁止采集", "entityKind": "leadgen",
                       "createdAt": 1})
    res = task_runner.start(env, {"taskId": "t2", "accountId": "a1",
                                  "keywords": ["火锅"], "videoMax": 2})
    assert res["code"] == "live_review_required"
    assert calls == []


def test_rejected_start_leaves_evidence(env):
    """拒绝也要留证据：执行历史一条 failed，任务记录标成 failed + 原因。"""
    from app import leadgen_pools
    from app.runtime import store as rt_store

    _seed_record(env, {"id": "t3", "name": "评论获客-证据", "entityKind": "leadgen",
                       "createdAt": 1, "runs": []})
    res = task_runner.start(env, {"taskId": "t3", "accountId": "a1",
                                  "keywords": ["火锅"]})
    assert res["success"] is False

    runs = rt_store.recent_runs(env, kind="leadgen", limit=5)
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["engine"] == "none"
    assert "未发送" in (runs[0]["message"] or "")
    assert set(runs[0]["stats"]) >= {"published", "interacted", "reached", "already"}

    rec = leadgen_pools.load_records(env)["t3"]
    assert rec["status"] == "failed"
    assert rec["endReason"] == "live_review_required"
    assert rec["runs"][-1]["endReason"] == "live_review_required"
    assert "未发送" in rec["lastMessage"]


def test_repeat_start_is_idempotent_error(env):
    """同一动作做两遍：两次都必须明确失败，且都说清原因（不静默成功）。"""
    _seed_record(env, {"id": "t4", "name": "评论获客-重复", "entityKind": "leadgen",
                       "createdAt": 1})
    first = task_runner.start(env, {"taskId": "t4", "accountId": "a1"})
    second = task_runner.start(env, {"taskId": "t4", "accountId": "a1"})
    assert first["success"] is False and second["success"] is False
    assert first["code"] == second["code"] == "live_review_required"


def test_start_through_ipc_returns_message(env):
    """壳拿到的报文必须带 msg，否则界面只会弹「任务启动失败」没有原因。"""
    _seed_record(env, {"id": "t5", "name": "评论获客-IPC", "entityKind": "leadgen",
                       "createdAt": 1})
    res = bridge.dispatch("start-task", [{"taskId": "t5", "accountId": "a1"}])
    assert res["success"] is False
    assert "未发送" in (res.get("msg") or "")
    assert res.get("code") == "live_review_required"


def test_events_carry_rejection_log(env):
    with TestClient(bridge_server.app) as client:
        _seed_record(env, {"id": "t6", "name": "评论获客-事件", "entityKind": "leadgen",
                           "createdAt": 1})
        task_runner.start(env, {"taskId": "t6", "accountId": "a1"})
        body = client.get("/ipc/events").json()
        assert body["events"]
        assert {"channel", "payload"} <= set(body["events"][0])
        assert client.get("/ipc/events").json()["events"] == []


# --------------------------------------------------------------------------
# 活跃态与停止通道（大屏/重启清场依赖）
# --------------------------------------------------------------------------
def test_stop_task_falls_back_to_viewkey_account(env):
    _fake_running("t_vk", "acct9", env)
    res = bridge.dispatch("stop-task", [{"viewKey": "douyin_acct9",
                                         "skipFinalize": True, "reason": "restarted"}])
    assert res["success"] is True and res["stopped"] == 1


def test_active_records_feed_dashboard(env):
    _fake_running("t_live", "acct-live", env)
    recs = task_runner.active_records()
    assert recs and recs[0]["taskId"] == "t_live"
    assert recs[0]["accountId"] == "acct-live"


def test_stale_running_record_is_reconciled_to_stopped(env):
    """幽灵 running 记录：没有活跃线程时必须归位 stopped，否则「重启」被拦死。"""
    _seed_record(env, {
        "id": "leadgen_x", "name": "幽灵任务", "status": "running",
        "createdAt": 1, "startedAt": 1, "endedAt": None, "endReason": "",
        "runs": [{"accountId": "a1", "automationTaskId": "task_a1_1",
                  "startedAt": 1, "endedAt": None, "endReason": ""}],
    })
    tasks = bridge._tasks("leadgen")({})
    rec = next(t for t in tasks if t["id"] == "leadgen_x")
    assert rec["status"] == "stopped"
    assert rec["endedAt"] and rec["endReason"] == "stopped"
    assert rec["runs"][0]["endedAt"]


def test_genuinely_running_record_is_kept(env):
    _seed_record(env, {
        "id": "leadgen_live", "name": "真运行", "status": "running",
        "createdAt": 1, "runs": [{"accountId": "a1", "automationTaskId": "t_live"}],
    })
    _fake_running("t_live", "a1", env)
    tasks = bridge._tasks("leadgen")({})
    rec = next(t for t in tasks if t["id"] == "leadgen_live")
    assert rec["status"] == "running"


def test_rejection_event_uses_finished_with_message(env, monkeypatch):
    """壳只在 status=finished 时把 finishMessage 写进账号日志。

    传 failed 只会显示「任务已挂起」，用户看不到「执行器未接入」这个原因，
    等于把拒绝变成了无解释的静默失败。
    """
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(event_bus, "publish", lambda ch, p: events.append((ch, p)))

    task_runner.start(env, {"taskId": "t7", "accountId": "a1"})
    status = [p for ch, p in events if p.get("type") == "status"]
    assert status, "必须推状态事件，否则界面停在运行中"
    assert status[0]["payload"]["status"] == "finished"
    assert "未发送" in status[0]["payload"]["finishMessage"]


# --------------------------------------------------------------------------
# 通道分流：养号不能被评论获客门禁挡住
# --------------------------------------------------------------------------
def test_nurture_mode_goes_to_warmup_runner_not_gate(env, monkeypatch):
    """页面养号和评论获客共用 start-task 通道。

    养号有自己的执行器（浏览推荐流 + 概率点赞），一旦被统一转进评论获客门禁，
    用户点「开始养号」就只会看到「执行器未接入」—— 功能直接不可用。
    """
    from app.api import task_store
    from app.runtime import registry

    called: dict = {}

    def fake_start(db_path, kind, task_id, trigger="manual"):
        called.update(kind=kind, task_id=task_id, db=str(db_path))
        return {"success": True, "runId": "run_nurture", "accountId": "acct-1",
                "engine": "shell"}

    monkeypatch.setattr(registry, "start", fake_start)

    res = bridge.dispatch("start-task", [{
        "taskId": "nurture_acct-1_1", "taskMode": "nurture", "accountId": "acct-1",
        "name": "主号", "nurtureDurationMin": 60, "nurtureDurationMax": 120,
        "stayMin": 8, "stayMax": 25, "nurtureLikePercent": 10,
    }])

    assert res["success"] is True
    assert res.get("code") != "executor_not_ready"
    assert called["kind"] == "warmup" and called["task_id"] == "nurture_acct-1_1"

    # 扁平配置必须包进 configSnapshot：runner 只从这里读配置
    rec = task_store.get_task(env, "warmup", "nurture_acct-1_1")
    assert rec is not None
    snap = rec["configSnapshot"]
    assert snap["nurtureDurationMin"] == 60
    assert snap["stayMin"] == 8 and snap["nurtureLikePercent"] == 10
    assert snap["accountId"] == "acct-1"


def test_nurture_rejects_stale_page_config_without_creating_run(env, monkeypatch):
    from app import nurture_config
    from app.api import task_store
    from app.runtime import registry

    nurture_config.save(env, {"durationMin": 5, "durationMax": 6,
                              "likePercent": 10, "collectPercent": 0,
                              "sharePercent": 0})
    monkeypatch.setattr(registry, "start", lambda *a, **kw: pytest.fail("must not start"))
    res = bridge.dispatch("start-task", [{
        "taskId": "nurture_stale", "taskMode": "nurture", "accountId": "acct-1",
        "name": "主号", "nurtureDurationMin": 5, "nurtureDurationMax": 5,
        "nurtureLikePercent": 10, "nurtureCollectPercent": 10,
        "nurtureSharePercent": 10,
    }])
    assert res["success"] is False
    assert "参数与已保存配置不一致" in res["error"]
    assert task_store.get_task(env, "warmup", "nurture_stale") is None


def test_nurture_rejects_fast_restart_without_creating_run(env, monkeypatch):
    from app.api import task_store
    from app.runtime import registry, store

    run_id = store.run_begin(env, "warmup", "previous", "主号", "acct-1")
    store.run_end(env, run_id, "completed")
    monkeypatch.setattr(registry, "start", lambda *a, **kw: pytest.fail("must not start"))
    res = bridge.dispatch("start-task", [{
        "taskId": "nurture_too_soon", "taskMode": "nurture", "accountId": "acct-1",
        "name": "主号", "nurtureDurationMin": 5, "nurtureDurationMax": 6,
    }])
    assert res["success"] is False and res["cooldown"] is True
    assert res["retryAfterSec"] > 0
    assert task_store.get_task(env, "warmup", "nurture_too_soon") is None
