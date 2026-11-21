"""吞评检测引擎与明细通道测试。

2026-11-17 补齐的链路：
1. run_entity 分流：sourceTypes 含 entity_comment_visibility 的任务走互检引擎，
   不再落「线索采集没有可执行目标」；
2. registry 启动前拦截：配置问题（账号不足、人设未绑定等）显式拒绝；
3. 常规互检执行：检测账号读样本视频评论区 → visible / not_visible / inconclusive
   / no_sample 四态落 leads（source=entity_comment_visibility）；
4. 明细过滤（结果状态/关键词/起始时间）、导出 CSV、删除关联记录、清空明细。

2026-11-18 新增：
5. 高速自检（backend_api）：被检账号用自己的会话核对勾选样本（limit 200、
   样本含回复时展开楼中楼、按 kind 分开核对、占用账号记 inconclusive）；
6. 回复样本：reply-comment sent_ok 回执作为 kind=reply 样本进入检测链路。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.runtime import store
from app.runtime import visibility as vis


LEADS_DDL = """CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY, nickname TEXT DEFAULT '', comment TEXT DEFAULT '',
    source_keyword TEXT DEFAULT '', source TEXT DEFAULT '', source_task_id TEXT DEFAULT '',
    source_url TEXT DEFAULT '', video TEXT DEFAULT '', video_id TEXT DEFAULT '',
    comment_id TEXT DEFAULT '', comment_user_id TEXT DEFAULT '', comment_time TEXT DEFAULT '',
    account TEXT DEFAULT '', status TEXT DEFAULT 'collected', score INTEGER DEFAULT 0,
    intent_level TEXT DEFAULT 'C', platform TEXT DEFAULT 'douyin',
    created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')"""

VIDEO = "https://www.douyin.com/video/7301234567890123456"
CHECKER, TARGET_A, TARGET_B = "acc_checker", "acc_target_a", "acc_target_b"


def _make_db(tmp_path) -> str:
    db = tmp_path / "vis.db"
    con = sqlite3.connect(db)
    con.execute(LEADS_DDL)
    con.execute("CREATE TABLE IF NOT EXISTS accounts ("
                "id TEXT PRIMARY KEY, name TEXT DEFAULT '', nickname TEXT DEFAULT '', "
                "platform TEXT DEFAULT 'douyin', status TEXT DEFAULT 'active')")
    for aid, nick in ((CHECKER, "检测号"), (TARGET_A, "被检A"), (TARGET_B, "被检B")):
        con.execute("INSERT OR IGNORE INTO accounts(id,nickname) VALUES(?,?)", (aid, nick))
    con.commit()
    con.close()
    return str(db)


def _seed_sample(db: str, target: str, text: str, url: str = VIDEO) -> None:
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS live_action_results ("
                "id INTEGER PRIMARY KEY, task_id TEXT DEFAULT '', account_id TEXT DEFAULT '', "
                "action TEXT DEFAULT '', status TEXT DEFAULT '', url TEXT DEFAULT '', "
                "text TEXT DEFAULT '', keyword TEXT DEFAULT '', note TEXT DEFAULT '', "
                "actions TEXT DEFAULT '', created_ms INTEGER DEFAULT 0)")
    con.execute(
        "INSERT INTO live_action_results(account_id,action,status,url,text,created_ms) "
        "VALUES(?,?,?,?,?,?)",
        (target, "post-comment", "sent_ok", url, text, 1700000000000))
    con.commit()
    con.close()


def _sample_key(db: str, target: str) -> str:
    """取该账号第一条样本的真实候选 key（与前端候选器同一推导口径）。"""
    from app import comment_likes

    page = comment_likes.service(db).candidates({"accountId": target})
    assert page.get("success") and page.get("items"), "样本候选应为空态拒绝之外的正常返回"
    return page["items"][0]["id"]


def _make_ctx(tmp_path, db: str, checker: str = CHECKER):
    from app.runtime import runners

    return runners.Ctx(kind="entity", db_path=db, task_id="vis_t1", name="吞评检测",
                       account_id=checker, run_id="vis_r1")


def _cfg(**extra) -> dict:
    cfg = {"sourceTypes": ["entity_comment_visibility"],
           "selectedAccounts": [CHECKER, TARGET_A],
           "visibilityDetectionMethod": "window",
           "visibilitySampleSource": "local",
           "visibilityLocalSampleIds": {TARGET_A: []},
           "visibilityLocalSampleKinds": ["main"]}
    cfg.update(extra)
    return cfg


