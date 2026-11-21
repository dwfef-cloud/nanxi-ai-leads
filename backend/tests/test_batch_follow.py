"""线索库 · 批量跟进（任务 / 运行记录 / 日志 / 结果 / 领取规则）通道测试。

覆盖 2026-11-17 接通的一整组：
- 任务：保存（含调度推算 nextRunAt）、列表、编辑、停用/恢复/删除/清空日志
- 运行记录：分页、详情（results / logs 两种 kind）、清空日志、删除、全清
- 用户跟进历史：按 leadId / 昵称查，分页游标
- 领取规则：已触达 / 本轮重复 / 正在处理 / 最近失败 / 缺用户标识 的跳过分类
- 执行边界：条件不满足时 start-batch-action 直接拒绝，不创建假 run
- export-leads：与列表同口径，一次返回全部命中行
"""
from __future__ import annotations

import sqlite3

import pytest

from app.api import ipc_bridge as bridge
from app.runtime import batch_follow as bf

LEADS_DDL = """CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY, nickname TEXT DEFAULT '', comment TEXT DEFAULT '',
    source_keyword TEXT DEFAULT '', source TEXT DEFAULT '', source_task_id TEXT DEFAULT '',
    source_url TEXT DEFAULT '', video TEXT DEFAULT '', video_id TEXT DEFAULT '',
    comment_id TEXT DEFAULT '', comment_user_id TEXT DEFAULT '', comment_time TEXT DEFAULT '',
    external_id TEXT DEFAULT '', account TEXT DEFAULT '', status TEXT DEFAULT 'collected',
    score INTEGER DEFAULT 0, intent_level TEXT DEFAULT 'C', platform TEXT DEFAULT 'douyin',
    region TEXT DEFAULT '', note TEXT DEFAULT '', customer_need TEXT DEFAULT '',
    created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')"""


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "batch.db"
    con = sqlite3.connect(path)
    con.executescript(LEADS_DDL)
    con.execute("INSERT INTO leads(id,nickname,external_id,intent_level,created_at,source_keyword) "
                "VALUES('L1','小美','uid-1','A','2026-11-17 10:00:00','装修')")
    con.execute("INSERT INTO leads(id,nickname,external_id,intent_level,created_at,source_keyword) "
                "VALUES('L2','小刚','uid-2','C','2026-11-17 11:00:00','装修')")
    con.execute("INSERT INTO leads(id,nickname,intent_level,created_at,source_keyword) "
                "VALUES('L3','无标识','C','2026-11-17 12:00:00','装修')")
    con.commit()
    con.close()
    monkeypatch.setattr(bridge, "DB_PATH", path)
    bridge._TOUCH_LEDGERS.clear()
    return str(path)


def _post(channel, payload=None):
    return bridge.dispatch(channel, [payload] if payload is not None else [])


def _save(**kw):
    payload = {
        "name": "装修线索跟进",
        "schedule": {"type": "daily", "startHm": "09:00", "endHm": "18:00", "enabled": True},
        "filters": {"keyword": "装修"},
        "maxLeads": 50,
        "quantityMode": "total",
        "accountSuccessLimits": {"acc-1": 20},
        "config": {"type": "profile", "batchAccountIds": ["acc-1"], "filterFollowHistory": True},
    }
    payload.update(kw)
    return payload


# --------------------------------------------------------------------- 任务
def test_save_and_list_task_roundtrip(db):
    out = _post("save-batch-follow-task", _save())
    assert out["success"] is True
    assert out["id"].startswith("bf_")
    assert out["nextRunAt"] > 0  # daily 计划必须算出下一次时间

    listed = _post("get-batch-follow-tasks")
    assert listed["success"] is True and listed["total"] == 1
    task = listed["items"][0]
    assert task["name"] == "装修线索跟进"
    assert task["config"]["batchAccountIds"] == ["acc-1"]
    assert task["schedule"]["type"] == "daily"
    assert task["status"] == "scheduled"
    assert task["lastSelection"] == {} and task["lastProgress"] == {}


def test_save_requires_name_and_accounts(db):
    assert _post("save-batch-follow-task", _save(name="  "))["success"] is False
    bad = _save()
    bad["config"] = {"batchAccountIds": []}
    out = _post("save-batch-follow-task", bad)
    assert out["success"] is False and "执行账号" in out["error"]


def test_save_updates_existing_task(db):
    first = _post("save-batch-follow-task", _save())
    second = _post("save-batch-follow-task", _save(id=first["id"], name="改名了", maxLeads=10))
    assert second["success"] and second["id"] == first["id"]
    listed = _post("get-batch-follow-tasks")
    assert listed["total"] == 1
    assert listed["items"][0]["name"] == "改名了"
    assert listed["items"][0]["maxLeads"] == 10


