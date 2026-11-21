"""监控任务明细通道契约（2026-11-16 补齐）。

页面早就有「删除任务 / 删除研判记录 / 允许再研判 / 视频主评 / 直播状态 / 旧成员」
入口，但通道此前未注册，dispatch 落到 _disabled 返回「功能未开通」，按钮全是死的。
这里锁住补齐后的行为契约：
- 删除任务连着命中记录与运行证据一起清（页面承诺「命中记录与日志也将清除」）；
- 研判明细只删未命中/指定行，命中记录保留；
- 「允许再研判」只清 monitor_seen 去重，不动线索库；
- 直播明细已接真实数据（monitor_live_targets/events）；群成员明细 2026-11-18
  接真实 leads（source=monitor_group，采集器 _group_pass）；无记录时显式空态。
"""
from __future__ import annotations

import sqlite3

from app.api import ipc_bridge
from app.runtime import registry, store
from test_runtime import _monitor_task, db  # noqa: F401  复用运行时套件的夹具与建任务助手


def _evidence_tables(db) -> None:
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS task_runtime_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, leadgen_task_id TEXT,
            account_id TEXT, ts INTEGER, level TEXT, message TEXT);
        CREATE TABLE IF NOT EXISTS live_action_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, action TEXT, keyword TEXT,
            title TEXT, url TEXT, text TEXT, status TEXT, code TEXT,
            created_ms INTEGER, account_id TEXT, run_id TEXT);
    """)
    con.commit()
    con.close()


def _insert_lead(con, lid: str, tid: str, intent: str) -> None:
    con.execute("INSERT INTO leads(id, source, source_task_id, comment, intent_level) "
                "VALUES(?,'monitor_comment',?,'评论',?)", (lid, tid, intent))


def test_delete_monitor_tasks_removes_task_and_evidence(db, monkeypatch):
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    _evidence_tables(db)
    tid = _monitor_task(db, "mon_del")
    store.seen_mark(db, tid, ["k1", "k2"])
    with sqlite3.connect(str(db)) as con:
        _insert_lead(con, "l1", tid, "hot")
        con.execute("INSERT INTO task_runtime_logs(task_id, ts, level, message) "
                    "VALUES(?,0,'info','x')", (tid,))
        con.execute("INSERT INTO runtime_runs(run_id, kind, task_id, started_ms, status) "
                    "VALUES('run1','monitor',?,0,'ok')", (tid,))
        con.execute("INSERT INTO runtime_cycles(run_id, kind, task_id, cycle_no, started_ms, "
                    "status) VALUES('run1','monitor',?,1,0,'ok')", (tid,))
        con.commit()
    out = ipc_bridge.dispatch("delete-monitor-tasks", [tid])
    assert out["success"] is True, out
    assert out["deleted"] == 1
    with sqlite3.connect(str(db)) as con:
        for table, where in (
            ("tasks", f"id='{tid}'"),
            ("monitor_seen", f"task_id='{tid}'"),
            ("leads", "id='l1'"),
            ("task_runtime_logs", f"task_id='{tid}'"),
            ("runtime_runs", "run_id='run1'"),
            ("runtime_cycles", "run_id='run1'"),
        ):
            n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
            assert n == 0, f"{table} 未随任务删除"


def test_delete_monitor_tasks_refuses_running_task(db, monkeypatch):
    from app.api import task_runner
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    _evidence_tables(db)
    tid = _monitor_task(db, "mon_running")
    monkeypatch.setattr(task_runner, "active_ids", lambda: [tid])
    out = ipc_bridge.dispatch("delete-monitor-tasks", [tid])
    assert out["success"] is False
    assert "运行中" in out["error"]
    with sqlite3.connect(str(db)) as con:
        n = con.execute("SELECT COUNT(*) FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert n == 1, "运行中的任务不允许被删除"


def test_delete_monitor_match_records_clear_misses_keeps_hits(db, monkeypatch):
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    tid = _monitor_task(db, "mon_match")
    with sqlite3.connect(str(db)) as con:
        for lid, intent in (("hit1", "hot"), ("miss1", "normal"), ("miss2", "")):
            _insert_lead(con, lid, tid, intent)
        con.commit()
    out = ipc_bridge.dispatch("delete-monitor-match-records",
                              [{"taskId": tid, "clearMisses": True}])
    assert out["success"] is True, out
    with sqlite3.connect(str(db)) as con:
        left = {r[0] for r in con.execute("SELECT id FROM leads WHERE source='monitor_comment'")}
    assert left == {"hit1"}, left


def test_delete_monitor_match_records_by_ids(db, monkeypatch):
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    tid = _monitor_task(db, "mon_ids")
    with sqlite3.connect(str(db)) as con:
        for lid in ("m1", "m2"):
            _insert_lead(con, lid, tid, "hot")
        con.commit()
    out = ipc_bridge.dispatch("delete-monitor-match-records",
                              [{"taskId": tid, "matchIds": ["m1"]}])
    assert out["success"] is True
    with sqlite3.connect(str(db)) as con:
        left = {r[0] for r in con.execute("SELECT id FROM leads WHERE source_task_id=?", (tid,))}
    assert left == {"m2"}, left


def test_clear_monitor_comment_seen_and_stats_read_monitor_seen(db, monkeypatch):
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    tid = _monitor_task(db, "mon_seen")
    store.seen_mark(db, tid, ["a", "b", "c"])
    stats = ipc_bridge.dispatch("get-monitor-comment-seen-stats", [{}])
    assert stats["success"] is True
    assert stats["stats"]["seen"] == 3, "已扫计数应读 monitor_seen，而不是别的模块的表"
    out = ipc_bridge.dispatch("clear-monitor-comment-seen", [{"taskId": tid}])
    assert out["success"] is True and out["cleared"] == 3, out
    stats = ipc_bridge.dispatch("get-monitor-comment-seen-stats", [{}])
    assert stats["stats"]["seen"] == 0
    with sqlite3.connect(str(db)) as con:
        n = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    assert n == 0, "清去重记录不允许动研判明细（线索库）"


def test_monitor_task_video_comments_reads_post_comment_receipts(db, monkeypatch):
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    _evidence_tables(db)
    tid = _monitor_task(db, "mon_vc")
    with sqlite3.connect(str(db)) as con:
        con.execute("INSERT INTO live_action_results(task_id, action, title, url, text, status, "
                    "code, created_ms) VALUES(?,'post-comment','视频A','https://v/1','主评内容',"
                    "'sent_ok','',1000)", (tid,))
        con.execute("INSERT INTO live_action_results(task_id, action, title, url, text, status, "
                    "code, created_ms) VALUES(?,'like-video','视频A','https://v/1','',"
                    "'sent_ok','',1001)", (tid,))
        con.commit()
    out = ipc_bridge.dispatch("get-monitor-task-video-comments",
                              [{"taskId": tid, "offset": 0, "limit": 20}])
    assert out["success"] is True
    assert out["total"] == 1, "只统计本任务的主评回执，不混入点赞等其他动作"
    assert out["items"][0]["text"] == "主评内容"


def test_monitor_live_and_group_channels_return_explicit_empty(db, monkeypatch):
    """直播明细已接通真实数据：无记录时空态 + 说明文案，不再谎称「未接通」。"""
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    live = ipc_bridge.dispatch("get-monitor-live-state", [{"taskId": "x"}])
    assert live["success"] is True and live["targets"] == []
    assert live["pending"] == {"judge": 0, "action": 0}
    assert "还没有直播监控记录" in live["note"], "空态要说明原因，不给假数据"
    groups = ipc_bridge.dispatch("get-monitor-group-history", [{"taskId": "x"}])
    assert groups["success"] is True and groups["items"] == []


def test_monitor_live_state_returns_real_targets(db, monkeypatch):
    """runtime 写过 monitor_live_targets/events 后，明细页能读到真实状态。"""
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    url = "https://live.douyin.com/123"
    store.live_target_upsert(db, task_id="t_live", target_key=url, type_="live",
                             name="测试间", account_id="acc1", account_name="小马",
                             state="listening", last_check_ms=1700000000000,
                             events_delta=3)
    eid = store.live_event_insert(db, task_id="t_live", target_key=url, kind="comment",
                                  nickname="小明", content="求带一下", account_id="acc1")
    store.live_event_insert(db, task_id="t_live", target_key=url, kind="enter",
                            nickname="小红", content="", account_id="acc1", outcome="signal")
    store.live_event_set_outcome(db, eid, "matched", keyword="带",
                                 note="优先关键词命中", actions="动作 1 成功 / 0 失败")
    store.live_target_bump(db, task_id="t_live", target_key=url, added=1, matched=1)

    state = ipc_bridge.dispatch("get-monitor-live-state", [{"taskId": "t_live"}])
    assert state["success"] is True and len(state["targets"]) == 1
    tgt = state["targets"][0]
    assert tgt["key"] == url and tgt["state"] == "listening"
    assert tgt["name"] == "测试间" and tgt["accountName"] == "小马"
    assert tgt["events"] == 3 and tgt["added"] == 1 and tgt["matched"] == 1
    assert state["pending"] == {"judge": 0, "action": 0}   # 唯一弹幕已 matched 且有动作回执
    assert state["note"] == ""

    events = ipc_bridge.dispatch("get-monitor-live-events",
                                 [{"taskId": "t_live", "limit": 50}])
    assert events["success"] is True and events["total"] == 2
    by_id = {it["id"]: it for it in events["items"]}
    ev = by_id[eid]
    assert ev["room"] == "直播间 123" and ev["user"] == "小明"
    assert ev["event"] == "求带一下" and ev["result"] == "命中"
    assert ev["eventType"] == "弹幕" and ev["keyword"] == "带"
    assert ev["actions"] == "动作 1 成功 / 0 失败"
    enter = [it for it in events["items"] if it["kind"] == "enter"][0]
    assert enter["result"] == "互动信号" and enter["eventType"] == "进入直播间"

    # 筛选：outcome=success 只出命中/信号；targetKey 过滤
    ok = ipc_bridge.dispatch("get-monitor-live-events",
                             [{"taskId": "t_live", "outcome": "success"}])
    assert ok["total"] == 2
    failed = ipc_bridge.dispatch("get-monitor-live-events",
                                 [{"taskId": "t_live", "outcome": "failed"}])
    assert failed["total"] == 0
    other = ipc_bridge.dispatch("get-monitor-live-events",
                                [{"taskId": "t_live", "targetKey": "https://live.douyin.com/999"}])
    assert other["total"] == 0


def test_delete_monitor_tasks_covers_media_crawler_live_rows(db, monkeypatch):
    """列表按 task_type LIKE '%live%' 兜底展示历史导入行（media_crawler_live），
    删除口径必须与列表一致——只删 monitor_live 会删 0 行（2026-11-20 用户实测）。"""
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    _evidence_tables(db)
    tid = "media_crawler_live_legacy_1"
    # 先用标准通道建表，再插历史导入类型行（tasks 表无独立 save_task_type 通道）
    _monitor_task(db, "mon_anchor")
    with sqlite3.connect(str(db)) as con:
        con.execute("INSERT INTO tasks(id, task_type, status, payload, created_at, updated_at) "
                    "VALUES(?, 'media_crawler_live', 'stopped', '{}', 1794581599881, 1794581599881)", (tid,))
        con.commit()
    out = ipc_bridge.dispatch("delete-monitor-tasks", [tid])
    assert out["success"] is True, out
    assert out["deleted"] == 1
    with sqlite3.connect(str(db)) as con:
        n = con.execute("SELECT COUNT(*) FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert n == 0


def test_delete_monitor_tasks_covers_media_crawler_live_rows(db, monkeypatch):
    """列表按 task_type LIKE '%live%' 兜底展示历史导入行（media_crawler_live），
    删除口径必须与列表一致——只删 monitor_live 会删 0 行（2026-11-20 用户实测）。"""
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    _evidence_tables(db)
    tid = "media_crawler_live_legacy_1"
    # 先用标准通道建表，再插历史导入类型行（tasks 表无独立 save_task_type 通道）
    _monitor_task(db, "mon_anchor")
    with sqlite3.connect(str(db)) as con:
        con.execute("INSERT INTO tasks(id, task_type, status, payload, created_at, updated_at) "
                    "VALUES(?, 'media_crawler_live', 'stopped', '{}', 1794581599881, 1794581599881)", (tid,))
        con.commit()
    out = ipc_bridge.dispatch("delete-monitor-tasks", [tid])
    assert out["success"] is True, out
    assert out["deleted"] == 1
    with sqlite3.connect(str(db)) as con:
        n = con.execute("SELECT COUNT(*) FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert n == 0
