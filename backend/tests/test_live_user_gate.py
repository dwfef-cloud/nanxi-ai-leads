"""直播建单「用户类型 / 粉丝数量」筛选 + 私信实时事件（2026-11-18）。

对应清单两条「部分实现」的收口：
- #82 直播采集 userTypeFilter / userFanCount：弹幕行不带粉丝数与认证信息，
  只能按 uid 打开用户主页核对（inspect-profile 只读通道）；核对不了的用户
  保留并告警，绝不悄悄丢人。没配筛选时完全不访问主页。
- #104 消息聚合 chat-new-message / chat-webhook-result：私信去重通过即发
  「X 收到 Y 的新私信」事件；Webhook 推送失败立刻发失败事件（消息中心红字）。
"""
from __future__ import annotations

import sqlite3
import threading
import types

import pytest

from app.api import event_bus
from app.runtime import autoreply, runners
from test_runtime import db  # noqa: F401  复用运行时套件的 db 夹具


# ---------------------------------------------------------------------------
# 档位解析（纯函数）
# ---------------------------------------------------------------------------

def test_parse_fans_understands_cn_units():
    assert runners._parse_fans("1.2万") == 12000
    assert runners._parse_fans("8000") == 8000
    assert runners._parse_fans("2.3w") == 23000
    assert runners._parse_fans("1.5亿") == 150000000
    assert runners._parse_fans("") is None
    assert runners._parse_fans("没写") is None


def test_fan_tier_boundaries_match_ui_options():
    # UI 档位：1=1000以下 2=1000-1w 3=1w-10w 4=10w-100w 5=100w以上
    assert runners._fan_tier(999) == "1"
    assert runners._fan_tier(1000) == "2"
    assert runners._fan_tier(9999) == "2"
    assert runners._fan_tier(10000) == "3"
    assert runners._fan_tier(99999) == "3"
    assert runners._fan_tier(100000) == "4"
    assert runners._fan_tier(1000000) == "5"
    assert runners._fan_tier(None) is None


# ---------------------------------------------------------------------------
# 用户筛选门禁
# ---------------------------------------------------------------------------

class _GateCtx:
    """最小 Ctx 替身：_live_user_gate 只用 db_path / engine / stop。"""

    def __init__(self, db_path):
        self.db_path = db_path
        self.engine = "shell"
        self.stop = threading.Event()


def _account_row(db, account_id="acc1", nickname="测试号"):
    with sqlite3.connect(str(db)) as con:
        con.execute("INSERT OR IGNORE INTO accounts(id, nickname, name) VALUES(?,?,?)",
                    (account_id, nickname, nickname))
        con.commit()


def test_gate_skips_profile_visit_when_no_filter(db):
    ctx = _GateCtx(db)
    cache: dict = {}
    visits = []

    def fake_execute(cmd, **kwargs):
        visits.append(cmd)
        return {"ok": True, "item": {"fansText": "5000"}}

    import app.runtime.browser as browser_mod
    orig = browser_mod.execute
    try:
        browser_mod.execute = fake_execute
        assert runners._live_user_gate(ctx, {}, "acc1", cache, "uid1", "张三", "测试号") is True
        assert visits == []          # 没配筛选：完全不访问主页
        assert cache == {}
    finally:
        browser_mod.execute = orig