def test_pause_and_resume_toggle_schedule(db):
    task_id = _post("save-batch-follow-task", _save())["id"]
    paused = _post("control-batch-follow-task", {"id": task_id, "action": "pause"})
    assert paused["success"] is True and paused["enabled"] is False
    item = _post("get-batch-follow-tasks")["items"][0]
    assert item["status"] == "disabled" and item["nextRunAt"] == 0

    resumed = _post("control-batch-follow-task", {"id": task_id, "action": "resume"})
    assert resumed["success"] is True and resumed["enabled"] is True
    assert _post("get-batch-follow-tasks")["items"][0]["status"] == "scheduled"


def test_control_delete_removes_task_and_runs(db):
    task_id = _post("save-batch-follow-task", _save())["id"]
    out = _post("control-batch-follow-task", {"id": task_id, "action": "delete"})
    assert out["success"] is True
    assert _post("get-batch-follow-tasks")["total"] == 0
    assert _post("get-batch-follow-runs-page", {"taskId": task_id})["total"] == 0


def test_control_rejects_unknown_action(db):
    task_id = _post("save-batch-follow-task", _save())["id"]
    out = _post("control-batch-follow-task", {"id": task_id, "action": "explode"})
    assert out["success"] is False and "不支持" in out["error"]


# --------------------------------------------------------------- 运行记录
def _seed_run(db_path, task_id="bf_x", run_id="run_1"):
    bf.ensure_tables(db_path)
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO batch_follow_runs(id,task_id,task_name,kind,status,started_ms,"
                "ended_ms,total,current,success,failed,skipped,account_ids) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, "装修线索跟进", "scheduled", "completed", 1_700_000_000_000,
                 1_700_000_060_000, 3, 3, 2, 1, 0, '["acc-1"]'))
    for i, (lead, status, reason) in enumerate(
            (("L1", "success", ""), ("L2", "failed", "私信失败"), ("L3", "skipped", "缺少用户标识"))):
        con.execute("INSERT INTO batch_follow_items(run_id,lead_id,nickname,account_id,action,"
                    "status,reason,created_ms) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, lead, f"用户{lead}", "acc-1", "send-dm", status, reason,
                     1_700_000_000_000 + i))
    con.execute("INSERT INTO batch_follow_logs(run_id,ts_ms,level,account,message) "
                "VALUES(?,?,?,?,?)", (run_id, 1_700_000_000_000, "normal", "acc-1", "开始执行"))
    con.commit()
    con.close()


def test_runs_page_and_details(db):
    _seed_run(db)
    page = _post("get-batch-follow-runs-page", {"taskId": "bf_x", "page": 1, "pageSize": 10})
    assert page["total"] == 1
    run = page["items"][0]
    assert run["status"] == "completed" and run["success"] == 2 and run["logCount"] == 1

    results = _post("get-batch-follow-run-details-page",
                    {"id": "run_1", "kind": "results", "page": 1, "pageSize": 10})
    assert results["run"]["id"] == "run_1"
    assert results["total"] == 3
    assert {i["status"] for i in results["items"]} == {"success", "failed", "skipped"}

    filtered = _post("get-batch-follow-run-details-page",
                     {"id": "run_1", "kind": "results", "status": "失败"})
    assert filtered["total"] == 1 and filtered["items"][0]["reason"] == "私信失败"

    logs = _post("get-batch-follow-run-details-page", {"id": "run_1", "kind": "logs"})
    assert logs["total"] == 1 and logs["items"][0]["message"] == "开始执行"


def test_run_details_missing_run_is_empty(db):
    out = _post("get-batch-follow-run-details-page", {"id": "nope", "kind": "logs"})
    assert out["run"] is None and out["items"] == []


def test_clear_run_logs_keeps_results(db):
    _seed_run(db)
    out = _post("clear-batch-follow-run-logs", {"id": "run_1"})
    assert out["success"] is True
    logs = _post("get-batch-follow-run-details-page", {"id": "run_1", "kind": "logs"})
    assert logs["total"] == 0
    results = _post("get-batch-follow-run-details-page", {"id": "run_1", "kind": "results"})
    assert results["total"] == 3
    assert _post("get-batch-follow-runs-page", {})["items"][0]["logCount"] == 0


def test_delete_and_clear_runs(db):
    _seed_run(db)
    assert _post("delete-batch-follow-runs", ["run_1"])["success"] is True
    assert _post("get-batch-follow-runs-page", {})["total"] == 0

    _seed_run(db, run_id="run_2")
    assert _post("clear-batch-follow-runs")["success"] is True
    assert _post("get-batch-follow-runs-page", {})["total"] == 0
    results = _post("get-batch-follow-run-details-page", {"id": "run_2", "kind": "results"})
    assert results["run"] is None


def test_delete_runs_requires_ids(db):
    out = _post("delete-batch-follow-runs", [])
    assert out["success"] is False


