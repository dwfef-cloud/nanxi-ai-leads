"""三类真机运行时：跑得起来、跑的是真数据、跑不动时不伪造。

这里不连真实平台：用桩替换「浏览器指令层」，验证的是**运行时这一层**的契约——
指令发出去之后，结果有没有真的变成线索 / 事件 / 每轮记录，失败有没有留原因。
真机链路（壳内浏览器）由 tools/acceptance_real.py 做端到端验收。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app import leadgen_pools
from app.api import task_store
from app.runtime import browser, registry, store

_LEADS_DDL = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY, nickname TEXT, source_url TEXT, source_keyword TEXT,
    platform TEXT, external_id TEXT, source_task_id TEXT, source TEXT,
    comment TEXT, video TEXT, referral_note TEXT, score INTEGER,
    intent_level TEXT, intent TEXT, tags TEXT, note TEXT, customer_need TEXT,
    status TEXT, account TEXT, throttled_note TEXT, fail_note TEXT,
    reject_reason TEXT, wechat_added_at TEXT, manual INTEGER, deal_amount REAL,
    deal_at TEXT, hue INTEGER, region TEXT, next_followup_at TEXT,
    lost_reason TEXT, conversion_amount REAL, created_at TEXT, updated_at TEXT,
    lost_reason_category TEXT, lost_reason_note TEXT, video_id TEXT,
    comment_id TEXT, comment_user_id TEXT, comment_time TEXT,
    matched_keywords TEXT, intent_category TEXT, sentiment TEXT,
    risk_level TEXT, insight_json TEXT, desensitized INTEGER
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, name TEXT, nickname TEXT, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS account_events (
    id TEXT PRIMARY KEY, account_id TEXT, action TEXT, detail TEXT,
    success INTEGER, created_at TEXT
);
"""


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "lead_system.db"
    con = sqlite3.connect(str(path))
    con.executescript(_LEADS_DDL)
    con.execute("INSERT INTO accounts(id,name,nickname,status,created_at) "
                "VALUES('acct-real','主号','小马','active','2026-01-01')")
    con.commit()
    con.close()
    store.init(path)
    registry.reset_for_tests()
    yield path
    registry.reset_for_tests()


@pytest.fixture()
def shell_online(monkeypatch):
    monkeypatch.setattr(browser, "status", lambda _db: {
        "shell": True, "playwright": True, "playwrightLoggedIn": True,
        "engine": "shell", "queued": 0})


def _entity_task(db, task_id="ent_1", **cfg) -> str:
    record = {"id": task_id, "name": "线索采集测试", "status": "ready",
              "createdAt": 1794581599881, "entityKind": "entity",
              "runs": [{"accountId": "acct-real", "nickname": "小马"}],
              "configSnapshot": {"keywords": "智能体", "videoMax": 2,
                                 "commentLimit": 10, **cfg},
              "stats": {}}
    leadgen_pools.save_task_record(db, record)
    return task_id


def test_comment_interaction_account_lease_blocks_runtime_start(db, shell_online):
    task = _entity_task(db)
    assert registry.claim_account("acct-real", "comment-like-run")
    try:
        result = registry.start(db, "entity", task)
        assert result["success"] is False and result["busy"] is True
        registry.release_account("acct-real", "wrong-owner")
        assert "acct-real" in registry.active_accounts()
    finally:
        registry.release_account("acct-real", "comment-like-run")
    assert "acct-real" not in registry.active_accounts()


def _monitor_task(db, task_id="mon_1", **cfg) -> str:
    task_store.save_task(db, "monitor", {
        "id": task_id, "name": "监控测试", "status": "ready",
        "createdAt": 1794581599881,
        "configSnapshot": {"videoUrls": ["https://www.douyin.com/video/7400000000000000001"],
                           "keywords": ["AI"], "maxCycles": 1,
                           "intervalSec": 60, **cfg},
    })
    return task_id


def _warmup_task(db, task_id="warm_1", **cfg) -> str:
    task_store.save_task(db, "warmup", {
        "id": task_id, "name": "养号测试", "status": "ready",
        "createdAt": 1794581599881,
        "configSnapshot": {"cycles": 1, "scrolls": 3, "dwellMs": 500,
                           "likeMax": 1, **cfg},
    })
    return task_id


# ---------------------------------------------------------------- 启动前置
def test_start_without_engine_is_rejected_not_faked(db):
    _entity_task(db)
    res = registry.start(db, "entity", "ent_1")
    assert res["success"] is False and res["noEngine"] is True
    assert "执行引擎" in res["error"]
    # 没有开始执行就不该有运行记录（不能先写一条 running 再失败）
    assert store.recent_runs(db) == []


def test_start_missing_task_is_rejected(db, shell_online):
    res = registry.start(db, "monitor", "does_not_exist")
    assert res["success"] is False and "找不到任务记录" in res["error"]


