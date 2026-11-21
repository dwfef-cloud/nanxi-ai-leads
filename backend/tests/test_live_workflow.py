"""Isolated integration checks. No production DB, desktop or platform writes."""
import json
import sqlite3
import time
from pathlib import Path
import subprocess
import sys

import pytest

from app import leadgen_pools
from app.api import task_runner, ipc_bridge
from app.execution import live_workflow as flow, live_scope, live_leadgen
from app.execution import leadgen_stream
from app.execution.persistent_gate import PersistentWriteSafetyGate
from app.runtime import browser, registry, store
from tools.prepare_live_workflow_scope import prepare
from tools.prepare_live_stream_scope import prepare as prepare_stream_scope

VIDEO = "https://www.douyin.com/video/7400000000000000001"
COMMENT = {"commentId": "123456789", "nickname": "潜客", "text": "工作流怎么做", "userId": "MS4wLjAB_target"}


def stored_actions(db, proposal_id):
    """服务端提案里保留完整目标（含 commentId）；出口会给页面裁掉平台标识。"""
    with sqlite3.connect(str(db)) as con:
        row = con.execute("SELECT body FROM live_workflow_proposals WHERE id=?", (proposal_id,)).fetchone()
    return json.loads(row[0])["actions"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY,nickname TEXT,platform TEXT)")
        con.execute("INSERT INTO accounts VALUES ('account','执行账号','douyin')")
    payload = {"taskId": "run-one", "leadgenTaskId": "task-one", "accountId": "account", "keywords": "工作流搭建",
               "enableVideoComment": True, "videoCommentMode": "custom", "videoCommentContent": "请问如何选择工作流的触发方式？",
               "enableComment": True, "commentReplyMode": "custom", "commentContent": "可以先整理输入输出，再拆分节点。",
               "enableDM": True, "dmContent": "{nickname}，你好，关于工作流的问题可以交流。", "intentionKeywords": "怎么做"}
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        if command["type"] == "search-videos":
            return {"ok": True, "items": [{"url": VIDEO, "title": "工作流搭建教程"}]}
        if command["type"] == "read-source-videos":
            return {"ok": True, "items": [{"url": VIDEO, "title": "入口里的视频",
                                           "author": "某作者", "publishedText": "3天前"}]}
        if command["type"] == "read-comments":
            return {"ok": True, "comments": [dict(COMMENT)]}
        # 人式浏览（2026-11-19）后，执行前会先「点封面进视频 → 停留观看 → 点下一个」。
        # 这三个都是只读/浏览类指令，不发任何写动作；白名单漏掉它们会误报
        # 「preview must never send」，把正常的浏览序列当成越权发送。
        if command["type"] == "open-video":
            return {"ok": True, "videoUrl": command.get("videoUrl"), "mode": "clicked-card"}
        if command["type"] == "watch-video":
            return {"ok": True, "watched": True, "seconds": command.get("seconds")}
        if command["type"] == "next-video":
            return {"ok": True, "videoUrl": command.get("videoUrl"), "urlChanged": True}
        pytest.fail("preview must never send: " + command["type"])
    monkeypatch.setattr(browser, "status", lambda _: {"engine": "shell"})
    monkeypatch.setattr(browser, "execute", execute)
    monkeypatch.setattr(task_runner.event_bus, "publish", lambda *a: None)
    monkeypatch.delenv("LEADS_LEADGEN_SCOPE", raising=False)
    monkeypatch.delenv("LEADS_LIVE_ACCEPTANCE_SCOPE", raising=False)
    monkeypatch.setattr(ipc_bridge, "DB_PATH", db)
    yield db, payload, calls
    registry.reset_for_tests()


def test_preview_reads_keywords_comments_and_never_writes(env):
    db, payload, calls = env
    result = flow.preview(db, payload)
    assert result["success"]
    assert [c["type"] for c in calls] == ["search-videos", "read-comments"]
    assert [a["type"] for a in result["actions"]] == ["post-comment", "reply-comment", "send-dm"]
    # 出口不允许出现平台 uid 级字段（2026-11-16 真机：actions 带 commentId 让整条
    # 预览被 assert_public_safe 判失败），但服务端提案里必须留着才能定点回复。
    for action in result["actions"]:
        assert "commentId" not in action and "commentUserId" not in action
    stored = stored_actions(db, result["proposalId"])
    assert stored[1]["commentId"] == COMMENT["commentId"]
    assert stored[2]["recipientId"] == COMMENT["userId"]
    assert result["actions"][2]["recipientId"] == COMMENT["userId"]
    assert result["actions"][2]["text"].startswith("潜客，你好")
    assert not registry._ACCOUNT_LEASES


@pytest.mark.parametrize("source,label", [("follow", "关注列表"), ("recommend", "推荐页"),
                                          ("like", "喜欢列表"), ("hashtag", "话题")])
def test_video_entry_sources_read_entry_list_and_plan_actions(env, source, label):
    """关注/推荐/喜欢/话题四个入口：先只读入口清单，再走同一套筛选与动作派生。"""
    db, payload, calls = env
    result = flow.preview(db, {**payload, "videoSources": [source]})
    assert result["success"]
    assert calls[0]["type"] == "read-source-videos"
    assert calls[0]["source"] == source
    assert [a["type"] for a in result["actions"]] == ["post-comment", "reply-comment", "send-dm"]
    # 入口名要落在动作上，确认弹窗才说得清「这条目标是哪个入口来的」；
    # 话题页是「入口 + 关键词」，关键词本身才是这一轮的目标标签。
    assert {a["keyword"] for a in result["actions"]} == ({payload["keywords"]} if source == "hashtag" else {label})
    assert result["actions"][0]["videoUrl"] == VIDEO


def test_hashtag_source_requires_keyword(env):
    db, payload, _ = env
    with pytest.raises(ValueError) as caught:
        flow.preview(db, {**payload, "videoSources": ["hashtag"], "keywords": ""})
    assert "话题来源需要先填写" in str(caught.value)


