"""私信关键词规则 / 整组发送 / 跨账号去重 / 钉钉加签 契约测试。

对应清单（v21→v22）：
  #25 话术模式：dmReplyMode=rules（关键词规则）后端消费，字段口径取证自
      SelfWarmup 打包资源（trigger/matchMode/keywords/pattern/ignoreCase/
      replyEnabled/followEnabled/templateItems），不是猜的。
  #28 消息条数上限：每次发送整组算一次（配额、轮数、通用一次都在组级记账）。
  #29 连续消息间隔：整组多条发送，组内消息按 dmMessageInterval 随机间隔。
  #30 每次自动操作前等待：整组发送前统一走 _action_delay（组内消息走间隔）。
  #21 跨账号私信去重：dmCrossAccountDedupe 开启时同一任务内同一线索只由一个账号私信。
  #37 消息推送：钉钉加签（HMAC-SHA256 + base64，timestamp+\\n+secret）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app.execution import live_scope
from app.runtime import autoreply, browser, monitor_engine


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "ar.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    return path


def _ctx(tmp_path: Path, task_id: str = "ar-1", account_id: str = "acc-1") -> "object":
    from app.runtime import runners

    return runners.Ctx(kind="warmup", db_path=_db(tmp_path), task_id=task_id,
                       name="测试自动回复", account_id=account_id, run_id="run-1",
                       trigger="test", engine="shell")


def _scope(monkeypatch, tmp_path: Path, ctx, count: int = 5) -> None:
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
        "dmTemplateItems": [{"text": "通用回复{nickname}"}],
        "intervalSecondsMin": 20,
        "intervalSecondsMax": 60,
        "dmMessageIntervalMin": 2,
        "dmMessageIntervalMax": 3,
    }
    base.update(kw)
    return base


def _conversations(*rows) -> dict:
    return {"ok": True, "conversations": [
        {"title": t, "time": tm, "preview": pv} for t, tm, pv in rows]}


def _threads(*threads) -> dict:
    return {"ok": True, "threads": list(threads)}


@pytest.fixture(autouse=True)
def _skip_action_delay(monkeypatch):
    monkeypatch.setattr(autoreply, "_action_delay", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 模式判定（#25）
# ---------------------------------------------------------------------------
def test_reply_mode_rules_is_recognized():
    assert autoreply._reply_mode(_cfg(dmReplyMode="rules"), "dm") == "rules"
    assert autoreply._reply_mode(_cfg(dmReplyMode="professional"), "dm") == "professional"
    # general / 未配置时回落到 dmSuggestionMode 的 AI / 模板
    assert autoreply._reply_mode(_cfg(dmReplyMode="general"), "dm") == "template"
    assert autoreply._reply_mode(_cfg(dmReplyMode="general", dmSuggestionMode="ai"),
                                 "dm") == "ai"


# ---------------------------------------------------------------------------
# 规则匹配
# ---------------------------------------------------------------------------
def _rule(**kw) -> dict:
    base = {"id": "r1", "name": "价格", "trigger": "message", "matchMode": "keyword",
            "keywords": "价格,报价", "templateItems": [{"text": "你好{nickname}"}]}
    base.update(kw)
    return base


def test_dm_rule_keyword_match_and_case():
    cfg = _cfg(dmReplyMode="rules", dmReplyRules=[_rule()])
    rule, index = autoreply._match_dm_rule(cfg, content="多少钱？价格呢")
    assert rule is not None and index == 0
    rule, _ = autoreply._match_dm_rule(cfg, content="今天天气不错")
    assert rule is None
    # 忽略大小写按页面勾选框：勾了才忽略
    cfg_ci = _cfg(dmReplyMode="rules", dmReplyRules=[_rule(keywords="IPHONE", ignoreCase=True)])
    rule, _ = autoreply._match_dm_rule(cfg_ci, content="想买iphone")
    assert rule is not None
    cfg_cs = _cfg(dmReplyMode="rules", dmReplyRules=[_rule(keywords="IPHONE")])
    rule, _ = autoreply._match_dm_rule(cfg_cs, content="想买iphone")
    assert rule is None


def test_dm_rule_regex_match_and_invalid_pattern():
    cfg = _cfg(dmReplyMode="rules",
               dmReplyRules=[_rule(matchMode="regex", pattern=r"价格\s*\d+")])
    rule, _ = autoreply._match_dm_rule(cfg, content="这价格 1999 出吗")
    assert rule is not None
    rule, _ = autoreply._match_dm_rule(cfg, content="不要价格")
    assert rule is None
    # 非法正则不命中，也不把任务炸掉
    cfg_bad = _cfg(dmReplyMode="rules",
                   dmReplyRules=[_rule(matchMode="regex", pattern="价格([")])
    rule, _ = autoreply._match_dm_rule(cfg_bad, content="价格1999")
    assert rule is None


def test_dm_rule_follow_trigger_not_matched_by_dm_chain():
    cfg = _cfg(dmReplyMode="rules",
               dmReplyRules=[_rule(trigger="follow", keywords="价格")])
    rule, _ = autoreply._match_dm_rule(cfg, content="价格")
    assert rule is None


def test_rule_group_texts_one_line_per_box():
    cfg = _cfg(dmReplyMode="rules",
               dmReplyRules=[_rule(templateItems=[{"text": "A1\nA2"}, {"text": "B1"}])])
    rule, _ = autoreply._match_dm_rule(cfg, content="价格")
    texts = autoreply._rule_group_texts(cfg, rule, nickname="小美", content="价格")
    assert len(texts) == 2
    assert texts[0] in {"A1", "A2"}
    assert texts[1] == "B1"


def test_dm_text_rules_single_text_compat():
    cfg = _cfg(dmReplyMode="rules", dmReplyRules=[_rule()])
    text = autoreply._dm_text(Path(":memory:"), cfg, "acc-1", "小美", "多少钱 价格")
    assert text == "你好小美"
    # 未命中且兜底为「仅记录」→ 空
    assert autoreply._dm_text(Path(":memory:"), cfg, "acc-1", "小美", "天气不错") == ""
    # 未命中且兜底为「使用通用话术」→ 模板
    cfg_fb = _cfg(dmReplyMode="rules", dmRuleFallback="general", dmReplyRules=[_rule()])
    assert autoreply._dm_text(Path(":memory:"), cfg_fb, "acc-1", "小美", "天气不错") == "通用回复小美"


# ---------------------------------------------------------------------------
# 主链路：整组发送 + 记账口径（#25/#28/#29/#30）
# ---------------------------------------------------------------------------
def _dm_fixture(monkeypatch, ctx, cfg, *, conversations, threads, sends_ok=True):
    sends: list[dict] = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return conversations
        if cmd["type"] == "read-dm-threads":
            return threads
        sends.append(cmd)
        return {"ok": True, "status": "sent_ok", "code": "message_visible_in_conversation"} \
            if sends_ok else {"ok": False, "status": "uncertain", "code": "message_not_visible_no_retry"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    return sends


def test_dm_pass_rules_hit_sends_group_and_counts_one_round(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(dmReplyMode="rules", dmReplyRules=[_rule(
        templateItems=[{"text": "你好{nickname}"}, {"text": "第二框"}])])
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "多少钱 价格")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "多少钱 价格"}]}))
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
        rounds = con.execute("SELECT rule_id, rounds FROM autoreply_rule_rounds").fetchall()
        users = con.execute("SELECT rounds FROM autoreply_users").fetchall()
    finally:
        con.close()
    # 整组两条按序发送
    assert [c["text"] for c in sends] == ["你好小美", "第二框"]
    assert got["sent"] == 2
    # 整组算一轮：规则轮数 +1、每用户轮数 +1、配额只扣 1 次
    assert [(r[0], r[1]) for r in rounds] == [("r1", 1)]
    assert [u[0] for u in users] == [1]
    from app.runtime import monitor_engine

    assert monitor_engine.quota_used(ctx.db_path, "acc-1", "send-dm", cfg) == 1


def test_dm_pass_rules_fallback_skip_records_without_sending(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(dmReplyMode="rules", dmReplyRules=[_rule()])
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "今天天气不错")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "今天天气不错"}]}))
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got["sent"] == 0
    assert sends == []
    row = sqlite3.connect(ctx.db_path).execute(
        "SELECT outcome, note FROM autoreply_events").fetchone()
    assert row[0] == "matched"
    assert "未命中关键词规则" in row[1]


def test_dm_pass_rules_fallback_general_uses_template_once(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(dmReplyMode="rules", dmRuleFallback="general", dmReplyRules=[_rule()])
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "天气不错")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "天气不错"}]}))
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
        assert got["sent"] == 1
        assert sends[0]["text"] == "通用回复小美"
        # 第二轮：换了新消息仍未命中规则 → 通用话术对该用户已触发过（仅一次）
        sends.clear()
        monkeypatch.setattr(browser, "execute", lambda cmd, **kw: (
            {"ok": True, "conversations": [{"title": "小美", "time": "11:00", "preview": "第二条"}]}
            if cmd["type"] == "read-dm-conversations" else
            {"ok": True, "threads": [{"title": "小美", "found": True, "isGroup": False,
                                      "messages": [{"time": "11:00", "fromMe": False,
                                                    "text": "第二条消息"}]}]}
            if cmd["type"] == "read-dm-threads" else sends.append(cmd) or
            {"ok": True, "status": "sent_ok"}))
        got2 = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                  86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got2["sent"] == 0
    assert sends == []


def test_dm_pass_template_mode_general_once_per_user(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg()
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "在吗")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]}))
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
        assert got["sent"] == 1
        sends.clear()
        monkeypatch.setattr(browser, "execute", lambda cmd, **kw: (
            {"ok": True, "conversations": [{"title": "小美", "time": "11:00", "preview": "再问"}]}
            if cmd["type"] == "read-dm-conversations" else
            {"ok": True, "threads": [{"title": "小美", "found": True, "isGroup": False,
                                      "messages": [{"time": "11:00", "fromMe": False,
                                                    "text": "再问一句"}]}]}
            if cmd["type"] == "read-dm-threads" else sends.append(cmd) or
            {"ok": True, "status": "sent_ok"}))
        got2 = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                  86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got2["sent"] == 0
    assert sends == []
    row = sqlite3.connect(ctx.db_path).execute(
        "SELECT outcome, note FROM autoreply_events ORDER BY id DESC LIMIT 1").fetchone()
    assert "通用话术对该用户已自动触发过" in row[1]


def test_dm_pass_sequence_mode_sends_all_template_boxes(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(dmSendMode="sequence",
               dmTemplateItems=[{"text": "第一"}, {"text": "第二"}])
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "在吗")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]}))
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert [c["text"] for c in sends] == ["第一", "第二"]
    assert got["sent"] == 2


def test_dm_pass_rule_rounds_gate_per_rule(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(actionUserMaxRounds=1, dmReplyMode="rules",
               dmReplyRules=[_rule(id="r1", keywords="价格"),
                             _rule(id="r2", name="售后", keywords="售后")])
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "价格")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "价格"}]}))
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10})
        assert got["sent"] == 1
        # 第二条消息仍命中 r1 → 该规则已达轮数上限；但换 r2 能命中的消息不受 r1 影响
        monkeypatch.setattr(browser, "execute", lambda cmd, **kw: (
            {"ok": True, "conversations": [{"title": "小美", "time": "11:00", "preview": "价格2"}]}
            if cmd["type"] == "read-dm-conversations" else
            {"ok": True, "threads": [{"title": "小美", "found": True, "isGroup": False,
                                      "messages": [{"time": "11:00", "fromMe": False,
                                                    "text": "价格第二遍"}]}]}
            if cmd["type"] == "read-dm-threads" else sends.append(cmd) or
            {"ok": True, "status": "sent_ok"}))
        got2 = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                  86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got2["sent"] == 0
    row = sqlite3.connect(ctx.db_path).execute(
        "SELECT note FROM autoreply_events ORDER BY id DESC LIMIT 1").fetchone()
    assert "已达回复轮数上限" in row[0]


def test_dm_pass_action_delay_runs_once_per_group(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(dmSendMode="sequence",
               dmTemplateItems=[{"text": "第一"}, {"text": "第二"}])
    _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "在吗")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "在吗"}]}))
    calls: list[int] = []
    # 覆盖 autouse fixture：这里要真的数调用次数
    monkeypatch.setattr(autoreply, "_action_delay", lambda *a, **k: calls.append(1))
    con = autoreply._connect(ctx.db_path)
    try:
        autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                           86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    # 每次自动操作前等待：整组只等一次，组内消息走「连续消息间隔」
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 跨账号私信去重（#21）
# ---------------------------------------------------------------------------
def test_cross_account_dedupe_blocks_second_account(tmp_path, monkeypatch):
    cfg = _cfg(dmCrossAccountDedupe=True)
    ctx1 = _ctx(tmp_path, account_id="acc-1")
    _scope(monkeypatch, tmp_path, ctx1)
    sends: list[dict] = []
    state = {"account": "acc-1"}

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", f"消息-{state['account']}"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "小美", "found": True, "isGroup": False,
                             "messages": [{"time": "10:00", "fromMe": False,
                                           "text": f"消息-{state['account']}"}]})
        sends.append(cmd)
        return {"ok": True, "status": "sent_ok", "code": "message_visible_in_conversation"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx1.db_path)
    try:
        got1 = autoreply._dm_pass(ctx1.db_path, ctx1, cfg, con, "acc-1", True,
                                  86400, 50, {"send-dm-chat": 10})
        assert got1["sent"] == 1
        # 同一任务内第二个账号：线索已被 acc-1 占用 → 不发
        ctx2 = _ctx(tmp_path, account_id="acc-2")
        state["account"] = "acc-2"
        got2 = autoreply._dm_pass(ctx1.db_path, ctx2, cfg, con, "acc-2", True,
                                  86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got2["sent"] == 0
    assert len(sends) == 1
    row = sqlite3.connect(ctx1.db_path).execute(
        "SELECT note FROM autoreply_events ORDER BY id DESC LIMIT 1").fetchone()
    assert "跨账号私信去重" in row[0] and "acc-1" in row[0]


def test_cross_account_dedupe_off_allows_independent_accounts(tmp_path, monkeypatch):
    cfg = _cfg(dmCrossAccountDedupe=False)
    ctx1 = _ctx(tmp_path, account_id="acc-1")
    _scope(monkeypatch, tmp_path, ctx1)
    sends: list[dict] = []

    def fake_execute(cmd, **kwargs):
        if cmd["type"] == "read-dm-conversations":
            return _conversations(("小美", "10:00", "在吗"))
        if cmd["type"] == "read-dm-threads":
            return _threads({"title": "小美", "found": True, "isGroup": False,
                             "messages": [{"time": "10:00", "fromMe": False,
                                           "text": f"内容-{len(sends)}"}]})
        sends.append(cmd)
        return {"ok": True, "status": "sent_ok", "code": "message_visible_in_conversation"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    con = autoreply._connect(ctx1.db_path)
    try:
        got1 = autoreply._dm_pass(ctx1.db_path, ctx1, cfg, con, "acc-1", True,
                                  86400, 50, {"send-dm-chat": 10})
        ctx2 = _ctx(tmp_path, account_id="acc-2")
        _scope(monkeypatch, tmp_path, ctx2)
        got2 = autoreply._dm_pass(ctx1.db_path, ctx2, cfg, con, "acc-2", True,
                                  86400, 50, {"send-dm-chat": 10})
    finally:
        con.close()
    assert got1["sent"] == 1 and got2["sent"] == 1
    assert len(sends) == 2


# ---------------------------------------------------------------------------
# 通知链路的私信话术（规则优先，通用兜底；#32 跨通知共用一次）
# ---------------------------------------------------------------------------
def test_notice_dm_texts_follows_rule_priority(tmp_path):
    db = _db(Path(tmp_path))
    con = autoreply._connect(db)
    try:
        cfg = _cfg(dmReplyMode="rules", dmReplyRules=[
            _rule(id="m1", name="互关", trigger="mutual_follow",
                  templateItems=[{"text": "互关专属"}]),
            _rule(id="f1", name="普通关注", trigger="follow",
                  templateItems=[{"text": "普通关注"}])])
        # 确认互关 → 互关专用规则优先
        texts, rid, _ = autoreply._notice_dm_texts(
            db, con, cfg, task_id="ar-1", account_id="acc-1", scene="mutual_follow",
            followed=None, nickname="小美", content="关注了你", user_key="k1")
        assert texts == ["互关专属"] and rid == "m1"
        # 普通关注场景 → 普通关注规则
        texts, rid, _ = autoreply._notice_dm_texts(
            db, con, cfg, task_id="ar-1", account_id="acc-1", scene="follow_unfollowed",
            followed=None, nickname="小美", content="关注了你", user_key="k1")
        assert texts == ["普通关注"] and rid == "f1"
        # 点赞场景不走关注规则：页面原话「评论、点赞等触发的主动私信仍用通用话术」
        texts, rid, note = autoreply._notice_dm_texts(
            db, con, cfg, task_id="ar-1", account_id="acc-1", scene="like",
            followed=None, nickname="小美", content="赞了你的作品", user_key="k1")
        assert texts == ["通用回复小美"] and rid == "" and note == ""
    finally:
        con.close()


def test_notice_dm_texts_general_once_across_notices(tmp_path):
    db = _db(Path(tmp_path))
    con = autoreply._connect(db)
    try:
        cfg = _cfg()      # 模板模式（通用话术）
        autoreply._bump_user(con, "acc-1", "k1")
        con.commit()
        texts, _rid, why = autoreply._notice_dm_texts(
            db, con, cfg, task_id="ar-1", account_id="acc-1", scene="like",
            followed=None, nickname="小美", content="赞了你的作品", user_key="k1")
        assert texts == []
        assert "通用话术" in why and "仅一次" in why
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 钉钉加签（#37）
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return b'{"errcode":0,"errmsg":"ok"}'

    @property
    def status(self):
        return 200


def _notify_cfg(**kw) -> dict:
    base = {"enableWebhook": True, "webhookType": "dingtalk",
            "webhookUrlDingtalk": "https://oapi.dingtalk.com/robot/send?access_token=tok",
            "webhookSecretDingtalk": "SECtest-secret"}
    base.update(kw)
    return base


def test_dingtalk_sign_appended_and_verifiable(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=6.0):
        captured["url"] = req.full_url
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = monitor_engine.notify(_notify_cfg(), {"nickname": "小美", "comment": "在吗",
                                                   "videoUrl": "", "score": 80}, "测试任务")
    assert result["ok"] is True
    url = captured["url"]
    assert "timestamp=" in url and "sign=" in url
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    expected = base64.b64encode(hmac.new(
        b"SECtest-secret", f'{params["timestamp"]}\nSECtest-secret'.encode("utf-8"),
        hashlib.sha256).digest()).decode("utf-8")
    assert params["sign"] == expected


def test_dingtalk_without_secret_keeps_url_clean(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=6.0):
        captured["url"] = req.full_url
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cfg = _notify_cfg(webhookSecretDingtalk="")
    result = monitor_engine.notify(cfg, {"nickname": "小美", "comment": "在吗",
                                         "videoUrl": "", "score": 80}, "测试任务")
    assert result["ok"] is True
    assert "timestamp=" not in captured["url"]


def test_dingtalk_sign_function_matches_official_algorithm():
    # 官方文档样例口径：sign = base64(hmac_sha256(secret, f"{ts}\n{secret}"))
    sign = monitor_engine.dingtalk_sign("secret", 1600000000000)
    again = monitor_engine.dingtalk_sign("secret", 1600000000000)
    assert sign == again
    assert base64.b64decode(sign)   # 合法 base64


def test_feishu_webhook_not_signed(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=6.0):
        captured["url"] = req.full_url
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cfg = {"enableWebhook": True, "webhookType": "feishu",
           "webhookUrlFeishu": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
           "webhookSecretDingtalk": "SECtest-secret"}
    result = monitor_engine.notify(cfg, {"nickname": "小美", "comment": "在吗",
                                         "videoUrl": "", "score": 80}, "测试任务")
    assert result["ok"] is True
    assert "sign=" not in captured["url"]


# ---------------------------------------------------------------------------
# #25 关键词规则「关注用户」在私信链路落地
# ---------------------------------------------------------------------------
_UID = "MS4wLjABAAAAtest-uid-01"


def _follow_step_recorder(monkeypatch, *, ok=True):
    steps: list[dict] = []

    def fake_step(db_path, ctx_, cfg_, account_id, action, **kw):
        steps.append({"action": action, **kw})
        return {"action": action, "ok": ok, "status": "sent_ok" if ok else "failed",
                "code": "sent_ok" if ok else "follow_control_not_verified",
                "text": kw.get("text", "")}

    monkeypatch.setattr(autoreply, "_comment_step", fake_step)
    return steps


def test_dm_rule_follow_enabled_runs_before_dm(tmp_path, monkeypatch):
    """规则勾「关注用户」且读到主页锚点：先关注再发私信组，回执进事件。"""
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(dmReplyMode="rules",
               dmReplyRules=[_rule(id="r1", keywords="价格", followEnabled=True)])
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "价格")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False, "uid": _UID,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "价格"}]}))
    steps = _follow_step_recorder(monkeypatch)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10, "follow-user": 100},
                                 {"follow-user": 10}, {"follow-user": 0})
        actions_row = con.execute(
            "SELECT outcome, actions FROM autoreply_events ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        con.close()
    assert steps and steps[0]["action"] == "follow-user"
    assert steps[0]["recipient_id"] == _UID
    assert [c["text"] for c in sends] == ["你好小美"]
    assert got["sent"] == 1
    assert actions_row[0] == "acted"
    assert '"follow-user"' in str(actions_row[1])


def test_dm_rule_follow_skipped_without_uid(tmp_path, monkeypatch):
    """读不到对方主页标识：关注跳过（不按昵称猜人），私信照发。"""
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(dmReplyMode="rules",
               dmReplyRules=[_rule(id="r1", keywords="价格", followEnabled=True)])
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "价格")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False, "uid": "",
                          "messages": [{"time": "10:00", "fromMe": False, "text": "价格"}]}))
    steps = _follow_step_recorder(monkeypatch)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10, "follow-user": 100},
                                 {"follow-user": 10}, {"follow-user": 0})
    finally:
        con.close()
    assert steps == []
    assert [c["text"] for c in sends] == ["你好小美"]
    assert got["sent"] == 1


def test_dm_rule_follow_failure_does_not_block_dm(tmp_path, monkeypatch):
    """关注动作失败不影响私信发送：页面上两个动作互相独立。"""
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(dmReplyMode="rules",
               dmReplyRules=[_rule(id="r1", keywords="价格", followEnabled=True)])
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "价格")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False, "uid": _UID,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "价格"}]}))
    steps = _follow_step_recorder(monkeypatch, ok=False)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10, "follow-user": 100},
                                 {"follow-user": 10}, {"follow-user": 0})
    finally:
        con.close()
    assert [s["action"] for s in steps] == ["follow-user"]
    assert [c["text"] for c in sends] == ["你好小美"]
    assert got["sent"] == 1


def test_dm_rule_follow_respects_task_cap(tmp_path, monkeypatch):
    """关注已达任务上限：规则「关注用户」跳过，私信照发。"""
    ctx = _ctx(tmp_path)
    _scope(monkeypatch, tmp_path, ctx)
    cfg = _cfg(dmReplyMode="rules",
               dmReplyRules=[_rule(id="r1", keywords="价格", followEnabled=True)])
    sends = _dm_fixture(
        monkeypatch, ctx, cfg,
        conversations=_conversations(("小美", "10:00", "价格")),
        threads=_threads({"title": "小美", "found": True, "isGroup": False, "uid": _UID,
                          "messages": [{"time": "10:00", "fromMe": False, "text": "价格"}]}))
    steps = _follow_step_recorder(monkeypatch)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._dm_pass(ctx.db_path, ctx, cfg, con, "acc-1", True,
                                 86400, 50, {"send-dm-chat": 10, "follow-user": 100},
                                 {"follow-user": 10}, {"follow-user": 10})
    finally:
        con.close()
    assert steps == []
    assert [c["text"] for c in sends] == ["你好小美"]
    assert got["sent"] == 1


def test_rules_follow_enabled_extends_scope():
    """rules + followEnabled + 私信回复开启：作用域要带 follow-user（壳按动作名过滤）。"""
    on = _cfg(watchMessages=True, actionMessageReply=True, dmReplyMode="rules",
              dmReplyRules=[_rule(followEnabled=True)])
    assert autoreply._dm_rule_follow_wanted(on, watch_dm=True) is True
    # 规则没勾「关注用户」→ 不需要
    off = _cfg(watchMessages=True, actionMessageReply=True, dmReplyMode="rules",
               dmReplyRules=[_rule(followEnabled=False)])
    assert autoreply._dm_rule_follow_wanted(off, watch_dm=True) is False
    # 没开私信回复 → 规则根本不触发，不需要
    assert autoreply._dm_rule_follow_wanted(on, watch_dm=False) is False
    assert autoreply._dm_rule_follow_wanted(
        _cfg(watchMessages=True, actionMessageReply=False, dmReplyMode="rules",
             dmReplyRules=[_rule(followEnabled=True)]), watch_dm=True) is False