# ---------------------------------------------------------------- 线索采集
def test_entity_source_and_comment_filters_follow_desktop_configuration(db, shell_online, monkeypatch):
    video = "https://www.douyin.com/video/7400000000000000001"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE live_action_results(action TEXT,status TEXT,url TEXT,keyword TEXT)")
        con.execute("INSERT INTO live_action_results VALUES('search-video','selected',?,'智能体开发')", (video,))
    calls = []
    def execute(cmd, **kwargs):
        calls.append(cmd)
        assert cmd["type"] == "read-comments", "指定视频不能搜索隐藏的旧关键词"
        assert cmd["videoUrls"] == [video]
        return {"ok": True, "comments": [
            {"commentId": "101010", "nickname": "自己", "text": "正好想搭建智能体", "videoUrl": video},
            {"commentId": "202020", "nickname": "用户甲", "text": "正好想搭建自己的智能体", "videoUrl": video},
            {"commentId": "303030", "nickname": "用户乙", "text": "正好想搭建自己的智能体", "videoUrl": video},
            {"commentId": "404040", "nickname": "用户丙", "text": "今天挺热闹的", "videoUrl": video}]}
    monkeypatch.setattr(browser, "execute", execute)
    _entity_task(db, sourceTypes=["video_specific"], keywords="火锅", specifiedUrls=video,
                 commentIncludeKeywords="正好想搭建", commentExcludeNicknames="自己", maxCollect=1,
                 maxCommentsPerVideo=10)
    assert registry.start(db, "entity", "ent_1")["success"]
    assert registry.wait_idle(db)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT nickname,status FROM leads").fetchall() == [("用户甲", "pending_outreach")]
        assert con.execute("SELECT source_keyword FROM leads").fetchone()[0] == "智能体开发"
    assert len(calls) == 1


def test_entity_run_writes_real_leads(db, shell_online, monkeypatch):
    seen_accounts: list[str] = []

    def fake_execute(cmd, *, db_path, timeout=None, log=None, stop_check=None,
                     account_id="", engine=""):
        # 账号必须随指令一起下发：多账号隔离全靠这个字段路由到对应的
        # 常驻窗口 + session partition，漏了就会全落到默认登录态上。
        seen_accounts.append(str(account_id or ""))
        if cmd["type"] == "search-videos":
            return {"ok": True, "items": [
                {"videoId": "7400000000000000001",
                 "url": "https://www.douyin.com/video/7400000000000000001",
                 "author": "做AI的老王", "title": "用智能体做客服，省了两个人"},
            ]}
        if cmd["type"] == "read-comments":
            return {"ok": True, "comments": [
                {"nickname": "小林", "text": "这个智能体多少钱？我们公司也想上",
                 "videoId": "7400000000000000001",
                 "videoUrl": "https://www.douyin.com/video/7400000000000000001"},
                {"nickname": "阿飞", "text": "哈哈哈", "videoId": "7400000000000000001"},
            ]}
        return {"ok": False, "error": "unexpected"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    _entity_task(db)
    res = registry.start(db, "entity", "ent_1")
    assert res["success"] is True
    assert registry.wait_idle(db, timeout=10), "运行时没有在超时内收尾"

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM leads WHERE source_task_id='ent_1' ORDER BY id")]
    con.close()
    assert rows, "线索采集必须真的往 leads 里写数据"
    # 评论走的是与评论获客同一条入库管道（source=keyword_comment），
    # 关键证据是：带任务归属、真的写进了库。
    assert all(r["source_task_id"] == "ent_1" for r in rows)
    assert any("智能体" in str(r["comment"] or "") for r in rows), "评论内容要真的落库"
    assert seen_accounts, "每条指令都要带上任务账号，否则多账号会共用默认登录态"
    assert len(set(seen_accounts)) == 1, "同一任务的所有指令必须落在同一个账号上"

    runs = store.recent_runs(db)
    assert runs and runs[0]["status"] == "completed"
    assert runs[0]["stats"]["leads"] >= 1
    assert runs[0]["stats"]["videos"] >= 1

    rec = leadgen_pools.load_records(db)["ent_1"]
    assert rec["status"] == "completed"
    assert rec["stats"]["leads"] >= 1
    assert rec["runs"], "任务记录里要能看到这一次运行批次"


def test_entity_without_targets_does_not_touch_platform(db, shell_online, monkeypatch):
    calls: list = []
    monkeypatch.setattr(browser, "execute",
                        lambda cmd, **kw: calls.append(cmd) or {"ok": True})
    _entity_task(db, keywords="", videoUrls=[], authorUrls=[])
    registry.start(db, "entity", "ent_1")
    assert registry.wait_idle(db, timeout=10)
    assert calls == [], "没有目标时不许发任何平台指令"
    runs = store.recent_runs(db)
    assert runs[0]["status"] == "failed"
    assert "没有可执行目标" in runs[0]["message"]


def test_engine_failure_is_recorded_not_swallowed(db, shell_online, monkeypatch):
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: {
        "ok": False, "error": "抖音账号未登录（检测到登录墙）"})
    _entity_task(db)
    registry.start(db, "entity", "ent_1")
    assert registry.wait_idle(db, timeout=10)
    runs = store.recent_runs(db)
    assert runs and runs[0]["status"] == "failed"
    assert "未登录" in runs[0]["message"]
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------- 监控
def test_monitor_dedupes_same_comments_across_runs(db, shell_online, monkeypatch):
    comments = [
        {"nickname": "小林", "text": "求 AI 客服方案", "videoId": "7400000000000000001"},
        {"nickname": "阿飞", "text": "路过看看", "videoId": "7400000000000000001"},
    ]
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: {
        "ok": True, "comments": list(comments), "note": ""})
    _monitor_task(db)

    registry.start(db, "monitor", "mon_1")
    assert registry.wait_idle(db, timeout=15)
    first = store.recent_runs(db)[0]
    assert first["status"] == "completed"
    assert first["stats"]["matched"] == 1, "只有含关键词的那条算命中"

    # 第二轮：同一批评论必须全部被去重，不能再算新命中
    registry.start(db, "monitor", "mon_1")
    assert registry.wait_idle(db, timeout=15)
    second = store.recent_runs(db)[0]
    assert second["run_id"] != first["run_id"]
    assert second["stats"]["matched"] == 0
    assert second["stats"]["new"] == 0

    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM leads WHERE source='monitor_comment'").fetchone()[0]
    con.close()
    assert n == 1, "同一评论不能重复变成线索"

    cycles = store.cycles_of(db, task_id="mon_1")
    assert cycles and cycles[0]["stats"]["read"] == 2


