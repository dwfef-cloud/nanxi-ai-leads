"""采集关键词（联想词扩词）· 通道与状态机测试。

覆盖点（契约全部取自 ui/assets/Settings-*.js，不是猜的）：
- state 字段：runId/status/accountId/seeds/total/completed/currentQuery/keywords/warnings/error
- status 取值：idle/running/stopping/completed/stopped/failed/verification
- 起始校验：没账号 / 没关键词 都不启动，且不建假 run
- 单实例：已有 running/verification 时再点开始必须拒绝，不能两个线程抢同一个窗口
- 采集：按「种子+a..z」展开，联想词跨查询去重、保序
- 单条失败：记 warning 并推进 completed（否则会永远卡在同一条）
- 撞验证：停在 verification 不自动重试；「继续采集」从 completed 处重跑
- 停止：status 先置 stopping，线程收尾后变 stopped
"""
from __future__ import annotations

import sqlite3

import pytest

from app.runtime import browser, keyword_collector


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "kwc.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(keyword_collector, "QUERY_GAP", (0, 0))
    monkeypatch.setattr(keyword_collector, "QUERY_TIMEOUT", 5.0)
    keyword_collector._STOP.clear()
    keyword_collector._WORKER = None
    return {"db": str(db)}


def _wait_idle(db, timeout=10.0):
    import time

    end = time.time() + timeout
    while time.time() < end:
        st = keyword_collector.latest(db)
        if st["status"] in {"completed", "stopped", "failed", "verification"}:
            return st
        time.sleep(0.02)
    raise AssertionError(f"采集没有在预期时间内结束（当前 {keyword_collector.latest(db)['status']}）")


def _fake_shell(monkeypatch, handler):
    calls = []

    def fake_execute(cmd, **kw):
        calls.append(cmd.get("keyword"))
        return handler(cmd, len(calls) - 1)

    monkeypatch.setattr(browser, "execute", fake_execute)
    return calls


def test_state_contract_shape(env):
    st = keyword_collector.latest(env["db"])
    assert set(st) == {"runId", "status", "accountId", "seeds", "total", "completed",
                       "currentQuery", "keywords", "warnings", "error"}
    assert st["status"] == "idle"
    assert st["seeds"] == [] and st["keywords"] == []


def test_start_requires_account_and_keywords(env):
    no_account = keyword_collector.start(env["db"], {"accountId": "", "keywords": "获客"})
    assert no_account["success"] is False
    assert "账号" in no_account["error"]

    no_seed = keyword_collector.start(env["db"], {"accountId": "acc-1", "keywords": "  \n "})
    assert no_seed["success"] is False
    assert "关键词" in no_seed["error"]
    # 失败的启动不能留下 run 记录（否则界面会显示一个永远 idle 的假任务）
    assert keyword_collector.latest(env["db"])["runId"] == ""


def test_seeds_parsed_deduped_and_total_is_26_per_seed(env, monkeypatch):
    _fake_shell(monkeypatch, lambda cmd, i: {"ok": True, "items": []})
    res = keyword_collector.start(env["db"], {"accountId": "acc-1",
                                             "keywords": "获客\n获客\n智能体，定制"})
    assert res["success"] is True
    st = res["state"]
    assert st["seeds"] == ["获客", "智能体", "定制"]
    assert st["total"] == 3 * 26
    _wait_idle(env["db"])


def test_run_collects_keywords_deduped_in_order(env, monkeypatch):
    def handler(cmd, i):
        if i == 0:
            return {"ok": True, "items": ["获客a1", "获客a2", "重复词"]}
        if i == 1:
            return {"ok": True, "items": ["重复词", "获客b1"]}
        return {"ok": True, "items": []}

    calls = _fake_shell(monkeypatch, handler)
    keyword_collector.start(env["db"], {"accountId": "acc-1", "keywords": "获客"})
    st = _wait_idle(env["db"])
    assert st["status"] == "completed"
    assert st["completed"] == 26 and st["total"] == 26
    assert st["keywords"] == ["获客a1", "获客a2", "重复词", "获客b1"]
    # 展开顺序必须是 获客a / 获客b / …
    assert calls[0] == "获客a" and calls[1] == "获客b" and calls[-1] == "获客z"
    assert st["currentQuery"] == ""