def _read_result(comments, ok=True, note=""):
    return {"ok": ok, "comments": comments, "note": note}


def _patch_browser(monkeypatch, result):
    from app.runtime import browser
    seen: list[dict] = []

    def fake_execute(cmd, **_kw):
        seen.append(cmd)
        return result

    monkeypatch.setattr(browser, "execute", fake_execute)
    return seen


# --------------------------------------------------------------------------
# 启动校验
# --------------------------------------------------------------------------
def test_config_problem_accepts_backend_api_selfcheck():
    """高速模式按 UI 口径是「自检」：1 个账号即可，本地样本来源。"""
    assert vis.config_problem({"visibilityDetectionMethod": "backend_api",
                               "visibilitySampleSource": "local",
                               "selectedAccounts": ["a"]}) is None
    assert "本地评论" in vis.config_problem({"visibilityDetectionMethod": "backend_api",
                                            "visibilitySampleSource": "recommend",
                                            "selectedAccounts": ["a"]})
    assert "1 个被检账号" in vis.config_problem({"visibilityDetectionMethod": "backend_api",
                                                "visibilitySampleSource": "local",
                                                "selectedAccounts": []})


def test_config_problem_recommend_requires_persona_binding():
    """推荐页样本：人设模式要求每个账号都绑定智能体；表情模式不要求。"""
    assert "2 个账号" in vis.config_problem({"visibilitySampleSource": "recommend",
                                            "visibilityCommentMode": "emoji",
                                            "selectedAccounts": ["a"]})
    assert "账号人设" in vis.config_problem({"visibilitySampleSource": "recommend",
                                            "visibilityCommentMode": "persona",
                                            "selectedAccounts": ["a", "b"]})
    assert vis.config_problem({"visibilitySampleSource": "recommend",
                               "visibilityCommentMode": "emoji",
                               "selectedAccounts": ["a", "b"]}) is None
    assert "评论内容" in vis.config_problem({"visibilitySampleSource": "recommend",
                                            "visibilityCommentMode": "custom",
                                            "selectedAccounts": ["a", "b"]})


def test_config_problem_requires_two_accounts():
    assert "2 个账号" in vis.config_problem({"selectedAccounts": ["a"]})
    assert vis.config_problem(_cfg()) is None


def test_run_entity_dispatches_visibility(monkeypatch, tmp_path):
    from app.runtime import runners

    called = {}

    def fake_run(ctx, rec, cfg):
        called["cfg"] = cfg

    monkeypatch.setattr(vis, "run_visibility", fake_run)
    runners.run_entity(str(tmp_path / "x.db"),
                       {"id": "t", "configSnapshot": _cfg(keywords=["不该被消费"])},
                       runners.Ctx(kind="entity", db_path=tmp_path / "x.db",
                                   task_id="t", name="", account_id="a", run_id="r"))
    assert called["cfg"]["sourceTypes"] == ["entity_comment_visibility"]


def test_registry_start_rejects_visibility_problem(tmp_path, monkeypatch):
    """推荐页样本已可执行；但「账号人设」模式要求每个互检账号都绑定智能体。"""
    from app.runtime import registry

    db = _make_db(tmp_path)
    from app import leadgen_pools

    rec = {"id": "vis_bad", "name": "推荐页互检", "entityKind": "entity",
           "configSnapshot": _cfg(visibilitySampleSource="recommend",
                                  visibilityCommentMode="persona")}
    leadgen_pools.save_task_record(db, rec)
    monkeypatch.setattr(registry.browser, "status", lambda _db: {"engine": "shell"})
    res = registry.start(db, "entity", "vis_bad")
    assert res["success"] is False
    assert "人设" in res["error"]


# --------------------------------------------------------------------------
# 互检执行
# --------------------------------------------------------------------------
def _run(tmp_path, monkeypatch, db, cfg, read_result):
    _patch_browser(monkeypatch, read_result)
    ctx = _make_ctx(tmp_path, db)
    from app.runtime import runners

    monkeypatch.setattr(runners, "log", lambda *a, **k: None)
    vis.run_visibility(ctx, {"id": ctx.task_id, "name": "吞评检测"}, cfg)
    return ctx