def test_ai_skip_moves_to_next_video_and_keeps_reply_on_that_video(env, monkeypatch):
    db, payload, _ = env
    other = "https://www.douyin.com/video/7400000000000000002"
    calls, titles = [], []
    def execute(cmd, **kwargs):
        calls.append(cmd)
        if cmd["type"] == "search-videos":
            return {"ok": True, "items": [{"url": VIDEO, "title": "无关视频"}, {"url": other, "title": "工作流搭建"}]}
        assert cmd["type"] == "read-comments"
        return {"ok": True, "comments": [dict(COMMENT)]}
    def generate(db, prompt, candidate):
        titles.append(candidate["title"])
        return ({"ok": False, "code": "not_suitable", "reason": "AI 判断跳过该视频：话题无关"}
                if candidate["title"] == "无关视频" else {"ok": True, "text": "工作流节点的输入输出应该如何设计？"})
    monkeypatch.setattr(browser, "execute", execute)
    monkeypatch.setattr(live_leadgen, "generate", generate)
    selected = flow.preview(db, {**payload, "videoCommentMode": "ai"})
    assert titles == ["无关视频", "工作流搭建"]
    assert all(a["videoUrl"] == other for a in selected["actions"])
    with sqlite3.connect(db) as con:
        skipped = con.execute("SELECT status,code FROM live_action_results WHERE action='generate-comment'").fetchall()
    assert skipped == [("skipped", "AI 判断跳过该视频：话题无关")]
    assert all(c["type"] in {"search-videos", "read-comments"} for c in calls)


@pytest.mark.parametrize("code,expected", [("not_suitable", 5), ("invalid_length", 1)])
def test_ai_skip_is_bounded_and_invalid_response_does_not_try_other_videos(env, monkeypatch, code, expected):
    db, payload, _ = env
    calls = []
    monkeypatch.setattr(browser, "execute", lambda *a, **k: {"ok": True, "items": [
        {"url": f"https://www.douyin.com/video/{7400000000000000000 + i}", "title": f"视频{i}"} for i in range(1, 8)]})
    monkeypatch.setattr(live_leadgen, "generate", lambda db, p, c:
                        calls.append(c) or {"ok": False, "code": code, "reason": "明确原因"})
    with pytest.raises(ValueError):
        flow.preview(db, {**payload, "videoCommentMode": "ai"})
    assert len(calls) == expected
    with sqlite3.connect(db) as con:
        assert not con.execute("SELECT name FROM sqlite_master WHERE name='live_workflow_proposals'").fetchone()
    assert not registry._ACCOUNT_LEASES


def test_stop_during_generation_never_creates_proposal(env, monkeypatch):
    db, payload, _ = env
    def generate(*args):
        flow.cancel(account_id="account")
        return {"ok": True, "text": "已生成但停止后不得继续使用的正文"}
    monkeypatch.setattr(live_leadgen, "generate", generate)
    with pytest.raises(ValueError, match="已停止准备"):
        flow.preview(db, {**payload, "videoCommentMode": "ai"})
    assert not registry._ACCOUNT_LEASES


def test_native_helper_binds_proposal_and_signatures_for_all_actions(env, tmp_path, monkeypatch):
    db, payload, _ = env
    selected = flow.preview(db, payload)
    output = tmp_path / "approved.json"
    prepared = prepare(db, selected["proposalId"], selected["binding"], output)
    assert prepared["payload"] == payload
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE", str(output))
    assert live_scope.task_allowed(payload)
    assert not live_scope.task_allowed({**payload, "taskId": "other-run"})
    for action in stored_actions(db, selected["proposalId"]):
        cmd = live_scope.signed_command(action["type"], account_id="account", task_id="task-one", video_url=VIDEO,
                text=action["text"], comment_id=action.get("commentId", ""), comment_author=action.get("commentAuthor", ""),
                recipient_id=action.get("recipientId", ""))
        assert live_scope.command_allowed(cmd)
        for field in ("text", "commentId", "commentAuthor", "recipientId", "accountId", "videoUrl"):
            assert not live_scope.command_allowed({**cmd, field: "changed"})
    assert leadgen_pools.load_records(db)["task-one"]["liveReviewedRunId"] == "run-one"


def test_stream_scope_binds_settings_and_signs_runtime_discovered_video(env, tmp_path, monkeypatch):
    from app import paths

    db, payload, _ = env
    payload.update(videoMax=2, interactionLimit=3, enableComment=False, enableDM=False,
                   enableFollow=False, enableProfileShare=False, enableCommentShare=False)
    output = tmp_path / "stream.json"
    prepared = prepare_stream_scope(db, payload, output)
    scope = json.loads(output.read_text(encoding="utf8"))
    assert scope["mode"] == "stream" and scope["videoUrls"] == ["*"]
    assert scope["actions"] == {"post-comment": 3}
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE", str(output))
    assert live_scope.task_allowed(payload)
    assert not live_scope.task_allowed({**payload, "interactionLimit": 100})
    command = live_scope.signed_command("post-comment", account_id="account", task_id="task-one",
        video_url=VIDEO, text="围绕当前视频和讨论区的测试评论")
    assert live_scope.command_allowed(command)
    assert not live_scope.command_allowed({**command, "text": "被篡改的评论"})
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "backend-data")
    registered = ipc_bridge._activate_leadgen_scope({"scope": scope, "taskPayload": payload})
    assert registered["success"] is True
    assert Path(registered["scopeFile"]).is_file()


def test_desktop_activated_scope_is_visible_to_reused_backend(env, tmp_path, monkeypatch):
    from app import paths

    db, payload, _ = env
    selected = flow.preview(db, payload)
    prepared = prepare(db, selected["proposalId"], selected["binding"], tmp_path / "approved.json")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "backend-data")
    monkeypatch.delenv("LEADS_LEADGEN_SCOPE", raising=False)
    monkeypatch.delenv("LEADS_LEADGEN_SCOPE_DIR", raising=False)

    registered = ipc_bridge.dispatch("activate-leadgen-scope", [{
        "scope": json.loads(Path(prepared["scopeFile"]).read_text(encoding="utf-8")),
        "taskPayload": prepared["payload"],
    }])

    assert registered["success"] is True
    assert Path(registered["scopeFile"]).is_file()
    assert live_scope.task_allowed(prepared["payload"])
    assert not live_scope.task_allowed({**prepared["payload"], "interactionLimit": 99})


