from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_and_list_lead() -> None:
    payload = {
        "nickname": "test-user",
        "source_url": "https://www.douyin.com/video/123",
        "source_keyword": "装修",
        "platform": "douyin",
        "external_id": "aweme-123",
        "tags": ["high-intent"],
        "note": "from comment",
    }
    created = client.post("/api/leads", json=payload)
    assert created.status_code == 200
    lead = created.json()
    assert lead["nickname"] == "test-user"

    listed = client.get("/api/leads")
    assert listed.status_code == 200
    assert len(listed.json()) >= 1


def test_deduplicate_external_lead() -> None:
    payload = {
        "nickname": "dup-user",
        "source_url": "https://www.douyin.com/video/999",
        "source_keyword": "获客",
        "platform": "douyin",
        "external_id": "aweme-dup",
    }
    first = client.post("/api/leads", json=payload)
    second = client.post("/api/leads", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_import_raw_media_crawler_payload() -> None:
    payload = [
        {
            "nickname": "crawler-user",
            "source_url": "https://www.douyin.com/video/456",
            "source_keyword": "获客",
            "platform": "douyin",
            "external_id": "aweme-456",
            "tags": ["comment"],
            "note": "想了解价格",
        }
    ]
    response = client.post("/api/leads/import/raw", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data[0]["score"] >= 30


def test_create_task() -> None:
    response = client.post("/api/tasks", json={"task_type": "crawl", "payload": {"keyword": "装修"}})
    assert response.status_code == 200
    data = response.json()
    assert data["task_type"] == "crawl"

    patched = client.patch(f"/api/tasks/{data['id']}", json={"status": "done"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "done"