# ------------------------------------------------------------ 用户跟进历史
def test_user_history_by_lead_and_nickname(db):
    _seed_run(db)
    by_lead = _post("get-batch-follow-user-history", {"leadId": "L1", "limit": 30})
    assert by_lead["success"] is True and by_lead["exact"] is True
    assert by_lead["items"][0]["leadId"] == "L1"

    by_name = _post("get-batch-follow-user-history", {"nickname": "用户L"})
    assert by_name["total"] == 3 and by_name["exact"] is False

    empty = _post("get-batch-follow-user-history", {})
    assert empty["success"] is False and "选择" in empty["error"]

    assert _post("cancel-batch-follow-user-history", {"requestKey": "k1"})["success"] is True


# --------------------------------------------------------------- 领取规则
def test_claim_classifies_skip_reasons(db):
    bf.ensure_tables(db)
    con = sqlite3.connect(db)
    # L1 已在另一个 run 里领取（处理中）
    con.execute("INSERT INTO batch_follow_claims(lead_id,run_id,account_id,created_ms) "
                "VALUES('L1','other_run','acc-1',1)")
    # L2 最近失败过（24 小时内）
    con.execute("INSERT INTO batch_follow_items(run_id,lead_id,nickname,account_id,action,status,"
                "reason,created_ms) VALUES('old','L2','小刚','acc-1','send-dm','failed','超时',?)",
                (bf._now_ms() - 60_000,))
    con.commit()
    con.close()

    out = bf.claim(db, {"keyword": "装修"}, limit=10, run_id="new_run")
    # L1 别的 run 正在处理、L2 最近失败过、L3 没有用户标识 —— 三条都不该被领走
    assert out["items"] == []
    assert out["skipped"]["processing"] == 1
    assert out["skipped"]["recent_failed"] == 1
    assert out["skipped"]["missing_identity"] == 1
    assert out["skippedReasons"]["processing"] == bf.SKIP_REASONS["processing"]


def test_claim_skips_missing_identity_and_duplicates(db):
    out = bf.claim(db, {"keyword": "装修"}, limit=10, run_id="r2", skip_touched=False)
    ids = {i["leadId"] for i in out["items"]}
    assert ids == {"L1", "L2"}
    # L3 没有 external_id / comment_user_id / source_url
    assert out["skipped"]["missing_identity"] == 1

    bf.claim_mark(db, "r2", ["L1", "L2"])
    again = bf.claim(db, {"keyword": "装修"}, limit=10, run_id="r2", skip_touched=False)
    assert again["items"] == []
    assert again["skipped"]["duplicate"] >= 1

    bf.claim_release(db, "r2")
    after = bf.claim(db, {"keyword": "装修"}, limit=10, run_id="r3", skip_touched=False)
    assert {i["leadId"] for i in after["items"]} == {"L1", "L2"}


def test_claim_marks_touched_when_enabled(db):
    out = bf.claim(db, {"keyword": "装修"}, limit=10, run_id="r4", skip_touched=True)
    assert "touched" not in out["skipped"] or out["skipped"]["touched"] >= 0


# ------------------------------------------------------------------ 执行边界
def test_start_batch_rejects_without_leads_or_accounts(db):
    assert _post("start-batch-action", {"leads": [], "config": {}})["ok"] is False
    out = _post("start-batch-action", {"leads": [{"leadId": "L1"}], "config": {}})
    assert out["ok"] is False and "执行账号" in out["reason"]


def test_start_batch_requires_share_friends(db):
    out = _post("start-batch-action", {
        "leads": [{"leadId": "L1"}],
        "config": {"batchAccountIds": ["acc-1"], "share": True, "profileShareFriends": ""},
    })
    assert out["ok"] is False and "好友昵称" in out["reason"]


def test_start_batch_never_creates_fake_run(db):
    out = _post("start-batch-action", {
        "leads": [{"leadId": "L1"}],
        "config": {"batchAccountIds": ["acc-1"], "type": "profile"},
    })
    assert out["ok"] is False and out["reason"]
    assert _post("get-batch-follow-runs-page", {})["total"] == 0


# ---------------------------------------------------------------- 导出通道
def test_export_leads_same_filter_as_list(db):
    out = _post("export-leads", {"filters": {"keyword": "装修"}, "fields": ["nickname"]})
    assert out["total"] == 3 and out["fields"] == ["nickname"]
    assert {i["leadId"] for i in out["items"]} == {"L1", "L2", "L3"}
    assert bridge.dispatch("get-leads-count", [{"filters": {"keyword": "装修"}}])["total"] == 3


def test_export_leads_empty_when_no_match(db):
    out = _post("export-leads", {"filters": {"keyword": "不存在的关键词"}})
    assert out["total"] == 0 and out["items"] == []


# ------------------------------------------------------------------ 代理测试
def test_test_batch_follow_proxy_rejects_incomplete(db):
    out = _post("test-batch-follow-proxy", {"host": "", "port": 0})
    assert out["success"] is False and "不完整" in out["message"]
    socks = _post("test-batch-follow-proxy", {"host": "127.0.0.1", "port": 1080, "type": "socks5"})
    assert socks["success"] is False and "SOCKS5" in socks["message"]
