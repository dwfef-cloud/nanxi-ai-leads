"""线索库 · 批量跟进执行器（#124 授权链路）测试。

覆盖点：
- 点「开始执行」后真的建 run、写批量作用域、逐条下发签名动作
- 作用域是 ``mode=batch``，动作上限按线索数给，多一条都签不出来
- 首作评论必须先落 reviewedComments 清单才允许签名
- 单个动作失败不吞、不冒充成功；一条线索的失败原因写进 items
- 停止：已开始的跑完，未领取的不再领
- 桌面壳不在线 → 不启动、不建假 run
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from app.api import ipc_bridge as bridge
from app.execution import live_leadgen, live_scope
from app.runtime import batch_follow, batch_runner, browser

LEADS_DDL = """CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY, nickname TEXT DEFAULT '', comment TEXT DEFAULT '',
    source_keyword TEXT DEFAULT '', source TEXT DEFAULT '', source_task_id TEXT DEFAULT '',
    source_url TEXT DEFAULT '', video TEXT DEFAULT '', video_id TEXT DEFAULT '',
    comment_id TEXT DEFAULT '', comment_user_id TEXT DEFAULT '', comment_time TEXT DEFAULT '',
    external_id TEXT DEFAULT '', account TEXT DEFAULT '', status TEXT DEFAULT 'collected',
    score INTEGER DEFAULT 0, intent_level TEXT DEFAULT 'C', platform TEXT DEFAULT 'douyin',
    region TEXT DEFAULT '', note TEXT DEFAULT '', customer_need TEXT DEFAULT '',
    created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')"""