def test_desktop_scope_activation_rejects_changed_payload(env, tmp_path, monkeypatch):
    from app import paths

    db, payload, _ = env
    selected = flow.preview(db, payload)
    prepared = prepare(db, selected["proposalId"], selected["binding"], tmp_path / "approved.json")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "backend-data")
    scope = json.loads(Path(prepared["scopeFile"]).read_text(encoding="utf-8"))
    result = ipc_bridge.dispatch("activate-leadgen-scope", [{
        "scope": scope, "taskPayload": {**prepared["payload"], "interactionLimit": 99},
    }])
    assert result["success"] is False
    # 2026-11-20：拒绝理由必须精确到「哪一项参数变了」，否则用户不知道该改什么。
    assert "interactionLimit" in result["error"]
    assert "99" in result["error"]
    assert "保存并启动" in result["error"]
    assert result["problems"], result
    assert not live_scope.desktop_leadgen_scope_file("task-one", "account").exists()


def test_desktop_scope_activation_accepts_entry_without_keywords(env, tmp_path, monkeypatch):
    """推荐页/关注列表本来就没有关键词，授权不能因为 keywords 为空就拒绝启动。

    2026-11-20 用户实测：视频入口选「推荐页」必现「任务授权范围与启动参数不一致」。
    真因是校验把「关键词非空」当成所有入口的必要条件，报错文案又把具体原因吞掉了。
    """
    from app import paths

    db, payload, _ = env
    payload.update(taskId="run-rec", leadgenTaskId="task-rec", videoSources=["recommend"],
                   keywords="", enableVideoComment=True, videoCommentMode="custom",
                   videoCommentContent="这个思路挺实用，学到了", enableMainVideoLike=True)
    output = tmp_path / "recommend.json"
    prepared = prepare_stream_scope(db, payload, output)
    scope = json.loads(output.read_text(encoding="utf-8"))
    assert scope["keywords"] == []
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "backend-data")
    result = ipc_bridge._activate_leadgen_scope({"scope": scope, "taskPayload": prepared["payload"]})
    assert result["success"] is True, result
    assert Path(result["scopeFile"]).is_file()


def test_scope_activation_names_each_broken_field(env, tmp_path, monkeypatch):
    """各条拒绝理由都要能独立读懂：过期 / 账号不符 / 没有可执行动作。"""
    from app import paths

    db, payload, _ = env
    payload.update(taskId="run-x", leadgenTaskId="task-x", videoSources=["recommend"], keywords="")
    output = tmp_path / "diag.json"
    prepared = prepare_stream_scope(db, payload, output)
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "backend-data")

    scope = json.loads(output.read_text(encoding="utf-8"))
    scope["expiresAt"] = int(time.time()) - 5
    expired = ipc_bridge._activate_leadgen_scope({"scope": scope, "taskPayload": prepared["payload"]})
    assert expired["success"] is False and "过期" in expired["error"], expired

    scope = json.loads(output.read_text(encoding="utf-8"))
    wrong = ipc_bridge._activate_leadgen_scope(
        {"scope": scope, "taskPayload": {**prepared["payload"], "accountId": "another"}})
    assert wrong["success"] is False and "another" in wrong["error"], wrong

    scope = json.loads(output.read_text(encoding="utf-8"))
    scope["actions"] = {}
    empty = ipc_bridge._activate_leadgen_scope({"scope": scope, "taskPayload": prepared["payload"]})
    assert empty["success"] is False and "互动动作" in empty["error"], empty


def test_python_signatures_are_accepted_by_actual_node_worker_for_all_three_actions(env, tmp_path, monkeypatch):
    db, payload, _ = env
    selected = flow.preview(db, payload)
    output = tmp_path / "approved.json"
    prepare(db, selected["proposalId"], selected["binding"], output)
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE", str(output))
    commands = [live_scope.signed_command(a["type"], account_id="account", task_id="task-one", video_url=VIDEO,
                text=a["text"], comment_id=a.get("commentId", ""), comment_author=a.get("commentAuthor", ""),
                recipient_id=a.get("recipientId", "")) for a in stored_actions(db, selected["proposalId"])]
    script = "const p=JSON.parse(require('fs').readFileSync(0,'utf8'));const {scopeFor}=require('./desktop/live-platform');" \
             "process.stdout.write(JSON.stringify(p.commands.map(c=>!!scopeFor(c,p.file))));"
    result = subprocess.run(["node", "-e", script], input=json.dumps({"file": str(output), "commands": commands}, ensure_ascii=False),
                            text=True, encoding="utf8", capture_output=True, check=True,
                            cwd=Path(__file__).resolve().parents[2], timeout=15)
    assert json.loads(result.stdout) == [True, True, True]


