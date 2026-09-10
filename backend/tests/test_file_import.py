from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_file_based_media_crawler_import(tmp_path: Path) -> None:
    data_file = tmp_path / "mediacrawler.jsonl"
    data_file.write_text(
        "\n".join(
            [
                '{"nickname":"file-user-1","source_url":"https://www.douyin.com/video/801","source_keyword":"装修","platform":"douyin","external_id":"file-801","note":"报价"}',
                '{"nickname":"file-user-2","source_url":"https://www.douyin.com/video/802","source_keyword":"获客","platform":"douyin","external_id":"file-802","note":"怎么做"}',
            ]
        ),
        encoding="utf-8",
    )

    task = client.post(
        "/api/tasks",
        json={
            "task_type": "media_crawler_import",
            "payload": {"file_path": str(data_file)},
        },
    ).json()

    ran = client.post(f"/api/tasks/{task['id']}/run")
    assert ran.status_code == 200
    assert ran.json()["status"] == "done"

    leads = client.get("/api/leads").json()
    assert any(lead["external_id"] == "file-801" for lead in leads)
