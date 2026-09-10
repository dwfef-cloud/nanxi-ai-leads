from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_create_import_task_from_ui_payload() -> None:
    response = client.post(
        "/api/tasks",
        json={
            "task_type": "media_crawler_import",
            "payload": {"raw_text": '[{"nickname":"ui-user","source_url":"https://www.douyin.com/video/901","source_keyword":"合作","platform":"douyin","external_id":"ui-901"}]'},
        },
    )
    assert response.status_code == 200
    task = response.json()
    assert task["task_type"] == "media_crawler_import"