def test_monitor_passes_configured_author_and_comment_limits(db, shell_online, monkeypatch):
    commands = []

    def execute(cmd, **kwargs):
        commands.append(dict(cmd))
        if cmd["type"] == "read-author-works":
            return {"ok": True, "items": [{"url": "https://www.douyin.com/video/7400000000000000002"}]}
        return {"ok": True, "comments": [], "note": ""}

    monkeypatch.setattr(browser, "execute", execute)
    _monitor_task(db, videoUrls=[], authorUrls=["https://www.douyin.com/user/test"],
                  maxAuthorWorksPerCheck=7, maxCommentsPerVideo=180,
                  filterPinnedAuthorWorks=True, commentWindowMinutes=60,
                  expandSubComments=True)

    assert registry.start(db, "monitor", "mon_1")["success"] is True
    assert registry.wait_idle(db, timeout=15)
    author = next(cmd for cmd in commands if cmd["type"] == "read-author-works")
    comments = next(cmd for cmd in commands if cmd["type"] == "read-comments")
    assert author["limit"] == 7
    assert author["filterPinnedAuthorWorks"] is True
    assert comments["limit"] == 120
    assert comments["commentWindowMinutes"] == 60
    assert comments["expandSubComments"] is True


def test_monitor_applies_comment_exclusion_and_location_filters(db, shell_online, monkeypatch):
    comments = [
        {"nickname": "目标用户", "text": "AI 客服想了解", "videoId": "v1", "location": "上海"},
        {"nickname": "屏蔽用户", "text": "AI 客服想了解", "videoId": "v1", "location": "上海"},
        {"nickname": "区域外用户", "text": "AI 客服想了解", "videoId": "v1", "location": "北京"},
        {"nickname": "关键词屏蔽", "text": "AI 已经合作", "videoId": "v1", "location": "上海"},
    ]
    monkeypatch.setattr(browser, "execute", lambda cmd, **kwargs: {
        "ok": True, "comments": comments, "note": ""})
    _monitor_task(db, excludeCommenters=["屏蔽用户"],
                  excludeCommentKeywords=["已经合作"],
                  locationFilterMode="include", locationFilterRegions=["上海"])

    assert registry.start(db, "monitor", "mon_1")["success"] is True
    assert registry.wait_idle(db, timeout=15)
    con = sqlite3.connect(str(db))
    names = [row[0] for row in con.execute(
        "SELECT nickname FROM leads WHERE source='monitor_comment' ORDER BY nickname")]
    con.close()
    assert names == ["目标用户"]
    cycles = store.cycles_of(db, task_id="mon_1")
    assert cycles[0]["stats"]["filtered"] == 3


