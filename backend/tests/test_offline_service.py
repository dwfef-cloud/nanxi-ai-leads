"""Visible offline workflow: own DB, cancellation, restarts and live isolation."""
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import offline_runs
from app.execution.offline_service import OfflineRunService, TERMINAL
from app.runtime import browser


def finish(service, run_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = service.get(run_id)
        if result["status"] in TERMINAL:
            return result
        time.sleep(.01)
    pytest.fail("offline worker did not finish")


def confirmed(action, scenario):
    body = {"status_code": 0}
    if action.action in {"post_comment", "reply_comment"}:
        body["comment"] = {"cid": "offline-comment"}
    elif action.action == "dm":
        body["message"] = {"id": "offline-message"}
    else:
        body["is_favorite"] = True
    receipt = ({"copied": True, "url": action.target} if action.action == "copy_share_link"
               else {"http_status": 200, "body": body})
    return {"execution": {"simulated": True, "code": "fixture_confirmed"}, "receipt": receipt}


@pytest.fixture
def service(tmp_path):
    instance = OfflineRunService(tmp_path / "offline.db", pace=0)
    yield instance
    instance.shutdown()


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setattr(offline_runs, "_service", service)
    app = FastAPI()
    app.include_router(offline_runs.router)
    return TestClient(app)


def test_actual_node_desktop_fixtures_run_all_actions_without_live_transport(service, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline run reached a live browser/queue")
    monkeypatch.setattr(browser, "execute", forbidden)
    monkeypatch.setattr(browser.desktop_commands, "publish", forbidden)
    item = finish(service, service.start()["id"])
    assert item["status"] == "completed"
    assert set(item["stats"].values()) == {2}
    assert len(item["logs"]) == 10
    assert not any(item["realStats"].values())
    assert item["progress"] == 100


def test_same_sample_remains_deduplicated_after_restart(tmp_path):
    db = tmp_path / "offline.db"
    service = OfflineRunService(db, driver=confirmed, pace=0)
    first = finish(service, service.start()["id"])
    service.shutdown()
    restarted = OfflineRunService(db, driver=confirmed, pace=0)
    assert restarted.get(first["id"])["stats"] == first["stats"]
    again = finish(restarted, restarted.start()["id"])
    assert again["status"] == "completed"
    assert again["already"] == 6  # duplicate parent comments skip their reply/DM plans
    assert not any(again["stats"].values())
    second_sample = finish(restarted, restarted.start(sample="B")["id"])
    assert set(second_sample["stats"].values()) == {2}
    restarted.shutdown()


def test_uncertain_stops_and_stays_blocked_after_restart(service):
    first = finish(service, service.start(scenario="uncertain")["id"])
    assert first["status"] == "needs_confirmation"
    assert len(first["logs"]) == 1
    assert not any(first["stats"].values())
    again = finish(service, service.start(scenario="uncertain")["id"])
    assert again["logs"][0]["status"] == "skipped_uncertain_pending_manual"
    assert again["status"] == "needs_confirmation"


def test_declined_comment_does_not_count_or_start_interactions(service):
    item = finish(service, service.start(suite="leadgen", scenario="declined")["id"])
    assert item["status"] == "failed"
    assert item["failed"] == 2
    assert not any(item["stats"].values())
    assert [row["label"] for row in item["logs"]] == ["发表评论", "发表评论"]


def test_stop_before_first_action_is_immediate_and_does_not_reserve(tmp_path):
    calls = []
    service = OfflineRunService(tmp_path / "offline.db", driver=lambda *args: calls.append(args), pace=2)
    item = service.start()
    service.stop(item["id"])
    ended = finish(service, item["id"])
    assert ended["status"] == "stopped"
    assert calls == []
    assert ended["logs"] == []
    service.shutdown()


def test_stop_during_action_records_confirmation_but_starts_no_followup(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def driver(action, scenario):
        calls.append(action)
        entered.set()
        assert release.wait(3)
        return confirmed(action, scenario)
    service = OfflineRunService(tmp_path / "offline.db", driver=driver, pace=0)
    item = service.start()
    assert entered.wait(3)
    assert service.stop(item["id"])["status"] == "stopping"
    release.set()
    ended = finish(service, item["id"])
    assert ended["status"] == "stopped"
    assert ended["stats"]["simulatedComments"] == 1
    assert len(calls) == 1
    service.shutdown()


def test_restart_marks_stale_runs_interrupted_instead_of_resending(tmp_path):
    db = tmp_path / "offline.db"
    service = OfflineRunService(db, driver=confirmed, pace=0)
    item = finish(service, service.start()["id"])
    service.shutdown()
    item["status"] = "running"
    service._save(item)
    restarted = OfflineRunService(db, driver=lambda *_: pytest.fail("restart resent actions"), pace=0)
    assert restarted.get(item["id"])["status"] == "interrupted"
    assert restarted.get(item["id"])["logs"][-1]["status"] == "interrupted"
    restarted.shutdown()


@pytest.mark.parametrize("options", [{"allowWrite": True}, {"mode": "live"}, {"accountId": "existing"},
                                     {"videoUrl": "https://www.douyin.com/video/123456"},
                                     {"scenario": "arbitrary-script"}, {"sample": "../../production"}])
def test_api_rejects_live_flags_accounts_urls_and_unknown_scenarios(client, options):
    assert client.post("/api/offline/runs", json=options).status_code == 422
    assert client.get("/api/offline/runs").json()["runs"] == []


def test_api_same_origin_start_read_stop_and_missing_record(client, service):
    service.pace = 2
    assert client.post("/api/offline/runs", json={}, headers={"Origin": "https://foreign.example"}).status_code == 403
    first = client.post("/api/offline/runs", json={}, headers={"Origin": "http://testserver"})
    assert first.status_code == 201
    key = first.json()["id"]
    assert client.post("/api/offline/runs", json={}).status_code == 409
    assert client.get("/api/offline/runs/" + key).json()["mode"] == "offline"
    assert client.post("/api/offline/runs/" + key + "/stop", json={}).status_code == 200
    assert finish(service, key)["status"] == "stopped"
    assert client.get("/api/offline/runs/missing").status_code == 404
    assert client.post("/api/offline/runs/missing/stop", json={}).status_code == 404


def test_nurture_decline_option_reports_input_error_without_creating_run(client):
    assert client.post("/api/offline/runs", json={"suite": "nurture", "scenario": "declined"}).status_code == 422
    assert client.get("/api/offline/runs").json()["runs"] == []


def test_second_host_preserves_active_owner_and_can_stop_it(tmp_path):
    db = tmp_path / "shared-offline.db"
    owner = OfflineRunService(db, driver=confirmed, pace=2)
    item = owner.start()
    observer = OfflineRunService(db, driver=confirmed, pace=0)
    assert observer.get(item["id"])["status"] == "running"
    with pytest.raises(RuntimeError, match="已有离线测试"):
        observer.start()
    observer.stop(item["id"])
    assert finish(owner, item["id"])["status"] == "stopped"
    owner.shutdown()
    observer.shutdown()