def test_soft_failure_records_warning_and_advances(env, monkeypatch):
    def handler(cmd, i):
        if i == 0:
            return {"ok": False, "error": "未找到搜索输入框"}
        return {"ok": True, "items": ["获客b1"]}

    _fake_shell(monkeypatch, handler)
    keyword_collector.start(env["db"], {"accountId": "acc-1", "keywords": "获客"})
    st = _wait_idle(env["db"])
    assert st["status"] == "completed"
    assert st["warnings"] and st["warnings"][0]["query"] == "获客a"
    assert "搜索输入框" in st["warnings"][0]["message"]
    # 失败也要推进，否则同一个 query 会被无限重试
    assert st["completed"] == 26


def test_verification_pauses_and_resume_retries_same_query(env, monkeypatch):
    state = {"blocked": True}
    calls = _fake_shell(monkeypatch, lambda cmd, i: (
        {"ok": False, "error": "页面被安全验证拦住", "needVerify": True} if state["blocked"]
        else {"ok": True, "items": [cmd["keyword"] + "-词"]}))

    started = keyword_collector.start(env["db"], {"accountId": "acc-1", "keywords": "获客"})
    st = _wait_idle(env["db"])
    assert st["status"] == "verification"
    assert st["completed"] == 0
    assert st["currentQuery"] == "获客a"
    assert "验证" in st["error"]
    assert calls == ["获客a"], "撞验证后不应继续打接口"

    # 用户处理完验证，点「继续采集」
    state["blocked"] = False
    resumed = keyword_collector.resume(env["db"], {"runId": st["runId"]})
    assert resumed["success"] is True
    assert resumed["state"]["status"] == "running"
    st = _wait_idle(env["db"])
    assert st["status"] == "completed"
    assert calls[1] == "获客a", "继续时必须重跑被拦下的那一条，不能跳过"
    assert st["completed"] == 26


def test_resume_is_noop_when_not_verifying(env, monkeypatch):
    _fake_shell(monkeypatch, lambda cmd, i: {"ok": True, "items": []})
    keyword_collector.start(env["db"], {"accountId": "acc-1", "keywords": "获客"})
    st = _wait_idle(env["db"])
    again = keyword_collector.resume(env["db"], {"runId": st["runId"]})
    assert again["success"] is True
    assert "无需继续" in again.get("note", "")
    assert keyword_collector.latest(env["db"])["status"] == "completed"


def test_second_start_rejected_while_running(env, monkeypatch):
    import threading

    gate = threading.Event()

    def handler(cmd, i):
        gate.wait(5)
        return {"ok": True, "items": []}

    _fake_shell(monkeypatch, handler)
    first = keyword_collector.start(env["db"], {"accountId": "acc-1", "keywords": "获客"})
    assert first["success"] is True
    second = keyword_collector.start(env["db"], {"accountId": "acc-2", "keywords": "智能体"})
    assert second["success"] is False
    assert "已有采集任务" in second["error"]
    assert second["state"]["runId"] == first["state"]["runId"]
    gate.set()
    _wait_idle(env["db"])


def test_stop_marks_stopping_then_stopped(env, monkeypatch):
    import threading

    gate = threading.Event()

    def handler(cmd, i):
        gate.wait(5)
        return {"ok": True, "items": []}

    _fake_shell(monkeypatch, handler)
    started = keyword_collector.start(env["db"], {"accountId": "acc-1", "keywords": "获客"})
    run_id = started["state"]["runId"]
    stopped = keyword_collector.stop(env["db"], {"runId": run_id})
    assert stopped["success"] is True
    assert stopped["state"]["status"] == "stopping"
    gate.set()
    st = _wait_idle(env["db"])
    assert st["status"] == "stopped"
    # 停止后再点开始必须能起来（不能因为 _STOP 残留而立刻退出）
    keyword_collector.start(env["db"], {"accountId": "acc-1", "keywords": "获客"})
    assert keyword_collector.latest(env["db"])["status"] in {"running", "completed"}
    _wait_idle(env["db"])


def test_unknown_run_id_rejected_on_stop(env, monkeypatch):
    _fake_shell(monkeypatch, lambda cmd, i: {"ok": True, "items": []})
    keyword_collector.start(env["db"], {"accountId": "acc-1", "keywords": "获客"})
    _wait_idle(env["db"])
    res = keyword_collector.stop(env["db"], {"runId": "kwc_not_exists"})
    assert res["success"] is False
    assert "不一致" in res["error"]
