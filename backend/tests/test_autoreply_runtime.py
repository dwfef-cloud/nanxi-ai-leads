"""自动回复运行时（私信链路）契约测试。

真机背景（2026-11-17）：
  * 抖音 PC 网页版没有互动通知页（/notifications、/message、/notification 全 404），
    但私信页 /chat?isPopup=1 可读：会话列表 [data-e2e=conversation-item]、
    正文 .MessageItemTextbubbleTextContent、方向 .MessageItemTextisFromMe、
    发送键 .messageMsgInputpublishBtn。
  * 所以自动回复先接通「私信」这一条真实链路，点赞/新增关注如实说明没有事件源。

这里覆盖的是「读到的数据怎么判、怎么回、怎么记账」，壳侧的页面选择器由
autoreply-dm.js 的契约断言兜住（真机验证另行跑）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from app.api import self_warmup
from app.execution import live_scope
from app.runtime import autoreply, browser, runners


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "ar.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    return path


def _ctx(tmp_path: Path, task_id: str = "ar-1", account_id: str = "acc-1") -> runners.Ctx:
    return runners.Ctx(kind="warmup", db_path=_db(tmp_path), task_id=task_id, name="测试自动回复",
                       account_id=account_id, run_id="run-1", trigger="test", engine="shell")


def _scope(monkeypatch, tmp_path: Path, ctx, count: int = 5) -> None:
    """给本轮私信动作准备一份真实的 monitor 作用域（壳侧要求 mode=monitor + HMAC）。"""
    monkeypatch.setenv("LEADS_MONITOR_SCOPE_DIR", str(tmp_path / "scopes"))
    live_scope.write_monitor_scope(task_id=ctx.task_id, account_id=ctx.account_id,
                                   actions={"send-dm-chat": count}, hours=1)


def _cfg(**kw) -> dict:
    base = {
        "taskMode": "auto-reply",
        "watchMessages": True,
        "actionMessageReply": True,
        "excludeGroupChats": True,
        "enableAutoActions": True,
        "dmTemplateItems": [{"text": "你好{nickname}，看到你的消息了"}],
        "intervalSecondsMin": 20,
        "intervalSecondsMax": 60,
        "dmMessageIntervalMin": 2,
        "dmMessageIntervalMax": 3,
    }
    base.update(kw)
    return base


_REAL_ACTION_DELAY = autoreply._action_delay


@pytest.fixture(autouse=True)
def _skip_action_delay(monkeypatch):
    """私信发送前的「每次自动操作前等待」默认 20-30 秒，测试里直接跳过。"""
    monkeypatch.setattr(autoreply, "_action_delay", lambda *a, **k: None)


def _conversations(*rows) -> dict:
    return {"ok": True, "conversations": [
        {"title": t, "time": tm, "preview": pv} for t, tm, pv in rows]}


def _threads(*threads) -> dict:
    return {"ok": True, "threads": list(threads)}


def test_event_table_maps_into_frontend_payload(tmp_path):
    ctx = _ctx(tmp_path)
    autoreply._event(ctx.db_path, task_id=ctx.task_id, account_id="acc-1", source="dm",
                     conversation="小美", nickname="小美", content="在吗",
                     outcome="acted", note="命中关键词：价格", actions=[{"ok": True}])
    out = self_warmup.list_events(ctx.db_path, {"taskId": ctx.task_id})
    assert out["success"] is True
    assert out["sourceAvailable"] is True
    assert out["total"] == 1
    event = out["events"][0]
    assert event["eventType"] == "message"
    assert event["nickname"] == "小美"
    assert event["status"] == "success"
    assert event["outcome"] == "acted"


def test_outcome_status_mapping_covers_every_branch():
    mapping = self_warmup._OUTCOME_STATUS
    for outcome in ("acted", "matched", "filtered", "dedup", "skipped", "failed"):
        assert outcome in mapping
    assert mapping["acted"] == "success"
    assert mapping["failed"] == "failed"


def test_conversation_fingerprint_skips_unchanged_rounds(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    frames = []

    def fake_execute(cmd, **kwargs):
        frames.append(cmd["type"])
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", "在吗"))
        return _threads({"title": "小美", "found": True, "isGroup": False,
                         "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]})

    monkeypatch.setattr(browser, "execute", fake_execute)
    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        first = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True,
                                   86400, 50, {})
        second = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True,
                                    86400, 50, {})
    finally:
        con.close()
    assert first["scanned"] == 1
    # 第二轮会话指纹没变：不再读会话消息
    assert second["scanned"] == 0
    assert frames.count("read-dm-threads") == 1


def test_lookback_never_gates_private_messages(tmp_path, monkeypatch):
    """页面原话「私信不受影响」：回溯窗口只约束通知，私信老会话照样要回。"""
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    sent = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("老客户", "昨天 12:30", "还在吗"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "老客户", "found": True, "isGroup": False,
                             "messages": [{"time": "昨天 12:30", "fromMe": False,
                                           "text": "还在吗"}]})
        sent.append(cmd)
        return {"ok": True, "status": "sent_ok", "code": "message_visible_in_conversation"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True,
                                 0, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got["scanned"] == 1
    assert got["sent"] == 1, "回溯窗口为 0 时私信仍应发送（私信不受该窗口限制）"
    assert len(sent) == 1


def test_group_chat_is_skipped(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    sent = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("行业交流群", "10:00", "有人吗"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "行业交流群", "found": True, "isGroup": True,
                             "headTitle": "行业交流群(305)",
                             "messages": [{"time": "10:00", "fromMe": False, "text": "有人吗"}]})
        sent.append(cmd)
        return {"ok": True}

    monkeypatch.setattr(browser, "execute", fake_execute)
    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got["sent"] == 0
    assert sent == []
    rows = sqlite3.connect(ctx.db_path).execute(
        "SELECT outcome, note FROM autoreply_events").fetchall()
    assert rows and rows[0][0] == "skipped"
    assert "群聊" in rows[0][1]


def test_last_message_from_me_does_not_reply(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    sent = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", "好的"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "小美", "found": True, "isGroup": False,
                             "messages": [{"time": "09:00", "fromMe": False, "text": "在吗"},
                                          {"time": "10:00", "fromMe": True, "text": "好的"}]})
        sent.append(cmd)
        return {"ok": True}

    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got["sent"] == 0
    assert sent == []


def test_duplicate_message_is_not_replied_twice(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    sent = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", "在吗"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "小美", "found": True, "isGroup": False,
                             "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]})
        sent.append(cmd)
        return {"ok": True}

    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True, 86400, 50,
                           {"send-dm-chat": 10})
        # 换一个「新」的会话指纹，逼它再读一次同一条消息
        con.execute("UPDATE autoreply_conversations SET fingerprint='x'")
        con.commit()
        second = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True, 86400, 50,
                                    {"send-dm-chat": 10})
    finally:
        con.close()
    assert second["sent"] == 0
    labels = [row[0] for row in sqlite3.connect(ctx.db_path).execute(
        "SELECT outcome FROM autoreply_events").fetchall()]
    assert "dedup" in labels


def test_successful_send_records_quota_and_user_rounds(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    executed = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", "在吗"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "小美", "found": True, "isGroup": False,
                             "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]})
        executed.append(cmd)
        return {"ok": True, "status": "sent_ok", "code": "message_visible_in_conversation"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
        rounds = con.execute("SELECT rounds FROM autoreply_users").fetchall()
    finally:
        con.close()
    assert got["sent"] == 1
    assert executed and executed[0]["type"] == "send-dm-chat"
    assert executed[0]["commentAuthor"] == "小美"
    assert executed[0]["text"] == "你好小美，看到你的消息了"
    assert [r[0] for r in rounds] == [1]
    from app.runtime import monitor_engine

    assert monitor_engine.quota_used(ctx.db_path, "acc-1", "send-dm", _cfg()) == 1


def test_uncertain_send_is_not_counted_as_success(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", "在吗"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "小美", "found": True, "isGroup": False,
                             "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]})
        return {"ok": False, "status": "uncertain", "code": "message_not_visible_no_retry"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got["sent"] == 0
    row = sqlite3.connect(ctx.db_path).execute(
        "SELECT outcome, note FROM autoreply_events").fetchone()
    assert row[0] == "skipped"
    assert "message_not_visible_no_retry" in row[1]


def test_send_requires_reply_switch_on(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    executed = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", "在吗"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "小美", "found": True, "isGroup": False,
                             "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]})
        executed.append(cmd)
        return {"ok": True}

    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, _cfg(actionMessageReply=False), con,
                                 "acc-1", True, 86400, 50, {})
    finally:
        con.close()
    assert got["sent"] == 0
    assert executed == []
    row = sqlite3.connect(ctx.db_path).execute(
        "SELECT outcome, note FROM autoreply_events").fetchone()
    assert row[0] == "matched"
    assert "未勾选" in row[1]


def test_judge_excludes_listed_user_and_requires_keyword():
    ok, reason = autoreply._judge(Path(":memory:"), _cfg(excludeUsers="小美"), "小美", "在吗")
    assert ok is False and "排除" in reason
    ok, reason = autoreply._judge(Path(":memory:"), _cfg(keywords="价格,怎么买"), "小美", "在吗")
    assert ok is False and "关键词" in reason
    ok, reason = autoreply._judge(Path(":memory:"), _cfg(keywords="价格"), "小美", "多少钱？价格呢")
    assert ok is True and "命中关键词" in reason


def test_dm_text_renders_placeholders_and_rotates():
    cfg = _cfg()
    text = autoreply._dm_text(Path(":memory:"), cfg, "acc-1", "小美", "在吗")
    assert text == "你好小美，看到你的消息了"
    assert autoreply._dm_text(Path(":memory:"), _cfg(dmTemplateItems=[]), "acc-1", "小美", "在吗") == ""


def test_send_dm_chat_scope_is_signed_and_verifiable(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_MONITOR_SCOPE_DIR", str(tmp_path / "scopes"))
    live_scope.write_monitor_scope(task_id="ar-1", account_id="acc-1",
                                   actions={"send-dm-chat": 3}, hours=1, nickname="我")
    cmd = live_scope.signed_command("send-dm-chat", account_id="acc-1", task_id="ar-1",
                                    video_url="chat", text="你好", comment_author="小美")
    assert cmd["type"] == "send-dm-chat"
    assert cmd["commentAuthor"] == "小美"
    assert cmd["scopeId"]
    assert live_scope.command_allowed(cmd, "acc-1") is True
    # 改正文即失效：签名绑定会话标题与正文
    broken = dict(cmd, text="换一句")
    assert live_scope.command_allowed(broken, "acc-1") is False


def test_send_dm_chat_scope_rejects_missing_action(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_MONITOR_SCOPE_DIR", str(tmp_path / "scopes2"))
    live_scope.write_monitor_scope(task_id="ar-2", account_id="acc-1",
                                   actions={"reply-comment": 3}, hours=1)
    with pytest.raises(ValueError):
        live_scope.signed_command("send-dm-chat", account_id="acc-1", task_id="ar-2",
                                  video_url="chat", text="你好", comment_author="小美")


def test_registry_routes_auto_reply_to_its_own_runner(tmp_path, monkeypatch):
    """自动回复任务不再被 registry 拒绝，也不会被当成养号去刷推荐流。"""
    from app.api import task_store
    from app.runtime import registry

    db = _db(tmp_path)
    task_store.save_task(db, "warmup", {
        "id": "ar-9", "name": "自动回复测试",
        "configSnapshot": _cfg(taskMode="nurture", watchMessages=True, dmReplyMode="general"),
    })
    monkeypatch.setattr(browser, "status", lambda _db: {"engine": "shell"})
    monkeypatch.setattr(runners, "resolve_account", lambda _db, _rec, _cfg: "acc-1")
    started = {}
    monkeypatch.setattr(runners, "run_auto_reply", lambda db_path, rec, ctx: started.update(
        {"task": ctx.task_id}))
    monkeypatch.setattr(runners, "run_warmup", lambda *a, **k: started.update({"warmup": True}))

    out = registry.start(db, "warmup", "ar-9", trigger="test")
    assert out.get("success") is True, out
    registry.wait_idle(db, 3)
    assert started.get("task") == "ar-9"


def test_auto_reply_config_detection():
    assert runners.is_auto_reply_config(_cfg()) is True
    assert runners.is_auto_reply_config({"taskMode": "nurture", "scrolls": 3}) is False


def test_shell_module_contract():
    """壳侧发送模块的硬约束：必须验签、必须拦群聊、必须要求 scope.mode=monitor。"""
    root = Path(__file__).resolve().parents[2]
    source = (root / "desktop" / "autoreply-dm.js").read_text(encoding="utf-8")
    assert 'const ACTION = "send-dm-chat"' in source
    assert 'scope.mode !== "monitor"' in source
    assert "timingSafeEqual" in source
    assert "group_chat_skipped" in source
    assert "existing_draft_preserved" in source
    assert "message_visible_in_conversation" in source
    assert "attempt_consumed" in source
    agent = (root / "desktop" / "douyin_agent.js").read_text(encoding="utf-8")
    assert 'cmd.type === "send-dm-chat"' in agent
    assert 'cmd.type === "read-dm-conversations"' in agent
    assert 'cmd.type === "read-dm-threads"' in agent


def test_desktop_offline_failure_is_retried_next_round(tmp_path, monkeypatch):
    """桌面端不在线不算「处理过」：下一条轮次还要能再试，否则一次掉线就永久丢消息。"""
    ctx = _ctx(tmp_path)
    attempts = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", "在吗"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "小美", "found": True, "isGroup": False,
                             "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]})
        attempts.append(cmd)
        return {"ok": False, "code": "desktop_required", "status": "skipped",
                "error": "真机验收必须使用当前桌面账号会话"}

    _scope(monkeypatch, tmp_path, ctx)
    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True, 86400, 50,
                           {"send-dm-chat": 10})
        con.execute("UPDATE autoreply_conversations SET fingerprint='y'")
        con.commit()
        second = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True, 86400, 50,
                                    {"send-dm-chat": 10})
    finally:
        con.close()
    assert len(attempts) == 2          # 第二轮又试了一次
    assert second["sent"] == 0
    outcomes = [row[0] for row in sqlite3.connect(ctx.db_path).execute(
        "SELECT outcome FROM autoreply_events").fetchall()]
    assert outcomes.count("skipped") == 2
    assert "dedup" not in outcomes


def test_desktop_offline_failure_is_retried_next_round(tmp_path, monkeypatch):
    """桌面端不在线不算「处理过」：下一条轮次还要能再试，否则一次掉线就永久丢消息。"""
    ctx = _ctx(tmp_path)
    attempts = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", "在吗"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "小美", "found": True, "isGroup": False,
                             "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]})
        attempts.append(cmd)
        return {"ok": False, "code": "desktop_required", "status": "skipped",
                "error": "真机验收必须使用当前桌面账号会话"}

    _scope(monkeypatch, tmp_path, ctx)
    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    try:
        autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True, 86400, 50,
                           {"send-dm-chat": 10})
        con.execute("UPDATE autoreply_conversations SET fingerprint='y'")
        con.commit()
        second = autoreply._dm_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", True, 86400, 50,
                                    {"send-dm-chat": 10})
    finally:
        con.close()
    assert len(attempts) == 2          # 第二轮又试了一次
    assert second["sent"] == 0
    outcomes = [row[0] for row in sqlite3.connect(ctx.db_path).execute(
        "SELECT outcome FROM autoreply_events").fetchall()]
    assert outcomes.count("skipped") == 2
    assert "dedup" not in outcomes


# ---------------------------------------------------------------------------
# 作品评论链路（2026-11-17）：事件源 = 自己作品主页 + 视频页评论区
# ---------------------------------------------------------------------------
def _scope_comment(monkeypatch, tmp_path, ctx, count: int = 10, nickname: str = "AI学长") -> None:
    monkeypatch.setenv("LEADS_MONITOR_SCOPE_DIR", str(tmp_path / "scopes"))
    live_scope.write_monitor_scope(
        task_id=ctx.task_id, account_id=ctx.account_id,
        actions={"like-comment": count, "reply-comment": count,
                 "follow-user": count, "send-dm": count},
        hours=1, nickname=nickname)


def _works(*urls) -> dict:
    return {"ok": True, "author": "AI学长",
            "items": [{"videoId": u.rsplit("/", 1)[-1], "url": u, "title": "作品"} for u in urls]}


def _comments(*rows) -> dict:
    return {"ok": True, "comments": [dict(row) for row in rows]}


def _new_comment(**kw) -> dict:
    base = {"nickname": "小美", "text": "这个课程多少钱", "commentId": "7712345678901234567",
            "userId": "MS4wLjABAAAA", "videoId": "7700496842106146102",
            "publishedText": "1小时前",
            "publishedText": "1小时前",
            "videoUrl": "https://www.douyin.com/video/7700496842106146102",
            "caption": "生物智能体怎么做"}
    base.update(kw)
    return base


def _cfg_comment(**kw) -> dict:
    base = _cfg(watchComments=True, actionCommentLike=True, actionCommentReply=True,
                actionCommentFollow=False, actionCommentDm=False,
                commentSuggestionMode="template",
                replyTemplateItems=["谢谢{nickname}，价格私信你了"],
                actionDelayMin=1, actionDelayMax=1)
    base.update(kw)
    return base


def _run_comment(tmp_path, monkeypatch, *, cfg=None, comments=None, sent_record=None,
                 caps=None, used=None, action_result=None):
    ctx = _ctx(tmp_path)
    _scope_comment(monkeypatch, tmp_path, ctx)
    monkeypatch.setattr(autoreply, "_action_delay", lambda *_a, **_k: None)
    cfg = cfg or _cfg_comment()
    comments = comments if comments is not None else [_new_comment()]
    calls = sent_record if sent_record is not None else []

    def fake_execute(cmd, **kwargs):
        kind = cmd.get("type")
        if kind == "read-author-works":
            return _works("https://www.douyin.com/video/7700496842106146102")
        if kind == "read-comments":
            return _comments(*comments)
        calls.append(cmd)
        return dict(action_result or {"ok": True, "status": "sent_ok",
                                      "code": "state_transition_verified"})

    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx.db_path)
    caps = caps if caps is not None else {"like-comment": 5, "reply-comment": 5}
    used = used if used is not None else {"like-comment": 0, "reply-comment": 0}
    try:
        got = autoreply._comment_pass(ctx.db_path, ctx, cfg, con, ctx.account_id,
                                      86400, {}, caps, used, "AI学长")
    finally:
        con.close()
    return ctx, got, calls, used


def test_comment_pass_likes_and_replies_new_comment(tmp_path, monkeypatch):
    ctx, got, calls, used = _run_comment(tmp_path, monkeypatch)
    assert got["scanned"] == 1
    assert got["acted"] == 2, "点赞 + 回复两条动作都应执行"
    kinds = [c["type"] for c in calls]
    assert kinds == ["like-comment", "reply-comment"]
    assert calls[1]["text"] == "谢谢小美，价格私信你了"
    assert calls[0]["commentId"] == "7712345678901234567"
    assert used == {"like-comment": 1, "reply-comment": 1}
    events = self_warmup.list_events(ctx.db_path, {"taskId": ctx.task_id})["events"]
    acted = [e for e in events if e["status"] == "success"]
    assert acted and "点赞评论" in acted[0]["reason"]


def test_already_liked_is_skipped_not_reported_as_failure(tmp_path, monkeypatch):
    """目标本来就点过赞：是「无需动作」，不是失败。

    真机表现是每条重试都记一条 warn「未成功：already_liked」，
    运维看不出这和真出错有什么区别。
    """
    from app.api import task_logs

    cfg = _cfg_comment(actionCommentReply=False)
    ctx, got, calls, _ = _run_comment(
        tmp_path, monkeypatch, cfg=cfg, caps={"like-comment": 5}, used={"like-comment": 0},
        action_result={"ok": False, "status": "skipped", "code": "already_liked"})
    assert got["scanned"] == 1 and got["acted"] == 0
    assert [c["type"] for c in calls] == ["like-comment"]
    events = self_warmup.list_events(ctx.db_path, {"taskId": ctx.task_id})["events"]
    assert events and events[0]["status"] == "ignored", events   # skipped → ignored（前端口径）
    assert "未重复执行" in events[0]["reason"]
    logs = task_logs.list_for(ctx.db_path, ctx.task_id)
    assert not [row for row in logs if "未成功" in str(row.get("message"))], logs
    assert any("无需动作" in str(row.get("message")) for row in logs), logs


def test_comment_pass_ignores_own_comment(tmp_path, monkeypatch):
    ctx, got, calls, _ = _run_comment(tmp_path, monkeypatch,
                                      comments=[_new_comment(nickname="AI学长")])
    assert got["scanned"] == 0 and got["acted"] == 0
    assert calls == []


def test_comment_pass_dedupes_same_comment(tmp_path, monkeypatch):
    ctx, got, calls, _ = _run_comment(tmp_path, monkeypatch)
    assert got["acted"] == 2
    # 同一批评论第二轮：不再重复动作
    _scope_comment(monkeypatch, tmp_path, ctx)
    con = autoreply._connect(ctx.db_path)
    try:
        again = autoreply._comment_pass(ctx.db_path, ctx, _cfg_comment(), con, ctx.account_id,
                                        86400, {}, {"like-comment": 5, "reply-comment": 5},
                                        {"like-comment": 1, "reply-comment": 1}, "AI学长")
    finally:
        con.close()
    assert again["acted"] == 0
    assert len(calls) == 2, "去重后不应再发任何命令"


def test_comment_pass_respects_per_run_cap(tmp_path, monkeypatch):
    ctx, got, calls, used = _run_comment(
        tmp_path, monkeypatch, caps={"like-comment": 0, "reply-comment": 0},
        used={"like-comment": 0, "reply-comment": 0})
    assert got["acted"] == 2
    ctx2, got2, calls2, _ = _run_comment(
        tmp_path, monkeypatch, comments=[_new_comment(commentId="7712345678901234999", text="多少钱")],
        caps={"like-comment": 1, "reply-comment": 0}, used={"like-comment": 1, "reply-comment": 0})
    assert [c["type"] for c in calls2] == ["reply-comment"], "点赞已达上限，只应回评论"


def test_dm_ai_five_tries_and_never_falls_back_to_template(tmp_path, monkeypatch):
    """页面原话（私信侧）：「本轮最多尝试 5 次，不降级固定话术」。

    以前只试 1 次就回落模板池 —— 模板里明明配着话术，用户会以为 AI 在回。
    """
    ctx = _ctx(tmp_path)
    from app.runtime import monitor_engine

    monkeypatch.setattr(autoreply.time, "sleep", lambda *_: None)
    calls = []
    monkeypatch.setattr(monitor_engine, "dm_draft",
                        lambda *a, **k: calls.append(1) or {"ok": False, "reason": "模型超时"})
    cfg = _cfg(dmSuggestionMode="ai", dmTemplateItems=["模板私信"])

    assert autoreply._dm_text(ctx.db_path, cfg, "acc-1", "小美", "在吗") == ""
    assert len(calls) == autoreply.MAX_DM_AI_TRIES == 5


def test_dm_expression_applies_to_ai_and_template(tmp_path, monkeypatch):
    """页面：「文案末尾追加随机表情…AI 与固定话术均生效」（dmUseRandomSuffix）。"""
    ctx = _ctx(tmp_path)
    from app.runtime import monitor_engine

    monkeypatch.setattr(autoreply.time, "sleep", lambda *_: None)
    monkeypatch.setattr(monitor_engine, "dm_draft",
                        lambda *a, **k: {"ok": True, "texts": ["AI 私信"]})
    tail = ("😊", "🙌", "✨")

    on = _cfg(dmSuggestionMode="template", dmTemplateItems=["模板私信"], dmUseRandomSuffix=True)
    seen = {autoreply._dm_text(ctx.db_path, on, "acc-1", "小美", "在吗") for _ in range(40)}
    assert seen <= {"模板私信", *{"模板私信" + t for t in tail}}
    assert any(t in g for g in seen for t in tail)      # 至少若干次真的带上了表情

    ai_on = dict(on, dmSuggestionMode="ai")
    ai_seen = {autoreply._dm_text(ctx.db_path, ai_on, "acc-1", "小美", "在吗") for _ in range(40)}
    assert any(t in g for g in ai_seen for t in tail)   # AI 分支也要生效

    off = dict(on, dmUseRandomSuffix=False)
    assert autoreply._dm_text(ctx.db_path, off, "acc-1", "小美", "在吗") == "模板私信"


def test_comment_reply_ai_failure_never_falls_back_to_template(tmp_path, monkeypatch):
    """页面原话：AI 失败要记失败等下轮，不许换成固定话术。"""
    ctx = _ctx(tmp_path)
    from app.runtime import monitor_engine

    calls = []
    monkeypatch.setattr(monitor_engine, "reply_text",
                        lambda *a, **k: calls.append(1) or {"ok": False, "code": "ai_failed",
                                                            "reason": "模型超时"})
    draft = autoreply._comment_text(ctx.db_path, _cfg_comment(commentSuggestionMode="ai"),
                                    "acc-1", "小美", "多少钱", 0)
    assert draft["ok"] is False and draft["code"] == "ai_failed"
    assert len(calls) == autoreply.MAX_COMMENT_AI_TRIES == 5
    assert "5 次" in draft["reason"]


def test_reply_mode_reads_the_fields_the_page_actually_writes(tmp_path, monkeypatch):
    """评论/私信的 AI-模板开关必须读页面真实字段。

    2026-11-17 实测踩坑：后端读 replyMode（前端不存在）与 dmSendMode
    （那是「每次一条 / 连续多条」），结果是评论恒走 AI、私信恒走模板，
    用户在选择器上点什么都没用。
    """
    ctx = _ctx(tmp_path)
    from app.runtime import monitor_engine

    ai_calls = []
    monkeypatch.setattr(monitor_engine, "reply_text",
                        lambda *a, **k: ai_calls.append(1) or {"ok": True, "text": "AI 生成"})
    monkeypatch.setattr(monitor_engine, "dm_draft",
                        lambda *a, **k: ai_calls.append(1) or {"ok": True, "texts": ["AI 私信"]})

    # 选模板 → 必须走模板，不能被 AI 抢
    tpl = _cfg_comment(commentSuggestionMode="template",
                       replyTemplateItems=["模板回复"])
    assert autoreply._comment_text(ctx.db_path, tpl, "acc-1", "小美", "在吗", 0)["text"] \
        == "模板回复"
    assert not ai_calls

    # 选 AI → 必须走 AI
    ai = _cfg_comment(commentSuggestionMode="ai", replyTemplateItems=["模板回复"])
    assert autoreply._comment_text(ctx.db_path, ai, "acc-1", "小美", "在吗", 0)["text"] \
        == "AI 生成"
    assert ai_calls

    # 私信同理：dmSendMode 只是 rotate/sequence，不该影响 AI-模板判断
    dm_ai = dict(_cfg(dmSuggestionMode="ai", dmSendMode="sequence",
                      dmTemplateItems=["模板私信"]))
    assert autoreply._dm_text(ctx.db_path, dm_ai, "acc-1", "小美", "在吗") == "AI 私信"
    dm_tpl = dict(_cfg(dmSuggestionMode="template", dmSendMode="rotate",
                       dmTemplateItems=["模板私信"]))
    assert autoreply._dm_text(ctx.db_path, dm_tpl, "acc-1", "小美", "在吗") == "模板私信"

    # 两个分字段都没配时才回落到 suggestionMode
    assert autoreply._reply_mode({"suggestionMode": "ai"}, "comment") == "ai"
    assert autoreply._reply_mode({"suggestionMode": "template"}, "dm") == "template"


def test_comment_template_rotates_line_by_line(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    cfg = _cfg_comment(replyTemplateItems=["第一条", "第二条"])
    got = [autoreply._comment_text(ctx.db_path, cfg, "acc-1", "小美", "在吗", i)["text"]
           for i in range(3)]
    assert got == ["第一条", "第二条", "第一条"]


def test_comment_expression_only_when_enabled(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    cfg = _cfg_comment(replyTemplateItems=["谢谢{nickname}"])
    plain = autoreply._comment_text(ctx.db_path, cfg, "acc-1", "小美", "在吗", 0)["text"]
    assert plain == "谢谢小美"
    from app.runtime import monitor_engine

    monkeypatch.setattr(monitor_engine, "_suffix", lambda text, on: text + "😊")
    decorated = autoreply._comment_text(ctx.db_path, _cfg_comment(
        replyTemplateItems=["谢谢{nickname}"], enableCommentExpression=True),
        "acc-1", "小美", "在吗", 0)["text"]
    assert decorated.endswith("😊")


def test_action_delay_uses_configured_range(tmp_path, monkeypatch):
    """每次自动操作前按 actionDelayMin/Max 随机等待（这里只看取的区间对不对）。"""
    # autouse fixture 把 _action_delay 换成了空实现，本测试要还原真身
    monkeypatch.setattr(autoreply, "_action_delay", _REAL_ACTION_DELAY)
    ctx = _ctx(tmp_path)
    seen = {}

    def fake_uniform(lo, hi):
        seen["lo"], seen["hi"] = lo, hi
        return 0.0          # 立刻到期，避免测试真的等 20 秒

    monkeypatch.setattr(autoreply.random, "uniform", fake_uniform)
    autoreply._action_delay({"actionDelayMin": 20, "actionDelayMax": 30}, ctx)
    assert (seen["lo"], seen["hi"]) == (20.0, 30.0)
    seen.clear()
    autoreply._action_delay({}, ctx)          # 未配置时按页面默认 20–30 秒
    assert (seen["lo"], seen["hi"]) == (20.0, 30.0)


def test_caps_and_reset_time_follow_config(tmp_path):
    cfg = {"autoLikeLimitEnabled": True, "autoLikeLimitMin": 5, "autoLikeLimitMax": 5,
           "autoLikeLimitEnabledX": 1, "actionCountAutoResetEnabled": True,
           "actionCountAutoResetTime": "03:30"}
    caps = autoreply._draw_caps(cfg, {"like-comment": 1})
    assert caps["like-comment"] == 5
    assert autoreply._next_reset_at(cfg) > apreply_now()
    assert autoreply._next_reset_at({"actionCountAutoResetEnabled": False}) == 0.0


def apreply_now() -> float:
    import time as _t

    return _t.time()


def test_comment_pass_skips_author_reply_and_system_rows(tmp_path, monkeypatch):
    """真机校准：作者自己的回复昵称是「昵称 作者」，还有「群聊 · xxx」系统行。"""
    ctx, got, calls, _ = _run_comment(tmp_path, monkeypatch, comments=[
        _new_comment(nickname="AI学长小马 作者", text="因为不好听",
                     commentId="7712345678901234001"),
        _new_comment(nickname="小美", text="群聊 · AI智能体教师交流群",
                     commentId="7712345678901234002"),
        _new_comment(),
    ])
    assert got["scanned"] == 1, "只有那条真实用户留言要处理"
    assert got["acted"] == 2
    assert len(calls) == 2


def test_comment_age_parses_absolute_date():
    """真机：评论时间常是「11月05日 20:42」，页面解析不认，后端必须认。"""
    import time as _t

    now = _t.mktime((2026, 11, 17, 21, 0, 0, 0, 0, -1))
    assert autoreply._comment_age_minutes("刚刚", now) == 0
    assert autoreply._comment_age_minutes("3小时前", now) == 180
    assert autoreply._comment_age_minutes("2天前", now) == 2880
    age = autoreply._comment_age_minutes("11月05日 20:42", now)
    assert age is not None and 11 * 24 * 60 <= age <= 13 * 24 * 60
    assert autoreply._comment_age_minutes("2026-11-16", now) is not None
    assert autoreply._comment_age_minutes("没有时间", now) is None


def test_comment_pass_drops_comments_outside_window(tmp_path, monkeypatch):
    ctx, got, calls, _ = _run_comment(
        tmp_path, monkeypatch,
        comments=[_new_comment(publishedText="11月05日 20:42", commentId="7712345678901234777")])
    assert got["scanned"] == 0 and got["acted"] == 0, "窗口外（24h）的老评论不该动作"
    assert calls == []


def test_comment_age_parses_absolute_date():
    """真机：评论时间常是「11月05日 20:42」，页面解析不认，后端必须认。"""
    import time as _t

    now = _t.mktime((2026, 11, 17, 21, 0, 0, 0, 0, -1))
    assert autoreply._comment_age_minutes("刚刚", now) == 0
    assert autoreply._comment_age_minutes("3小时前", now) == 180
    assert autoreply._comment_age_minutes("2天前", now) == 2880
    age = autoreply._comment_age_minutes("11月05日 20:42", now)
    assert age is not None and 11 * 24 * 60 <= age <= 13 * 24 * 60
    assert autoreply._comment_age_minutes("2026-11-16", now) is not None
    assert autoreply._comment_age_minutes("没有时间", now) is None


def test_comment_pass_drops_comments_outside_window(tmp_path, monkeypatch):
    ctx, got, calls, _ = _run_comment(
        tmp_path, monkeypatch,
        comments=[_new_comment(publishedText="11月05日 20:42", commentId="7712345678901234777")])
    assert got["scanned"] == 0 and got["acted"] == 0, "窗口外（24h）的老评论不该动作"
    assert calls == []


def test_like_comment_sends_comment_text_as_target(tmp_path, monkeypatch):
    """壳侧按「commentId + 作者 + 正文」三者核对目标，点赞命令必须带评论正文。"""
    ctx, got, calls, _ = _run_comment(tmp_path, monkeypatch)
    like = [c for c in calls if c["type"] == "like-comment"][0]
    assert like["text"] == "这个课程多少钱"
    assert like["commentAuthor"] == "小美"
    reply = [c for c in calls if c["type"] == "reply-comment"][0]
    assert reply["text"] != like["text"], "回复正文不能被评论正文占位"


# ---------------------------------------------------------------- 专业模式规则
# 页面口径（SelfWarmupDmProfessional）：
#   dmProfessionalMessages 里的每组有 mode（keyword / scene / sequence）；
#   dmProfessionalMatchOrder 为 "custom" 时数组顺序即优先级，否则固定按
#   scene（mutual_follow 先于 follow）→ keyword → sequence 排。
#   从上到下匹配、命中即停；sequence 是「首次触达兜底」，永远在最后。

def _rule(mode, *, id="r", scene=None, keywords="", peer="all", history="all", texts=None):
    rule = {"id": id, "mode": mode, "items": [{"text": t} for t in (texts or [])]}
    if scene:
        rule["scene"] = scene
    if keywords:
        rule["keywords"] = keywords
    if mode == "keyword":
        rule["peerFollowFilter"] = peer
    if mode == "scene":
        rule["keywordHistoryFilter"] = history
    return rule


def _prof(**kw):
    return _cfg(dmReplyMode="professional",
                dmProfessionalMessages=kw.pop("rules"), **kw)


def test_professional_default_order_scene_then_keyword_then_fallback():
    rules = [
        _rule("sequence", id="s1", texts=["兜底"]),
        _rule("keyword", id="k1", keywords="价格", texts=["关键词组"]),
        _rule("scene", id="c1", scene="comment", texts=["场景组"]),
    ]
    cfg = _prof(rules=rules)          # 未配 matchOrder → 按页面固定顺序
    order = [r["id"] for r in autoreply._professional_rules(cfg)]
    assert order == ["c1", "k1", "s1"], "scene 在前、keyword 次之、首次触达固定最后"


def test_professional_custom_order_follows_array():
    rules = [
        _rule("keyword", id="k1", keywords="价格", texts=["关键词组"]),
        _rule("scene", id="c1", scene="comment", texts=["场景组"]),
        _rule("sequence", id="s1", texts=["兜底"]),
    ]
    cfg = _prof(rules=rules, dmProfessionalMatchOrder="custom")
    order = [r["id"] for r in autoreply._professional_rules(cfg)]
    assert order == ["k1", "c1", "s1"], "custom 就是数组顺序，上移下移只换数组位置"


def test_professional_first_hit_stops():
    rules = [
        _rule("keyword", id="k1", keywords="价格", texts=["第一组"]),
        _rule("keyword", id="k2", keywords="价格、多少钱", texts=["第二组"]),
    ]
    cfg = _prof(rules=rules, dmProfessionalMatchOrder="custom")
    rule, why = autoreply._match_professional_rule(cfg, content="这个价格多少")
    assert rule["id"] == "k1" and "第 1 组" in why


def test_professional_keywords_split_by_comma_and_newline():
    rule = _rule("keyword", keywords="价格，多少钱、费用\n报价;优惠", texts=["x"])
    assert autoreply._split_keywords(rule["keywords"]) == \
        ["价格", "多少钱", "费用", "报价", "优惠"]
    cfg = _prof(rules=[rule])
    assert autoreply._match_professional_rule(cfg, content="能给个优惠吗")[0] is not None


def test_professional_ignore_case_defaults_on():
    rule = _rule("keyword", keywords="price", texts=["x"])
    cfg = _prof(rules=[rule])
    assert autoreply._match_professional_rule(cfg, content="what PRICE")[0] is not None
    rule["ignoreCase"] = False
    assert autoreply._match_professional_rule(
        _prof(rules=[rule]), content="what PRICE")[0] is None


def test_professional_fallback_only_when_never_touched():
    """首次触达：同一任务同一账号此前尚未自动触达该用户时才发。"""
    cfg = _prof(rules=[_rule("sequence", id="s1", texts=["兜底"])])
    hit, why = autoreply._match_professional_rule(cfg, content="在吗", touched=False)
    assert hit is not None and "首次触达" in why
    miss, why2 = autoreply._match_professional_rule(cfg, content="在吗", touched=True)
    assert miss is None and "未命中" in why2


def test_professional_no_fallback_configured_sends_nothing():
    """页面原话：未配置首次触达时，前面的规则都未命中则不发送。"""
    cfg = _prof(rules=[_rule("keyword", id="k1", keywords="价格", texts=["x"])])
    rule, why = autoreply._match_professional_rule(
        cfg, content="随便聊聊", touched=False)
    assert rule is None and "没有可用的首次触达" in why


def test_professional_peer_follow_unknown_never_matches():
    """「对方已关注本账号」这类条件，判断不了就不发 —— 宁可这轮不动。"""
    cfg = _prof(rules=[_rule("keyword", keywords="价格", peer="followed", texts=["x"])])
    assert autoreply._match_professional_rule(
        cfg, content="价格", followed=None)[0] is None
    assert autoreply._match_professional_rule(
        cfg, content="价格", followed=True)[0] is not None


def test_professional_scene_rule_needs_matching_event():
    cfg = _prof(rules=[
        _rule("scene", id="c1", scene="like", texts=["点赞场景"]),
        _rule("sequence", id="s1", texts=["兜底"]),
    ])
    hit, _ = autoreply._match_professional_rule(cfg, content="", scene="like", touched=False)
    assert hit["id"] == "c1"
    other, _ = autoreply._match_professional_rule(cfg, content="", scene="comment", touched=False)
    assert other["id"] == "s1", "场景不匹配就落到后面的兜底"


def test_professional_text_comes_from_hit_group_only():
    cfg = _prof(rules=[
        _rule("keyword", id="k1", keywords="价格", texts=["这组的话"]),
        _rule("sequence", id="s1", texts=["兜底的话"]),
    ], dmUseRandomSuffix=False)
    text = autoreply._dm_text(Path("/tmp/x.db"), cfg, "acc-1", "小美", "价格多少")
    assert text == "这组的话", "命中哪组就用哪组的消息集合，不能串到别的组"


def test_professional_expression_applies_to_rule_text():
    cfg = _prof(rules=[_rule("sequence", id="s1", texts=["兜底"])],
                dmUseRandomSuffix=True)
    samples = {autoreply._dm_text(Path("/tmp/x.db"), cfg, "acc-1", "小美", "在吗")
               for _ in range(60)}
    assert "兜底" in samples, "有无表情都算命中"
    assert any(s != "兜底" for s in samples), "开关打开时应当出现带表情的版本"


# ---------------------------------------------------------------- 专业模式规则
# 页面口径（SelfWarmupDmProfessional）：
#   dmProfessionalMessages 里的每组有 mode（keyword / scene / sequence）；
#   dmProfessionalMatchOrder 为 "custom" 时数组顺序即优先级，否则固定按
#   scene（mutual_follow 先于 follow）→ keyword → sequence 排。
#   从上到下匹配、命中即停；sequence 是「首次触达兜底」，永远在最后。

def _rule(mode, *, id="r", scene=None, keywords="", peer="all", history="all", texts=None):
    rule = {"id": id, "mode": mode, "items": [{"text": t} for t in (texts or [])]}
    if scene:
        rule["scene"] = scene
    if keywords:
        rule["keywords"] = keywords
    if mode == "keyword":
        rule["peerFollowFilter"] = peer
    if mode == "scene":
        rule["keywordHistoryFilter"] = history
    return rule


def _prof(**kw):
    return _cfg(dmReplyMode="professional",
                dmProfessionalMessages=kw.pop("rules"), **kw)


def test_professional_default_order_scene_then_keyword_then_fallback():
    rules = [
        _rule("sequence", id="s1", texts=["兜底"]),
        _rule("keyword", id="k1", keywords="价格", texts=["关键词组"]),
        _rule("scene", id="c1", scene="comment", texts=["场景组"]),
    ]
    cfg = _prof(rules=rules)          # 未配 matchOrder → 按页面固定顺序
    order = [r["id"] for r in autoreply._professional_rules(cfg)]
    assert order == ["c1", "k1", "s1"], "scene 在前、keyword 次之、首次触达固定最后"


def test_professional_custom_order_follows_array():
    rules = [
        _rule("keyword", id="k1", keywords="价格", texts=["关键词组"]),
        _rule("scene", id="c1", scene="comment", texts=["场景组"]),
        _rule("sequence", id="s1", texts=["兜底"]),
    ]
    cfg = _prof(rules=rules, dmProfessionalMatchOrder="custom")
    order = [r["id"] for r in autoreply._professional_rules(cfg)]
    assert order == ["k1", "c1", "s1"], "custom 就是数组顺序，上移下移只换数组位置"


def test_professional_first_hit_stops():
    rules = [
        _rule("keyword", id="k1", keywords="价格", texts=["第一组"]),
        _rule("keyword", id="k2", keywords="价格、多少钱", texts=["第二组"]),
    ]
    cfg = _prof(rules=rules, dmProfessionalMatchOrder="custom")
    rule, why = autoreply._match_professional_rule(cfg, content="这个价格多少")
    assert rule["id"] == "k1" and "第 1 组" in why


def test_professional_keywords_split_by_comma_and_newline():
    rule = _rule("keyword", keywords="价格，多少钱、费用\n报价;优惠", texts=["x"])
    assert autoreply._split_keywords(rule["keywords"]) == \
        ["价格", "多少钱", "费用", "报价", "优惠"]
    cfg = _prof(rules=[rule])
    assert autoreply._match_professional_rule(cfg, content="能给个优惠吗")[0] is not None


def test_professional_ignore_case_defaults_on():
    rule = _rule("keyword", keywords="price", texts=["x"])
    cfg = _prof(rules=[rule])
    assert autoreply._match_professional_rule(cfg, content="what PRICE")[0] is not None
    rule["ignoreCase"] = False
    assert autoreply._match_professional_rule(
        _prof(rules=[rule]), content="what PRICE")[0] is None


def test_professional_fallback_only_when_never_touched():
    """首次触达：同一任务同一账号此前尚未自动触达该用户时才发。"""
    cfg = _prof(rules=[_rule("sequence", id="s1", texts=["兜底"])])
    hit, why = autoreply._match_professional_rule(cfg, content="在吗", touched=False)
    assert hit is not None and "首次触达" in why
    miss, why2 = autoreply._match_professional_rule(cfg, content="在吗", touched=True)
    assert miss is None and "未命中" in why2


def test_professional_no_fallback_configured_sends_nothing():
    """页面原话：未配置首次触达时，前面的规则都未命中则不发送。"""
    cfg = _prof(rules=[_rule("keyword", id="k1", keywords="价格", texts=["x"])])
    rule, why = autoreply._match_professional_rule(
        cfg, content="随便聊聊", touched=False)
    assert rule is None and "没有可用的首次触达" in why


def test_professional_peer_follow_unknown_never_matches():
    """「对方已关注本账号」这类条件，判断不了就不发 —— 宁可这轮不动。"""
    cfg = _prof(rules=[_rule("keyword", keywords="价格", peer="followed", texts=["x"])])
    assert autoreply._match_professional_rule(
        cfg, content="价格", followed=None)[0] is None
    assert autoreply._match_professional_rule(
        cfg, content="价格", followed=True)[0] is not None


def test_professional_scene_rule_needs_matching_event():
    cfg = _prof(rules=[
        _rule("scene", id="c1", scene="like", texts=["点赞场景"]),
        _rule("sequence", id="s1", texts=["兜底"]),
    ])
    hit, _ = autoreply._match_professional_rule(cfg, content="", scene="like", touched=False)
    assert hit["id"] == "c1"
    other, _ = autoreply._match_professional_rule(cfg, content="", scene="comment", touched=False)
    assert other["id"] == "s1", "场景不匹配就落到后面的兜底"


def test_professional_text_comes_from_hit_group_only():
    cfg = _prof(rules=[
        _rule("keyword", id="k1", keywords="价格", texts=["这组的话"]),
        _rule("sequence", id="s1", texts=["兜底的话"]),
    ], dmUseRandomSuffix=False)
    text = autoreply._dm_text(Path("/tmp/x.db"), cfg, "acc-1", "小美", "价格多少")
    assert text == "这组的话", "命中哪组就用哪组的消息集合，不能串到别的组"


def test_professional_expression_applies_to_rule_text():
    cfg = _prof(rules=[_rule("sequence", id="s1", texts=["兜底"])],
                dmUseRandomSuffix=True)
    samples = {autoreply._dm_text(Path("/tmp/x.db"), cfg, "acc-1", "小美", "在吗")
               for _ in range(60)}
    assert "兜底" in samples, "有无表情都算命中"
    assert any(s != "兜底" for s in samples), "开关打开时应当出现带表情的版本"