def test_run_visibility_marks_visible(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "装修避坑看这条")
    cfg = _cfg(visibilityLocalSampleIds={TARGET_A: [_sample_key(db, TARGET_A)]})
    comments = [{"nickname": "被检A", "text": "装修避坑看这条"}]
    ctx = _run(tmp_path, monkeypatch, db, cfg, _read_result(comments))

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM leads WHERE source='entity_comment_visibility'").fetchone()
    assert row["visibility_status"] == "visible"
    assert row["visibility_target_account"] == TARGET_A
    assert row["visibility_sample_content"] == "装修避坑看这条"
    assert row["account"] == CHECKER
    counts = ctx.stats["visibilitySampleCounts"]
    assert counts["normal"] == 1 and counts["requested"] == 1
    assert ctx.stats["visibility_visible"] == 1
    assert ctx.stats["visibilityAccounts"][0]["status"] == "visible"


def test_run_visibility_marks_not_visible(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "已被吞的评论")
    cfg = _cfg(visibilityLocalSampleIds={TARGET_A: [_sample_key(db, TARGET_A)]})
    comments = [{"nickname": "路人甲", "text": "别的评论"}]
    ctx = _run(tmp_path, monkeypatch, db, cfg, _read_result(comments))

    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status,visibility_reason FROM leads").fetchone()
    assert row[0] == "not_visible"
    assert "已读取当前页" in row[1]
    assert ctx.stats["visibility_suspected"] == 1


def test_run_visibility_inconclusive_when_read_fails(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "任意内容")
    cfg = _cfg(visibilityLocalSampleIds={TARGET_A: [_sample_key(db, TARGET_A)]})
    ctx = _run(tmp_path, monkeypatch, db, cfg,
               {"ok": False, "error": "页面超时", "comments": []})
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status,visibility_reason FROM leads").fetchone()
    assert row[0] == "inconclusive"
    assert "页面超时" in row[1]
    assert ctx.stats["visibility_inconclusive"] == 1


def test_run_visibility_empty_comment_area_is_inconclusive(tmp_path, monkeypatch):
    """评论区读到了但一条都没有：无法判定，不得误判成吞评。"""
    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "任意内容")
    cfg = _cfg(visibilityLocalSampleIds={TARGET_A: [_sample_key(db, TARGET_A)]})
    ctx = _run(tmp_path, monkeypatch, db, cfg, _read_result([]))
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status FROM leads").fetchone()
    assert row[0] == "inconclusive"


def test_run_visibility_no_sample_target(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    cfg = _cfg(visibilityLocalSampleIds={TARGET_A: []})
    ctx = _run(tmp_path, monkeypatch, db, cfg, _read_result([]))
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status,comment FROM leads").fetchone()
    assert row[0] == "no_sample"
    assert ctx.stats["visibilitySampleCounts"]["missing"] == 1
    assert ctx.stats["visibilityAccounts"][0]["status"] == "no_sample"


def test_run_visibility_respects_per_target_cap(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "样本一")
    _seed_sample(db, TARGET_A, "样本二")
    key = _sample_key(db, TARGET_A)
    cfg = _cfg(visibilityLocalSampleIds={TARGET_A: [key, key]},
               visibilitySamplesPerTarget=1)
    ctx = _run(tmp_path, monkeypatch, db, cfg,
               _read_result([{"nickname": "被检A", "text": "样本一"}]))
    assert ctx.stats["visibilitySampleCounts"]["requested"] == 1


def test_run_visibility_rejects_inside_runner(tmp_path, monkeypatch):
    """runner 内的防御性拦截：配置不合法时不开浏览器、不写库。"""
    db = _make_db(tmp_path)
    seen = _patch_browser(monkeypatch, _read_result([]))
    ctx = _make_ctx(tmp_path, db)
    monkeypatch.setattr("app.runtime.runners.log", lambda *a, **k: None)
    vis.run_visibility(ctx, {"id": "t"}, {"sourceTypes": ["entity_comment_visibility"],
                                          "visibilitySampleSource": "recommend",
                                          "selectedAccounts": [CHECKER, TARGET_A]})
    assert seen == []
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_run_visibility_same_sample_rerun_refreshes(tmp_path, monkeypatch):
    """重跑同一样本：不产生重复行，结论覆盖为最新。"""
    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "同一条评论")
    cfg = _cfg(visibilityLocalSampleIds={TARGET_A: [_sample_key(db, TARGET_A)]})
    _run(tmp_path, monkeypatch, db, cfg,
         _read_result([{"nickname": "被检A", "text": "同一条评论"}]))
    _run(tmp_path, monkeypatch, db, cfg,
         _read_result([{"nickname": "别人", "text": "评论没了"}]))
    con = sqlite3.connect(db)
    rows = con.execute("SELECT visibility_status FROM leads").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "not_visible"


