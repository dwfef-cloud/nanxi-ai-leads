"""定时排期执行器：窗口判定 / 幂等 / 不伪造执行 / 尊重既有安全机制。

这些用例覆盖的正是「配置能存能读但到点不执行」这类假成功最容易藏身的地方：
- 到点真的启动了吗（有引擎的类型）
- 没有引擎的类型会不会假装启动（必须 skip 并留原因）
- 同一窗口会不会反复触发（前端整表覆盖 + 重启）
- 熔断（safe_mode）时会不会照样跑
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from app import leadgen_pools, scheduler
from app import schedules as sched_store
from app.api import task_runner


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _day(h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 14, h, m, 0)


def _seed_schedule(db, kind: str, task_id: str, **entry) -> None:
    base = {"type": "daily", "enabled": True, "startHm": "09:00", "endHm": "18:00",
            "mode": "restart", "nextAction": "start", "nextActionAt": 0}
    base.update(entry)
    sched_store.save_map(db, kind, {task_id: base})


def _seed_leadgen_task(db, task_id: str = "leadgen_test_1",
                       account_id: str = "acct-1") -> str:
    leadgen_pools.save_task_record(db, {
        "id": task_id,
        "name": "验收任务",
        "status": "stopped",
        "createdAt": 1794581599881,
        "runs": [{"accountId": account_id, "nickname": "小号", "name": "测试号",
                  "keywords": "智能体",
                  "automationTaskId": "task_acct-1_1794581599882"}],
        "configSnapshot": {"taskMode": "interaction", "keywords": "智能体",
                           "videoMax": 5, "interactionLimit": 10},
        "stats": {},
    })
    return task_id


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "lead_system.db"


@pytest.fixture()
def no_run(monkeypatch):
    """默认把执行器打成「没在运行」，避免用例之间互相看到线程。"""
    monkeypatch.setattr(task_runner, "active_ids", lambda: [])
    monkeypatch.setattr(task_runner, "active_account_ids", lambda: [])


@pytest.fixture(autouse=True)
def fake_leadgen_engine(monkeypatch):
    """排期用例验证的是排期机制本身，与评论获客执行器是否已接入解耦。

    「执行器未接入时必须跳过」由 test_leadgen_skipped_when_executor_not_ready 单独覆盖。
    """
    monkeypatch.setattr(task_runner, "executor_ready", lambda: True)

    def _start(_db, payload):
        return {"success": True, "taskId": str(payload.get("taskId") or ""),
                "status": "running", "mode": "real", "runId": "lea_fake"}

    monkeypatch.setattr(task_runner, "start", _start)


def test_leadgen_skipped_when_executor_not_ready(db, no_run, monkeypatch):
    """执行器没接入「发表评论」时，排期必须跳过并说明原因，不能假装启动。"""
    monkeypatch.setattr(task_runner, "executor_ready", lambda: False)
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    res = scheduler.run_once(db, _ms(_day(10)))
    assert res["fired"] == []
    assert "发表评论" in res["skipped"][0]["detail"]


# ---------------------------------------------------------------- 窗口判定
def test_daily_window_normal_and_overnight():
    now = _ms(_day(10, 30))
    win = scheduler.current_window(now, "09:00", "18:00")
    assert win is not None and win[0] == "2026-11-14"

    assert scheduler.current_window(_ms(_day(19)), "09:00", "18:00") is None

    # 22:00–06:00 跨零点：凌晨 1 点属于「前一天」那个窗口
    night = scheduler.current_window(_ms(_day(1)), "22:00", "06:00")
    assert night is not None and night[0] == "2026-11-13"
    assert scheduler.current_window(_ms(_day(12)), "22:00", "06:00") is None


def test_due_actions_daily_start_then_stop():
    sched = {"type": "daily", "enabled": True, "startHm": "09:00", "endHm": "18:00"}
    inside = scheduler.due_actions("leadgen", "t1", sched, _ms(_day(10)))
    assert [a["action"] for a in inside] == ["start"]
    assert inside[0]["window"] == "w:2026-11-14"

    after = scheduler.due_actions("leadgen", "t1", sched, _ms(_day(19)))
    assert [a["action"] for a in after] == ["stop"]
    assert after[0]["window"] == "w:2026-11-14"


def test_disabled_and_future_once_do_nothing():
    sched = {"type": "daily", "enabled": False, "startHm": "09:00", "endHm": "18:00"}
    assert scheduler.due_actions("leadgen", "t1", sched, _ms(_day(10))) == []

    once = {"type": "once", "enabled": True, "runAt": _ms(_day(20))}
    assert scheduler.due_actions("leadgen", "t1", once, _ms(_day(10))) == []
    due = scheduler.due_actions("leadgen", "t1", once, _ms(_day(20, 1)))
    assert [a["action"] for a in due] == ["start"]
    assert due[0]["window"] == f"once:{_ms(_day(20))}"


# ---------------------------------------------------------------- 幂等
def test_same_window_fires_only_once(db, no_run, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, payload: calls.append(payload) or
                        {"success": True, "mode": "local"})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    first = scheduler.run_once(db, _ms(_day(10)))
    assert len(first["fired"]) == 1, first
    assert len(calls) == 1

    # 同一窗口再跑十遍都不该再启动（前端整表覆盖 / 重启都靠这条兜住）
    for _ in range(3):
        again = scheduler.run_once(db, _ms(_day(10, 5)))
        assert again["fired"] == []
        assert again["already"] >= 1
    assert len(calls) == 1


def test_next_window_day_fires_again(db, no_run, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, payload: calls.append(payload) or
                        {"success": True, "mode": "local"})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    scheduler.run_once(db, _ms(_day(10)))
    scheduler.run_once(db, _ms(_day(10) + timedelta(days=1)))
    assert len(calls) == 2


# ---------------------------------------------------------------- 不伪造
def test_kind_without_engine_is_skipped_not_faked(db, no_run):
    """引擎不可用（这里=没有桌面壳/未登录）时：到点必须 skip 并写明原因。

    这条用例守住的是同一件事——监控/养号/线索采集现在**有**运行时了，
    但「有运行时」不等于「现在就能跑」：没有已登录的浏览器时不能伪造启动。
    """
    from app.api import task_store

    task_store.save_task(db, "monitor", {
        "id": "mon_1", "name": "监控测试任务", "status": "ready",
        "createdAt": 1700000000000,
        "configSnapshot": {"videoUrls": ["https://www.douyin.com/video/1"]},
    })
    _seed_schedule(db, "monitor", "mon_1")
    res = scheduler.run_once(db, _ms(_day(10)))
    assert res["fired"] == []
    assert len(res["skipped"]) == 1
    detail = res["skipped"][0]["detail"]
    assert "执行引擎" in detail

    # 落库留痕，能解释「到点了但没跑」
    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT status, detail FROM schedule_runs").fetchall()
    con.close()
    assert rows and rows[0][0] == "skipped" and "执行引擎" in rows[0][1]


def test_engine_map_is_honest(db):
    """四类都有执行端；但「引擎是否就绪」必须由运行时如实报告。"""
    assert scheduler.ENGINES == {"leadgen": True, "entity": True,
                                 "monitor": True, "warmup": True}
    st = scheduler.status(db)
    # 测试环境没有桌面壳、也没登录过独立引擎 → 三类运行时都不可用，
    # 这不是「没实现」，而是「现在跑不了」，两者必须能区分。
    assert st["runtime"]["engine"]["engine"] == "none"
    assert st["runtime"]["engines"] == {"entity": False, "monitor": False,
                                        "warmup": False}


def test_missing_task_record_skips(db, no_run):
    _seed_schedule(db, "leadgen", "leadgen_does_not_exist")
    res = scheduler.run_once(db, _ms(_day(10)))
    assert res["fired"] == []
    assert "不存在" in res["skipped"][0]["detail"]


def test_busy_account_defers_with_retry(db, monkeypatch):
    monkeypatch.setattr(task_runner, "active_ids", lambda: [])
    monkeypatch.setattr(task_runner, "active_account_ids", lambda: ["acct-1"])
    started: list = []
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, p: started.append(p) or {"success": True})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    res = scheduler.run_once(db, _ms(_day(10)))
    assert res["fired"] == [] and started == []
    assert "正在运行" in res["skipped"][0]["detail"]

    con = sqlite3.connect(str(db))
    retry_at = con.execute("SELECT retry_at FROM schedule_runs").fetchone()[0]
    con.close()
    assert retry_at, "瞬时失败必须排重试，否则一个窗口的启动机会就永久丢了"


# ---------------------------------------------------------------- 真实链路
def test_start_leadgen_marks_task_running_and_logs(db, no_run, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, payload: captured.append(payload) or
                        {"success": True, "mode": "local"})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    scheduler.run_once(db, _ms(_day(10)))

    assert captured, "有引擎的类型到点必须真的调执行器"
    payload = captured[0]
    assert payload["accountId"] == "acct-1"
    assert payload["leadgenTaskId"] == "leadgen_test_1"
    assert payload["taskId"] == "task_acct-1_1794581599882"
    assert payload["keywords"] == "智能体"

    rec = leadgen_pools.load_records(db)["leadgen_test_1"]
    assert rec["status"] == "running"

    con = sqlite3.connect(str(db))
    log = con.execute("SELECT message FROM task_runtime_logs").fetchone()
    con.close()
    assert log and "定时启动" in log[0]


def test_stop_only_when_this_window_started(db, no_run, monkeypatch):
    stopped: list[str] = []
    monkeypatch.setattr(task_runner, "stop", lambda tid: stopped.append(tid) or
                        {"success": True})
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    # 没启动过的窗口：不写停止记录、也不调 stop
    res = scheduler.run_once(db, _ms(_day(19)))
    assert res["fired"] == [] and stopped == []
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0] == 0
    con.close()

    # 补一条「本窗口已启动」，再跑就该停
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO schedule_runs(kind,task_id,window_key,action,status,"
                "detail,run_ref,attempt,fired_at) VALUES('leadgen','leadgen_test_1',"
                "'w:2026-11-14','start','fired','','task_acct-1_1',1,'x')")
    con.commit()
    con.close()

    res = scheduler.run_once(db, _ms(_day(19)))
    assert len(res["fired"]) == 1
    assert stopped == ["task_acct-1_1"]


def test_once_disables_itself_after_fire(db, no_run, monkeypatch):
    monkeypatch.setattr(task_runner, "start", lambda _db, p: {"success": True})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1", type="once",
                   runAt=_ms(_day(10)))

    scheduler.run_once(db, _ms(_day(10, 1)))
    entry = sched_store.get_map(db, "leadgen")["leadgen_test_1"]
    assert entry["enabled"] is False, "一次性排期跑过后不能还挂着，否则第二天又触发"


def test_finished_run_releases_task_status(db, monkeypatch):
    """线程结束后必须把任务记录从 running 落回终态，否则列表永远显示「运行中」。"""
    seq = {"active": ["task_x"]}
    monkeypatch.setattr(task_runner, "active_ids", lambda: list(seq["active"]))
    monkeypatch.setattr(task_runner, "active_account_ids", lambda: [])
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, p: {"success": True, "mode": "local"})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    scheduler.run_once(db, _ms(_day(10)))
    assert leadgen_pools.load_records(db)["leadgen_test_1"]["status"] == "running"

    seq["active"] = []          # 模拟线程跑完退出
    res = scheduler.run_once(db, _ms(_day(10, 2)))
    assert res["closed"], res
    assert leadgen_pools.load_records(db)["leadgen_test_1"]["status"] == "completed"


def test_after_finish_rearms_with_interval(db, monkeypatch):
    """「结束后间隔执行」必须一轮轮转下去，并且间隔有下限。"""
    seq = {"active": ["task_x"]}
    monkeypatch.setattr(task_runner, "active_ids", lambda: list(seq["active"]))
    monkeypatch.setattr(task_runner, "active_account_ids", lambda: [])
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, p: {"success": True, "mode": "local"})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1", type="after_finish",
                   intervalHours=0, intervalMinutes=0,
                   nextAction="start", nextActionAt=_ms(_day(10)))

    scheduler.run_once(db, _ms(_day(10)))
    entry = sched_store.get_map(db, "leadgen")["leadgen_test_1"]
    assert entry["nextAction"] == "wait_finish", "启动后应等本轮结束再算下一轮"

    seq["active"] = []
    scheduler.run_once(db, _ms(_day(10, 2)))
    entry = sched_store.get_map(db, "leadgen")["leadgen_test_1"]
    assert entry["nextAction"] == "start"
    # 0 分钟会被抬到 5 分钟下限，否则跑完立刻再来一轮
    assert entry["nextActionAt"] == _ms(_day(10, 2)) + 5 * 60 * 1000

    # 到点后再触发一次，窗口键不同 → 能重复执行
    calls = []
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, p: calls.append(p) or {"success": True})
    scheduler.run_once(db, _ms(_day(10, 8)))
    assert calls, "第二轮到点必须真的再启动一次"


# ---------------------------------------------------------------- 安全机制
def test_safe_mode_blocks_everything(db, no_run, monkeypatch):
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, p: pytest.fail("熔断期间不允许启动任务"))
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE runtime_state (id TEXT PRIMARY KEY, safe_mode_active INTEGER)")
    con.execute("INSERT INTO runtime_state VALUES('default', 1)")
    con.commit()
    con.close()

    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")
    res = scheduler.run_once(db, _ms(_day(10)))
    assert res.get("safeMode") is True
    assert res["fired"] == []


def test_max_concurrent_caps_starts(db, monkeypatch):
    monkeypatch.setattr(task_runner, "active_account_ids", lambda: [])
    monkeypatch.setattr(task_runner, "active_ids", lambda: ["busy-1", "busy-2"])
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, p: pytest.fail("已达并发上限，不该再启动"))
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE scheduler_config (id INTEGER PRIMARY KEY, "
                "strategy TEXT, max_concurrent INTEGER)")
    con.execute("INSERT INTO scheduler_config VALUES(1,'round_robin',2)")
    con.commit()
    con.close()

    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")
    res = scheduler.run_once(db, _ms(_day(10)))
    assert res["fired"] == [] and "并发上限" in res["skipped"][0]["detail"]


def test_status_reports_engines_and_upcoming(db, no_run):
    _seed_schedule(db, "leadgen", "leadgen_test_1")
    _seed_schedule(db, "warmup", "warm_1")
    st = scheduler.status(db)
    assert st["engines"]["leadgen"] is True
    assert st["engines"]["warmup"] is True
    assert st["runtime"]["engine"]["engine"] == "none", "测试环境没有真机引擎"
    kinds = {i["kind"] for i in st["upcoming"]}
    assert kinds == {"leadgen", "warmup"}
    assert st["running"] is False, "测试环境 SCHEDULER_OFF=1，不能起线程"


# ------------------------------------------------------- 执行器离线暂停整轮
@pytest.fixture(autouse=True)
def _reset_offline_state():
    """离线状态是模块级的，跨用例必须清，否则断言会互相污染。"""
    scheduler._STATE["offline_since_ms"] = None
    scheduler._STATE["offline_notice"] = ""
    yield
    scheduler._STATE["offline_since_ms"] = None
    scheduler._STATE["offline_notice"] = ""


def _fake_clock(monkeypatch, start_ms: int):
    """可推进的假物理钟。离线时长必须按物理时间算，不能被业务时间带跑。"""
    box = {"t": int(start_ms)}
    monkeypatch.setattr(scheduler, "_now_wall_ms", lambda: box["t"])
    return box


def test_offline_within_grace_still_tries(db, no_run, monkeypatch):
    """刚离线（壳重启那几十秒）不能暂停：否则马上到点的任务会白丢一个窗口。"""
    monkeypatch.setattr(scheduler, "_executor_online", lambda _db: (False, "none"))
    clock = _fake_clock(monkeypatch, _ms(_day(10)))
    calls: list[dict] = []
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, payload: calls.append(payload) or
                        {"success": True, "mode": "local"})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    res = scheduler.run_once(db, _ms(_day(10)))
    assert res.get("pausedByOffline") is None
    assert res["fired"], "宽限期内仍应尝试执行"
    assert scheduler._STATE["offline_since_ms"] == clock["t"], "首次发现离线要记时间"


def test_offline_beyond_grace_pauses_whole_tick(db, no_run, monkeypatch):
    """离线超过宽限期：整轮暂停，不逐条落 skipped（否则一夜刷屏把真原因淹掉）。"""
    monkeypatch.setattr(scheduler, "_executor_online", lambda _db: (False, "none"))
    clock = _fake_clock(monkeypatch, _ms(_day(10)))
    calls: list[dict] = []
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, payload: calls.append(payload) or
                        {"success": True, "mode": "local"})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    t0 = _ms(_day(10))
    scheduler.run_once(db, t0)  # 宽限期内：照常尝试一次
    con = sqlite3.connect(str(db))
    n1 = con.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0]
    con.close()

    # 物理钟往前推过宽限期；业务时间跳到第二天，确实有任务到点
    clock["t"] = t0 + scheduler.OFFLINE_GRACE_MS + 20_000
    calls.clear()
    res = scheduler.run_once(db, _ms(_day(10) + timedelta(days=1)))

    assert res.get("pausedByOffline") is True
    assert res["fired"] == [] and res["skipped"] == []
    assert calls == [], "离线暂停时不该启动任何任务（哪怕它到点了）"
    assert "离线" in res["reason"] and "自动继续" in res["reason"]

    con = sqlite3.connect(str(db))
    n2 = con.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0]
    con.close()
    assert n2 == n1, "暂停轮不写 schedule_runs：恢复后这次还得照常触发"


def test_offline_recovery_resumes_without_manual_action(db, no_run, monkeypatch):
    """执行器回来后自动恢复：不需要人工点「继续」，也不会把暂停期间那次吞掉。"""
    state = {"online": False}
    monkeypatch.setattr(
        scheduler, "_executor_online",
        lambda _db: (state["online"], "shell" if state["online"] else "none"))
    clock = _fake_clock(monkeypatch, _ms(_day(10)))
    monkeypatch.setattr(task_runner, "start",
                        lambda _db, payload: {"success": True, "mode": "local"})
    _seed_leadgen_task(db)
    _seed_schedule(db, "leadgen", "leadgen_test_1")

    t0 = _ms(_day(10))
    scheduler.run_once(db, t0)
    clock["t"] = t0 + scheduler.OFFLINE_GRACE_MS + 20_000
    paused = scheduler.run_once(db, _ms(_day(10) + timedelta(days=1)))
    assert paused.get("pausedByOffline") is True
    assert scheduler._STATE["offline_notice"]

    state["online"] = True
    res = scheduler.run_once(db, _ms(_day(10) + timedelta(days=2)))
    assert scheduler._STATE["offline_since_ms"] is None
    assert scheduler._STATE["offline_notice"] == ""
    assert res.get("pausedByOffline") is None
    assert res["fired"], "恢复后应当能正常触发"


def test_status_exposes_executor_state(db, no_run, monkeypatch):
    monkeypatch.setattr(scheduler, "_executor_online", lambda _db: (False, "none"))
    st = scheduler.status(db)
    ex = st["executor"]
    assert ex["online"] is False and ex["engine"] == "none"
    assert ex["paused"] is False, "还没超过宽限期，不能显示成暂停"
    assert ex["graceSec"] == scheduler.OFFLINE_GRACE_MS // 1000
