from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_crawler_demo_task_run() -> None:
    task = client.post("/api/crawler/import-demo").json()
    assert task["task_type"] == "media_crawler_import"

    ran = client.post(f"/api/tasks/{task['id']}/run")
    assert ran.status_code == 200
    assert ran.json()["status"] == "done"

    leads = client.get("/api/leads").json()
    assert len(leads) >= 2