# --------------------------------------------------------------------------
# 明细过滤 / 导出 / 删除
# --------------------------------------------------------------------------
def test_visibility_filters_logic():
    from app.api import ipc_bridge as ib

    where, params = ib._visibility_filters({"visibilityStatus": "failed"})
    assert "visible" in where and params == []
    where, params = ib._visibility_filters({"visibilityStatus": "not_visible"})
    assert params == ["not_visible"]
    where, params = ib._visibility_filters({"keyword": "装修"})
    assert params == ["%装修%", "%装修%", "%装修%"]
    where, params = ib._visibility_filters({"visibilityStatus": "all", "keyword": ""})
    assert where == "" and params == []


def test_lead_public_exposes_visibility_fields(tmp_path):
    db = _make_db(tmp_path)
    store.insert_visibility_lead(
        db, task_id="t1", target_account=TARGET_A, target_name="被检A",
        sample_kind="main", sample_content="样本正文", video_url=VIDEO,
        status="visible", reason="在评论区找到该评论", account=CHECKER)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM leads").fetchone())
    from app.api import ipc_bridge as ib

    item = ib._lead_public(row)
    assert item["visibilityStatus"] == "visible"
    assert item["visibilityTargetAccountId"] == TARGET_A
    assert item["visibilityTargetAccountName"] == "被检A"
    assert item["visibilitySampleContent"] == "样本正文"
    assert item["visibilitySampleResult"] == "本条样本正常"
    assert "visibilityCleanup" not in item


def test_export_visibility_details_writes_csv(tmp_path, monkeypatch):
    from app.api import ipc_bridge as ib
    from pathlib import Path

    db = _make_db(tmp_path)
    store.insert_visibility_lead(
        db, task_id="t1", target_account=TARGET_A, target_name="被检A",
        sample_kind="main", sample_content="样本正文", video_url=VIDEO,
        status="not_visible", reason="已读取当前页 60 条评论，未见该样本", account=CHECKER)
    monkeypatch.setattr(ib, "DB_PATH", Path(db))
    monkeypatch.setattr(ib, "EXPORT_DIR", tmp_path / "exports")
    res = ib._export_visibility_details({"taskId": "t1", "visibilityStatus": "all"})
    assert res["success"] is True and res["count"] == 1
    blob = (tmp_path / "exports" / "visibility").glob("visibility_export_*.csv")
    content = next(blob).read_text(encoding="utf-8-sig")
    assert "疑似吞评" in content and "样本正文" in content
    # 状态筛选同样生效
    res2 = ib._export_visibility_details({"taskId": "t1", "visibilityStatus": "visible"})
    assert res2["success"] is False


def test_delete_visibility_related(tmp_path, monkeypatch):
    from app.api import ipc_bridge as ib
    from pathlib import Path

    db = _make_db(tmp_path)
    lid = store.insert_visibility_lead(
        db, task_id="t1", target_account=TARGET_A, target_name="被检A",
        sample_kind="main", sample_content="样本正文", video_url=VIDEO,
        status="visible", reason="ok", account=CHECKER)
    _seed_sample(db, TARGET_A, "样本正文")  # post-comment 回执（会被删）
    _seed_sample(db, CHECKER, "检测号自己的主评")  # 其他账号的主评（保留）
    con = sqlite3.connect(db)
    con.execute("INSERT INTO live_action_results(account_id,action,status,url,created_ms) "
                "VALUES(?,?,?,?,?)", (TARGET_A, "search-video", "selected", VIDEO, 1))
    con.commit()
    con.close()

    monkeypatch.setattr(ib, "DB_PATH", Path(db))
    res = ib._delete_visibility_related({"taskId": "t1", "leadIds": [lid]})
    assert res["success"] is True
    assert res["marked"] == 1
    assert res["videosRemoved"] == 1
    assert res["commentsRemoved"] == 1
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_cleanup FROM leads WHERE id=?", (lid,)).fetchone()
    assert row[0] == "deleted"
    # 其他账号的主评保留
    left = con.execute("SELECT COUNT(*) FROM live_action_results WHERE account_id=?",
                       (CHECKER,)).fetchone()[0]
    assert left == 1


