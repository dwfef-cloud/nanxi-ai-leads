"""真机采集链路：指令下发 → 壳回传 → 入库 → 打分推进 → 双通道日志。

这里用「模拟壳」替代真实浏览器：从 /desktop/command 取指令，把构造好的评论
回传 /desktop/result，验证后端编排与入库是否正确。真实 DOM 交互由桌面壳
douyin_agent.js 负责，属 B 组真机验收项。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app import collector
from app.api import desktop_commands, event_bus, ipc_bridge as bridge, task_runner


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "lead_system.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE leads (id TEXT PRIMARY KEY, nickname TEXT, comment TEXT, "
        "source_keyword TEXT, source TEXT, source_task_id TEXT, source_url TEXT, "
        "video TEXT, video_id TEXT, comment_id TEXT, comment_user_id TEXT, "
        "account TEXT, note TEXT, region TEXT, matched_keywords TEXT, status TEXT, score INTEGER, "
        "intent_level TEXT, tags TEXT, created_at TEXT, updated_at TEXT)")
    con.execute(
        "CREATE TABLE system_settings (category TEXT NOT NULL, key TEXT NOT NULL, "
        "value TEXT, updated_at TEXT, PRIMARY KEY (category, key))")
    con.commit()
    con.close()

    monkeypatch.setattr(event_bus, "EVENTS_DB", tmp_path / "events.db")
    monkeypatch.setattr(bridge, "DB_PATH", db)
    desktop_commands.reset_for_tests()
    desktop_commands.mark_alive()      # 假装桌面壳执行器在线（真实环境靠轮询心跳）
    task_runner.reset_for_tests()
    yield db
    desktop_commands.reset_for_tests()
    task_runner.reset_for_tests()


def _enable_real(db) -> None:
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO system_settings(category,key,value) "
                "VALUES('collector','real','1')")
    con.commit()
    con.close()


def _fake_shell(keyword: str, comments: list[dict], timeout: float = 6.0) -> dict:
    """扮演桌面壳：取指令 → 回传结果。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cmd = desktop_commands.next_command()
        if cmd and cmd.get("type") == "collect-comments":
            desktop_commands.submit_result(cmd["id"], {
                "ok": True, "comments": comments, "stats": {"videos": 2},
                "note": "", "keyword": keyword})
            return cmd
        time.sleep(0.05)
    raise AssertionError("没有等到采集指令")