@pytest.mark.parametrize("pipe_encoding", ["gbk", "utf-8"])
def test_scope_helper_cli_roundtrips_chinese_paths_through_node(env, tmp_path, pipe_encoding):
    db, payload, _ = env
    selected = flow.preview(db, payload)
    output = tmp_path / "获客系统开发" / "确认文件.approved"
    helper = Path(__file__).resolve().parents[1] / "tools" / "prepare_live_workflow_scope.py"
    script = """
const fs = require('fs'), cp = require('child_process');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const stdout = cp.execFileSync(input.python, [input.helper, '--db', input.db,
  '--proposal-id', input.proposalId, '--binding', input.binding, '--output', input.output],
  {encoding: 'utf8', windowsHide: true,
   env: {...process.env, PYTHONIOENCODING: input.pipeEncoding, PYTHONUTF8: '0'}});
const prepared = JSON.parse(stdout);
if (prepared.scopeFile !== input.output) throw Error('helper changed the approved path');
fs.copyFileSync(prepared.scopeFile, input.output + '.next');
fs.renameSync(input.output + '.next', input.output + '.active');
process.stdout.write(JSON.stringify(prepared));
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps({
        "python": sys.executable, "helper": str(helper), "db": str(db),
        "proposalId": selected["proposalId"], "binding": selected["binding"],
        "output": str(output), "pipeEncoding": pipe_encoding,
    }), text=True, encoding="utf8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    prepared = json.loads(result.stdout)
    assert prepared["success"] and prepared["payload"] == payload
    assert output.read_bytes() == Path(str(output) + ".active").read_bytes()


@pytest.mark.parametrize("mutation", ["binding", "expired", "account", "history"])
def test_helper_rejects_stale_or_changed_proposals(env, tmp_path, mutation):
    db, payload, _ = env
    result = flow.preview(db, payload)
    binding = result["binding"]
    if mutation == "binding":
        binding = "wrong"
    elif mutation == "account":
        with sqlite3.connect(db) as con:
            con.execute("UPDATE accounts SET nickname='另一个人'")
    elif mutation == "expired":
        with sqlite3.connect(db) as con:
            body = json.loads(con.execute("SELECT body FROM live_workflow_proposals").fetchone()[0])
            body["expiresAt"] = 0
            con.execute("UPDATE live_workflow_proposals SET body=?", (json.dumps(body),))
            binding = flow.binding_of(body)
    else:
        gate = PersistentWriteSafetyGate(db, namespace="live")
        assert gate.reserve(flow.action_key("account", result["actions"][0]))[0]
    output = tmp_path / "blocked.json"
    with pytest.raises(ValueError):
        prepare(db, result["proposalId"], binding, output)
    assert not output.exists()


@pytest.mark.parametrize("change", [{"enableWarmup": True, "warmupMin": 0}, {"videoSources": ["live"]},
    {"videoCommentContent": "短"}, {"accountId": "unknown"}, {"keywords": ""}, {"taskMode": "scrape"}])
def test_invalid_configuration_never_prepares_scope(env, change):
    db, payload, _ = env
    with pytest.raises(ValueError):
        flow.preview(db, {**payload, **change})
    assert not registry._ACCOUNT_LEASES


def test_cannot_bypass_intent_filter_or_use_missing_recipient(env, monkeypatch):
    db, payload, _ = env
    payload = {**payload, "enableVideoComment": False, "enableComment": False}
    for comment in ({**COMMENT, "text": "学习"}, {**COMMENT, "userId": ""}, {**COMMENT, "commentId": ""}):
        monkeypatch.setattr(browser, "execute", lambda cmd, **k:
            {"ok": True, "items": [{"url": VIDEO, "title": "工作流搭建"}]} if cmd["type"] == "search-videos" else {"ok": True, "comments": [comment]})
        with pytest.raises(ValueError):
            flow.preview(db, payload)
    assert not registry._ACCOUNT_LEASES


def test_no_intent_match_keeps_main_draft_and_explicitly_reports_skipped_interactions(env, monkeypatch):
    db, payload, _ = env
    monkeypatch.setattr(browser, "execute", lambda cmd, **k:
        {"ok": True, "items": [{"url": VIDEO, "title": "工作流搭建"}]} if cmd["type"] == "search-videos" else
        {"ok": True, "comments": [{**COMMENT, "text": "学习"}]})
    selected = flow.preview(db, payload)
    assert [a["type"] for a in selected["actions"]] == ["post-comment"]
    assert [a["type"] for a in selected["skippedActions"]] == ["reply-comment", "send-dm"]


@pytest.mark.parametrize("stop_after,expected", [(None, 3), ("uncertain", 1), ("stopped", 1), ("sent_fail", 1)])
def test_reviewed_run_counts_only_confirmed_actions_and_stops(env, tmp_path, monkeypatch, stop_after, expected):
    db, payload, _ = env
    result = flow.preview(db, payload)
    prepared = prepare(db, result["proposalId"], result["binding"], tmp_path / "scope.json")
    scope = json.loads((tmp_path / "scope.json").read_text(encoding="utf8"))
    ctx = task_runner.RunCtx(task_id="run-one", leadgen_id="task-one", account_id="account", payload=payload, db_path=db)
    ctx.run_id = store.run_begin(db, "leadgen", ctx.task_id, "离线联调", "account")
    calls = []
    def perform(db, action, **kwargs):
        calls.append((action, kwargs))
        if stop_after == "stopped":
            ctx.stop.set()
            return {"ok": True, "status": "sent_ok"}
        return {"ok": stop_after is None, "status": stop_after or "sent_ok"}
    monkeypatch.setattr(live_leadgen, "perform", perform)
    monkeypatch.setattr(live_leadgen, "generate", lambda *a: pytest.fail("approved plan must not regenerate"))
    flow.run_reviewed(ctx, scope)
    assert len(calls) == expected
    rec = leadgen_pools.load_records(db)["task-one"]
    if stop_after is None:
        assert rec["status"] == "completed"
        assert rec["stats"]["published"] == 1 and rec["stats"]["interacted"] == 2
        assert rec["stats"]["messages"] == 1
        assert rec["stats"]["reached"] == 1
        assert calls[1][1]["comment_id"] == COMMENT["commentId"]
        assert calls[2][1]["recipient_id"] == COMMENT["userId"]
    elif stop_after == "stopped":
        assert rec["status"] == "stopped"
        assert rec["stats"]["published"] == 1
    else:
        assert rec["status"] == "failed" and rec["stats"]["published"] == 0
        assert rec["stats"].get("uncertain", 0) == (1 if stop_after == "uncertain" else 0)
    # The shipped renderer saves 'running' after start-task returns.
    ipc_bridge._save_leadgen_task({"id": "task-one", "status": "running", "stats": {},
                                   "runs": [{"automationTaskId": "run-one"}]})
    assert leadgen_pools.load_records(db)["task-one"]["status"] == rec["status"]


def test_reviewed_main_comment_reads_fresh_context_before_sending(env, tmp_path, monkeypatch):
    db, payload, _ = env
    payload.update(videoMax=1, interactionLimit=1, enableMainVideoLike=False,
                   enableVideoComment=True, enableLike=False, enableComment=False,
                   enableDM=False, enableFollow=False, enableProfileShare=False,
                   enableCommentShare=False, stayMin=0, stayMax=0)
    monkeypatch.setattr(browser, "execute", lambda cmd, **_: (
        {"ok": True, "items": [{"url": VIDEO, "title": "工作流搭建"}]} if cmd["type"] == "search-videos" else
        {"ok": True, "comments": [{"commentId": COMMENT["commentId"], "text": COMMENT["text"]}]}
        if cmd["type"] == "read-comments" else pytest.fail(f"unexpected browser command: {cmd['type']}")))
    proposal = flow.preview(db, payload)
    prepare(db, proposal["proposalId"], proposal["binding"], tmp_path / "scope.json")
    scope = json.loads((tmp_path / "scope.json").read_text(encoding="utf8"))
    events = []
    original_execute = browser.execute
    def execute(cmd, **kwargs):
        events.append(cmd["type"])
        return original_execute(cmd, **kwargs)
    monkeypatch.setattr(browser, "execute", execute)
    monkeypatch.setattr(live_leadgen, "perform", lambda *a, **k:
        events.append("post-comment") or {"ok": True, "status": "sent_ok"})
    ctx = task_runner.RunCtx(task_id="run-context", leadgen_id="task-context", account_id="account",
                             payload=payload, db_path=db)
    ctx.run_id = store.run_begin(db, "leadgen", ctx.task_id, "评论上下文复核", "account")

    flow.run_reviewed(ctx, scope)

    assert events[-2:] == ["read-comments", "post-comment"]
    assert ctx.stats["executionCommentsRead"] == 1


def test_reviewed_comment_stops_when_fresh_context_cannot_be_read(env, tmp_path, monkeypatch):
    db, payload, _ = env
    payload.update(videoMax=1, interactionLimit=1, enableMainVideoLike=False,
                   enableVideoComment=True, enableLike=False, enableComment=False,
                   enableDM=False, enableFollow=False, enableProfileShare=False,
                   enableCommentShare=False, stayMin=0, stayMax=0)
    monkeypatch.setattr(browser, "execute", lambda cmd, **_: (
        {"ok": True, "items": [{"url": VIDEO, "title": "工作流搭建"}]} if cmd["type"] == "search-videos" else
        {"ok": True, "comments": []} if cmd["type"] == "read-comments" else
        pytest.fail(f"unexpected browser command: {cmd['type']}")))
    proposal = flow.preview(db, payload)
    prepare(db, proposal["proposalId"], proposal["binding"], tmp_path / "scope.json")
    scope = json.loads((tmp_path / "scope.json").read_text(encoding="utf8"))
    sent = []
    monkeypatch.setattr(live_leadgen, "perform", lambda *a, **k: sent.append(True) or {"ok": True, "status": "sent_ok"})
    ctx = task_runner.RunCtx(task_id="run-empty-context", leadgen_id="task-empty-context", account_id="account",
                             payload=payload, db_path=db)
    ctx.run_id = store.run_begin(db, "leadgen", ctx.task_id, "评论区读取失败", "account")

    flow.run_reviewed(ctx, scope)

    assert sent == []
    assert ctx.stats["failed"] == 1


def test_stream_searches_next_keyword_only_after_current_videos_are_processed(env, monkeypatch):
    db, payload, _ = env
    payload.update(taskId="stream-run", enableVideoComment=True, enableComment=False, enableDM=False,
                   enableFollow=False, enableProfileShare=False, enableCommentShare=False,
                   stayMin=0, stayMax=0, intervalMin=0, intervalMax=0, videoMax=2,
                   interactionLimit=4, keywords="第一词,第二词")
    config = __import__("app.execution.leadgen_config", fromlist=["normalize"]).normalize(payload, sample=False)
    scope = {"mode": "stream", "runTaskId": "stream-run", "keywords": ["第一词", "第二词"],
             "effectiveConfig": config, "actions": {"post-comment": 4}}
    first = "https://www.douyin.com/video/7400000000000000002"
    second = "https://www.douyin.com/video/7400000000000000003"
    third = "https://www.douyin.com/video/7400000000000000004"
    events = []
    def browser_call(cmd, **_):
        events.append(("browser", cmd["type"], cmd.get("keyword")))
        if cmd["type"] == "search-videos":
            if cmd["keyword"] == "第一词":
                return {"ok": True, "items": [{"url": first, "title": "视频一"}, {"url": second, "title": "视频二"}]}
            return {"ok": True, "items": [{"url": third, "title": "视频三"}]}
        # 人式浏览：进视频点封面、看完点「下一个」；本用例只验证关键词顺序，两者都走兜底。
        if cmd["type"] == "open-video":
            return {"ok": True, "mode": "clicked-card"}
        if cmd["type"] == "next-video":
            return {"ok": False, "error": "当前页没有「下一个」切换控件"}
        if cmd["type"] == "read-comments":
            return {"ok": True, "comments": [{"commentId": "1", "text": "围绕视频的讨论"}]}
        pytest.fail(f"unexpected browser command: {cmd['type']}")
    def plan(_db, per_video, _account, **_):
        url = per_video["specifiedUrls"]
        events.append(("plan", url))
        return {"actions": [{"type": "post-comment", "videoUrl": url, "keyword": per_video["_sourceKeyword"],
                             "title": "标题", "text": "围绕视频讨论的已配置评论内容"}],
                "planStats": {"commentsRead": 2}}
    monkeypatch.setattr(browser, "execute", browser_call)
    monkeypatch.setattr("app.execution.leadgen_plan.build", plan)
    monkeypatch.setattr(live_leadgen, "perform", lambda _db, kind, **kw:
        events.append(("send", kind, kw["video_url"])) or {"ok": True, "status": "sent_ok"})
    monkeypatch.setattr(task_runner, "_finish", lambda *a, **k: None)
    monkeypatch.setattr(task_runner, "_emit_task", lambda *a, **k: None)
    monkeypatch.setattr(task_runner, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(task_runner, "_log", lambda *a, **k: None)
    monkeypatch.setattr(task_runner, "_action", lambda *a, **k: None)
    monkeypatch.setattr(task_runner.event_bus, "publish", lambda *a, **k: None)
    ctx = task_runner.RunCtx(task_id="stream-run", leadgen_id="task-stream", account_id="account",
                             payload=payload, db_path=db)

    leadgen_stream.run(ctx, scope)

    # 第一词的两条视频是随机抽取的（先后不定），但都必须在搜第二词之前处理完
    search2_index = events.index(("browser", "search-videos", "第二词"))
    processed = [e for e in events if e[0] in {"plan", "send"} and e[-1] in {first, second}]
    assert sorted({e[-1] for e in processed}) == sorted([first, second])
    assert all(events.index(e) < search2_index for e in processed)
    assert {e[2] for e in events if e[0] == "send"} == {first, second, third}
    assert ctx.stats["published"] == 3


def test_stream_main_comment_prompt_uses_untrusted_discussion_context(monkeypatch):
    from app import ai_personas

    prompts = []
    monkeypatch.setattr(ai_personas, "_llm_settings", lambda _db: {"model": "fixture"})
    monkeypatch.setattr(ai_personas, "_call_chat", lambda cfg, prompt:
        prompts.append(prompt) or '{"should_like":false,"should_reply":true,"reply_content":"围绕讨论的问题补充一个具体观点。","thought":"与视频讨论相关"}')
    result = live_leadgen.generate("unused", {}, {"title": "工作流怎么拆分", "discussionContext": "- 节点连接顺序怎么安排？"})

    assert result["ok"]
    assert "节点连接顺序怎么安排" in prompts[0]
    assert "不可信数据" in prompts[0]


def test_stream_skips_video_when_comment_context_is_empty(env, monkeypatch):
    from app.execution.leadgen_plan import build

    db, payload, _ = env
    calls = []
    def execute(cmd, **_):
        calls.append(cmd["type"])
        if cmd["type"] == "inspect-video":
            return {"ok": True, "item": {"title": "工作流搭建教程"}}
        if cmd["type"] == "read-comments":
            return {"ok": True, "comments": []}
        pytest.fail("unexpected read command")
    monkeypatch.setattr(browser, "execute", execute)
    class Check:
        def __call__(self):
            return None
        def stopped(self):
            return False
    plan = build(db, {**payload, "videoSources": ["specific"], "specifiedUrls": VIDEO,
                      "_sourceKeyword": "工作流", "_streamMode": True},
                 flow._account(db, "account"), check=Check(), progress=lambda _text: None)

    assert plan["actions"] == []
    assert "未读到可用评论区上下文" in plan["skippedActions"][0]["reason"]
    assert calls == ["inspect-video", "read-comments"]


def test_preparation_can_be_stopped_before_generating_or_sending(env, monkeypatch):
    db, payload, _ = env
    def execute(*args, **kwargs):
        assert task_runner.stop("run-one")["status"] == "stopping"
        return {"ok": True, "items": [{"url": VIDEO, "title": "工作流搭建"}]}
    monkeypatch.setattr(browser, "execute", execute)
    monkeypatch.setattr(live_leadgen, "generate", lambda *a: pytest.fail("stopped"))
    with pytest.raises(ValueError, match="已停止"):
        flow.preview(db, payload)
    assert not registry._ACCOUNT_LEASES


def test_no_confirmed_scope_does_not_fall_back_to_legacy(env, monkeypatch, tmp_path):
    db, payload, _ = env
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"scopeId": "legacy", "secret": "x"*40, "expiresAt": time.time()+60,
        "accountId": "account", "leadgenTaskId": "task-one", "videoUrls": [VIDEO], "actions": {"post-comment": 1}}))
    monkeypatch.setenv("LEADS_LIVE_ACCEPTANCE_SCOPE", str(legacy))
    monkeypatch.setenv("LEADS_LEADGEN_SCOPE", str(tmp_path / "missing.json"))
    assert not live_scope.load_scope()
    assert not live_scope.task_allowed(payload)


def test_empty_search_reports_shell_diagnostics_instead_of_generic_advice(env, monkeypatch):
    """搜索拿不到视频时，错误必须说清「平台回了什么」，不能只让用户去看筛选记录。

    真机现场：探针同参数能拿到 3 条，preview 却报「没有可执行的真实动作」，
    skippedActions 为空、壳回传的 diag（页面视频链接 26 个）被丢弃 —— 用户无从定位。
    """
    db, payload, _ = env
    monkeypatch.setattr(browser, "execute", lambda cmd, **k: {
        "ok": True, "items": [], "note": "搜索页未解析到视频卡片",
        "diag": {"videoLinks": 26, "waitedMs": 8000, "sample": "页面原文不应外传"}})
    with pytest.raises(ValueError) as info:
        flow.preview(db, payload)
    message = str(info.value)
    assert "工作流搭建" in message and "0 条" in message
    assert "页面视频链接 26 个" in message
    assert "搜索页未解析到视频卡片" in message
    assert "请查看筛选" not in message
    assert "页面原文不应外传" not in message          # 诊断只带计数，不把页面文本带到界面
    assert not registry._ACCOUNT_LEASES


def test_missing_video_title_is_reported_as_page_problem_not_filter_reason(env, monkeypatch):
    """页面已核对为目标视频、但读不到标题时，必须归因到「页面读不到」。

    以前这种情况只报「没有可执行的真实动作；请查看筛选、历史去重和AI判断记录」，
    用户去改筛选条件怎么改都没用 —— 真正缺的是标题。
    """
    db, payload, _ = env
    def execute(cmd, **kwargs):
        if cmd["type"] == "search-videos":
            return {"ok": True, "items": [{"url": VIDEO, "title": ""}]}
        if cmd["type"] == "inspect-video":
            return {"ok": True, "item": {"url": VIDEO, "title": ""},
                    "diag": {"tries": 20, "titleMissing": True}}
        pytest.fail("preview must never send: " + cmd["type"])
    monkeypatch.setattr(browser, "execute", execute)
    with pytest.raises(ValueError) as info:
        flow.preview(db, payload)
    message = str(info.value)
    assert "未读到视频标题" in message
    assert "重试 20 次仍读不到标题" in message
    assert "页确认打开" in message or "视频页已确认打开" in message


def test_detail_page_without_title_never_overwrites_the_search_card_title(env, monkeypatch):
    """详情页读不到标题时，不能用空字符串盖掉搜索卡片已经拿到的标题。

    {**candidate, **item} 直接合并会把好标题擦成空串，后面所有标题过滤、
    AI 判定都跟着变成「没有标题」——这是真机里「指定视频永远跳过」的另一半原因。
    """
    db, payload, _ = env
    payload = {**payload, "minVideoLike": 5}
    def execute(cmd, **kwargs):
        if cmd["type"] == "search-videos":
            return {"ok": True, "items": [{"url": VIDEO, "title": "工作流搭建教程"}]}
        if cmd["type"] == "inspect-video":
            return {"ok": True, "item": {"url": VIDEO, "title": "", "likeCount": "128"},
                    "diag": {"tries": 20, "titleMissing": True}}
        if cmd["type"] == "read-comments":
            return {"ok": True, "comments": [dict(COMMENT)]}
        pytest.fail("preview must never send: " + cmd["type"])
    monkeypatch.setattr(browser, "execute", execute)
    result = flow.preview(db, payload)
    main = [a for a in result["actions"] if a["type"] == "post-comment"]
    assert main and main[0]["title"] == "工作流搭建教程"


def test_empty_search_reports_shell_diagnostics_instead_of_generic_advice(env, monkeypatch):
    """搜索拿不到视频时，错误必须说清「平台回了什么」，不能只让用户去看筛选记录。

    真机现场：探针同参数能拿到 3 条，preview 却报「没有可执行的真实动作」，
    skippedActions 为空、壳回传的 diag（页面视频链接 26 个）被丢弃 —— 用户无从定位。
    """
    db, payload, _ = env
    monkeypatch.setattr(browser, "execute", lambda cmd, **k: {
        "ok": True, "items": [], "note": "搜索页未解析到视频卡片",
        "diag": {"videoLinks": 26, "waitedMs": 8000, "sample": "页面原文不应外传"}})
    with pytest.raises(ValueError) as info:
        flow.preview(db, payload)
    message = str(info.value)
    assert "工作流搭建" in message and "0 条" in message
    assert "页面视频链接 26 个" in message
    assert "搜索页未解析到视频卡片" in message
    assert "请查看筛选" not in message
    assert "页面原文不应外传" not in message          # 诊断只带计数，不把页面文本带到界面
    assert not registry._ACCOUNT_LEASES


def test_missing_video_title_is_reported_as_page_problem_not_filter_reason(env, monkeypatch):
    """页面已核对为目标视频、但读不到标题时，必须归因到「页面读不到」。

    以前这种情况只报「没有可执行的真实动作；请查看筛选、历史去重和AI判断记录」，
    用户去改筛选条件怎么改都没用 —— 真正缺的是标题。
    """
    db, payload, _ = env
    def execute(cmd, **kwargs):
        if cmd["type"] == "search-videos":
            return {"ok": True, "items": [{"url": VIDEO, "title": ""}]}
        if cmd["type"] == "inspect-video":
            return {"ok": True, "item": {"url": VIDEO, "title": ""},
                    "diag": {"tries": 20, "titleMissing": True}}
        pytest.fail("preview must never send: " + cmd["type"])
    monkeypatch.setattr(browser, "execute", execute)
    with pytest.raises(ValueError) as info:
        flow.preview(db, payload)
    message = str(info.value)
    assert "未读到视频标题" in message
    assert "重试 20 次仍读不到标题" in message
    assert "页确认打开" in message or "视频页已确认打开" in message


def test_detail_page_without_title_never_overwrites_the_search_card_title(env, monkeypatch):
    """详情页读不到标题时，不能用空字符串盖掉搜索卡片已经拿到的标题。

    {**candidate, **item} 直接合并会把好标题擦成空串，后面所有标题过滤、
    AI 判定都跟着变成「没有标题」——这是真机里「指定视频永远跳过」的另一半原因。
    """
    db, payload, _ = env
    payload = {**payload, "minVideoLike": 5}
    def execute(cmd, **kwargs):
        if cmd["type"] == "search-videos":
            return {"ok": True, "items": [{"url": VIDEO, "title": "工作流搭建教程"}]}
        if cmd["type"] == "inspect-video":
            return {"ok": True, "item": {"url": VIDEO, "title": "", "likeCount": "128"},
                    "diag": {"tries": 20, "titleMissing": True}}
        if cmd["type"] == "read-comments":
            return {"ok": True, "comments": [dict(COMMENT)]}
        pytest.fail("preview must never send: " + cmd["type"])
    monkeypatch.setattr(browser, "execute", execute)
    result = flow.preview(db, payload)
    main = [a for a in result["actions"] if a["type"] == "post-comment"]
    assert main and main[0]["title"] == "工作流搭建教程"


WORKS = [{"url": f"https://www.douyin.com/video/74000000000000001{i}",
          "title": f"主页作品{i}", "pinned": i == 0} for i in range(6)]


def warmup_execute(cmd, **kwargs):
    """养护/首作链路的只读桩：搜索、评论、主页资料与作品列表。"""
    if cmd["type"] == "search-videos":
        return {"ok": True, "items": [{"url": VIDEO, "title": "工作流搭建教程"}]}
    if cmd["type"] == "read-comments":
        return {"ok": True, "comments": [dict(COMMENT)]}
    if cmd["type"] == "inspect-profile":
        return {"ok": True, "item": {"userId": COMMENT["userId"], "nickname": COMMENT["nickname"],
                                     "gender": "female", "age": 30, "private": False}}
    if cmd["type"] == "read-author-works":
        assert cmd["authorUrls"] == ["https://www.douyin.com/user/" + COMMENT["userId"]]
        return {"ok": True, "items": [dict(w) for w in WORKS]}
    raise AssertionError("planning dispatched a write: " + cmd["type"])


def test_warmup_and_profile_first_actions_plan_before_dm(env, monkeypatch):
    """养护/主页首作：作品互动排在关注、私信之前；置顶作品被过滤。"""
    db, payload, _ = env
    monkeypatch.setattr(browser, "execute", warmup_execute)
    result = flow.preview(db, {**payload, "interactionLimitMin": 20, "interactionLimitMax": 20,
                               "enableFollow": True,
                               "enableWarmup": True, "warmupMin": 2, "warmupMax": 2,
                               "commentOnProfileFirstWork": True, "profileFirstWorkLikePercent": 100,
                               "profileFirstWorkCollectPercent": 0, "enableProfileFirstComment": True,
                               "profileFirstCommentFallbackMode": "skip"})
    assert result["success"]
    types = [a["type"] for a in result["actions"]]
    # 主评、回复之后：养护 2 作品 ×（点赞+收藏）=4，随后是首作与关注/私信
    assert types[:2] == ["post-comment", "reply-comment"]
    warm = [a for a in result["actions"] if a.get("keyword") == "主页养护"]
    assert len(warm) == 4 and all(a["profileTarget"] == "1" for a in warm)
    first = [a for a in result["actions"] if a.get("keyword") == "主页首作"]
    assert all(a["videoUrl"] == WORKS[1]["url"] for a in first)   # 置顶的 WORKS[0] 被过滤
    # 养护随机抽中的作品可能与首作重合：重合时首作点赞必须去重跳过（同键不重复互动）
    if WORKS[1]["url"] in {a["videoUrl"] for a in warm}:
        assert not [a for a in first if a["type"] == "like-video"]
        assert any(s["type"] == "like-video" and "已互动" in s["reason"] for s in result["skippedActions"])
    else:
        assert [a["type"] for a in first].count("like-video") == 1
    assert types.count("like-video") == 2 + len([a for a in first if a["type"] == "like-video"])
    assert types.count("favorite-video") == 2
    assert types[-2:] == ["follow-user", "send-dm"]
    comment = [a for a in first if a["type"] == "post-comment"]
    assert comment and comment[0]["browseUrl"] != WORKS[1]["url"]
    assert comment[0]["browseUrl"] in {w["url"] for w in WORKS}
    # 服务端提案保留完整动作，页面出口不出现 uid 级字段
    stored = stored_actions(db, result["proposalId"])
    assert all("profileTarget" in a for a in stored if a.get("keyword") == "主页养护")
    for action in result["actions"]:
        assert "commentUserId" not in action and "userId" not in action


def test_profile_first_skips_when_gender_filter_excludes(env, monkeypatch):
    db, payload, _ = env
    monkeypatch.setattr(browser, "execute", warmup_execute)
    result = flow.preview(db, {**payload, "interactionLimitMin": 20, "interactionLimitMax": 20,
                               "enableFollow": True, "enableWarmup": True, "commentOnProfileFirstWork": True,
                               "profileFirstGenderFilter": "male"})
    assert result["success"]
    assert not [a for a in result["actions"] if a.get("keyword") in {"主页养护", "主页首作"}]
    assert any(s["type"] == "profile-warmup" and "性别" in s["reason"]
               for s in result["skippedActions"])
    # 关注与私信本身不受首作性别筛选影响（那是 DM 专属 targetGender 的职责）
    assert [a["type"] for a in result["actions"]][-2:] == ["follow-user", "send-dm"]


def test_no_first_work_reply_fallback_plans_reply_comment(env, monkeypatch):
    """无首作 + 兜底「无首作回评」：改为对原评论生成一条回复。"""
    db, payload, _ = env
    def execute(cmd, **kwargs):
        if cmd["type"] == "read-author-works":
            return {"ok": True, "items": []}
        return warmup_execute(cmd, **kwargs)
    monkeypatch.setattr(browser, "execute", execute)
    # 关掉常规回复：兜底回评与常规回复指向同一条评论，开着会被 action_key 去重拦下
    result = flow.preview(db, {**payload, "interactionLimitMin": 20, "interactionLimitMax": 20,
                               "enableFollow": True, "enableWarmup": True, "enableComment": False,
                               "commentOnProfileFirstWork": True,
                               "enableProfileFirstComment": True, "profileFirstCommentFallbackMode": "reply"})
    assert result["success"]
    types = [a["type"] for a in result["actions"]]
    assert types.count("reply-comment") == 1  # 无首作兜底回评
    assert types.count("post-comment") == 1   # 仅主评，无首作评论
    assert any(s["type"] == "profile-warmup" and "未读到可互动作品" in s["reason"]
               for s in result["skippedActions"])


def test_prepare_scope_carries_favorite_video_actions(env, monkeypatch, tmp_path):
    """养护/首作的收藏动作必须进作用域签名清单，否则壳侧会当成未授权动作。"""
    db, payload, _ = env
    monkeypatch.setattr(browser, "execute", warmup_execute)
    selected = flow.preview(db, {**payload, "interactionLimitMin": 20, "interactionLimitMax": 20,
                                 "enableFollow": True, "enableWarmup": True, "warmupMin": 2, "warmupMax": 2,
                                 "commentOnProfileFirstWork": True, "profileFirstWorkLikePercent": 100,
                                 "profileFirstWorkCollectPercent": 100, "enableProfileFirstComment": False})
    out = tmp_path / "scope.json"
    prepared = prepare(db, selected["proposalId"], selected["binding"], out)
    assert prepared["success"]
    scope = json.loads(out.read_text(encoding="utf-8"))
    assert scope["actions"].get("favorite-video", 0) >= 1
    assert scope["actions"].get("like-video", 0) >= 1
    assert {a["type"] for a in scope["reviewedActions"]} <= flow.ACTIONS
    assert all(a.get("profileTarget") == "1" for a in scope["reviewedActions"]
               if a.get("keyword") in {"主页养护", "主页首作"})