def test_delete_entity_lead_records_clear_all(tmp_path, monkeypatch):
    from app.api import ipc_bridge as ib
    from pathlib import Path

    db = _make_db(tmp_path)
    store.insert_visibility_lead(
        db, task_id="t1", target_account=TARGET_A, target_name="被检A",
        sample_kind="main", sample_content="样本正文", video_url=VIDEO,
        status="visible", reason="ok", account=CHECKER)
    monkeypatch.setattr(ib, "DB_PATH", Path(db))
    res = ib._delete_entity_lead_records({"taskId": "t1", "clearAll": True})
    assert res["success"] is True and res["deleted"] == 1
    stats = res["stats"]
    assert stats["visibility_visible"] == 0
    assert stats["visibilitySampleCounts"]["requested"] == 0
    assert stats["visibilityAccounts"] == []
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM leads WHERE source_task_id='t1'").fetchone()[0] == 0


def test_visibility_ignores_same_text_in_replies(tmp_path, monkeypatch):
    """样本是「我发的主评」。楼中楼里出现同样一句话，不能算我的评论还在。"""
    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "装修避坑看这条")
    cfg = _cfg(visibilityLocalSampleIds={TARGET_A: [_sample_key(db, TARGET_A)]})
    comments = [{"nickname": "被检A", "text": "装修避坑看这条", "depth": 1, "parentIdx": 0},
                {"nickname": "路人甲", "text": "别的评论", "depth": 0}]
    ctx = _run(tmp_path, monkeypatch, db, cfg, _read_result(comments))

    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status FROM leads "
                      "WHERE source='entity_comment_visibility'").fetchone()
    assert row[0] == "not_visible"
    assert ctx.stats["visibility_suspected"] == 1


# --------------------------------------------------------------------------
# 回复样本（reply-comment 回执 → kind=reply）
# --------------------------------------------------------------------------
def _seed_reply_sample(db: str, target: str, text: str, url: str = VIDEO) -> str:
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS live_action_results ("
                "id INTEGER PRIMARY KEY, task_id TEXT DEFAULT '', account_id TEXT DEFAULT '', "
                "action TEXT DEFAULT '', status TEXT DEFAULT '', url TEXT DEFAULT '', "
                "text TEXT DEFAULT '', keyword TEXT DEFAULT '', note TEXT DEFAULT '', "
                "actions TEXT DEFAULT '', created_ms INTEGER DEFAULT 0)")
    con.execute(
        "INSERT INTO live_action_results(account_id,action,status,url,text,created_ms) "
        "VALUES(?,?,?,?,?,?)",
        (target, "reply-comment", "sent_ok", url, text, 1700000000000))
    con.commit()
    con.close()
    from app import comment_likes

    page = comment_likes.service(db).candidates({"accountId": target, "kinds": ["reply"]})
    assert page.get("success") and page.get("items"), "回复样本候选应正常返回"
    return page["items"][0]["id"]


def _patch_browser_calls(monkeypatch, result):
    from app.runtime import browser

    calls: list[tuple[dict, dict]] = []

    def fake_execute(cmd, **kw):
        calls.append((cmd, kw))
        return result

    monkeypatch.setattr(browser, "execute", fake_execute)
    return calls


def _run_selfcheck(tmp_path, monkeypatch, db, cfg, read_result):
    calls = _patch_browser_calls(monkeypatch, read_result)
    ctx = _make_ctx(tmp_path, db)
    monkeypatch.setattr("app.runtime.runners.log", lambda *a, **k: None)
    vis.run_visibility(ctx, {"id": ctx.task_id, "name": "吞评检测"}, cfg)
    return ctx, calls


def _selfcheck_cfg(db, target, *, kinds=None):
    return _cfg(visibilityDetectionMethod="backend_api",
                selectedAccounts=[target],
                visibilityLocalSampleIds={target: [_sample_key(db, target)]},
                **({"visibilityLocalSampleKinds": kinds} if kinds else {}))


# --------------------------------------------------------------------------
# 高速自检（backend_api）
# --------------------------------------------------------------------------
def test_selfcheck_visible_uses_own_session(tmp_path, monkeypatch):
    """高速自检：被检账号用自己的会话读，不需要互检账号；纯主评样本不展开楼中楼。"""
    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "自检样本")
    cfg = _selfcheck_cfg(db, TARGET_A)
    ctx, calls = _run_selfcheck(tmp_path, monkeypatch, db, cfg,
                                _read_result([{"nickname": "被检A", "text": "自检样本"}]))
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd["type"] == "read-comments"
    assert cmd["limit"] == vis.SELF_CHECK_LIMIT
    assert kw["account_id"] == TARGET_A
    assert cmd.get("expandSubComments") is None
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status,account FROM leads "
                      "WHERE source='entity_comment_visibility'").fetchone()
    assert row[0] == "visible"
    assert row[1] == TARGET_A
    assert ctx.stats["visibility_visible"] == 1
    assert ctx.stats["visibilityAccounts"][0]["status"] == "visible"