def test_gate_filters_by_fan_tier_and_caches_uid(db):
    _account_row(db)
    ctx = _GateCtx(db)
    cache: dict = {}
    calls = []

    def fake_execute(cmd, **kwargs):
        calls.append(cmd)
        assert cmd["type"] == "inspect-profile"
        assert cmd["commentAuthor"] in {"张三", "李四", "王五", "赵六", "孙七", "周八"}
        if cmd["recipientId"] in {"uid2", "uid6"}:
            return {"ok": True, "item": {"fansText": "500"}}        # 1000 以下
        return {"ok": True, "item": {"fansText": "1.2万"}}          # 其余 1w-10w

    import app.runtime.browser as browser_mod
    orig = browser_mod.execute
    try:
        browser_mod.execute = fake_execute
        cfg = {"userFanCount": "3"}          # 1w-10w
        assert runners._live_user_gate(ctx, cfg, "acc1", cache, "uid1", "张三", "测试号") is True
        assert runners._live_user_gate(ctx, cfg, "acc1", cache, "uid1", "张三", "测试号") is True
        assert len(calls) == 1               # 同一 uid 只核对一次
        # uid2 粉丝 500：配「1000-1w」不符 → 过滤；uid6 同样 500 粉、配「1000以下」→ 保留
        # （uid2 的结论按 uid 缓存，换档位要用新 uid，与运行中配置不变的行为一致）
        assert runners._live_user_gate(ctx, {"userFanCount": "2"}, "acc1", cache,
                                       "uid2", "李四", "测试号") is False
        assert runners._live_user_gate(ctx, {"userFanCount": "1"}, "acc1", cache,
                                       "uid6", "周八", "测试号") is True
        assert runners._live_user_gate(ctx, {"userTypeFilter": "2"}, "acc1", cache,
                                       "uid3", "王五", "测试号") is False
        assert runners._live_user_gate(ctx, {"userTypeFilter": "3"}, "acc1", cache,
                                       "uid4", "赵六", "测试号") is False
        assert runners._live_user_gate(ctx, {"userTypeFilter": "1"}, "acc1", cache,
                                       "uid5", "孙七", "测试号") is True   # 普通用户：无徽章
    finally:
        browser_mod.execute = orig


def test_gate_keeps_user_when_profile_unreadable(db):
    _account_row(db)
    ctx = _GateCtx(db)

    def fake_execute(cmd, **kwargs):
        return {"ok": False, "error": "页面打不开"}

    import app.runtime.browser as browser_mod
    orig = browser_mod.execute
    try:
        browser_mod.execute = fake_execute
        # 主页核对失败：返回 None（保留用户），绝不判 False 悄悄丢人
        assert runners._live_user_gate(ctx, {"userFanCount": "2"}, "acc1", {},
                                       "uid1", "张三", "测试号") is None
        # 没有弹幕行没有 uid：同样保留
        assert runners._live_user_gate(ctx, {"userFanCount": "2"}, "acc1", {},
                                       "", "张三", "测试号") is None
    finally:
        browser_mod.execute = orig


# ---------------------------------------------------------------------------
# 私信实时事件（#104）
# ---------------------------------------------------------------------------

def _isolate_events(monkeypatch, tmp_path):
    # 事件库换成测试专用的临时库：全量跑时共享的 bridge_events.db 会被别的
    # 测试 drain 走，导致事件「丢」；隔离后每个测试只见自己发布的事件。
    monkeypatch.setattr(event_bus, "EVENTS_DB", tmp_path / "bridge_events_test.db")


def test_new_dm_publishes_chat_new_message(db, monkeypatch, tmp_path):
    _isolate_events(monkeypatch, tmp_path)
    monkeypatch.setattr(autoreply, "_account_label", lambda db_path, aid: "小马号")
    autoreply._emit_chat_new_message(db, "acc1", "路人甲")
    events = [e for e in event_bus.drain(500) if e.get("channel") == "chat-new-message"]
    assert events, "chat-new-message 事件没有入队"
    payload = events[0]["payload"]
    assert payload["receiverNickname"] == "小马号"
    assert payload["sender"] == "路人甲"
    assert payload["accountId"] == "acc1"


def test_account_label_reads_nickname(db):
    _account_row(db, "acc9", "九号机")
    assert autoreply._account_label(db, "acc9") == "九号机"
    assert autoreply._account_label(db, "missing") == ""


def test_webhook_failure_publishes_chat_webhook_result(db, monkeypatch, tmp_path):
    _isolate_events(monkeypatch, tmp_path)
    class _FakeEngine:
        @staticmethod
        def notify(cfg, lead, task_name, timeout=6.0):
            return {"ok": False, "code": "no_url", "error": "未填写 Webhook 地址"}

    import app.runtime.monitor_engine as me
    monkeypatch.setattr(me, "notify", _FakeEngine.notify)
    ctx = types.SimpleNamespace(db_path=db, task_id="t1", account_id="acc1", name="测试任务")
    autoreply._notify(db, {"enableWebhook": True}, ctx, title="张三", content="你好", reply="嗯")
    events = [e for e in event_bus.drain(500) if e.get("channel") == "chat-webhook-result"]
    assert events, "webhook 失败事件没有入队"
    payload = events[0]["payload"]
    assert payload["success"] is False
    assert "Webhook" in payload["message"]