def test_monitor_uses_configured_ai_judge_and_keeps_only_matches(db, shell_online, monkeypatch):
    comments = [
        {"nickname": "咨询用户", "text": "AI 客服怎么收费", "videoId": "v1"},
        {"nickname": "闲聊用户", "text": "AI 客服真有意思", "videoId": "v1"},
    ]
    judgments = []

    def execute(cmd, **kwargs):
        return {"ok": True, "comments": comments, "note": ""}

    def judge(_db, payload, candidate, *, video_only=False):
        judgments.append((payload, candidate, video_only))
        return {"matched": candidate["nickname"] == "咨询用户", "reason": "明确询问价格"}

    monkeypatch.setattr(browser, "execute", execute)
    from app.execution import live_leadgen
    monkeypatch.setattr(live_leadgen, "classify_intent", judge)
    _monitor_task(db, useAiJudge=True, aiRole="智能体顾问",
                  aiPriorityKeywords="收费,报价")

    assert registry.start(db, "monitor", "mon_1")["success"] is True
    assert registry.wait_idle(db, timeout=15)
    con = sqlite3.connect(str(db))
    names = [row[0] for row in con.execute(
        "SELECT nickname FROM leads WHERE source='monitor_comment'")]
    con.close()
    assert names == ["咨询用户"]
    # 命中「优先关键词」的评论直接判高意向，不再调用 AI；只有没命中的那条走模型
    assert len(judgments) == 1
    assert judgments[0][1]["nickname"] == "闲聊用户"
    cycle_stats = store.cycles_of(db, task_id="mon_1")[0]["stats"]
    assert cycle_stats["matched"] == 1 and cycle_stats["filtered"] == 1


def test_monitor_ai_judge_failure_does_not_promote_comment(db, shell_online, monkeypatch):
    monkeypatch.setattr(browser, "execute", lambda cmd, **kwargs: {
        "ok": True, "comments": [{"nickname": "咨询用户", "text": "AI 客服怎么收费", "videoId": "v1"}],
        "note": ""})
    from app.execution import live_leadgen
    monkeypatch.setattr(live_leadgen, "classify_intent",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("model offline")))
    _monitor_task(db, useAiJudge=True)

    assert registry.start(db, "monitor", "mon_1")["success"] is True
    assert registry.wait_idle(db, timeout=15)
    con = sqlite3.connect(str(db))
    count = con.execute("SELECT COUNT(*) FROM leads WHERE source='monitor_comment'").fetchone()[0]
    con.close()
    stats = store.cycles_of(db, task_id="mon_1")[0]["stats"]
    assert count == 0
    assert stats["aiFailed"] == 1 and stats["filtered"] == 1


# ---------------------------------------------------------------- 养号
def test_warmup_writes_account_events(db, shell_online, monkeypatch):
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: {
        "ok": True, "liked": 1,
        "actions": [{"round": 1, "dwellMs": 500, "like": "liked", "caption": "推荐视频A"},
                    {"round": 2, "dwellMs": 500, "like": "", "caption": "推荐视频B"}]})
    _warmup_task(db)
    registry.start(db, "warmup", "warm_1")
    assert registry.wait_idle(db, timeout=15)

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    events = [dict(r) for r in con.execute(
        "SELECT action, detail, success FROM account_events ORDER BY action")]
    con.close()
    actions = [e["action"] for e in events]
    assert actions.count("watch") == 2, "每浏览一屏要落一条真实事件"
    assert actions.count("like") == 1
    assert all(int(e["success"]) == 1 for e in events)

    runs = store.recent_runs(db)
    assert runs[0]["status"] == "completed"
    assert runs[0]["stats"]["watched"] == 2 and runs[0]["stats"]["liked"] == 1


# ---------------------------------------------------------------- 并发与停止
def test_same_account_cannot_run_two_runtimes(db, shell_online, monkeypatch):
    import threading
    gate = threading.Event()

    def slow(cmd, **kw):
        gate.wait(3)
        return {"ok": True, "comments": []}

    monkeypatch.setattr(browser, "execute", slow)
    _monitor_task(db, "mon_a")
    _warmup_task(db, "warm_a")
    assert registry.start(db, "monitor", "mon_a")["success"] is True
    blocked = registry.start(db, "warmup", "warm_a")
    assert blocked["success"] is False and blocked.get("busy") is True
    assert "正在执行其他任务" in blocked["error"]
    gate.set()
    registry.wait_idle(db, timeout=15)


def test_stop_marks_run_stopped(db, shell_online, monkeypatch):
    def slow(cmd, **kw):
        import time
        for _ in range(60):
            time.sleep(0.05)
        return {"ok": True, "comments": []}

    monkeypatch.setattr(browser, "execute", slow)
    _monitor_task(db, "mon_stop", maxCycles=3)
    assert registry.start(db, "monitor", "mon_stop")["success"] is True
    registry.stop(db, "monitor", "mon_stop", wait=10)
    assert registry.wait_idle(db, timeout=10)
    runs = store.recent_runs(db)
    assert runs[0]["status"] == "stopped"