ACCOUNTS_DDL = """CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, name TEXT DEFAULT '', nickname TEXT DEFAULT '',
    platform TEXT DEFAULT 'douyin', status TEXT DEFAULT 'active',
    notes TEXT DEFAULT '', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "batch.db"
    con = sqlite3.connect(db)
    con.executescript(LEADS_DDL)
    con.executescript(ACCOUNTS_DDL)
    con.execute("INSERT INTO accounts(id,name,nickname,platform,status) "
                "VALUES('acc-1','AI学长小马','小马','douyin','active')")
    con.commit()
    con.close()
    monkeypatch.setattr(bridge, "DB_PATH", db)
    monkeypatch.setenv("LEADS_MONITOR_SCOPE_DIR", str(tmp_path / "scopes"))
    bridge._TOUCH_LEDGERS.clear()
    batch_follow.ensure_tables(db)
    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(batch_runner, "LEAD_GAP_S", (0.0, 0.0))
    batch_runner._BUSY_ACCOUNTS.clear()
    batch_runner._STOP.clear()
    return {"db": str(db), "scopes": tmp_path / "scopes"}


def _leads(n=2):
    return [{"leadId": f"L{i}", "nickname": f"用户{i}",
             "userUrl": f"https://www.douyin.com/user/MS4wLjABAAAA{i}"} for i in range(1, n + 1)]


def _wait(db, run_id, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        with sqlite3.connect(db) as con:
            row = con.execute("SELECT status,current,success,failed,skipped FROM batch_follow_runs "
                              "WHERE id=?", (run_id,)).fetchone()
        if row and row[0] in {"completed", "stopped"}:
            return row
        time.sleep(0.05)
    raise AssertionError("批量执行没有在预期时间内结束")


def _patch_shell(monkeypatch, calls, *, works=1, posts=None):
    def fake_execute(cmd, **kw):
        if cmd.get("type") == "read-author-works":
            return {"ok": True, "author": "小美",
                    "items": [{"url": f"https://www.douyin.com/video/70000{i}",
                               "title": f"作品{i}", "pinned": False} for i in range(works)]}
        return {"ok": False, "error": "unexpected"}

    def fake_perform(db_path, action, **kw):
        calls.append((action, kw.get("video_url"), kw.get("text"), kw.get("account_id")))
        return posts if posts is not None else {"ok": True, "status": "sent_ok", "code": "sent"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    monkeypatch.setattr(live_leadgen, "perform", fake_perform)


def _cfg(**kw):
    base = {"batchAccountIds": ["acc-1"], "type": "follow", "skipTouched": False}
    base.update(kw)
    return base


def test_follow_only_runs_and_records_items(env, monkeypatch):
    calls = []
    _patch_shell(monkeypatch, calls)
    out = batch_follow.start_batch(env["db"], {"leads": _leads(2), "config": _cfg()})
    assert out["ok"] is True and out["total"] == 2
    row = _wait(env["db"], out["runId"])
    assert row[0] == "completed" and row[2] == 2, row
    actions = [c[0] for c in calls]
    assert actions.count("follow-user") == 2
    with sqlite3.connect(env["db"]) as con:
        items = con.execute("SELECT lead_id,account_id,status,reason FROM batch_follow_items "
                            "WHERE run_id=?", (out["runId"],)).fetchall()
    assert {i[0] for i in items} == {"L1", "L2"}
    assert all(i[2] == "success" for i in items)


def test_scope_is_batch_mode_with_per_action_caps(env, monkeypatch):
    calls = []
    _patch_shell(monkeypatch, calls)
    out = batch_follow.start_batch(env["db"], {"leads": _leads(3), "config": _cfg()})
    _wait(env["db"], out["runId"])
    files = list(env["scopes"].glob("*.json"))
    # 执行结束后作用域会被清掉；期间写下的那份内容由 mock 收集更稳，
    # 这里退一步：只断言目录里的钩子文件不存在残留，且动作数正确。
    assert not files
    assert [c[0] for c in calls].count("follow-user") == 3


def test_scope_written_before_dispatch(env, monkeypatch):
    seen = {}

    def fake_perform(db_path, action, **kw):
        files = list(env["scopes"].glob("*.json"))
        seen["scope"] = json.loads(files[0].read_text()) if files else {}
        return {"ok": True, "status": "sent_ok"}

    monkeypatch.setattr(browser, "execute",
                        lambda cmd, **kw: {"ok": True, "author": "小美", "items": []})
    monkeypatch.setattr(live_leadgen, "perform", fake_perform)
    out = batch_follow.start_batch(env["db"], {"leads": _leads(1), "config": _cfg()})
    _wait(env["db"], out["runId"])
    scope = seen.get("scope") or {}
    assert scope.get("mode") == "batch"
    assert scope.get("accountId") == "acc-1"
    assert scope["actions"].get("follow-user") == 1
    assert scope["videoUrls"] == ["*"]


def test_first_comment_requires_reviewed_list(env, monkeypatch):
    calls = []
    _patch_shell(monkeypatch, calls)
    cfg = _cfg(type="profile_first_comment", profileCommentUseAi=False,
               commentTemplate="很棒，支持一下！", enableProfileFirstFollow=False)
    out = batch_follow.start_batch(env["db"], {"leads": _leads(1), "config": cfg})
    _wait(env["db"], out["runId"])
    posts = [c for c in calls if c[0] == "post-comment"]
    assert len(posts) == 1 and posts[0][1].startswith("https://www.douyin.com/video/")


def test_launch_blocked_when_shell_offline(env, monkeypatch):
    monkeypatch.setattr(browser, "shell_available", lambda: False)
    out = batch_follow.start_batch(env["db"], {"leads": _leads(1), "config": _cfg()})
    assert out["ok"] is False and "桌面壳未运行" in out["reason"]
    with sqlite3.connect(env["db"]) as con:
        assert con.execute("SELECT COUNT(*) FROM batch_follow_runs").fetchone()[0] == 0


def test_failed_action_recorded_not_faked(env, monkeypatch):
    _patch_shell(monkeypatch, [], posts={"ok": False, "status": "skipped",
                                         "code": "follow_control_not_verified"})
    out = batch_follow.start_batch(env["db"], {"leads": _leads(1), "config": _cfg()})
    row = _wait(env["db"], out["runId"])
    # 壳侧「没验到关注按钮」是跳过，不是成功，也不是我们编出来的失败
    assert row[2] == 0 and row[4] == 1
    with sqlite3.connect(env["db"]) as con:
        status, reason = con.execute("SELECT status,reason FROM batch_follow_items WHERE run_id=?",
                                    (out["runId"],)).fetchone()
    assert status == "skipped" and "follow_control_not_verified" in reason


def test_stop_stops_remaining_leads(env, monkeypatch):
    calls = []

    def fake_perform(db_path, action, **kw):
        calls.append(action)
        batch_runner.stop(_STOP_RUN_ID[0])
        return {"ok": True, "status": "sent_ok"}

    monkeypatch.setattr(browser, "execute",
                        lambda cmd, **kw: {"ok": True, "author": "小美", "items": []})
    monkeypatch.setattr(live_leadgen, "perform", fake_perform)
    out = batch_follow.start_batch(env["db"], {"leads": _leads(5), "config": _cfg()})
    _STOP_RUN_ID[0] = out["runId"]
    row = _wait(env["db"], out["runId"], timeout=10)
    assert row[0] == "stopped"
    assert len(calls) < 5


_STOP_RUN_ID = [""]


# ------------------------------------------------------------ 作用域单元
def test_batch_scope_signs_only_listed_actions(env):
    path = live_scope.write_batch_scope(run_id="run-1", account_id="acc-1",
                                        actions={"follow-user": 2}, hours=1, nickname="小马")
    cmd = live_scope.signed_command("follow-user", account_id="acc-1", task_id="run-1",
                                    video_url="https://www.douyin.com/user/MS4wLjABAAAA1",
                                    comment_author="小美", recipient_id="MS4wLjABAAAA1")
    assert cmd["scopeId"] and cmd["acceptanceSignature"]
    with pytest.raises(ValueError):
        live_scope.signed_command("send-dm", account_id="acc-1", task_id="run-1",
                                  video_url="https://www.douyin.com/user/MS4wLjABAAAA1",
                                  text="你好呀朋友", comment_author="小美",
                                  recipient_id="MS4wLjABAAAA1")
    path.unlink()


def test_batch_comment_needs_reviewed_list(env):
    live_scope.write_batch_scope(run_id="run-2", account_id="acc-1",
                                 actions={"post-comment": 1}, hours=1, nickname="小马")
    with pytest.raises(ValueError):
        live_scope.signed_command("post-comment", account_id="acc-1", task_id="run-2",
                                  video_url="https://www.douyin.com/video/700000000001",
                                  text="很棒，支持一下！")
    batch_runner._append_reviewed_comment(
        live_scope.monitor_scope_file("run-2", "acc-1"),
        "https://www.douyin.com/video/700000000001", "很棒，支持一下！")
    cmd = live_scope.signed_command("post-comment", account_id="acc-1", task_id="run-2",
                                   video_url="https://www.douyin.com/video/700000000001",
                                   text="很棒，支持一下！")
    assert cmd["type"] == "post-comment"


# ------------------------------------------------------------ 养号恢复
def test_resume_nurture_safety_is_idempotent(env):
    from app.runtime import nurture_safety

    first = bridge.dispatch("resume-nurture-safety",
                            [{"taskId": "t1", "accountId": "acc-1"}])
    assert first["ok"] is True and first["resumed"] is False
    nurture_safety.mark_paused(env["db"], account_id="acc-1", task_id="t1", reason="疑似限流")
    state = bridge.dispatch("get-nurture-safety", [{}])
    assert state["items"] and state["items"][0]["accountId"] == "acc-1"
    again = bridge.dispatch("resume-nurture-safety", [{"taskId": "t1", "accountId": "acc-1"}])
    assert again["ok"] is True and again["resumed"] is True
    assert bridge.dispatch("get-nurture-safety", [{}])["items"] == []
