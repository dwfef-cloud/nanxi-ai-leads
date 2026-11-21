"""No desktop session, network request, or production DB is used in these tests."""
from __future__ import annotations

import pytest

from app.execution.offline_simulation import (
    OfflineSimulator, SimulatedAction, classify_fixture, validate,
)
from app.execution.write_safety import WriteResult, WriteSafetyGate, WriteStatus


VIDEO = "https://www.douyin.com/video/7400000000000000001"


@pytest.mark.parametrize("action,target,text,body", [
    ("post_comment", VIDEO, "这条视频的说明很清楚", {"comment": {"cid": "c1"}}),
    ("reply_comment", "comment_1", "谢谢分享", {"comment": {"cid": "c2"}}),
    ("dm", "recipient_1", "您好，想进一步了解", {"message": {"id": "m1"}}),
    ("favorite", VIDEO, "", {"is_favorite": True}),
])
def test_offline_contract_confirms_each_action(action, target, text, body):
    calls = []

    def fixture(request):
        calls.append(request)
        return {"http_status": 200, "body": {"status_code": 0, **body}}

    simulator = OfflineSimulator(fixture)
    request = SimulatedAction(action, "account_1", target, text)
    assert simulator.run(request) == "simulated_ok"
    assert simulator.run(request) == "skipped_already_sent"
    assert calls == [request]
    assert simulator.counts[action] == 1


def test_copy_link_is_not_platform_repost():
    request = SimulatedAction("copy_share_link", "account_1", VIDEO)
    simulator = OfflineSimulator(lambda _: {"copied": True, "url": VIDEO})
    assert simulator.run(request) == "simulated_ok"
    assert simulator.counts["copy_share_link"] == 1
    assert "share" not in simulator.counts


@pytest.mark.parametrize("receipt", [
    None,
    {"http_status": 200, "body": {}},
    {"http_status": 200, "body": {"status_code": 0}},
    {"http_status": 503, "body": {"status_code": 0, "comment": {"cid": "c1"}}},
])
def test_uncertain_comment_never_retries(receipt):
    calls = []
    request = SimulatedAction("post_comment", "account_1", VIDEO, "这条内容很有帮助")
    simulator = OfflineSimulator(lambda _: calls.append(1) or receipt)
    assert simulator.run(request) == "uncertain"
    assert simulator.run(request) == "skipped_uncertain_pending_manual"
    assert calls == [1]
    assert simulator.counts["post_comment"] == 0


def test_fixture_exception_is_uncertain():
    request = SimulatedAction("dm", "account_1", "recipient_1", "您好")
    def crash(_):
        raise TimeoutError("fixture timeout")
    simulator = OfflineSimulator(crash)
    assert simulator.run(request) == "uncertain"
    assert simulator.run(request) == "skipped_uncertain_pending_manual"


def test_explicit_decline_can_be_retried_but_is_not_counted():
    request = SimulatedAction("favorite", "account_1", VIDEO)
    simulator = OfflineSimulator(lambda _: {
        "http_status": 403, "body": {"status_code": 1001}})
    assert simulator.run(request) == "simulated_fail"
    assert simulator.run(request) == "simulated_fail"
    assert simulator.counts["favorite"] == 0


@pytest.mark.parametrize("action_request", [
    SimulatedAction("post_comment", "account_1", "http://www.douyin.com/video/7400000000000000001", "有效评论内容"),
    SimulatedAction("post_comment", "account_1", VIDEO, "短"),
    SimulatedAction("dm", "", "recipient_1", "您好"),
    SimulatedAction("dm", "account_1", "https://evil.example/", "您好"),
    SimulatedAction("copy_share_link", "account_1", VIDEO, "不应有文案"),
])
def test_invalid_input_never_calls_fixture(action_request):
    def forbidden(_):
        raise AssertionError("invalid input reached fixture")
    assert validate(action_request)
    assert OfflineSimulator(forbidden).run(action_request).startswith("skipped_")


def test_reservation_blocks_duplicate_before_receipt():
    gate = WriteSafetyGate()
    assert gate.reserve("comment:a:v") == (True, "allowed")
    assert gate.reserve("comment:a:v") == (False, "uncertain_pending_manual")
    gate.record("comment:a:v", WriteResult(WriteStatus.SENT_FAIL, True, False, "declined"))
    assert gate.reserve("comment:a:v") == (True, "allowed")


def test_confirmation_requires_action_specific_evidence():
    post = SimulatedAction("post_comment", "a", VIDEO, "有效评论内容")
    assert classify_fixture(post, {"http_status": 200,
                                   "body": {"status_code": 0, "message": {"id": "m1"}}}) == "uncertain"