def _wait_idle(timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not task_runner.busy():
            return
        time.sleep(0.05)
    raise AssertionError("任务未结束")


def test_real_mode_off_by_default(env):
    """A real task without native review must remain closed."""
    assert task_runner.real_mode_enabled(env) is False
    res = task_runner.start(env, {"taskId": "t0", "accountId": "a1",
                                  "keywords": "智能体", "interactionLimit": 1})
    assert res["success"] is False and res["code"] == "live_review_required"


def test_collected_comment_keeps_original_relative_time_and_deduplicates(env):
    item = {"commentId": "1234567890123", "nickname": "需求用户", "text": "正好想搭建自己的智能体",
            "videoUrl": "https://www.douyin.com/video/7400000000000000001",
            "publishedText": "3月前·福建"}
    assert collector.ingest_comments(env, [item], "智能体开发")["inserted"] == 1
    assert collector.ingest_comments(env, [item], "智能体开发")["duplicated"] == 1
    with sqlite3.connect(env) as con:
        note, region = con.execute("SELECT note,region FROM leads").fetchone()
    assert note == "平台显示时间：3月前·福建"
    assert region == "福建"


# --------------------------------------------------------------------------
# 采集（读评论 → 入库 → 打分推进 → 去重）已归位到「线索采集」引擎：
#   app/runtime/runners.py::run_entity（registry.start(db, "entity", ...)）
# 等价覆盖见 tests/test_runtime.py::test_entity_run_writes_real_leads /
# test_engine_failure_is_recorded_not_swallowed / monitor 去重用例。
# 评论获客本身是写动作（发表主评），执行器未接入，见 test_task_runner.py。
# --------------------------------------------------------------------------

def test_desktop_endpoints(env):
    from fastapi.testclient import TestClient
    import bridge_server

    with TestClient(bridge_server.app) as client:
        assert client.get("/desktop/command").json()["command"] is None
        desktop_commands.publish({"type": "collect-comments", "keyword": "智能体"})
        got = client.get("/desktop/command").json()["command"]
        assert got["keyword"] == "智能体"
        assert client.post("/desktop/result", json={"id": got["id"], "ok": True}).json()["success"]
        status = client.get("/desktop/collector-status").json()
        assert status["realMode"] in (True, False) and "queue" in status


def test_ingest_skips_empty_text(env):
    res = collector.ingest_comments(env, [{"nickname": "空评", "text": "  "}], "智能体")
    assert res == {"inserted": 0, "duplicated": 0, "skipped": 1}


def test_engine_selection_prefers_configured(env):
    con = sqlite3.connect(env)
    con.execute("INSERT OR REPLACE INTO system_settings(category,key,value) "
                "VALUES('collector','engine','electron')")
    con.commit()
    con.close()
    assert task_runner.real_engine(env) == "electron"

    con = sqlite3.connect(env)
    con.execute("INSERT OR REPLACE INTO system_settings(category,key,value) "
                "VALUES('collector','engine','playwright')")
    con.commit()
    con.close()
    assert task_runner.real_engine(env) == "playwright"


def test_playwright_without_login_fails_fast(env, monkeypatch):
    """未登录时不能真的去开浏览器：必须立刻给出可操作的提示。"""
    from app import douyin_runner
    from app.runtime import browser

    desktop_commands.reset_for_tests()          # 壳离线
    monkeypatch.setattr(douyin_runner, "available", lambda: True)
    monkeypatch.setattr(douyin_runner, "logged_in", lambda: False)

    started = time.time()
    res = browser.execute({"type": "collect-comments", "keyword": "智能体"}, db_path=env)
    assert res["ok"] is False
    assert res["engine"] == "none"
    assert "未登录" in res["error"] or "桌面壳未加载" in res["error"]
    assert time.time() - started < 3, "未登录时不许真去开浏览器白等"


def test_electron_engine_fails_fast_without_agent(env):
    """壳没加载执行器（未重开）时必须立刻报错，而不是白等采集超时。"""
    from app.runtime import browser

    desktop_commands.reset_for_tests()          # 清掉心跳，模拟壳未轮询
    started = time.time()
    res = browser.execute({"type": "collect-comments", "keyword": "智能体"}, db_path=env)
    assert res["ok"] is False
    assert res["engine"] == "none"
    assert "未加载采集执行器" in res["error"]
    assert time.time() - started < 3, "必须快速失败，不能等采集超时"
    assert desktop_commands.peek_queue() == []   # 不留下没人消费的指令


def test_collect_once_requires_agent_online(env):
    """壳没开时必须给出可操作提示，不能静默超时。"""
    desktop_commands.reset_for_tests()          # 清心跳 = 壳未开
    res = bridge.desktop_collect_once({"keyword": "装修", "videoMax": 3})
    assert res["success"] is False
    assert "执行器不在线" in res["error"]


def test_collect_once_rejects_empty_keyword(env):
    res = bridge.desktop_collect_once({"keyword": "  "})
    assert res["success"] is False
    assert "关键词" in res["error"]


def test_collect_once_roundtrip_without_ingest(env, monkeypatch):
    """壳在线：试跑返回样例评论，默认不写库。"""
    import threading
    threading.Thread(target=_fake_shell, args=(
        "装修", [{"nickname": "小王", "text": "装修多少钱一平", "commentId": "c1"}]),
        daemon=True).start()
    before = sqlite3.connect(env).execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    res = bridge.desktop_collect_once({"keyword": "装修", "videoMax": 3, "timeout": 20})
    assert res["success"] is True
    assert res["commentCount"] == 1
    assert res["sample"][0]["text"].startswith("装修多少钱")
    after = sqlite3.connect(env).execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    assert after == before                  # 未加 ingest 不落库


def test_collect_once_ingest_when_asked(env):
    import threading
    threading.Thread(target=_fake_shell, args=(
        "装修", [{"nickname": "小王", "text": "装修多少钱一平", "commentId": "c9"}]),
        daemon=True).start()
    res = bridge.desktop_collect_once({"keyword": "装修", "videoMax": 3,
                                       "timeout": 20, "ingest": True})
    assert res["success"] is True
    assert res["ingest"]["inserted"] == 1
    row = sqlite3.connect(env).execute(
        "SELECT source, source_keyword FROM leads WHERE comment_id='c9'").fetchone()
    assert row == ("keyword_comment", "装修")