def test_dashboard_stop_by_view_key_stops_only_selected_account(db, shell_online, monkeypatch):
    import threading
    from app.api import ipc_bridge

    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    entered = threading.Event()
    commands = []

    def execute(cmd, **kwargs):
        commands.append(cmd)
        entered.set()
        assert kwargs["stop_check"].__self__.wait(2), "停止请求没有送到养号线程"
        return {"ok": False, "stopped": True}

    monkeypatch.setattr(browser, "execute", execute)
    _warmup_task(db, task_id="nurture_acct-real_stop", cycles=10)
    assert registry.start(db, "warmup", "nurture_acct-real_stop")["success"]
    assert entered.wait(1)
    assert ipc_bridge.dispatch("stop-task", [{"viewKey": "douyin_other"}])["stopped"] == 0
    assert registry.active("warmup")
    result = ipc_bridge.dispatch("stop-task", [{"viewKey": "douyin_acct-real"}])
    assert result["stopped"] == 1
    assert registry.wait_idle(db, timeout=3)
    assert len(commands) == 1, "手动停止后不能再下发下一轮"
    assert store.recent_runs(db)[0]["status"] == "stopped"


# ------------------------------------------------- 多账号：指令必须带账号
def test_shell_command_carries_account_id(monkeypatch):
    """壳按指令里的 accountId 选 session partition / 常驻窗口。

    少了这个字段，多账号会全部落到默认登录态上——看起来「多账号都配好了」，
    实际都在用同一个号跑，是最隐蔽的一类假成功。
    """
    from app.api import desktop_commands

    monkeypatch.setattr(desktop_commands, "agent_alive", lambda: True)
    captured: dict = {}

    def fake_publish(payload):
        captured.update(payload)
        return "cmd_test_1"

    monkeypatch.setattr(desktop_commands, "publish", fake_publish)
    monkeypatch.setattr(desktop_commands, "wait_result",
                        lambda cid, timeout=0, stop_check=None: {"ok": True, "items": []})

    res = browser.execute({"type": "search-videos", "keyword": "火锅"},
                          db_path=None, account_id="acct-b")
    assert res["ok"] is True
    assert captured.get("accountId") == "acct-b"


def test_shell_command_without_account_keeps_default(monkeypatch):
    """单账号/默认账号：不硬塞 accountId，保持旧契约（壳走默认 session）。"""
    from app.api import desktop_commands

    monkeypatch.setattr(desktop_commands, "agent_alive", lambda: True)
    captured: dict = {}
    monkeypatch.setattr(desktop_commands, "publish",
                        lambda payload: (captured.update(payload) or "cmd_test_2"))
    monkeypatch.setattr(desktop_commands, "wait_result",
                        lambda cid, timeout=0, stop_check=None: {"ok": True})

    browser.execute({"type": "search-videos", "keyword": "火锅"}, db_path=None)
    assert "accountId" not in captured



# ---------------------------------------------------------------- 页面设置生效
def test_warmup_uses_page_settings(db, shell_online, monkeypatch):
    """页面设的时长/停留/点赞概率必须真的下发到壳。

    历史 bug：页面设 60-120 分钟、点赞 10%，runner 只认 cycles/dwellMs/likeMax，
    实际跑 1 轮 6 屏、0 点赞 —— 设置形同虚设。
    """
    from app.runtime import runners

    seen: dict = {}

    def fake_execute(cmd, **kw):
        seen.update(cmd)
        return {"ok": True, "liked": 0,
                "actions": [{"round": 1, "dwellMs": cmd.get("dwellMs"), "like": ""}]}

    monkeypatch.setattr(browser, "execute", fake_execute)
    _warmup_task(db, cycles=2, scrolls=3,
                 nurtureDurationMin=60, nurtureDurationMax=120,
                 stayMin=8, stayMax=9, nurtureLikePercent=35)
    registry.start(db, "warmup", "warm_1")
    assert registry.wait_idle(db, timeout=15)

    assert seen["type"] == "warmup-watch"
    assert 8000 <= seen["dwellMs"] <= 9000, "停留要按页面的 8-9 秒随机，而不是写死 dwellMs"
    assert seen["likeProbability"] == 35.0, "点赞概率要按页面设置下发"
    assert seen["scrolls"] == 3
    assert seen["deadlineMs"] > 0
    # 时长预算来自页面区间（60-120 分钟）
    for _ in range(10):
        assert 3600 <= runners._warmup_budget_s(60, 120) <= 7200


def test_warmup_last_round_respects_remaining_time():
    from app.runtime.runners import _warmup_scrolls_left

    assert _warmup_scrolls_left(6, 20000, 99) == 3
    assert _warmup_scrolls_left(6, 20000, 346) == 6
    assert _warmup_scrolls_left(6, 20000, 20) == 0


