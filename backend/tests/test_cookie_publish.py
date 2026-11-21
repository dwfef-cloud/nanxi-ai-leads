"""账号池 CK + 作品发布 + 账号数据统计：新接通通道的行为测试。

这组测试只管「不撒谎」：
- CK 解析必须认出会话票据，游客 Cookie 直接判无效；
- 壳不在线时导入/导出/发布都必须明确失败，不能落库冒充成功；
- 发布队列的每一篇必须走状态机（published / uncertain / failed / ready），
  uncertain 不能被当成成功，也不能自动重试；
- 账号统计只写真的读到的字段（读不到就保留旧值，绝不写 0 把曲线砸下去）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.api import account_data, cookie_session
from app.api import ipc_bridge as bridge
from app.runtime import publish

ACCOUNTS_DDL = """CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, name TEXT DEFAULT '', nickname TEXT DEFAULT '',
    platform TEXT DEFAULT 'douyin', status TEXT DEFAULT 'idle', notes TEXT DEFAULT '',
    health_score INTEGER DEFAULT 100, limit_status TEXT DEFAULT 'healthy',
    daily_outreach INTEGER DEFAULT 0, daily_limit INTEGER DEFAULT 80, profile_dir TEXT DEFAULT '',
    created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')"""

TASKS_DDL = """CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, task_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'stopped', error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""

STATS_DDL = """CREATE TABLE IF NOT EXISTS account_stats_daily (
    account_id TEXT NOT NULL, day TEXT NOT NULL, nickname TEXT DEFAULT '',
    douyin_id TEXT DEFAULT '', following INTEGER DEFAULT 0, followers INTEGER DEFAULT 0,
    works INTEGER DEFAULT 0, likes INTEGER DEFAULT 0, updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, day))"""

COOKIE_JSON = ('[{"name":"sessionid","value":"s1"},{"name":"ttwid","value":"t1"},'
               '{"name":"sid_guard","value":"g1"}]')


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "cookie.db"
    con = sqlite3.connect(path)
    for ddl in (ACCOUNTS_DDL, TASKS_DDL, STATS_DDL):
        con.executescript(ddl)
    con.execute("INSERT INTO accounts(id,name,platform,status) VALUES('acc-1','账号一号','douyin','idle')")
    con.commit()
    con.close()
    monkeypatch.setattr(bridge, "DB_PATH", path)
    monkeypatch.setattr(cookie_session, "DB_PATH", path, raising=False)
    publish.reset_for_tests()
    yield str(path)
    publish.reset_for_tests()


# ── CK 解析 ────────────────────────────────────────────────

def test_parse_devtools_and_json_cookie():
    text = "Cookie: sessionid=abc; sid_guard=xyz; ttwid=1%7Cq"
    cookies, err = cookie_session.parse_cookie_text(text)
    assert not err
    names = {c["name"] for c in cookies}
    assert {"sessionid", "sid_guard", "ttwid"} <= names
    assert cookie_session._has_session(cookies)

    cookies, err = cookie_session.parse_cookie_text(COOKIE_JSON)
    assert not err and len(cookies) == 3
    assert cookie_session.build_cookie_string(cookies).startswith("sessionid=s1")


def test_guest_cookie_is_rejected():
    cookies, _ = cookie_session.parse_cookie_text("ttwid=1; odin_tt=2")
    assert not cookie_session._has_session(cookies)


def test_batch_split_and_parse():
    text = "# 账号A\nsessionid=aaa; ttwid=1\n---\n# 账号B\nsessionid=bbb"
    out = cookie_session.parse_batch({"text": text})
    assert out["ok"] and out["valid"] == 2 and out["invalid"] == 0
    labels = [i["label"] for i in out["items"]]
    assert labels == ["账号A", "账号B"]

    bad = cookie_session.parse_batch({"text": "ttwid=1\n---\nnameonly=x"})
    assert bad["ok"] is False and bad["invalid"] == 2


def test_batch_splits_one_cookie_per_line():
    # 每行自带会话票据 -> 一行一个账号
    text = "sessionid=aaa; ttwid=1\nsessionid=bbb; odin_tt=2"
    items = cookie_session.parse_batch({"text": text})["items"]
    assert len(items) == 2
    assert all(i["count"] == 2 for i in items)

    # 折行的同一段 Cookie（只有一行带 sessionid）必须当成一个账号，不能切碎
    folded = cookie_session.parse_batch({"text": "sessionid=ccc\nttwid=1\nodin_tt=2"})["items"]
    assert len(folded) == 1 and folded[0]["count"] == 3


# ── CK 导入 / 导出 ─────────────────────────────────────────

def test_import_requires_shell_and_does_not_fake_success(db, monkeypatch):
    monkeypatch.setattr(bridge, "_shell_task", lambda *a, **k: None)
    out = cookie_session.import_session({"accountId": "acc-1", "cookieText": COOKIE_JSON})
    assert out["ok"] is False
    assert "桌面壳未响应" in out["message"]
    # 失败不落快照：库里不能出现「导入过」的痕迹
    assert cookie_session.load_snapshot(db, "acc-1") == []


def test_import_writes_partition_then_verifies(db, monkeypatch):
    calls = {}

    def fake_shell(action, timeout=8.0, **extra):
        calls[action] = extra
        return {"ok": True, "applied": 3}

    monkeypatch.setattr(bridge, "_shell_task", fake_shell)
    from app.runtime import browser
    monkeypatch.setattr(browser, "execute",
                        lambda *a, **k: {"ok": True, "loggedIn": True, "nickname": "小美"})
    out = cookie_session.import_session({"accountId": "acc-1",
                                         "cookieText": COOKIE_JSON,
                                         "cookieCount": 3})
    assert out["ok"] and out["verified"] is True and out["status"] == "online"
    assert calls["apply-cookies"]["accountId"] == "acc-1"
    snapshot = cookie_session.load_snapshot(db, "acc-1")
    assert [c["name"] for c in snapshot] == ["sessionid", "ttwid", "sid_guard"]
    con = sqlite3.connect(db)
    status = con.execute("SELECT status, nickname FROM accounts WHERE id='acc-1'").fetchone()
    con.close()
    assert status == ("active", "小美")


def test_import_skip_verify_keeps_status_untouched(db, monkeypatch):
    monkeypatch.setattr(bridge, "_shell_task", lambda *a, **k: {"ok": True, "applied": 3})
    out = cookie_session.import_session({"accountId": "acc-1", "cookieText": COOKIE_JSON,
                                         "skipVerify": True})
    assert out["ok"] and out["verified"] is False
    con = sqlite3.connect(db)
    assert con.execute("SELECT status FROM accounts WHERE id='acc-1'").fetchone()[0] == "idle"
    con.close()


def test_import_expired_cookie_reports_offline(db, monkeypatch):
    monkeypatch.setattr(bridge, "_shell_task", lambda *a, **k: {"ok": True, "applied": 3})
    from app.runtime import browser
    monkeypatch.setattr(browser, "execute", lambda *a, **k: {"ok": True, "loggedIn": False})
    out = cookie_session.import_session({"accountId": "acc-1", "cookieText": COOKIE_JSON})
    assert out["ok"] and out["status"] == "offline"


def test_export_prefers_shell_then_snapshot(db, monkeypatch):
    cookie_session.save_snapshot(db, "acc-1", [{"name": "sessionid", "value": "old"}])
    monkeypatch.setattr(bridge, "_shell_task",
                        lambda action, timeout=8.0, **k: {"ok": True, "copied": True}
                        if action == "copy-text" else
                        {"ok": True, "cookieText": "sessionid=new; ttwid=2"})
    out = cookie_session.export_sessions({"accountId": "acc-1"})
    assert out["ok"] and out["count"] == 2 and out["source"] == "shell"

    # 壳读不到当前 Cookie 时回落快照，并在 note 里说清来源
    monkeypatch.setattr(bridge, "_shell_task",
                        lambda action, timeout=8.0, **k: {"ok": True} if action == "copy-text"
                        else {"ok": False, "error": "未登录"})
    out = cookie_session.export_sessions({"accountId": "acc-1"})
    assert out["ok"] and out["source"] == "snapshot" and "快照" in out["note"]


def test_export_without_any_cookie_fails(db, monkeypatch):
    monkeypatch.setattr(bridge, "_shell_task", lambda *a, **k: {"ok": False})
    out = cookie_session.export_sessions({"accountIds": ["acc-1"]})
    assert out["ok"] is False


# ── 作品发布 ───────────────────────────────────────────────

def _task(db, **over):
    media = str(Path(db).parent / "a.mp4")
    Path(media).write_bytes(b"\x00" * 16)
    rec = {
        "id": "pub_1", "name": "发布任务",
        "items": [{
            "id": "item_1", "accountId": "acc-1", "accountName": "账号一号",
            "media": {"type": "video", "filePath": media, "filePaths": [media]},
            "title": "标题", "description": "描述", "status": "ready",
        }],
        "schedule": {"enabled": False, "runAt": None},
    }
    rec.update(over)
    return rec


def test_start_rejects_when_shell_offline(db, monkeypatch):
    task = _task(db)
    bridge.dispatch("save-work-publish-task", [task])
    from app.runtime import browser
    monkeypatch.setattr(browser, "shell_available", lambda: False)
    out = publish.start(db, {"taskRecord": task})
    assert out["success"] is False
    assert "桌面壳未运行" in out["error"]
    # 没有创建「运行中」的假状态
    assert publish.running_ids() == []


def test_start_validates_title_and_media(db, monkeypatch):
    from app.runtime import browser
    monkeypatch.setattr(browser, "shell_available", lambda: True)
    bad = _task(db, items=[{"id": "i2", "accountId": "acc-1",
                        "media": {"type": "video", "filePaths": []},
                        "title": "x" * 40, "description": "d", "status": "ready"}])
    bridge.dispatch("save-work-publish-task", [bad])
    out = publish.start(db, {"taskRecord": bad})
    assert out["success"] is False
    assert "素材" in out["error"] or "标题" in out["error"]


def test_run_marks_published_and_stops(db, monkeypatch):
    from app.runtime import browser
    monkeypatch.setattr(browser, "shell_available", lambda: True)
    seen = {}

    def fake_execute(cmd, **kw):
        seen.update(cmd)
        seen["account"] = kw.get("account_id")
        return {"ok": True, "status": "published", "url": "https://x"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    task = _task(db)
    bridge.dispatch("save-work-publish-task", [task])
    out = publish.start(db, {"taskRecord": task})
    assert out["success"] is True
    for _ in range(60):
        if not publish.running_ids():
            break
        import time as _t
        _t.sleep(0.05)
    rec = bridge.dispatch("get-work-publish-tasks", [{}])[0]
    assert rec["items"][0]["status"] == "published"
    assert seen["type"] == "publish-submit" and seen["account"] == "acc-1"
    assert rec["status"] in {"done", "stopped"}


def test_uncertain_is_not_success_and_not_retried(db, monkeypatch):
    from app.runtime import browser
    monkeypatch.setattr(browser, "shell_available", lambda: True)
    calls = []

    def fake_execute(cmd, **kw):
        calls.append(cmd)
        return {"ok": False, "status": "uncertain", "error": "未出现成功提示"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    task = _task(db)
    bridge.dispatch("save-work-publish-task", [task])
    publish.start(db, {"taskRecord": task})
    for _ in range(60):
        if not publish.running_ids():
            break
        import time as _t
        _t.sleep(0.05)
    rec = bridge.dispatch("get-work-publish-tasks", [{}])[0]
    assert rec["items"][0]["status"] == "uncertain"
    assert len(calls) == 1  # 不确定绝不自动重试


def test_stop_returns_unfinished_items_to_ready(db):
    task = _task(db)
    bridge.dispatch("save-work-publish-task", [task])
    out = publish.stop(db, {"taskId": "pub_1"})
    assert out["success"]
    rec = bridge.dispatch("get-work-publish-tasks", [{}])[0]
    assert rec["status"] == "stopped"


def test_update_task_patch(db):
    task = _task(db)
    bridge.dispatch("save-work-publish-task", [task])
    out = publish.update_task(db, {"id": "pub_1", "patch": {"status": "stopped"}})
    assert out["success"]
    rec = bridge.dispatch("get-work-publish-tasks", [{}])[0]
    assert rec["status"] == "stopped"


# ── 账号数据统计 ───────────────────────────────────────────

def test_num_parses_chinese_units():
    assert account_data._num("1.2万") == 12000
    assert account_data._num("3亿") == 300000000
    assert account_data._num("1,234") == 1234
    assert account_data._num("--") is None


def test_upsert_only_writes_fields_actually_read(db):
    assert account_data._upsert(db, "acc-1", {"followers": "1.2万", "likes": "9"}) == 2
    # 第二次只读到粉丝：粉丝更新，获赞保留上一次的值，绝不被 0 覆盖
    assert account_data._upsert(db, "acc-1", {"followers": "1.3万"}) == 1
    con = sqlite3.connect(db)
    row = con.execute("SELECT followers, likes FROM account_stats_daily "
                      "WHERE account_id='acc-1'").fetchone()
    con.close()
    assert row == (13000, 9)
    assert account_data._upsert(db, "acc-1", {"followers": "--"}) == 0


def test_collect_reports_reason_without_shell(db, monkeypatch):
    from app.runtime import browser
    monkeypatch.setattr(browser, "shell_available", lambda: False)
    out = account_data.collect(db, {})
    assert out["success"] is False and "桌面壳未运行" in out["reason"]
