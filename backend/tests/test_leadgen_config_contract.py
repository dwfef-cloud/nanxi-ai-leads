"""Isolated settings -> plan -> signed worker contract. No live accounts/network."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from app.execution import leadgen_config as cfg, live_workflow as flow, live_leadgen, live_scope
from app.api import task_runner, ipc_bridge
from app.runtime import browser, registry, store
from tools.prepare_live_workflow_scope import prepare

def stored_actions(db, proposal_id):
    """提案库里的完整动作（含 commentId）；页面出口会裁掉平台标识字段。"""
    with sqlite3.connect(str(db)) as con:
        row = con.execute("SELECT body FROM live_workflow_proposals WHERE id=?", (proposal_id,)).fetchone()
    return json.loads(row[0])["actions"]

VIDEO = "https://www.douyin.com/video/7400000000000000001"
OTHER = "https://www.douyin.com/video/7400000000000000002"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    db = tmp_path / "isolated.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY,nickname TEXT,platform TEXT)")
        con.executemany("INSERT INTO accounts VALUES (?,?,?)", [("a", "测试甲", "douyin"), ("b", "测试乙", "douyin")])
    calls = []
    comments = [{"commentId": str(123456700 + i), "nickname": "用户" + str(i), "text": "想请教工作流怎么搭建？",
                 "userId": "MS4wLjAB_user" + str(i), "publishedText": "1小时前 · 广东"} for i in range(5)]
    def read(command, **kwargs):
        calls.append(command)
        if command["type"] == "search-videos":
            return {"ok": True, "items": [{"url": VIDEO, "title": "工作流教程"}, {"url": OTHER, "title": "智能体开发"}]}
        if command["type"] == "inspect-video":
            return {"ok": True, "item": {"title": "指定视频教程"}}
        if command["type"] == "read-comments":
            return {"ok": True, "comments": comments}
        pytest.fail("planning dispatched a write: " + command["type"])
    monkeypatch.setattr(browser, "status", lambda _: {"engine": "shell"})
    monkeypatch.setattr(browser, "execute", read)
    monkeypatch.setattr(task_runner.event_bus, "publish", lambda *a: None)
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    for name in ("LEADS_LEADGEN_SCOPE", "LEADS_LEADGEN_SCOPE_DIR", "LEADS_COMMENT_LIKE_SCOPE", "LEADS_LIVE_ACCEPTANCE_SCOPE"):
        monkeypatch.delenv(name, raising=False)
    payload = {"taskId": "run", "leadgenTaskId": "task", "accountId": "a", "keywords": "智能体开发",
               "enableVideoComment": True, "videoCommentMode": "custom", "videoCommentContent": "想请教节点的输入输出如何设计？",
               "videoMin": 2, "videoMax": 2, "interactionLimitMin": 10, "interactionLimitMax": 10,
               "commentReplyMode": "custom", "commentContent": "可以先明确输入输出，再梳理流程。",
               "dmContent": "{nickname}，你好，可以交流下工作流搭建。", "commentIntentMatchMode": "keyword",
               "intentionKeywords": "怎么", "stayMin": 0, "stayMax": 0, "intervalMin": 0, "intervalMax": 0}
    yield db, payload, calls, comments
    task_runner.reset_for_tests()
    registry.reset_for_tests()


def test_all_keywords_and_separators_preserve_phrases():
    assert cfg.words('智能体、工作流;定制；开发 咨询\n自动化,智能体，"AI agent"') == [
        "智能体", "工作流", "定制", "开发", "咨询", "自动化", "AI agent"]
    assert cfg.words(json.dumps('带"引号"的 phrase', ensure_ascii=False) + '；工作流') == ['带"引号"的 phrase', '工作流']


def test_video_stay_defaults_match_real_user_pacing():
    """默认停留 8-15 秒（2026-11-20 全网口径：单视频均值 8.2 秒、
    50.35% 用户完整观看 1-10 秒）。设置页填了值仍以设置页为准。"""
    config = cfg.normalize({"keywords": "智能体开发", "enableVideoComment": True,
                            "videoCommentMode": "custom", "videoCommentContent": "有效的获客评论正文"})
    assert (config["stayMin"], config["stayMax"]) == (8, 15)
    config = cfg.normalize({"keywords": "智能体开发", "enableVideoComment": True,
                            "videoCommentMode": "custom", "videoCommentContent": "有效的获客评论正文",
                            "stayMin": 40, "stayMax": 60})
    assert (config["stayMin"], config["stayMax"]) == (40, 60)


def test_ad_skip_defaults_on_and_accepts_explicit_toggle():
    base = {"keywords": "智能体开发", "enableVideoComment": True,
            "videoCommentMode": "custom", "videoCommentContent": "有效的获客评论正文"}
    assert cfg.normalize(base)["skipAdVideos"] is True
    assert cfg.normalize({**base, "skipAdVideos": False})["skipAdVideos"] is False
    with pytest.raises(ValueError, match="开关设置格式无效"):
        cfg.normalize({**base, "skipAdVideos": "false"})


def test_infer_publish_time_from_comment_ages():
    """发布时间推断：取最早一条评论（最大 ageMinutes）折算回相对时间。"""
    from app.execution import leadgen_stream
    assert leadgen_stream.infer_publish_time([
        {"ageMinutes": 30}, {"ageMinutes": 4320}, {"ageMinutes": 120}]) == "3天前"
    assert leadgen_stream.infer_publish_time([{"ageMinutes": 0.5}, {"ageMinutes": 0}]) == "刚刚"
    assert leadgen_stream.infer_publish_time([{"ageMinutes": 90}]) == "1小时前"
    assert leadgen_stream.infer_publish_time([{"ageMinutes": 10080 * 3}]) == "3周前"
    # 没有可解析的时间 → 空串（不猜）
    assert leadgen_stream.infer_publish_time([{"ageMinutes": None}, {"text": "x"}]) == ""
    assert leadgen_stream.infer_publish_time([]) == ""
    assert leadgen_stream.infer_publish_time(None) == ""


@pytest.mark.parametrize("change", [
    {"interactionLimit": 0}, {"interactionLimitMin": 3, "interactionLimitMax": 2},
    {"videoMin": 0}, {"intervalMax": float("nan")}, {"likeMin": -1}, {"mainVideoLikePercent": 101},
    {"videoCommentContent": "短"}, {"videoCommentContent": "有效模板正文\n短"},
    {"commentTimeFilter": "custom", "commentTimeCustomValue": 366, "commentTimeCustomUnit": "day"},
    {"locationFilterRegions": "广东"}, {"searchSort": "1"}, {"enableWarmup": True},
    {"videoSources": ["live"]}, {"videoSources": ["search", "specific"]},
])
def test_invalid_settings_fail_before_any_browser_work(isolated, change):
    db, payload, calls, _ = isolated
    with pytest.raises(ValueError):
        flow.preview(db, {**payload, **change})
    assert calls == []
    assert not registry._ACCOUNT_LEASES


@pytest.mark.parametrize("time_value,seconds", [("1y", 31536000), ("1mo", 2592000), ("1w", 604800),
    ("3d", 259200), ("3600", 3600), (3600, 3600), ("all", None)])
def test_page_time_values_roundtrip(time_value, seconds):
    assert cfg.time_window({"commentTimeFilter": time_value}, "commentTime") == seconds


def test_video_and_comment_filters_explain_unknown_and_excluded_values(isolated):
    _, payload, _, comments = isolated
    config = cfg.normalize({**payload, "minVideoLike": 10000, "excludeTitleKeywords": "广告",
                            "excludeAuthorAccounts": "*医生*", "commentTimeFilter": "1d",
                            "locationFilterRegions": ["广东"], "excludeCommentKeywords": "抽奖"})
    assert cfg.video_reason(config, {"title": "广告教程"}) == "命中排除标题词"
    assert cfg.video_reason(config, {"title": "教程", "author": "李医生"}) == "命中排除作者"
    assert "未提供指标" in cfg.video_reason(config, {"title": "教程"})
    assert cfg.video_reason(config, {"title": "教程", "likeText": "1.2万"}) == ""
    assert cfg.comment_reason(config, comments[0], {"本人"}) == ""
    assert cfg.comment_reason(config, {**comments[0], "text": "抽奖"}, set()).startswith("排除评论词")
    assert cfg.comment_reason(config, {**comments[0], "publishedText": ""}, set()) == "评论时间未知"
    assert cfg.comment_reason(config, {**comments[0], "publishedText": "2天前 · 广东"}, set()) == "评论时间超出范围"
    assert "本人" in cfg.comment_reason(config, comments[0], {comments[0]["nickname"]})


def test_total_cap_one_includes_every_enabled_action(isolated):
    db, payload, calls, _ = isolated
    result = flow.preview(db, {**payload, "interactionLimit": 1, "enableComment": True,
                              "enableLike": True, "enableDM": True})
    assert len(result["actions"]) == 1
    assert result["actions"][0]["type"] == "post-comment"
    assert [c["type"] for c in calls] == ["search-videos", "read-comments"]


def test_ai_rejected_main_comment_keeps_like_video(isolated, monkeypatch):
    """AI 判定视频不适合评论时，只砍依赖主评场景的动作；点赞是独立开关，必须照做。

    2026-11-20 真机：AI 对每条推荐页视频都判「话题与业务无关」，旧逻辑把该视频
    已抽中的 like-video 也一并清空 → 用户设定的主视频点赞 20% 一条没点，
    看起来就是「动作没按设定执行」。"""
    from app.execution.leadgen_plan import build

    db, payload, calls, comments = isolated
    seen = []
    payload.update(enableMainVideoLike=True, mainVideoLikePercent=100,
                   enableVideoComment=True, videoCommentMode="ai", aiReplyMode=True,
                   enableLike=True, likeMin=1, likeMax=1,
                   enableComment=True, commentReplyMode="ai", commentContent="可以先明确输入输出",
                   commentMin=1, commentMax=1)
    monkeypatch.setattr("app.execution.live_leadgen.generate",
                        lambda *a, **k: {"ok": False, "code": "not_suitable", "reason": "话题与业务无关"})
    account = {"id": "a", "nickname": "测试甲"}

    class Check:
        def __call__(self):
            return None

        def stopped(self):
            return False

    result = build(db, payload, account, check=Check(), progress=seen.append,
                   comment_cache={VIDEO: {"ok": True, "comments": comments}})
    types = [a["type"] for a in result["actions"]]
    assert "like-video" in types, types
    assert "post-comment" not in types, types
    assert any("AI 判断" in t for t in seen), seen
    skipped = {s["type"] for s in result["skippedActions"]}
    assert "reply-comment" in skipped, skipped


def test_stream_comment_read_can_be_reused_by_planner(isolated):
    from app.execution.leadgen_plan import build

    db, payload, calls, comments = isolated
    payload.update(videoMin=1, videoMax=1)
    account = {"id": "a", "nickname": "测试甲"}
    class Check:
        def __call__(self):
            return None

        def stopped(self):
            return False

    result = build(db, payload, account, check=Check(), progress=lambda _: None,
                   comment_cache={VIDEO: {"ok": True, "comments": comments}})
    assert result["actions"]
    assert [call["type"] for call in calls] == ["search-videos"]


def test_multiple_videos_and_per_video_limits_make_exact_plan(isolated, tmp_path, monkeypatch):
    db, payload, _, _ = isolated
    payload.update(enableComment=True, commentMin=2, commentMax=2, enableLike=True, likeMin=1, likeMax=1)
    result = flow.preview(db, payload)
    assert len(result["actions"]) == 8
    stored = stored_actions(db, result["proposalId"])
    for video in (VIDEO, OTHER):
        selected = [a for a in stored if a["videoUrl"] == video]
        assert [a["type"] for a in selected] == ["post-comment", "like-comment", "reply-comment", "reply-comment"]
        assert len({a.get("commentId") for a in selected[1:]}) == 3
    prepared = prepare(db, result["proposalId"], result["binding"], tmp_path / "approved")
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE", prepared["scopeFile"])
    scope = live_scope.load_scope()
    assert scope["actions"] == {"post-comment": 2, "reply-comment": 4, "like-comment": 2}
    for action in stored:
        command = live_scope.signed_command(action["type"], account_id="a", task_id="task", video_url=action["videoUrl"],
            text=action["text"], comment_id=action.get("commentId", ""), comment_author=action.get("commentAuthor", ""),
            recipient_id=action.get("recipientId", ""))
        assert live_scope.command_allowed(command)
        assert not live_scope.command_allowed({**command, "text": "篡改正文"})


def test_specified_video_is_read_before_planning(isolated):
    db, payload, calls, _ = isolated
    result = flow.preview(db, {**payload, "videoSources": ["specific"], "keywords": "", "specifiedUrls": VIDEO})
    assert len(result["actions"]) == 1
    assert result["actions"][0]["keyword"] == "指定视频"
    assert [c["type"] for c in calls] == ["inspect-video", "read-comments"]


def test_all_mode_ignores_intent_keywords_but_honors_exclusions(isolated):
    db, payload, _, comments = isolated
    comments[0]["text"] = "普通讨论内容"
    result = flow.preview(db, {**payload, "enableVideoComment": False, "enableComment": True,
        "commentIntentMatchMode": "all", "intentionKeywords": "绝不命中", "excludeCommentKeywords": "普通"})
    assert all(a["commentId"] != comments[0]["commentId"] for a in stored_actions(db, result["proposalId"]))
    assert result["actions"]


def test_ai_intent_is_separate_from_generating_reply(isolated, monkeypatch):
    db, payload, _, comments = isolated
    seen = []
    def decide(db, config, candidate, **kwargs):
        seen.append(candidate["commentId"])
        return {"matched": candidate["commentId"] == comments[1]["commentId"], "reason": "独立意向判断"}
    monkeypatch.setattr(live_leadgen, "classify_intent", decide)
    result = flow.preview(db, {**payload, "enableVideoComment": False, "enableComment": True,
                              "aiReplyMode": True, "commentIntentMatchMode": "ai"})
    assert len(seen) == 10
    assert all(a["commentId"] == comments[1]["commentId"] for a in stored_actions(db, result["proposalId"]))


def test_templates_select_one_line_and_expand_nickname(isolated, monkeypatch):
    db, payload, _, _ = isolated
    monkeypatch.setattr(cfg.random, "choice", lambda choices: choices[-1])
    result = flow.preview(db, {**payload, "enableVideoComment": False, "enableDM": True, "dmLimit": 1,
                              "dmContent": "第一条有效模板正文\n{nickname}，你好，可以交流工作流。"})
    assert len(result["actions"]) == 1
    assert result["actions"][0]["text"] == "用户0，你好，可以交流工作流。"


def test_pause_and_stop_interrupt_delay_and_never_send(isolated, tmp_path, monkeypatch):
    db, payload, _, _ = isolated
    result = flow.preview(db, payload)
    prepared = prepare(db, result["proposalId"], result["binding"], tmp_path / "scope")
    scope = json.loads((tmp_path / "scope").read_text(encoding="utf8"))
    ctx = task_runner.RunCtx(task_id="run", leadgen_id="task", account_id="a", payload=payload, db_path=db)
    ctx.run_id = store.run_begin(db, "leadgen", "run", "离线暂停测试", "a")
    calls = []
    monkeypatch.setattr(live_leadgen, "perform", lambda *a, **k: calls.append(k) or {"ok": True, "status": "sent_ok"})
    ctx.pause.set()
    thread = threading.Thread(target=flow.run_reviewed, args=(ctx, scope))
    thread.start()
    time.sleep(0.12)
    assert calls == []
    ctx.stop.set()
    thread.join(timeout=1)
    assert not thread.is_alive() and calls == []
    with sqlite3.connect(db) as con:
        state = json.loads(con.execute("SELECT body FROM leadgen_checkpoints").fetchone()[0])
    assert state == {"status": "stopped", "completed": 0, "total": 2, "remaining": 2}


def test_unconfirmed_skip_is_not_success_and_checkpoint_survives(isolated, tmp_path, monkeypatch):
    db, payload, _, _ = isolated
    result = flow.preview(db, payload)
    prepare(db, result["proposalId"], result["binding"], tmp_path / "scope")
    scope = json.loads((tmp_path / "scope").read_text(encoding="utf8"))
    ctx = task_runner.RunCtx(task_id="run", leadgen_id="task", account_id="a", payload=payload, db_path=db)
    ctx.run_id = store.run_begin(db, "leadgen", "run", "离线统计测试", "a")
    monkeypatch.setattr(live_leadgen, "perform", lambda *a, **k: {"ok": False, "status": "skipped", "code": "already_done"})
    flow.run_reviewed(ctx, scope)
    record = __import__('app.leadgen_pools', fromlist=['']).load_records(db)["task"]
    assert record["stats"]["published"] == 0
    assert record["stats"]["skipped"] == 2
    with sqlite3.connect(db) as con:
        assert json.loads(con.execute("SELECT body FROM leadgen_checkpoints").fetchone()[0])["remaining"] == 0


def test_per_account_scope_files_do_not_replace_another_account(isolated, tmp_path, monkeypatch):
    import hashlib
    db, payload, _, _ = isolated
    directory = tmp_path / "accounts"
    directory.mkdir()
    for account in ("a", "b"):
        result = flow.preview(db, {**payload, "accountId": account})
        prepare(db, result["proposalId"], result["binding"], directory / (hashlib.sha256(account.encode()).hexdigest() + ".json"))
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE_DIR", str(directory))
    for account in ("a", "b"):
        assert live_scope.task_allowed({**payload, "accountId": account})
        assert live_scope.load_scope(account_id=account)["accountId"] == account


def test_confirmed_payload_cannot_change_limits_or_account(isolated, tmp_path, monkeypatch):
    db, payload, _, _ = isolated
    selected = flow.preview(db, payload)
    approved = prepare(db, selected["proposalId"], selected["binding"], tmp_path / "scope")
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE", approved["scopeFile"])
    assert live_scope.task_allowed(payload)
    assert not live_scope.task_allowed({**payload, "interactionLimit": 100})
    assert not live_scope.task_allowed({**payload, "accountId": "b"})
    assert "配置不一致" in live_scope.task_rejection_reason({**payload, "interactionLimit": 100})


def test_unbound_persona_uses_explicit_template_without_model_call(isolated, monkeypatch):
    db, payload, _, _ = isolated
    monkeypatch.setattr(live_leadgen, "generate", lambda *a: pytest.fail("no bound persona"))
    selected = flow.preview(db, {**payload, "videoCommentMode": "ai", "aiReplyMode": False})
    assert all(a["text"] == payload["videoCommentContent"] for a in selected["actions"])
    assert any("未绑定智能体" in w for w in selected["warnings"])
    with pytest.raises(ValueError, match="模板"):
        flow.preview(db, {**payload, "videoCommentMode": "ai", "aiReplyMode": False, "videoCommentContent": ""})


def test_persona_filter_without_persona_is_not_silently_ignored(isolated):
    db, payload, calls, _ = isolated
    with pytest.raises(ValueError, match="人设视频筛选"):
        flow.preview(db, {**payload, "enablePersonaVideoFilter": True, "aiReplyMode": False})
    assert calls == []


def test_private_recipient_is_unique_across_videos_and_keywords(isolated):
    db, payload, _, comments = isolated
    selected = flow.preview(db, {**payload, "enableVideoComment": False, "enableDM": True,
        "keywords": "工作流、智能体", "dmLimit": 10, "dmLimitMin": 10, "dmLimitMax": 10})
    assert len(selected["actions"]) == len(comments)
    assert len({a["recipientId"] for a in selected["actions"]}) == len(comments)


def test_plan_over_24_hours_fails_before_confirmation(isolated):
    db, payload, _, _ = isolated
    with pytest.raises(ValueError, match="24小时"):
        flow.preview(db, {**payload, "intervalMin": 86400, "intervalMax": 86400})


def test_uncertain_action_counts_attempt_but_not_success(isolated, tmp_path, monkeypatch):
    db, payload, _, _ = isolated
    selected = flow.preview(db, payload)
    prepare(db, selected["proposalId"], selected["binding"], tmp_path / "scope")
    scope = json.loads((tmp_path / "scope").read_text(encoding="utf8"))
    ctx = task_runner.RunCtx(task_id="run", leadgen_id="task", account_id="a", payload=payload, db_path=db)
    ctx.run_id = store.run_begin(db, "leadgen", "run", "离线结果待核验", "a")
    monkeypatch.setattr(live_leadgen, "perform", lambda *a, **k: {"ok": False, "status": "uncertain"})
    flow.run_reviewed(ctx, scope)
    assert ctx.stats["attempted"] == 1 and ctx.stats["remaining"] == 2
    assert ctx.stats["uncertain"] == 1 and ctx.stats["published"] == 0


def test_readonly_watch_can_be_paused_twice_without_new_random_duration(isolated, monkeypatch):
    db, payload, _, _ = isolated
    ctx = task_runner.RunCtx(task_id="run", leadgen_id="task", account_id="a", payload=payload, db_path=db)
    durations = []
    # _watch 每轮读两次 monotonic（开始 / 结束后折算剩余秒），三次调用共需 6 个值；
    # 只给 5 个会在第 3 轮折算剩余时长时 StopIteration，与「能否暂停两次」无关。
    clock = iter([0, 2, 2, 3, 3, 3])
    monkeypatch.setattr(flow.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(flow, "_boundary", lambda c: c.pause.clear() or not c.stop.is_set())
    def watch(command, **kwargs):
        durations.append(command["seconds"])
        if len(durations) < 3:
            ctx.pause.set()
            assert kwargs["stop_check"]()
            return {"ok": False, "stopped": True}
        return {"ok": True}
    monkeypatch.setattr(browser, "execute", watch)
    assert flow._watch(ctx, {"videoUrl": VIDEO}, 10)["ok"]
    assert durations == [10, 8, 7]


def test_watch_timeout_tracks_long_stay_and_stays_cancellable(monkeypatch):
    observed = []
    monkeypatch.setattr(browser, "_via_shell", lambda cmd, **kwargs: observed.append(kwargs) or {"ok": True})
    stop = lambda: False
    assert browser.execute({"type": "watch-video", "seconds": 3600}, db_path="unused", engine="shell", stop_check=stop)["ok"]
    assert observed[0]["timeout"] == 3690 and observed[0]["stop_check"] is stop
    assert not browser.execute({"type": "watch-video", "seconds": float("nan")}, db_path="unused", engine="shell")["ok"]


def test_filtered_leads_are_saved_without_overwriting_manual_followup(isolated):
    from app import collector
    db, payload, _, comments = isolated
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE leads (id TEXT PRIMARY KEY,nickname TEXT,comment TEXT,source_keyword TEXT,source TEXT,"
                    "source_task_id TEXT,source_url TEXT,video TEXT,video_id TEXT,comment_id TEXT,comment_user_id TEXT,"
                    "account TEXT,note TEXT,region TEXT,status TEXT,created_at TEXT,updated_at TEXT,"
                    "intent_level TEXT,score INTEGER,tags TEXT)")
    comments[0]["text"] = "抽奖工作流怎么搭建"
    flow.preview(db, {**payload, "enableComment": True, "excludeCommentKeywords": "抽奖"})
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT comment_id,status,intent_level FROM leads").fetchall()
        assert len(rows) == len(comments)
        assert (comments[0]["commentId"], "collected", "normal") in rows
        assert all(r[1:] == ("pending_outreach", "high") for r in rows if r[0] != comments[0]["commentId"])
        con.execute("UPDATE leads SET status='contacted',note='人工跟进',score=99")
    collector.annotate_intent(db, {**comments[1], "intentMatched": False, "intentReason": "后续判断"})
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT status,note,score FROM leads WHERE id=?", (collector.lead_id_of(comments[1]),)).fetchone() == ("contacted", "人工跟进", 99)


def test_refresh_never_marks_an_active_reviewed_account_stopped(isolated, monkeypatch):
    from app import leadgen_pools, comment_likes
    from types import SimpleNamespace
    db, _, _, _ = isolated
    initial = {"name": "离线对账", "configSnapshot": {}, "startedAt": 1}
    leadgen_pools.reviewed_run_update(db, "task", "a", "a-run", initial=initial)
    monkeypatch.setattr(task_runner, "active_records", lambda: [{"taskId": "a-run", "stopping": True}])
    monkeypatch.setattr(comment_likes, "service", lambda _: SimpleNamespace(active=lambda: []))
    result = ipc_bridge._reconcile_leadgen_records(leadgen_pools.load_records(db))
    assert result["task"]["status"] == "running"
    monkeypatch.setattr(task_runner, "active_records", lambda: [])
    result = ipc_bridge._reconcile_leadgen_records(leadgen_pools.load_records(db))
    assert result["task"]["status"] == "stopped"
    assert result["task"]["reviewedAccounts"]["a"]["endReason"] == "interrupted"


def test_native_preparation_gets_grace_period_without_false_stopped(isolated):
    from app import leadgen_pools
    db, _, _, _ = isolated
    initial = {"name": "离线启动衔接", "configSnapshot": {}, "startedAt": 1000}
    leadgen_pools.reviewed_run_update(db, "task", "a", "run", initial=initial)
    record = leadgen_pools.recover_reviewed_record(db, "task", set(), 1100)
    assert record["status"] == "running"
    record = leadgen_pools.recover_reviewed_record(db, "task", set(), 122000)
    assert record["status"] == "stopped"


def test_python_batch_signatures_match_actual_node_worker(isolated, tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    db, payload, _, _ = isolated
    selected = flow.preview(db, {**payload, "enableLike": True, "enableComment": True,
                               "enableMainVideoLike": True, "mainVideoLikePercent": 100})
    output = tmp_path / "scope.json"
    prepare(db, selected["proposalId"], selected["binding"], output)
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE", str(output))
    commands = [live_scope.signed_command(a["type"], account_id="a", task_id="task", video_url=a["videoUrl"],
        text=a["text"], comment_id=a.get("commentId", ""), comment_author=a.get("commentAuthor", ""),
        recipient_id=a.get("recipientId", "")) for a in stored_actions(db, selected["proposalId"])]
    script = ("const p=JSON.parse(require('fs').readFileSync(0,'utf8'));const live=require('./desktop/live-platform');"
              "process.stdout.write(JSON.stringify(p.commands.map(c=>{const s=live.scopeFor(c,p.file);"
              "return !!s && live.claimAttempt(s,c.type,c) && !live.claimAttempt(s,c.type,c);})));" )
    result = subprocess.run(["node", "-e", script], input=json.dumps({"file": str(output), "commands": commands}),
        text=True, capture_output=True, check=True, timeout=10, cwd=Path(__file__).resolve().parents[2])
    assert len(commands) == 8
    assert json.loads(result.stdout) == [True]*8


def test_main_and_reply_use_their_own_goals_prompts_and_styles(monkeypatch):
    prompts = []
    monkeypatch.setattr(live_leadgen.ai_personas, "_llm_settings", lambda _: {})
    monkeypatch.setattr(live_leadgen.ai_personas, "_call_chat", lambda _, prompt:
        prompts.append(prompt) or json.dumps({"should_reply": True, "reply_content": "可以先梳理输入输出和业务节点。"}))
    config = {"aiGoal": "回复目标", "aiStyle": "回复风格", "aiPrompt": "回复人设",
              "videoGoal": "主评目标", "videoStyle": "主评风格", "videoPrompt": "主评人设"}
    assert live_leadgen.generate("unused", config, {"title": "工作流"})["ok"]
    assert live_leadgen.generate("unused", config, {"title": "工作流", "comment": "怎么设计？"})["ok"]
    assert all(word in prompts[0] for word in ("主评目标", "主评风格", "主评人设"))
    assert all(word in prompts[1] for word in ("回复目标", "回复风格", "回复人设"))
    assert "回复人设" not in prompts[0] and "主评人设" not in prompts[1]


def test_form_binding_is_checked_per_account_at_start_instead_of_false_missing_template(isolated):
    _, payload, _, _ = isolated
    form = {k: v for k, v in payload.items() if k not in {"accountId", "taskId", "leadgenTaskId"}}
    form.update(aiReplyMode=False, videoCommentMode="ai", videoCommentContent="", selectedAccounts=["a"],
                accountPersonas={"a": "persona-one"}, enablePersonaVideoFilter=True)
    assert cfg.inspect(form)["success"]
    assert not cfg.inspect({**form, "accountId": "a"})["success"]


def test_allow_other_accounts_still_serializes_same_video_and_blocks_unknown_outcome(isolated, tmp_path, monkeypatch):
    from app.execution.persistent_gate import PersistentWriteSafetyGate
    db, payload, _, _ = isolated
    selected = flow.preview(db, {**payload, "videoCommentAllowOtherAccounts": True})
    approved = prepare(db, selected["proposalId"], selected["binding"], tmp_path / "scope")
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE", approved["scopeFile"])
    gate = PersistentWriteSafetyGate(db, namespace="live")
    shared = json.dumps(["shared-main", VIDEO], separators=(",", ":"))
    assert gate.reserve(shared)[0]
    monkeypatch.setattr(browser, "execute", lambda *a, **k: pytest.fail("pending shared target must not submit"))
    result = live_leadgen.perform(db, "post-comment", account_id="a", task_id="task", video_url=VIDEO,
                                 text=payload["videoCommentContent"])
    assert result["status"] == "skipped" and "uncertain" in result["code"]


def test_unknown_comment_region_cannot_pass_an_active_exclusion_filter(isolated):
    _, payload, _, comments = isolated
    config = cfg.normalize({**payload, "locationFilterRegions": ["广东"], "locationFilterMode": "exclude"})
    assert cfg.comment_reason(config, {**comments[0], "publishedText": "1小时前"}, set()) == "评论属地未知"


def test_shared_video_reservation_only_reopens_confirmed_results_when_explicitly_allowed(isolated):
    from app.execution.persistent_gate import PersistentWriteSafetyGate
    from app.execution.write_safety import WriteResult, WriteStatus
    db, _, _, _ = isolated
    gate = PersistentWriteSafetyGate(db, namespace="live")
    assert gate.reserve("shared")[0]
    gate.record("shared", WriteResult(WriteStatus.SENT_OK, False, False, "confirmed"))
    assert not gate.reserve("shared")[0]
    assert gate.reserve("shared", allow_sent=True)[0]
    assert not gate.reserve("shared", allow_sent=True)[0]


def test_leadgen_forwards_expand_sub_comments(isolated):
    """「采集二级评论」在评论获客页是个开关，但计划层以前从来不传给壳。"""
    db, payload, calls, _ = isolated
    flow.preview(db, {**payload, "enableLike": True, "expandSubComments": True})
    reads = [c for c in calls if c["type"] == "read-comments"]
    assert reads and reads[0]["expandSubComments"] is True

    calls.clear()
    flow.preview(db, {**payload, "enableLike": True})
    reads = [c for c in calls if c["type"] == "read-comments"]
    assert reads and "expandSubComments" not in reads[0]


def test_age_seconds_parses_relative_and_absolute_dates():
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert cfg.age_seconds({"publishedText": "刚刚"}, now=now) == 0
    assert cfg.age_seconds({"publishedText": "3天前"}, now=now) == 3 * 86400
    # 详情浮层兜底抽到的是绝对日期（北京时间展示）：
    assert cfg.age_seconds({"publishedText": "2026-11-18"}, now=now) is not None
    assert cfg.age_seconds({"publishedText": "11-18"}, now=now) == 2 * 86400 + 20 * 3600
    assert cfg.age_seconds({"publishedText": "11月18日"}, now=now) == 2 * 86400 + 20 * 3600
    assert cfg.age_seconds({"publishedText": "2026年11月18日"}, now=now) == 2 * 86400 + 20 * 3600
    # 跨年：今年的 12-25 还没到，应按去年算
    age = cfg.age_seconds({"publishedText": "12-25"}, now=now)
    assert age is not None and age > 300 * 86400
    age = cfg.age_seconds({"publishedText": "12月25日"}, now=now)
    assert age is not None and age > 300 * 86400
    assert cfg.age_seconds({"publishedText": ""}, now=now) is None


def test_prefill_candidate_keeps_feed_publish_time_for_time_filter(isolated):
    """单遍式透传的 feed 卡片信息不能被 inspect-video 结果覆盖丢失：
    设了「只处理 3 天内发布」时，卡片带的「2天前」应让视频通过；
    没有预填时（详情浮层读不到发布时间）才会判「发布时间未知」跳过。"""
    db, payload, _, _ = isolated
    config = {**payload, "videoSources": ["specific"], "keywords": "",
              "specifiedUrls": VIDEO, "recommendVideoTimeFilter": "3d",
              "_prefillCandidate": {"title": "feed 卡片标题", "publishedText": "2天前"}}
    result = flow.preview(db, config)
    assert len(result["actions"]) == 1
    # 无预填：inspect-video 只有标题、没有发布时间 → 全部跳过
    with pytest.raises(ValueError, match="发布时间未知"):
        flow.preview(db, {**config, "_prefillCandidate": {}})


def test_config_inspect_reports_title_filter_overlap_and_blocks_total_cancellation(isolated):
    _, payload, _, _ = isolated
    partial = cfg.inspect({**payload, "includeTitleKeywords": "智能体,工作流",
                           "excludeTitleKeywords": "智能体"})
    assert partial["success"]
    assert any("包含词“智能体”/排除词“智能体”" in item for item in partial["warnings"])

    cancelled = cfg.inspect({**payload, "includeTitleKeywords": "智能体开发,工作流搭建",
                             "excludeTitleKeywords": "智能体,工作流"})
    assert not cancelled["success"]
    assert any("完全抵消" in item for item in cancelled["errors"])


def test_config_inspect_explains_shared_budget_and_estimates_configured_time(isolated):
    _, payload, _, _ = isolated
    report = cfg.inspect({**payload, "keywords": "智能体,工作流", "videoMax": 2,
                          "interactionLimitMin": 3, "interactionLimitMax": 5,
                          "stayMin": 15, "stayMax": 30,
                          "intervalMin": 180, "intervalMax": 300})
    assert report["success"]
    assert any("下限为3次，低于目标视频数4" in item for item in report["warnings"])
    assert any(item.startswith("互动总上限：") and "3" in item and "5" in item
               for item in report["summary"])
    assert "视频赞、主评、评论赞、回复、私信、关注、分享共用此上限" in report["summary"][3]
    assert "停留与视频间隔预计：" in report["summary"][-1]
    assert "600" in report["summary"][-1] and "1020" in report["summary"][-1]
    assert "不含评论加载/阅读与互动处理时间" in report["summary"][-1]


def test_config_inspect_explains_unsupported_search_filters_and_missing_source_time(isolated):
    _, payload, _, _ = isolated
    unsupported = cfg.inspect({**payload, "searchSort": "1"})
    assert not unsupported["success"]
    assert any("当前桌面执行端暂不支持官方搜索排序" in item for item in unsupported["errors"])

    no_timestamp = cfg.inspect({**payload, "videoSources": ["like"],
                                "recommendVideoTimeFilter": "3d"})
    assert no_timestamp["success"]
    assert any("喜欢列表不提供视频发布时间" in item for item in no_timestamp["warnings"])
