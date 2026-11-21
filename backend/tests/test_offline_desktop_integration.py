"""Python task -> Node DOM fixture -> shipped executor -> persistent result ledger."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.execution.offline_simulation import OfflineSimulator, SimulatedAction
from app.execution.offline_tasks import run_leadgen_offline
from app.execution.persistent_gate import PersistentWriteSafetyGate


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "desktop" / "_verify" / "offline_action_driver.js"
VIDEO = "https://www.douyin.com/video/7400000000000000001"
OTHER = "https://www.douyin.com/video/7400000000000000002"


def desktop_fixture(action, **fixture_options):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for desktop offline integration")
    completed = subprocess.run([node, str(DRIVER)], input=json.dumps({
        **asdict(action), "fixture": fixture_options}, ensure_ascii=False) + "\n",
        text=True, encoding="utf-8", capture_output=True, timeout=10, check=True)
    result = json.loads(completed.stdout)
    assert result["execution"].get("simulated") is True
    return result["receipt"]


def test_entire_comment_task_uses_desktop_execution_and_survives_restart(tmp_path):
    db = tmp_path / "offline.db"
    calls = []
    waits = []
    def fixture(action):
        calls.append(action.target)
        return desktop_fixture(action)
    arguments = dict(payload={"taskId": "offline", "accountId": "fixture-account", "keywords": ["测试"]},
        search=lambda _: [VIDEO, VIDEO + "?source=duplicate", OTHER],
        generate=lambda _: "这是离线验收生成的评论", wait=lambda seconds: waits.append(seconds) or False,
        stopped=lambda: False)
    result = run_leadgen_offline(**arguments,
        simulator=OfflineSimulator(fixture, gate=PersistentWriteSafetyGate(db)))
    assert result["status"] == "completed"
    assert result["simulationStats"]["simulatedPublished"] == 2
    assert result["stats"]["published"] == 0
    assert calls == [VIDEO, OTHER]
    assert waits == [300]
    rerun = run_leadgen_offline(**arguments,
        simulator=OfflineSimulator(fixture, gate=PersistentWriteSafetyGate(db)))
    assert rerun["simulationStats"]["simulatedPublished"] == 0
    assert rerun["simulationStats"]["already"] == 3
    assert calls == [VIDEO, OTHER]


@pytest.mark.parametrize("options", [{"emptyReceipt": True}, {"throwAfterSubmit": True},
    {"receiptVideo": "other"}, {"receiptText": "wrong text"}])
def test_unconfirmed_desktop_comment_blocks_the_rest_of_task(tmp_path, options):
    calls = []
    simulator = OfflineSimulator(lambda action: calls.append(action.target) or desktop_fixture(action, **options),
        gate=PersistentWriteSafetyGate(tmp_path / "offline.db"))
    report = run_leadgen_offline({"taskId": "offline", "accountId": "fixture-account", "videoUrls": [VIDEO, OTHER]},
        simulator=simulator, search=lambda _: [], generate=lambda _: "这是离线验收评论内容",
        wait=lambda _: False, stopped=lambda: False)
    assert report["status"] == "needs_confirmation"
    assert report["simulationStats"]["simulatedPublished"] == 0
    assert calls == [VIDEO]


@pytest.mark.parametrize("action_name", ["favorite", "copy_share_link"])
def test_nurture_actions_are_verified_by_the_desktop_module(tmp_path, action_name):
    simulator = OfflineSimulator(desktop_fixture, gate=PersistentWriteSafetyGate(tmp_path / "offline.db"))
    assert simulator.run(SimulatedAction(action_name, "fixture-account", VIDEO)) == "simulated_ok"
    assert simulator.counts[action_name] == 1
    assert "share" not in simulator.counts


def test_comment_reply_and_dm_task_all_use_desktop_fixtures_and_persistent_dedup(tmp_path):
    db = tmp_path / "interactions.db"
    actions = []
    def fixture(action):
        actions.append(action.action)
        return desktop_fixture(action)
    reply = SimulatedAction("reply_comment", "fixture-account", "comment_1", "谢谢分享", VIDEO)
    dm = SimulatedAction("dm", "fixture-account", "recipient_1", "您好，想进一步了解")
    report = run_leadgen_offline({"taskId": "interactions", "accountId": "fixture-account", "videoUrls": [VIDEO]},
        simulator=OfflineSimulator(fixture, gate=PersistentWriteSafetyGate(db)),
        search=lambda _: [], generate=lambda _: "离线生成的评论内容",
        wait=lambda _: False, stopped=lambda: False, interactions=lambda _: [reply, dm, dm])
    assert report["status"] == "completed"
    assert report["simulationStats"]["simulatedPublished"] == 1
    assert report["simulationStats"]["simulatedInteracted"] == 2
    assert report["simulationStats"]["simulatedReached"] == 1
    assert all(value == 0 for value in report["stats"].values())
    assert actions == ["post_comment", "reply_comment", "dm"]
    restarted = OfflineSimulator(fixture, gate=PersistentWriteSafetyGate(db))
    assert restarted.run(reply) == "skipped_already_sent"
    assert restarted.run(dm) == "skipped_already_sent"
    assert actions == ["post_comment", "reply_comment", "dm"]


@pytest.mark.parametrize("action_name,options", [
    ("reply_comment", {"receiptComment": "other"}),
    ("reply_comment", {"emptyReceipt": True}),
    ("dm", {"messageStatus": "pending"}),
    ("dm", {"receiptRecipient": "other"}),
    ("dm", {"throwAfterSubmit": True}),
])
def test_desktop_interaction_uncertainty_survives_restart(tmp_path, action_name, options):
    action = (SimulatedAction(action_name, "fixture-account", "comment_1", "谢谢分享", VIDEO)
              if action_name == "reply_comment" else SimulatedAction(action_name, "fixture-account", "recipient_1", "您好"))
    db = tmp_path / "uncertain.db"
    simulator = OfflineSimulator(lambda request: desktop_fixture(request, **options),
        gate=PersistentWriteSafetyGate(db))
    assert simulator.run(action) == "uncertain"
    restarted = OfflineSimulator(lambda _: pytest.fail("uncertain action retried"), gate=PersistentWriteSafetyGate(db))
    assert restarted.run(action) == "skipped_uncertain_pending_manual"
    assert simulator.counts[action_name] == 0
