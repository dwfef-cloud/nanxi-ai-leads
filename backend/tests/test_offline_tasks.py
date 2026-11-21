from app.api import task_runner
from app.execution.offline_simulation import OfflineSimulator, SimulatedAction
from app.execution.offline_tasks import run_leadgen_offline
from app.runtime import browser


VIDEO = "https://www.douyin.com/video/7400000000000000001"
OTHER = "https://www.douyin.com/video/7400000000000000002"


def run(payload=None, *, fixture=None, generate=None, wait=None, stopped=None):
    return run_leadgen_offline(payload or {"taskId": "test", "accountId": "offline",
        "keywords": ["离线测试"]}, simulator=OfflineSimulator(fixture or (lambda _: {
            "http_status": 200, "body": {"status_code": 0, "comment": {"cid": "c1"}}})),
        search=lambda _: [VIDEO, VIDEO + "?from=search", OTHER],
        generate=generate or (lambda _: "离线生成的测试评论内容"),
        wait=wait or (lambda _: False), stopped=stopped or (lambda: False))


def test_search_generate_comment_cooldown_dedup_and_stats_are_offline():
    waits = []
    result = run(wait=lambda seconds: waits.append(seconds) or False)
    assert result["status"] == "completed"
    assert result["simulationStats"]["simulatedPublished"] == 2
    assert result["simulationStats"]["already"] == 1
    assert result["stats"] == task_runner.EMPTY_STATS
    assert waits == [300]


def test_uncertain_stops_before_second_video():
    calls = []
    result = run(fixture=lambda action: calls.append(action.target))
    assert result["status"] == "needs_confirmation"
    assert calls == [VIDEO]
    assert result["simulationStats"]["simulatedPublished"] == 0


def test_ai_failure_skips_without_template_comment():
    calls = []
    result = run(generate=lambda _: None, fixture=lambda action: calls.append(action))
    assert calls == []
    assert result["simulationStats"]["skipped"] == 2


def test_stop_during_cooldown_does_not_submit_next_comment():
    result = run(wait=lambda _: True)
    assert result["status"] == "stopped"
    assert result["simulationStats"]["simulatedPublished"] == 1


def test_stop_before_start_has_no_actions():
    result = run(stopped=lambda: True)
    assert result["status"] == "stopped"
    assert result["items"] == []


def test_offline_writes_cannot_be_enabled_by_payload_or_engine(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline action reached a live transport")
    monkeypatch.setattr(browser, "status", forbidden)
    monkeypatch.setattr(browser.desktop_commands, "publish", forbidden)
    for action in browser.OFFLINE_ONLY_ACTIONS:
        for engine in ["shell", "playwright", ""]:
            response = browser.execute({"type": action, "offline": True,
                "allowWrite": True}, db_path=None, engine=engine, account_id="existing-account")
            assert response["code"] == "live_writes_disabled"


def test_task_interactions_use_same_account_and_do_not_count_comments_as_people():
    calls = []
    def fixture(action):
        calls.append(action.action)
        body = {"status_code": 0}
        body["message" if action.action == "dm" else "comment"] = {"id": "fixture-id"}
        return {"http_status": 200, "body": body}
    result = run_leadgen_offline({"taskId": "offline", "accountId": "offline", "videoUrls": [VIDEO]},
        simulator=OfflineSimulator(fixture), search=lambda _: [], generate=lambda _: "离线测试评论内容",
        wait=lambda _: False, stopped=lambda: False,
        interactions=lambda _: [
            SimulatedAction("reply_comment", "offline", "comment_1", "谢谢分享"),
            SimulatedAction("dm", "wrong-account", "recipient_1", "禁止跨账号"),
            SimulatedAction("dm", "offline", "recipient_1", "您好"),
            SimulatedAction("dm", "offline", "recipient_1", "重复消息必须跳过"),
        ])
    assert result["simulationStats"]["simulatedInteracted"] == 2
    assert result["simulationStats"]["simulatedReached"] == 1
    assert result["stats"] == task_runner.EMPTY_STATS
    assert calls == ["post_comment", "reply_comment", "dm"]


def test_uncertain_dm_stops_all_remaining_interactions_and_videos():
    calls = []
    def fixture(action):
        calls.append(action.action)
        return {"http_status": 200, "body": {"status_code": 0, "comment": {"cid": "c1"}}}
    result = run_leadgen_offline({"taskId": "offline", "accountId": "offline", "videoUrls": [VIDEO, OTHER]},
        simulator=OfflineSimulator(fixture), search=lambda _: [], generate=lambda _: "离线测试评论内容",
        wait=lambda _: False, stopped=lambda: False,
        interactions=lambda _: [SimulatedAction("dm", "offline", "recipient_1", "您好"),
                                SimulatedAction("dm", "offline", "recipient_2", "您好")])
    assert result["status"] == "needs_confirmation"
    assert calls == ["post_comment", "dm"]
    assert result["simulationStats"]["simulatedReached"] == 0


def test_reply_inherits_current_video_and_cannot_use_another_video():
    calls = []
    def fixture(action):
        calls.append(action)
        return {"http_status": 200, "body": {"status_code": 0, "comment": {"cid": "c1"}}}
    report = run_leadgen_offline({"taskId": "offline", "accountId": "offline", "videoUrls": [VIDEO]},
        simulator=OfflineSimulator(fixture), search=lambda _: [], generate=lambda _: "离线测试评论内容",
        wait=lambda _: False, stopped=lambda: False,
        interactions=lambda _: [SimulatedAction("reply_comment", "offline", "comment_1", "谢谢分享"),
                                SimulatedAction("reply_comment", "offline", "comment_2", "谢谢分享", OTHER),
                                SimulatedAction("reply_comment", "offline", "comment_3", "谢谢分享", 123)])
    assert report["status"] == "completed"
    assert report["simulationStats"]["simulatedInteracted"] == 1
    assert report["simulationStats"]["skipped"] == 2
    assert calls[-1].video_url == VIDEO
    assert [action.target for action in calls[1:]] == ["comment_1"]
