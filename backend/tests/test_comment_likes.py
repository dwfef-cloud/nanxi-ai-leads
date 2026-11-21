"""Offline contracts: no network, no real account writes, isolated SQLite."""
import json
import re
import sqlite3
import threading
import time

import pytest

from app import comment_likes as likes, leadgen_pools
from app.api import ipc_bridge
from app.runtime import browser, registry

VIDEO = "https://www.douyin.com/video/7400000000000000001"


@pytest.fixture
def svc(tmp_path, monkeypatch):
    path = tmp_path / "likes.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE accounts(id TEXT PRIMARY KEY, nickname TEXT, platform TEXT);
            INSERT INTO accounts VALUES('a','测试甲','douyin'),('b','测试乙','douyin'),('x','其他','weibo');
            CREATE TABLE account_events(id INTEGER PRIMARY KEY, action TEXT);
            INSERT INTO account_events(action) VALUES('untouched');
            CREATE TABLE live_action_results(id INTEGER PRIMARY KEY, action TEXT, status TEXT,
                created_ms INTEGER, account_id TEXT, url TEXT, text TEXT);
        """)
    monkeypatch.setattr(likes.random, "randint", lambda *args: 0)
    monkeypatch.setattr(likes.event_bus, "publish", lambda *args: None)
    def no_transport(*args, **kwargs):
        raise AssertionError("simulation must never call a platform transport")
    monkeypatch.setattr(browser, "execute", no_transport)
    service = likes.CommentLikeService(path)
    yield service
    service.shutdown()


def record(svc, task="task", accounts=None, targets=None, **cfg):
    item = {"id": task, "name": "离线验收", "entityKind": "entity", "status": "pending",
            "configSnapshot": {"sourceTypes": ["comment_like"], "selectedAccounts": accounts or ["a"],
                "commentLikeSource": "manual", "commentLikeLimit": 0,
                "commentLikeIntervalMin": 1, "commentLikeIntervalMax": 1,
                "commentLikeManualTargets": targets or [{"videoUrl": VIDEO, "authorName": "测试作者", "content": "想了解工作流"}], **cfg}}
    leadgen_pools.save_task_record(svc.db_path, item)
    return item


def wait(svc, timeout=4):
    end = time.monotonic() + timeout
    while svc.active() and time.monotonic() < end:
        time.sleep(.01)
    assert not svc.active(), "offline worker did not finish"


def test_log_replay_and_live_event_share_timestamp(svc, monkeypatch):
    """Renderer merges history and events by timestamp and text."""
    from app.api import task_logs
    packets = []
    monkeypatch.setattr(likes.event_bus, "publish", lambda topic, payload: packets.append(payload))
    stamps = iter([1234567890000, 1234567890001])
    monkeypatch.setattr(likes, "now", lambda: next(stamps))
    svc._log({"id": "run-log", "taskId": "task", "mode": "offline"}, "已启动")
    saved = task_logs.list_for(svc.db_path, "task")
    assert len(saved) == len(packets) == 1
    assert saved[0]["ts"] == packets[0]["ts"] == 1234567890000
    assert saved[0]["message"] == packets[0]["message"]


@pytest.fixture
def scoped_like(svc, tmp_path, monkeypatch):
    from app.execution import live_scope
    target = {"videoUrl": VIDEO, "authorName": "测试作者", "content": "想了解工作流", "kind": "main"}
    cached = svc.normalize_target(target)
    cached["platform_ref"] = "123456789"
    with svc.connect() as con:
        con.execute("INSERT INTO comment_like_samples VALUES(?,?)", ("sample-one", json.dumps(cached)))
    config = {"secret": "x" * 40, "scopeId": "one-comment", "expiresAt": time.time() + 120,
              "accountId": "a", "nickname": "测试甲", "leadgenTaskId": "task", "videoUrls": [VIDEO],
              "actions": {"like-comment": 1}, "commentTargets": [{**target, "commentId": "123456789"}]}
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("LEADS_LIVE_ACCEPTANCE_SCOPE", str(path))
    monkeypatch.setattr(browser, "shell_available", lambda: True)
    item = record(svc, targets=[{**target, "sampleKey": "sample-one"}])
    yield item, config, path
    assert "a" not in registry.active_accounts()


def test_live_defaults_closed_and_cannot_use_edited_or_uncached_target(svc, monkeypatch):
    monkeypatch.delenv("LEADS_LIVE_ACCEPTANCE_SCOPE", raising=False)
    assert not svc.live_status()["available"]
    assert not svc.start(record(svc), mode="live")["success"]


def test_consumed_scope_disables_live_entry_without_another_attempt(svc, scoped_like):
    import hashlib
    item, config, path = scoped_like
    assert svc.live_status()["available"]
    attempts = path.parent / "attempts"
    attempts.mkdir()
    key = hashlib.sha256((config["scopeId"] + ":like-comment").encode()).hexdigest()
    (attempts / (key + ".claim")).write_text("{}", encoding="utf-8")
    assert not svc.live_status()["available"]
    assert "已使用" in svc.live_status()["note"]
    assert not svc.start(item, mode="live")["success"]
    assert svc.results({"taskId": item["id"]})["total"] == 0


@pytest.mark.parametrize("body", ["想了解工作流", "想了解 Agent，哪些步骤交给模型？"])
def test_local_comment_live_plan_requires_exact_readback_and_rejects_ambiguity(svc, scoped_like, body):
    _, config, path = scoped_like
    config["nickname"] = "测试作者"
    config["commentTargets"][0]["content"] = likes.normalized(body)
    path.write_text(json.dumps(config), encoding="utf-8")
    with svc.connect() as con:
        con.execute("UPDATE accounts SET nickname='测试作者' WHERE id='a'")
        cached = json.loads(con.execute("SELECT snapshot FROM comment_like_samples WHERE id='sample-one'").fetchone()[0])
        cached["content"] = likes.normalized(body)
        con.execute("UPDATE comment_like_samples SET snapshot=? WHERE id='sample-one'", (json.dumps(cached),))
        con.execute("INSERT INTO live_action_results(id,action,status,created_ms,account_id,url,text) VALUES(1,'post-comment','sent_ok',100,'a',?,?)", (VIDEO, body))
    item = record(svc, commentLikeSource="local", visibilityLocalSampleIds={"a": ["local:1"]})
    target = svc._live_plan(item, svc.validate(item))["targets"][0]
    assert target["liveRef"] == "123456789" and target["sampleKey"] == "sample-one"
    with svc.connect() as con:
        cached = json.loads(con.execute("SELECT snapshot FROM comment_like_samples WHERE id='sample-one'").fetchone()[0])
        cached["platform_ref"] = "987654321"
        con.execute("INSERT INTO comment_like_samples VALUES(?,?)", ("ambiguous", json.dumps(cached)))
    with pytest.raises(ValueError, match="多条平台评论"):
        svc._live_plan(item, svc.validate(item))
    with svc.connect() as con:
        con.execute("DELETE FROM comment_like_samples")
    with pytest.raises(ValueError, match="先读取视频评论"):
        svc._live_plan(item, svc.validate(item))


def test_scope_preparation_is_read_only_exact_and_never_reauthorizes_existing_file(svc, scoped_like, tmp_path):
    from tools.prepare_comment_like_scope import prepare
    with svc.connect() as con:
        con.execute("UPDATE accounts SET nickname='测试作者' WHERE id='a'")
        con.execute("INSERT INTO live_action_results VALUES(1,'post-comment','sent_ok',100,'a',?,?)", (VIDEO, "想了解工作流"))
    item = record(svc, commentLikeSource="local", visibilityLocalSampleIds={"a": ["local:1"]})
    output = tmp_path / "new-scope.json"
    public = prepare(svc.db_path, item["id"], output)
    config = json.loads(output.read_text(encoding="utf-8"))
    assert config["actions"] == {"like-comment": 1}
    assert config["commentTargets"] == [{"videoUrl": VIDEO, "authorName": "测试作者", "content": "想了解工作流", "commentId": "123456789"}]
    assert config["secret"] not in json.dumps(public) and "secret" not in public
    assert 0 < config["expiresAt"] - time.time() <= 1800
    assert leadgen_pools.load_records(svc.db_path)[item["id"]] == item
    assert svc.results({"taskId": item["id"]})["total"] == 0
    with pytest.raises(FileExistsError):
        prepare(svc.db_path, item["id"], output)
    with svc.connect() as con:
        con.execute("DELETE FROM comment_like_samples")
    with pytest.raises(ValueError, match="platform identity"):
        prepare(svc.db_path, item["id"], tmp_path / "uncached.json")
    assert not (tmp_path / "uncached.json").exists()


def test_scope_preparation_manual_uses_exact_cached_main_comment(svc, scoped_like, tmp_path):
    from tools.prepare_comment_like_scope import prepare
    item, _, _ = scoped_like
    output = tmp_path / "manual.json"
    public = prepare(svc.db_path, item["id"], output)
    config = json.loads(output.read_text(encoding="utf-8"))
    assert config["commentTargets"][0]["commentId"] == "123456789"
    assert config["commentTargets"][0]["authorName"] == "测试作者"
    assert config["nickname"] == "测试甲" and config["actions"] == {"like-comment": 1}
    assert config["secret"] not in json.dumps(public)
    assert leadgen_pools.load_records(svc.db_path)[item["id"]] == item


def test_native_scope_review_binds_config_and_preserves_uncertain_history(svc, scoped_like, tmp_path):
    from tools.prepare_comment_like_scope import prepare, preview
    from app.execution.persistent_gate import PersistentWriteSafetyGate
    item, _, _ = scoped_like
    reviewed = preview(svc.db_path, item["id"], check_history=True)
    item["configSnapshot"]["commentLikeLimit"] = 1
    leadgen_pools.save_task_record(svc.db_path, item)
    with pytest.raises(ValueError, match="已修改"):
        prepare(svc.db_path, item["id"], tmp_path / "changed.json", expected_binding=reviewed["binding"])
    target = svc.validate(item)["targets"][0]
    gate = PersistentWriteSafetyGate(svc.db_path, namespace="live")
    key = json.dumps(["comment-like-live", "a", target["targetKey"]])
    assert gate.reserve(key)[0]
    with pytest.raises(ValueError, match="禁止重复提交"):
        preview(svc.db_path, item["id"], check_history=True)
    assert not (tmp_path / "changed.json").exists()


def test_runtime_exposes_each_modes_latest_state_without_merging(svc):
    runs = [dict(id="sim", taskId="one", mode="offline", status="completed", startedAt=20,
                 stats={"commentLikeSuccess":7}),
            dict(id="real", taskId="one", mode="live", status="failed", startedAt=10,
                 stats={"commentLikeSuccess":0,"commentLikeUncertain":1})]
    with svc.connect() as con:
        for run in runs:
            con.execute("INSERT INTO comment_like_runs VALUES(?,?,?)", (run["id"], "one", json.dumps(run)))
    summary = svc.runtime()["taskModes"]["one"]
    assert summary["live"]["status"] == "failed"
    assert summary["live"]["stats"]["commentLikeSuccess"] == 0
    assert summary["offline"]["stats"]["commentLikeSuccess"] == 7


@pytest.mark.parametrize("change", ["edited", "uncached", "extra", "ambiguous", "reply", "invalid_ref"])
def test_scope_preparation_rejects_unbound_manual_targets(svc, scoped_like, tmp_path, change):
    from tools.prepare_comment_like_scope import prepare
    item, _, _ = scoped_like
    target = item["configSnapshot"]["commentLikeManualTargets"][0]
    if change == "edited":
        target["content"] = "改写的内容"
    elif change == "uncached":
        target.pop("sampleKey")
    elif change == "extra":
        item["configSnapshot"]["commentLikeManualTargets"].append(dict(target))
    else:
        with svc.connect() as con:
            sample = json.loads(con.execute("SELECT snapshot FROM comment_like_samples WHERE id='sample-one'").fetchone()[0])
            if change == "ambiguous":
                sample["platform_ref"] = "987654321"
                con.execute("INSERT INTO comment_like_samples VALUES('other',?)", (json.dumps(sample),))
            else:
                sample["kind" if change == "reply" else "platform_ref"] = "reply" if change == "reply" else "missing"
                con.execute("UPDATE comment_like_samples SET snapshot=? WHERE id='sample-one'", (json.dumps(sample),))
    leadgen_pools.save_task_record(svc.db_path, item)
    output = tmp_path / "rejected.json"
    with pytest.raises(ValueError):
        prepare(svc.db_path, item["id"], output)
    assert not output.exists()


@pytest.mark.parametrize("change", ["author", "account", "uncached", "extra_target", "expired", "nickname"])
def test_live_scope_exact_target_and_single_account_cap(svc, scoped_like, change):
    item, config, path = scoped_like
    cfg = item["configSnapshot"]
    if change == "author":
        cfg["commentLikeManualTargets"][0]["authorName"] = "其他作者"
    elif change == "account":
        cfg["selectedAccounts"] = ["b"]
    elif change == "uncached":
        cfg["commentLikeManualTargets"][0].pop("sampleKey")
    elif change == "extra_target":
        cfg["commentLikeManualTargets"].append({"videoUrl": VIDEO, "authorName": "甲", "content": "另一个目标"})
    elif change == "expired":
        config["expiresAt"] = time.time() - 1
    else:
        config["nickname"] = "不是当前账号"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert not svc.start(item, mode="live")["success"]


@pytest.mark.parametrize("response,expected", [
    ({"ok": True, "status": "sent_ok", "code": "comment_like_receipt_state_and_reload_verified"}, "success"),
    ({"ok": True, "status": "sent_ok", "code": "comment_like_receipt_and_state_verified"}, "uncertain"),
    ({"ok": True, "status": "sent_ok", "code": "just_http_200"}, "uncertain"),
    ({"ok": False, "status": "uncertain"}, "uncertain"),
    ({"ok": False, "status": "skipped", "code": "already_liked"}, "already_liked"),
    ({"ok": False, "status": "sent_fail", "code": "comment_like_declined"}, "failed")])
def test_live_evidence_stats_and_public_sanitization(svc, scoped_like, monkeypatch, response, expected):
    from app.execution import live_leadgen
    item, _, _ = scoped_like
    assert svc.start(item, mode="offline")["success"]
    wait(svc)
    calls = []
    def fake_perform(db, action, **kwargs):
        assert action == "like-comment" and kwargs["comment_id"] == "123456789"
        assert kwargs["comment_author"] == "测试作者"
        calls.append(kwargs)
        return response
    monkeypatch.setattr(live_leadgen, "perform", fake_perform)
    assert svc.start(item, mode="live")["success"]
    wait(svc)
    results = svc.results({"taskId": "task"})
    assert results["mode"] == "mixed" and results["items"][0]["status"] == expected
    ipc_bridge.assert_public_safe(results)
    assert "123456789" not in json.dumps(results) and "liveRef" not in json.dumps(results)
    saved = leadgen_pools.load_records(svc.db_path)["task"]
    assert saved["stats"]["commentLikeSuccess"] == int(expected == "success")
    assert saved["simulationStats"]["commentLikeSuccess"] == 1
    assert saved["runs"][0]["mode"] == "live" and saved["simulationRuns"][0]["mode"] == "offline"
    if expected in {"success", "uncertain", "already_liked"}:
        assert svc.start(item, mode="live")["success"]
        wait(svc)
        assert len(calls) == 1


def test_live_account_lease_blocks_reader_and_releases_after_stop(svc, scoped_like, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def fake_attempt(*args):
        entered.set()
        release.wait(3)
        return "uncertain", "停止后结果不确定"
    monkeypatch.setattr(svc, "_live_attempt", fake_attempt)
    assert svc.start(scoped_like[0], mode="live")["success"]
    assert entered.wait(2) and "a" in registry.active_accounts()
    assert svc.runtime()["mode"] == "live" and svc.runtime()["taskIds"] == ["task"]
    assert not registry.claim_account("a", "other")
    reader = svc.reader_start({"sessionId": "reader-12345678", "accountId": "a", "videoUrl": VIDEO})
    assert not reader["success"]
    assert svc.stop("task")["stopped"] == 1
    release.set()
    wait(svc)
    assert "a" not in registry.active_accounts()


def test_live_transport_exception_records_uncertain_and_blocks_retry(svc, scoped_like, monkeypatch):
    calls = []
    def fail(*args):
        calls.append(1)
        raise RuntimeError("transport disconnected")
    monkeypatch.setattr(svc, "_live_attempt", fail)
    for _ in range(2):
        assert svc.start(scoped_like[0], mode="live")["success"]
        wait(svc)
    assert len(calls) == 1
    assert svc.results({"taskId": "task"})["counts"]["uncertain"] == 2


def test_dashboard_tracks_exact_live_run_stop_and_retains_uncertain_result(svc, scoped_like, monkeypatch):
    from app.api import live_dashboard
    entered, release = threading.Event(), threading.Event()
    def attempt(*args):
        entered.set()
        release.wait(3)
        return "uncertain", "刷新后未保留，结果待核验"
    monkeypatch.setattr(svc, "_live_attempt", attempt)
    monkeypatch.setattr(likes, "service", lambda _: svc)
    started = svc.start(scoped_like[0], mode="live")
    try:
        assert started["success"] and entered.wait(2)
        card, = live_dashboard.snapshot(svc.db_path)["items"]
        assert card["runId"] == started["runId"] and card["accountId"] == "a"
        assert card["target"]["content"] == "想了解工作流"
        assert card["logs"] and card["canStop"] and not card["isSessionFinished"]
        assert "liveRef" not in json.dumps(card) and "targetKey" not in json.dumps(card)
        for payload in [{"taskKind": "comment_like", "taskId": "task"},
                        {"taskKind": "comment_like", "taskId": "task", "runId": "previous"},
                        {"taskKind": "nurture", "taskId": "task", "runId": started["runId"]}]:
            assert not live_dashboard.stop(svc.db_path, payload)["success"]
        assert svc.dashboard()[0]["status"] == "running"
        payload = {"taskKind": "comment_like", "taskId": "task", "runId": started["runId"]}
        assert live_dashboard.stop(svc.db_path, payload)["stopped"] == 1
        assert svc.dashboard()[0]["status"] == "stopping"
    finally:
        release.set()
    wait(svc)
    card, = svc.dashboard()
    assert card["isSessionFinished"] and not card["canStop"]
    assert card["stats"]["commentLikeSuccess"] == 0
    assert card["stats"]["commentLikeUncertain"] == 1 and "待核验" in card["phase"]
    assert not live_dashboard.stop(svc.db_path, payload)["success"]
    # Reloaded state and very many recent offline runs must not erase the real finish.
    with svc.connect() as con:
        raw, = con.execute("SELECT snapshot FROM comment_like_runs WHERE id=?", (started["runId"],)).fetchone()
        original = json.loads(raw)
        for index in range(105):
            offline = {**original, "id": f"offline-{index}", "mode": "offline",
                       "startedAt": original["startedAt"] + index + 1}
            con.execute("INSERT INTO comment_like_runs VALUES(?,?,?)", (offline["id"], "task", json.dumps(offline)))
    reloaded = likes.CommentLikeService(svc.db_path)
    try:
        assert [item["runId"] for item in reloaded.dashboard()] == [started["runId"]]
    finally:
        reloaded.shutdown()


def test_dashboard_old_completed_card_cannot_stop_new_run(svc, scoped_like, monkeypatch):
    monkeypatch.setattr(svc, "_live_attempt", lambda *args: ("not_found", "没有点击"))
    first = svc.start(scoped_like[0], mode="live")
    wait(svc)
    entered, release = threading.Event(), threading.Event()
    def attempt(*args):
        entered.set()
        release.wait(3)
        return "skipped", "用户停止，没有点击"
    monkeypatch.setattr(svc, "_live_attempt", attempt)
    second = svc.start(scoped_like[0], mode="live")
    try:
        assert second["success"] and entered.wait(2)
        assert not svc.stop("task", first["runId"])["success"]
        assert not svc._workers["task"][0].is_set()
        assert svc.stop("task", second["runId"])["stopped"] == 1
    finally:
        release.set()
    wait(svc)


def test_live_service_signs_transport_and_persists_verified_evidence(svc, scoped_like, monkeypatch):
    from app.execution.live_scope import command_allowed
    with svc.connect() as con:
        for name in ("task_id", "keyword", "title", "code", "run_id"):
            con.execute(f"ALTER TABLE live_action_results ADD COLUMN {name} TEXT")
    commands = []
    def fake_transport(command, **kwargs):
        assert command_allowed(command, "a") and kwargs["engine"] == "shell"
        commands.append(command)
        return {"ok": True, "status": "sent_ok", "code": "comment_like_receipt_state_and_reload_verified"}
    monkeypatch.setattr(browser, "execute", fake_transport)
    assert svc.start(scoped_like[0], mode="live")["success"]
    wait(svc)
    assert len(commands) == 1
    with svc.connect() as con:
        assert con.execute("SELECT action,status,code FROM live_action_results").fetchone() == (
            "like-comment", "sent_ok", "comment_like_receipt_state_and_reload_verified")
        assert con.execute("SELECT * FROM account_events").fetchall() == [(1, "untouched")]
    assert svc.results({"taskId": "task"})["counts"]["success"] == 1
    from tools.review_comment_like_readback import review
    original = svc.results({"taskId": "task"})["items"][0]
    stamp = original["updatedAt"] + 1
    target = scoped_like[1]["commentTargets"][0]
    accepted = {"taskId": "task", "accountId": "a", "target": target, "probe": {"already": True},
                "observedAt": stamp, "realResults": [original], "receipts": [{"comment": target["commentId"],
                    "video": VIDEO.rsplit("/", 1)[-1], "http": 200, "code": 0, "action": "1"}]}
    refreshed = {"taskId": "task", "accountId": "a", "videoUrl": VIDEO, "commentId": target["commentId"],
                 "http": 200, "code": 0, "readbackOk": True, "afterRefresh": True,
                 "userDigged": 0, "diggCount": 0, "observedAt": stamp + 1}
    for change in [{"commentId": "999999999"}, {"afterRefresh": False}, {"userDigged": 1}, {"observedAt": 0}]:
        with pytest.raises(ValueError, match="Evidence"):
            review(svc.db_path, accepted, {**refreshed, **change})
    assert svc.results({"taskId": "task"})["counts"]["success"] == 1
    assert review(svc.db_path, accepted, refreshed)["changed"]
    assert not review(svc.db_path, accepted, refreshed)["changed"]
    checked = svc.results({"taskId": "task"})
    assert checked["counts"]["success"] == 0 and checked["counts"]["uncertain"] == 1
    assert checked["items"][0]["originalAcceptance"]["status"] == "success"
    saved = leadgen_pools.load_records(svc.db_path)["task"]
    assert saved["stats"]["commentLikeSuccess"] == 0 and saved["stats"]["commentLikeUncertain"] == 1
    with svc.connect() as con:
        assert con.execute("SELECT status FROM live_action_results").fetchone()[0] == "uncertain"
        assert con.execute("SELECT status FROM write_reservations").fetchall() == [("uncertain",), ("uncertain",)]
        assert con.execute("SELECT COUNT(*) FROM task_runtime_logs WHERE message LIKE '%刷新复核%'").fetchone()[0] == 1
    ipc_bridge.assert_public_safe(checked)


@pytest.mark.parametrize("value", [True, -1, 101, 10**400, 1.5, "2", None, float("inf"), float("nan")])
def test_invalid_limit_is_rejected(svc, value):
    result = svc.start(record(svc, commentLikeLimit=value), mode="offline")
    assert result["success"] is False


@pytest.mark.parametrize("url", ["http://www.douyin.com/video/123456", "https://evil.com/video/123456",
    "https://www.douyin.com.evil.com/video/123456", "https://user@www.douyin.com/video/123456",
    "https://www.douyin.com:444/video/123456", "https://v.douyin.com/abc/", "https://www.douyin.com/video/12"])
def test_only_canonical_video_urls(url):
    with pytest.raises(ValueError):
        likes.video_url(url)


def test_canonical_identity_and_validation(svc):
    target = {"videoUrl": VIDEO + "?from=search#detail", "authorName": " @作者 ", "content": "Ａ  工作流\u200b"}
    first = svc.normalize_target(target)
    second = svc.normalize_target({**target, "videoUrl": VIDEO, "content": "A 工作流"})
    assert first["targetKey"] == second["targetKey"]
    item = record(svc, targets=[target, {**target, "videoUrl": VIDEO}])
    assert len(svc.validate(item)["targets"]) == 1
    for cfg in ({"selectedAccounts": ["missing"]}, {"selectedAccounts": ["x"]},
                {"commentLikeIntervalMin": 4, "commentLikeIntervalMax": 2}):
        assert svc.start(record(svc, **cfg), mode="offline")["success"] is False
    assert not likes.is_like_record({"configSnapshot": "bad"})
    assert not likes.is_like_record({"configSnapshot": {"sourceTypes": "comment_like"}})


def test_local_candidates_only_confirmed_same_account_with_pagination(svc):
    with svc.connect() as con:
        con.executemany("INSERT INTO live_action_results(action,status,created_ms,account_id,url,text) VALUES(?,?,?,?,?,?)",
            [("post-comment", "sent_ok", n+100, "a", VIDEO, f"正文{n}") for n in range(55)] + [
                ("post-comment", "uncertain", 999, "a", VIDEO, "不确定"),
                ("post-comment", "sent_ok", 999, "b", VIDEO, "其他账号")])
    first = svc.candidates({"accountId": "a", "kinds": ["main"]})
    second = svc.candidates({"accountId": "a", "cursor": first["nextCursor"], "kinds": ["main"]})
    assert len(first["items"]) == 50 and first["hasMore"]
    assert len(second["items"]) == 5 and not second["hasMore"]
    assert first["defaultSelectedIds"] == []
    assert first["items"][0]["content"] == "正文54"
    keys = [r["id"] for r in first["items"] + second["items"]]
    assert len(set(keys)) == 55
    assert all(re.fullmatch(r"main:[a-f0-9]{64}", key) for key in keys)
    assert svc.candidates({"accountId": "a", "kinds": ["main"]})["items"][0]["id"] == keys[0]
    assert len(svc.candidates({"accountId": "a", "minTimestamp": 153})["items"]) == 2
    selected = {"a": [r["id"] for r in first["items"]+second["items"]]}
    item = record(svc, commentLikeSource="local", visibilityLocalSampleIds=selected)
    assert len(svc.validate(item)["targets"]) == 55
    item["configSnapshot"]["visibilityLocalSampleIds"] = {"a": ["local:99999"]}
    assert svc.start(item, mode="offline")["success"] is False
    assert not svc.candidates({"accountId": "missing"})["success"]


def test_local_candidates_emit_reply_samples(svc):
    """回复评论（reply-comment sent_ok 回执）与主评一样进入候选，kind 与键前缀区分。"""
    with svc.connect() as con:
        con.executemany("INSERT INTO live_action_results(action,status,created_ms,account_id,url,text) VALUES(?,?,?,?,?,?)",
            [("post-comment", "sent_ok", 100, "a", VIDEO, "主评正文"),
             ("reply-comment", "sent_ok", 200, "a", VIDEO, "回复正文"),
             ("reply-comment", "uncertain", 300, "a", VIDEO, "未确认回复"),
             ("reply-comment", "sent_ok", 400, "b", VIDEO, "其他账号回复")])
    page = svc.candidates({"accountId": "a"})
    assert page["success"]
    items = page["items"]
    assert {i["kind"] for i in items} == {"main", "reply"}
    assert "回复正文" in {i["content"] for i in items}
    assert "未确认回复" not in {i["content"] for i in items}
    assert all(re.fullmatch(r"(main|reply):[a-f0-9]{64}", i["id"]) for i in items)
    # kinds 过滤继续生效
    assert [i["kind"] for i in svc.candidates({"accountId": "a", "kinds": ["main"]})["items"]] == ["main"]
    assert [i["kind"] for i in svc.candidates({"accountId": "a", "kinds": ["reply"]})["items"]] == ["reply"]
    # 按键选择：回复样本可被精确选中并解析（选中解析走 normalize_target）
    reply = [i for i in items if i["kind"] == "reply"][0]
    only = svc.candidates({"accountId": "a", "selectedIds": [reply["id"]]})
    assert [i["kind"] for i in only["items"]] == ["reply"]
    target = svc.normalize_target({**reply, "videoUrl": reply["videoUrl"]})
    assert target["kind"] == "reply"
    # main 键保持旧推导：同一条主评的键与只有主评时一致（旧保存的选择不受影响）
    main = [i for i in items if i["kind"] == "main"][0]
    assert main["id"] == svc.candidates({"accountId": "a", "kinds": ["main"]})["items"][0]["id"]
    # 选中不存在或已变更的类型仍然拒绝
    bad = record(svc, commentLikeSource="local", visibilityLocalSampleIds={"a": [reply["id"]]},
                 visibilityLocalSampleKinds=["main"])
    assert svc.start(bad, mode="offline")["success"] is False


def test_local_selection_keys_bind_account_and_original_record(svc):
    with svc.connect() as con:
        con.execute("INSERT INTO live_action_results(id,action,status,created_ms,account_id,url,text) VALUES(1,'post-comment','sent_ok',100,'a',?,?)", (VIDEO, "原始正文"))
    key = svc.candidates({"accountId": "a"})["items"][0]["id"]
    assert svc.candidates({"accountId": "b", "selectedIds": [key]})["items"] == []
    legacy = record(svc, commentLikeSource="local", visibilityLocalSampleIds={"a": ["local:1"]})
    assert svc.validate(legacy)["targets"][0]["content"] == "原始正文"
    current = record(svc, commentLikeSource="local", visibilityLocalSampleIds={"a": [key]})
    assert svc.validate(current)["targets"][0]["content"] == "原始正文"
    with svc.connect() as con:
        con.execute("UPDATE live_action_results SET text='修改后的正文' WHERE id=1")
    with pytest.raises(ValueError, match="部分已选评论"):
        svc.validate(current)


@pytest.mark.parametrize("patch,status", [({}, "simulated_success"), ({"after": 1}, "uncertain"),
    ({"before": 0}, "uncertain"), ({"code": True}, "uncertain"), ({"http": 503}, "uncertain"),
    ({"accountId": "b"}, "uncertain"), ({"target": "other"}, "uncertain"),
    ({"code": 1}, "failed"), ({"http": 403}, "failed"), ({"status": "already_liked"}, "already_liked")])
def test_receipts_require_exact_binding_and_state(patch, status):
    receipt = likes.fixture_receipt("a", "target", "success")
    assert likes.classify_receipt({**receipt, **patch}, "a", "target") == status


def test_per_account_cap_results_filters_and_real_stats_unchanged(svc):
    targets = [{"videoUrl": VIDEO, "authorName": "甲", "content": f"评论{n}"} for n in range(3)]
    item = record(svc, accounts=["a", "b"], targets=targets, commentLikeLimit=1)
    item["stats"] = {"commentLikeSuccess": 7, "likes": 9}
    item["runs"] = [{"accountId": "a", "mode": "live", "startedAt": 42}]
    leadgen_pools.save_task_record(svc.db_path, item)
    assert svc.start(item, mode="offline")["success"]
    wait(svc)
    results = svc.results({"taskId": "task", "limit": 2})
    assert results["total"] == 6 and len(results["items"]) == 2
    assert results["counts"]["simulated_success"] == 2
    assert results["counts"]["skipped"] == 4
    assert svc.results({"taskId": "task", "accountId": "b", "status": "skipped"})["total"] == 2
    saved = leadgen_pools.load_records(svc.db_path)["task"]
    assert saved["status"] == "completed" and saved["executionMode"] == "offline"
    assert saved["stats"] == item["stats"] and saved["runs"] == item["runs"]
    assert len(saved["simulationRuns"]) == 2
    assert saved["simulationStats"]["commentLikeSuccess"] == 2
    with svc.connect() as con:
        assert con.execute("SELECT * FROM account_events").fetchall() == [(1, "untouched")]
        assert con.execute("SELECT DISTINCT namespace FROM write_reservations").fetchall() == [("offline",)]
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "runtime_runs" not in tables
    ipc_bridge.assert_public_safe(results)


def test_dedup_survives_new_task_and_service_restart(svc):
    assert svc.start(record(svc), mode="offline")["success"]
    wait(svc)
    restarted = likes.CommentLikeService(svc.db_path, fixture=lambda *args: pytest.fail("duplicate reached fixture"))
    try:
        assert restarted.start(record(svc, "second"), mode="offline")["success"]
        wait(restarted)
        assert restarted.results({"taskId": "second"})["counts"]["already_liked"] == 1
    finally:
        restarted.shutdown()


def test_uncertain_stops_remaining_targets_and_blocks_future_attempt(svc):
    targets = [{"videoUrl": VIDEO, "authorName": "甲", "content": f"内容{n}"} for n in range(2)]
    assert svc.start(record(svc, targets=targets), mode="offline", scenario="uncertain")["success"]
    wait(svc)
    assert svc.results({"taskId": "task"})["total"] == 1
    saved = leadgen_pools.load_records(svc.db_path)["task"]
    assert saved["status"] == "failed" and saved["simulationStats"]["commentLikeSuccess"] == 0
    svc.fixture = lambda *args: pytest.fail("uncertain attempt must not retry")
    assert svc.start(record(svc, "retry", targets=targets), mode="offline")["success"]
    wait(svc)
    assert svc.results({"taskId": "retry"})["counts"]["uncertain"] == 1


def test_stop_interrupts_long_interval_and_no_next_action(svc, monkeypatch):
    monkeypatch.setattr(likes.random, "randint", lambda *args: 60)
    targets = [{"videoUrl": VIDEO, "authorName": "甲", "content": f"内容{n}"} for n in range(2)]
    item = record(svc, targets=targets)
    assert svc.start(item, mode="offline")["success"]
    end = time.monotonic()+3
    while svc.results({"taskId": "task"})["total"] == 0 and time.monotonic() < end:
        time.sleep(.01)
    assert not svc.start(record(svc, "busy"), mode="offline")["success"]
    start = time.monotonic()
    assert svc.stop("task")["stopped"] == 1
    wait(svc, timeout=1)
    assert time.monotonic()-start < 1
    assert svc.results({"taskId": "task"})["total"] == 1
    assert leadgen_pools.load_records(svc.db_path)["task"]["status"] == "stopped"


def test_real_start_is_rejected_everywhere(svc, monkeypatch):
    item = record(svc)
    assert svc.start(item, mode="live")["code"] == "live_writes_disabled"
    monkeypatch.setattr(browser, "status", lambda *args: pytest.fail("wrong engine reached"))
    assert registry.start(svc.db_path, "entity", "task")["code"] == "live_writes_disabled"
    # Restore the actual function only for this explicit gate contract.
    monkeypatch.undo()
    assert browser.execute({"type": "like-comment"}, db_path=svc.db_path, engine="shell")["code"] == "live_writes_disabled"


def test_reader_filters_rows_keeps_opaque_identity_and_never_writes(svc, monkeypatch):
    packets = []
    monkeypatch.setattr(likes.event_bus, "publish", lambda channel, packet: packets.append((channel, packet)))
    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(registry, "active_accounts", lambda: [])
    calls = []
    def read(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return {"ok": True, "comments": [
            {"commentId": "raw-123", "nickname": "作者", "text": "工作流", "parentCommentId": "0"},
            {"commentId": "raw-123", "nickname": "作者", "text": "工作流"},
            {"commentId": "raw-456", "nickname": "回复者", "text": "回复", "parentCommentId": "raw-123"},
            {"nickname": "另一个", "text": "别的视频", "videoUrl": VIDEO[:-1]+"2"},
            {"nickname": "", "text": "缺少作者"}]}
    monkeypatch.setattr(browser, "execute", read)
    done = threading.Event()
    original = svc._read
    def run(*args):
        try:
            original(*args)
        finally:
            done.set()
    monkeypatch.setattr(svc, "_read", run)
    args = {"sessionId": "reader-test-123", "accountId": "a", "videoUrl": VIDEO, "limit": 5}
    assert svc.reader_start(args)["success"] and done.wait(2)
    packet = packets[-1][1]
    assert packet["done"] and len(packet["rows"]) == 1
    assert "raw-123" not in json.dumps(packet)
    ipc_bridge.assert_public_safe(packet)
    row = packet["rows"][0]
    target = {**row, "sampleKey": row["key"]}
    assert svc.normalize_target(target)["sampleKey"] == row["key"]
    with pytest.raises(ValueError, match="修改"):
        svc.normalize_target({**target, "content": "被修改"})
    assert calls[0][0]["type"] == "read-comments" and calls[0][1]["account_id"] == "a"
    # 默认不读楼中楼：开关没开就不该往壳里塞 expandSubComments
    assert "expandSubComments" not in calls[0][0]
    assert not svc.reader_start({**args, "limit": 61})["success"]


def test_reader_include_replies_keeps_kind_and_asks_shell_to_expand(svc, monkeypatch):
    """楼中楼开关：以前后端直接拒绝（「暂不支持」），现在要能读、并把回复标成 reply。"""
    packets = []
    monkeypatch.setattr(likes.event_bus, "publish", lambda channel, packet: packets.append((channel, packet)))
    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(registry, "active_accounts", lambda: [])
    calls = []

    def read(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return {"ok": True, "comments": [
            {"commentId": "7001", "nickname": "主评人", "text": "视频不错", "depth": 0},
            {"commentId": "7002", "nickname": "回复人", "text": "我也觉得", "depth": 1, "parentIdx": 0},
            {"commentId": "7003", "nickname": "路过", "text": "别的视频", "depth": 0,
             "videoUrl": VIDEO[:-1] + "2"}]}

    monkeypatch.setattr(browser, "execute", read)
    done = threading.Event()
    original = svc._read

    def run(*args):
        try:
            original(*args)
        finally:
            done.set()

    monkeypatch.setattr(svc, "_read", run)
    args = {"sessionId": "reader-reply-123", "accountId": "a", "videoUrl": VIDEO,
            "limit": 10, "includeReplies": True}
    assert svc.reader_start(args)["success"] and done.wait(2)

    cmd = calls[0][0]
    assert cmd["expandSubComments"] is True
    packet = packets[-1][1]
    rows = packet["rows"]
    assert [row["content"] for row in rows] == ["视频不错", "我也觉得"]
    assert rows[0]["kind"] == "main" and rows[1]["kind"] == "reply"
    # 平台评论 id 不上页面：父评论关系只落到本地快照里
    assert "7001" not in json.dumps(packet) and "7002" not in json.dumps(packet)
    ipc_bridge.assert_public_safe(packet)
    with svc.connect() as con:
        snapshot = json.loads(con.execute(
            "SELECT snapshot FROM comment_like_samples WHERE id=?", (rows[1]["key"],)).fetchone()[0])
    assert snapshot["kind"] == "reply" and snapshot["parentCommentId"] == "7001"
    assert snapshot["platform_ref"] == "7002"


def test_restart_marks_unowned_running_run_interrupted(svc):
    item = record(svc)
    snapshot = {"id": "orphan", "taskId": item["id"], "status": "running", "accounts": ["a"],
                "startedAt": 1, "ownerPid": 0, "stats": {}}
    with svc.connect() as con:
        con.execute("INSERT INTO comment_like_runs VALUES(?,?,?)", ("orphan", "task", json.dumps(snapshot)))
    recovered = likes.CommentLikeService(svc.db_path)
    assert leadgen_pools.load_records(svc.db_path)["task"]["status"] == "interrupted"
    recovered.shutdown()


def test_reconcile_keeps_restart_recovery_status(svc, monkeypatch):
    item = record(svc)
    item["status"] = "running"
    stale = {"task": item}
    leadgen_pools.update_task_record(svc.db_path, "task", {"status": "interrupted"})
    monkeypatch.setattr(ipc_bridge, "DB_PATH", svc.db_path)
    monkeypatch.setattr(likes, "service", lambda _: svc)
    monkeypatch.setattr(ipc_bridge.task_runner, "active_ids", lambda: [])
    assert ipc_bridge._reconcile_leadgen_records(stale)["task"]["status"] == "interrupted"


def test_ipc_save_does_not_promote_simulation_counters(svc, monkeypatch):
    monkeypatch.setattr(ipc_bridge, "DB_PATH", svc.db_path)
    monkeypatch.setattr(likes, "service", lambda _: svc)
    item = record(svc)
    item["stats"] = {"commentLikeSuccess": 999}
    item["simulationStats"] = {"commentLikeSuccess": 999}
    saved = ipc_bridge._save_entity_leadgen_task(item)
    assert saved["success"] and saved["stats"] == {} and "simulationStats" not in saved
    assert ipc_bridge._start_comment_like_simulation({"taskId": "task"})["code"] == "live_writes_disabled"


@pytest.mark.parametrize("scenario,expected,task_status", [("declined", "failed", "failed"),
    ("not_found", "not_found", "completed"), ("already_liked", "already_liked", "completed")])
def test_non_success_is_separate_from_new_likes(svc, scenario, expected, task_status):
    assert svc.start(record(svc), mode="offline", scenario=scenario)["success"]
    wait(svc)
    result = svc.results({"taskId": "task"})
    assert result["counts"][expected] == 1 and result["counts"]["simulated_success"] == 0
    assert leadgen_pools.load_records(svc.db_path)["task"]["status"] == task_status


def test_reader_stop_and_busy_do_not_start_other_reader(svc, monkeypatch):
    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(registry, "active_accounts", lambda: ["a"])
    args = {"sessionId": "reader-stopped-123", "accountId": "a", "videoUrl": VIDEO}
    assert not svc.reader_start(args)["success"]
    monkeypatch.setattr(registry, "active_accounts", lambda: [])
    entered, finished = threading.Event(), threading.Event()
    def read(cmd, **kwargs):
        entered.set()
        end = time.monotonic()+2
        while not kwargs["stop_check"]() and time.monotonic() < end:
            time.sleep(.01)
        finished.set()
        return {"ok": False, "error": "stopped"}
    monkeypatch.setattr(browser, "execute", read)
    assert svc.reader_start(args)["success"] and entered.wait(1)
    assert not svc.reader_start({**args, "sessionId": "another-session"})["success"]
    assert svc.reader_stop(args)["stopped"] == 1 and finished.wait(1)


def test_http_save_start_result_log_and_real_guard(svc, monkeypatch):
    from fastapi.testclient import TestClient
    import bridge_server
    monkeypatch.setattr(ipc_bridge, "DB_PATH", svc.db_path)
    monkeypatch.setattr(likes, "service", lambda _: svc)
    client = TestClient(bridge_server.app)
    def call(channel, value=None):
        response = client.post("/ipc/"+channel, json={"args": [] if value is None else [value]})
        assert response.status_code == 200
        packet = response.json()
        assert packet["success"], packet
        return packet["data"]
    assert call("save-entity-leadgen-task", record(svc))["success"]
    assert call("start-entity-leadgen-task", {"taskId": "task"})["success"] is False
    assert call("start-comment-like-simulation", {"taskId": "task", "executionMode": "offline"})["success"]
    wait(svc)
    result = call("get-comment-like-results", {"taskId": "task"})
    assert result["counts"]["simulated_success"] == 1 and result["mode"] == "offline"
    logs = call("get-entity-leadgen-task-runtime-logs", {"taskId": "task"})
    assert logs and all("离线模拟" in row["message"] for row in logs)
    assert call("get-comment-like-runtime")["taskIds"] == []
    assert call("stop-comment-like-simulation", {"taskId": "task"})["success"]