def test_warmup_finished_event_matches_persisted_result(db, shell_online, monkeypatch):
    from app.runtime import runners
    events = []
    def publish(channel, payload):
        if channel == "nurture-task-finished":
            with sqlite3.connect(db) as con:
                assert con.execute("SELECT status FROM runtime_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "completed"
        events.append((channel, payload))
    monkeypatch.setattr(runners.event_bus, "publish", publish)
    monkeypatch.setattr(browser, "execute", lambda *a, **k: {
        "ok": True, "liked": 0, "actions": [{"round": 1, "dwellMs": 500}]})
    _warmup_task(db, cycles=1, scrolls=1, dwellMs=500, likeMax=0)
    assert registry.start(db, "warmup", "warm_1")["success"]
    assert registry.wait_idle(db)
    finished = [p for channel, p in events if channel == "nurture-task-finished"]
    assert len(finished) == 1
    assert finished[0]["reason"] == "completed"
    assert finished[0]["accountId"] == "acct-real"
    assert finished[0]["progress"]["nurture"]["videosWatched"] == 1


def test_warmup_keeps_legacy_rounds_config(db, shell_online, monkeypatch):
    """老配置只给 cycles/scrolls：不能被新逻辑拉成 1 小时。"""
    calls = {"n": 0}

    def fake_execute(cmd, **kw):
        calls["n"] += 1
        return {"ok": True, "liked": 0,
                "actions": [{"round": 1, "dwellMs": cmd.get("dwellMs"), "like": ""}]}

    monkeypatch.setattr(browser, "execute", fake_execute)
    _warmup_task(db, cycles=2, scrolls=2, dwellMs=500, likeMax=0)
    registry.start(db, "warmup", "warm_1")
    assert registry.wait_idle(db, timeout=15)
    assert calls["n"] == 2, "有 cycles 的老配置要按轮数结束"


# ---------------------------------------------------------------- 页面设置生效
def test_warmup_uses_page_settings(db, shell_online, monkeypatch):
    """页面设的时长/停留/点赞概率必须真的下发到壳。

    历史 bug：页面设 60-120 分钟、点赞 10%，runner 只认 cycles/dwellMs/likeMax，
    实际跑 1 轮 6 屏、0 点赞 —— 设置形同虚设。
    """
    from app.runtime import runners

    seen: dict = {}

    def fake_execute(cmd, **kw):
        seen.update(cmd)
        return {"ok": True, "liked": 0,
                "actions": [{"round": 1, "dwellMs": cmd.get("dwellMs"), "like": ""}]}

    monkeypatch.setattr(browser, "execute", fake_execute)
    _warmup_task(db, cycles=2, scrolls=3,
                 nurtureDurationMin=60, nurtureDurationMax=120,
                 stayMin=8, stayMax=9, nurtureLikePercent=35)
    registry.start(db, "warmup", "warm_1")
    assert registry.wait_idle(db, timeout=15)

    assert seen["type"] == "warmup-watch"
    assert 8000 <= seen["dwellMs"] <= 9000, "停留要按页面的 8-9 秒随机，而不是写死 dwellMs"
    assert seen["likeProbability"] == 35.0, "点赞概率要按页面设置下发"
    assert seen["scrolls"] == 3
    # 时长预算来自页面区间（60-120 分钟）
    for _ in range(10):
        assert 3600 <= runners._warmup_budget_s(60, 120) <= 7200


def test_warmup_keeps_legacy_rounds_config(db, shell_online, monkeypatch):
    """老配置只给 cycles/scrolls：不能被新逻辑拉成 1 小时。"""
    calls = {"n": 0}

    def fake_execute(cmd, **kw):
        calls["n"] += 1
        return {"ok": True, "liked": 0,
                "actions": [{"round": 1, "dwellMs": cmd.get("dwellMs"), "like": ""}]}

    monkeypatch.setattr(browser, "execute", fake_execute)
    _warmup_task(db, cycles=2, scrolls=2, dwellMs=500, likeMax=0)
    registry.start(db, "warmup", "warm_1")
    assert registry.wait_idle(db, timeout=15)
    assert calls["n"] == 2, "有 cycles 的老配置要按轮数结束"


# ------------------------------------------------ 界面「卡住」的两种真实成因
def test_warmup_pushes_progress_events(db, shell_online, monkeypatch):
    """养号必须推 nurture-progress 事件，否则账号卡片一直停在「养号初始化…」。

    历史：后端只发 self-warmup-task-event，页面的 nurture-progress 分支永远收不到，
    5 分钟养号在界面上看起来像卡死（2026-11-14 用户反馈）。
    """
    from app.api import event_bus
    from app.runtime import runners

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(event_bus, "publish",
                        lambda ch, payload: published.append((ch, payload)))
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: {
        "ok": True, "liked": 0,
        "actions": [{"round": 1, "dwellMs": 500, "like": ""},
                    {"round": 2, "dwellMs": 500, "like": ""}]})

    _warmup_task(db, cycles=1, scrolls=2, dwellMs=500, likeMax=0)
    registry.start(db, "warmup", "warm_1")
    assert registry.wait_idle(db, timeout=15)

    progress = [p for ch, p in published
                if ch == "automation-data" and p.get("type") == "nurture-progress"]
    assert progress, "养号没有推送进度事件，界面只能一直显示启动时的文案"
    body = progress[-1]["payload"]
    assert body["videosWatched"] == 2, "进度要用真实浏览屏数，不猜"
    assert body["accountId"] == "acct-real"
    assert body["taskId"] == "warm_1"
    assert "养号中" in body["action"]
    assert any(p.get("type") == "nurture-actions" for ch, p in published
               if ch == "automation-data"), "互动统计也要推，否则卡片上点赞数一直是 0"