def test_selfcheck_busy_target_is_inconclusive(tmp_path, monkeypatch):
    """被检账号正被其他任务占用：不开其会话、不猜结论，记 inconclusive。"""
    from app.runtime import registry

    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "自检样本")
    cfg = _selfcheck_cfg(db, TARGET_A)
    monkeypatch.setattr(registry, "active_accounts", lambda: [TARGET_A])
    ctx, calls = _run_selfcheck(tmp_path, monkeypatch, db, cfg,
                                _read_result([{"nickname": "被检A", "text": "自检样本"}]))
    assert calls == []
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status,visibility_reason FROM leads").fetchone()
    assert row[0] == "inconclusive"
    assert "正在执行其他任务" in row[1]
    assert ctx.stats["visibility_inconclusive"] == 1


def test_selfcheck_reply_sample_matches_replies(tmp_path, monkeypatch):
    """回复样本只和楼中楼回复比；样本含回复时读取命令带 expandSubComments。"""
    db = _make_db(tmp_path)
    key = _seed_reply_sample(db, TARGET_A, "楼中楼回复样本")
    cfg = _cfg(visibilityDetectionMethod="backend_api",
               selectedAccounts=[TARGET_A],
               visibilityLocalSampleIds={TARGET_A: [key]},
               visibilityLocalSampleKinds=["reply"])
    comments = [{"nickname": "路人甲", "text": "别的评论", "depth": 0},
                {"nickname": "被检A", "text": "楼中楼回复样本", "depth": 1, "parentIdx": 0}]
    ctx, calls = _run_selfcheck(tmp_path, monkeypatch, db, cfg, _read_result(comments))
    cmd, kw = calls[0]
    assert cmd.get("expandSubComments") is True
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status,visibility_sample_kind FROM leads "
                      "WHERE source='entity_comment_visibility'").fetchone()
    assert row[0] == "visible"
    assert row[1] == "reply"
    assert ctx.stats["visibility_visible"] == 1


def test_selfcheck_main_sample_ignores_reply_rows(tmp_path, monkeypatch):
    """主评样本在自检里同样不被楼中楼里的同句话骗过。"""
    db = _make_db(tmp_path)
    _seed_sample(db, TARGET_A, "同一句话")
    cfg = _selfcheck_cfg(db, TARGET_A)
    comments = [{"nickname": "被检A", "text": "同一句话", "depth": 1, "parentIdx": 0}]
    ctx, _ = _run_selfcheck(tmp_path, monkeypatch, db, cfg, _read_result(comments))
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status FROM leads").fetchone()
    assert row[0] == "not_visible"
    assert ctx.stats["visibility_suspected"] == 1


def test_window_reply_sample_uses_checker_session_with_expand(tmp_path, monkeypatch):
    """常规互检的回复样本：检测账号会话读取、展开楼中楼后按回复节点核对。"""
    db = _make_db(tmp_path)
    key = _seed_reply_sample(db, TARGET_A, "互检回复样本")
    cfg = _cfg(visibilityLocalSampleIds={TARGET_A: [key]},
               visibilityLocalSampleKinds=["reply"])
    comments = [{"nickname": "被检A", "text": "互检回复样本", "depth": 1, "parentIdx": 0}]
    ctx, calls = _run_selfcheck(tmp_path, monkeypatch, db, cfg, _read_result(comments))
    cmd, kw = calls[0]
    assert cmd.get("expandSubComments") is True
    assert cmd["limit"] == vis.READ_LIMIT
    assert kw["account_id"] == CHECKER
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status,account FROM leads").fetchone()
    assert row[0] == "visible"
    assert row[1] == CHECKER


def test_selfcheck_no_sample_target(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    cfg = _cfg(visibilityDetectionMethod="backend_api",
               selectedAccounts=[TARGET_A],
               visibilityLocalSampleIds={TARGET_A: []})
    ctx, calls = _run_selfcheck(tmp_path, monkeypatch, db, cfg, _read_result([]))
    assert calls == []
    con = sqlite3.connect(db)
    row = con.execute("SELECT visibility_status FROM leads").fetchone()
    assert row[0] == "no_sample"
    assert ctx.stats["visibilitySampleCounts"]["missing"] == 1
    assert ctx.stats["visibilityAccounts"][0]["status"] == "no_sample"
