from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_detail_endpoints() -> None:
    lead = client.post(
        "/api/leads",
        json={
            "nickname": "detail-endpoint-user",
            "source_url": "https://www.douyin.com/video/900",
            "source_keyword": "咨询",
            "platform": "douyin",
            "external_id": "detail-endpoint-900",
        },
    ).json()
    account = client.post("/api/accounts", json={"name": "acct-detail", "platform": "douyin"}).json()
    conversation = client.post("/api/conversations", json={"lead_id": lead["id"], "account_name": "acct-detail"}).json()
    task = client.post("/api/tasks", json={"task_type": "demo", "payload": {}}).json()

    assert client.get(f"/api/leads/{lead['id']}").status_code == 200
    assert client.get(f"/api/accounts/{account['id']}").status_code == 200
    assert client.get(f"/api/conversations/{conversation['id']}").status_code == 200
    assert client.get(f"/api/tasks/{task['id']}").status_code == 200
    cancelled = client.post(f"/api/tasks/{task['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "paused"