def test_stale_running_runs_are_reaped(db):
    """进程退出后遗留的 running 记录必须在启动时收尾成 aborted。

    否则页面一直显示「运行中」、执行历史堆僵尸记录 —— 用户看到的就是「卡住了」。
    """
    run_id = store.run_begin(db, "warmup", "warm_1", "养号测试", "acct-real", "manual", "shell")
    cid = store.cycle_begin(db, run_id, "warmup", "warm_1", 1)

    assert store.reap_stale_runs(db, "服务重启") == 1

    run_row = [r for r in store.recent_runs(db) if r["run_id"] == run_id][0]
    assert run_row["status"] == "aborted"
    assert "服务重启" in run_row["message"]
    cyc = [c for c in store.cycles_of(db, run_id=run_id) if c["id"] == cid][0]
    assert cyc["status"] == "aborted"


def test_active_automation_tasks_ignores_stale_db_rows(db):
    """库里 status=running 的历史行不能当成「正在运行」。

    以前实况大屏读的是表里的状态，9 月遗留的 66 条 pending 行让它永远有卡片、
    永远「运行中」。运行态只认活线程，没有线程就是空列表。
    """
    from app.api import aggregate

    task_store.save_task(db, "warmup", {
        "id": "stale_1", "name": "历史养号", "status": "running",
        "configSnapshot": {"accountId": "acct-real"},
    })
    registry.reset_for_tests()
    assert aggregate.active_automation_tasks({}) == []


# ------------------------------------------- 养号日志要「能读、能对账」
def _warmup_logs(db, task_id="warm_1") -> list[str]:
    from app.api import task_logs

    return [str(r.get("message") or "")
            for r in task_logs.list_for(db, task_id, limit=200)]


def test_warmup_logs_are_clean_and_reconcilable(db, shell_online, monkeypatch):
    """日志要一眼对上设置：一行参数、每轮一行进度、一行结束。

    2026-11-14 用户对着日志提的问题都在这里兜住：
    - 「已下发指令 warmup-watch（）」空括号、每轮刷一遍；
    - 每轮重复一大段「没定位到点赞控件」，真正的进度被挤走；
    - 一次点击出现两条启动日志（运行时已启动 + 养号参数）。
    """
    def fake(cmd, **kw):
        return {"ok": True, "liked": 0,
                "actions": [{"round": n, "dwellMs": 400, "like": "skip-by-probability"}
                            for n in range(1, 3)]}

    monkeypatch.setattr(browser, "execute", fake)
    _warmup_task(db, cycles=2, scrolls=2, dwellMs=400, likeMax=0,
                 nurtureLikePercent=10)
    assert registry.start(db, "warmup", "warm_1")["success"] is True
    assert registry.wait_idle(db, timeout=15)

    msgs = _warmup_logs(db)
    joined = "\n".join(msgs)
    assert "（）" not in joined, f"日志里不能出现空括号：\n{joined}"
    assert "养号参数（运行 " in joined, "参数行要带运行号，多次运行才分得清"
    assert sum(1 for m in msgs if "运行时已启动" in m) == 0, "养号不再重复打启动日志"

    rounds = [m for m in msgs if m.startswith("第 ") and "轮：" in m]
    assert len(rounds) == 2, rounds
    assert all("进度" in m for m in rounds), "每轮必须带已跑/总秒数，才能和设置对账"
    assert "跳过 2" in rounds[0], "概率跳过次数必须能看见（否则无法核对 10%）"


def test_warmup_manual_stop_is_not_a_failure(db, shell_online, monkeypatch):
    """手动停止不能记成「浏览失败」——用户会以为养号跑挂了。"""
    import threading

    gate = threading.Event()

    def slow(cmd, **kw):
        gate.wait(6)
        return {"ok": False, "error": "已手动停止", "stopped": True}

    monkeypatch.setattr(browser, "execute", slow)
    _warmup_task(db, cycles=5, scrolls=2, dwellMs=400, likeMax=0,
                 nurtureDurationMin=5, nurtureDurationMax=5)
    assert registry.start(db, "warmup", "warm_1")["success"] is True
    # 等第一轮真的开始（否则停下的是「还没进循环」的空运行，没有 cycle 可查）
    for _ in range(100):
        if store.cycles_of(db, task_id="warm_1"):
            break
        time.sleep(0.05)
    registry.stop(db, "warmup", "warm_1")
    gate.set()
    assert registry.wait_idle(db, timeout=10)

    msgs = "\n".join(_warmup_logs(db))
    assert "浏览失败" not in msgs, msgs
    assert "已手动停止" in msgs
    run = store.recent_runs(db)[0]
    assert run["status"] == "stopped"
    cycles = store.cycles_of(db, run_id=run["run_id"])
    assert cycles and cycles[0]["status"] == "stopped", cycles


