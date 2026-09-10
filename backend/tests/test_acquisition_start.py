from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_start_acquisition_creates_and_runs_task() -> None:
    response = client.post(
        "/api/crawler/start",
        json={
            "keyword": "装修",
            "count": 3,
            "source_mode": "search",
            "tag": "comment",
            "note": "优先联系",
            "auto_run": True,
        },
    )
    assert response.status_code == 200
    task = response.json()
    assert task["task_type"] == "media_crawler_import"
    assert task["status"] == "done"

    leads = client.get("/api/leads").json()
    assert len([lead for lead in leads if lead["source_keyword"] == "装修"]) >= 3
