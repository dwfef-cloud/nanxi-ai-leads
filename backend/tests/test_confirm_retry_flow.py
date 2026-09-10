from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_confirm_send_and_conversation_status() -> None:
    lead = client.post(
        "/api/leads",
        json={
            "nickname": "confirm-user",
            "source_url": "https://www.douyin.com/video/990",
            "source_keyword": "合作",
            "platform": "douyin",
            "external_id": "confirm-990",
        },
    ).json()
    conversation = client.post("/api/conversations", json={"lead_id": lead["id"], "account_name": "default"}).json()

    updated = client.patch(f"/api/conversations/{conversation['id']}", json={"status": "waiting"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "waiting"

    sent = client.post(f"/api/conversations/{conversation['id']}/confirm-send", json={"content": "已发送"})
    assert sent.status_code == 200
    assert sent.json()["content"] == "已发送"


def test_retry_failed_task() -> None:
    task = client.post(
        "/api/tasks",
        json={"task_type": "unsupported", "payload": {}},
    ).json()
    failed = client.post(f"/api/tasks/{task['id']}/run")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"

    retry = client.post(f"/api/tasks/{task['id']}/retry")
    assert retry.status_code == 200
    assert retry.json()["status"] == "failed"
    assert retry.json()["error_message"]