def test_warmup_executor_timeout_stops_without_retry(db, shell_online, monkeypatch):
    calls = []

    def no_result(cmd, **kw):
        calls.append(cmd)
        return {"ok": False, "error": "等待壳回传超时"}

    monkeypatch.setattr(browser, "execute", no_result)
    _warmup_task(db, cycles=3, nurtureDurationMin=5, nurtureDurationMax=5)
    assert registry.start(db, "warmup", "warm_1")["success"] is True
    assert registry.wait_idle(db, timeout=10)

    assert len(calls) == 1
    run = store.recent_runs(db)[0]
    assert run["status"] == "failed"
    assert run["stats"].get("watched", 0) == 0
    messages = "\n".join(_warmup_logs(db))
    assert "养号未完成" in messages
    assert "养号已跑满设定时长" not in messages


def test_stopped_command_does_not_remain_in_queue():
    from app.api import desktop_commands

    desktop_commands.reset_for_tests()
    try:
        cmd_id = desktop_commands.publish({"type": "warmup-watch"})
        assert desktop_commands.wait_result(cmd_id, timeout=1,
                                            stop_check=lambda: True) is None
        assert desktop_commands.next_command("test-shell") is None
        assert desktop_commands.stats()["inflight"] == 0
    finally:
        desktop_commands.reset_for_tests()


def test_run_engine_is_pinned_for_whole_run(db, monkeypatch):
    """一次运行中途不能换引擎。

    壳心跳有 30s 窗口，长指令跑着时会断一下；如果每条指令重新判引擎，同一次养号
    会一半跑在用户已登录的壳里、一半跑在独立浏览器里（日志里那条
    「独立引擎执行 warmup-watch：成功」），点赞数就落到另一个会话上了。
    """
    calls: list[str] = []

    monkeypatch.setattr(browser, "status", lambda _db: {"engine": "playwright"})
    monkeypatch.setattr(browser, "_via_shell",
                        lambda *a, **kw: (calls.append("shell"), {"ok": True})[1])
    monkeypatch.setattr(browser, "_via_playwright",
                        lambda *a, **kw: (calls.append("playwright"), {"ok": True})[1])

    browser.execute({"type": "warmup-watch"}, db_path=db, engine="shell")
    browser.execute({"type": "warmup-watch"}, db_path=db)  # 未钉住时按当下引擎
    assert calls == ["shell", "playwright"]


def test_command_log_has_no_empty_parens_and_skips_warmup_noise(monkeypatch):
    """指令日志：有内容才写括号，养号每轮不再刷一行「已下发指令」（2026-11-14）。"""
    from app.api import desktop_commands

    lines: list[str] = []
    monkeypatch.setattr(desktop_commands, "publish", lambda payload: "cmd_x")
    monkeypatch.setattr(desktop_commands, "wait_result", lambda *a, **kw: {"ok": True})

    def run(cmd):
        browser._via_shell(cmd, timeout=1,
                           log=lambda m, l="normal": lines.append(m),
                           stop_check=None)

    run({"type": "search-videos", "keyword": "AI 客服"})
    run({"type": "warmup-watch", "scrolls": 6})
    run({"type": "read-comments", "videoUrls": []})

    assert lines == ["已下发指令 search-videos（AI 客服），等待壳回传…",
                     "已下发指令 read-comments，等待壳回传…"], lines
    assert not any("（）" in m for m in lines)




# ---------------------------------------------------------------- 楼中楼开关
def test_entity_comment_read_forwards_expand_switch(db, shell_online, monkeypatch):
    """「采集二级评论」开关在 UI 上一直有，但这条链路从来没传给壳 —— 开了等于没开。"""
    _entity_task(db)
    commands: list[dict] = []

    def execute(cmd, **_kw):
        commands.append(dict(cmd))
        return {"ok": True, "comments": [], "note": ""}

    monkeypatch.setattr(browser, "execute", execute)
    from app.runtime import runners

    ctx = runners.Ctx(kind="entity", db_path=db, task_id="ent_1", name="线索采集测试",
                      account_id="acct-real", run_id="run_expand")
    runners._entity_target(ctx, {"type": "video",
                                 "value": "https://www.douyin.com/video/7400000000000000001"},
                           1, 10, 60, {"expandSubComments": True}, 0)
    read = next(cmd for cmd in commands if cmd["type"] == "read-comments")
    assert read["expandSubComments"] is True

    # 关着的时候不能硬塞，避免默认去点「展开」拖慢每一轮
    commands.clear()
    runners._entity_target(ctx, {"type": "video",
                                 "value": "https://www.douyin.com/video/7400000000000000001"},
                           1, 10, 60, {}, 0)
    read = next(cmd for cmd in commands if cmd["type"] == "read-comments")
    assert "expandSubComments" not in read
