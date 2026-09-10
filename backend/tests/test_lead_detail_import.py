from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_lead_detail_and_text_import() -> None:
    lead = client.post(
        "/api/leads",
        json={
            "nickname": "detail-user",
            "source_url": "https://www.douyin.com/video/555",
            "source_keyword": "合作",
            "platform": "douyin",
            "external_id": "detail-555",
            "note": "报价",
        },
    ).json()

    detail = client.get(f"/api/leads/{lead['id']}")
    assert detail.status_code == 200
    assert detail.json()["nickname"] == "detail-user"

    text = """
    {"nickname":"jsonl-user","source_url":"https://www.douyin.com/video/556","source_keyword":"装修","platform":"douyin","external_id":"jsonl-556","note":"多少钱"}
    """
    imported = client.post("/api/leads/import/text", json={"content": text})
    assert imported.status_code == 200
    assert imported.json()[0]["nickname"] == "jsonl-user"
