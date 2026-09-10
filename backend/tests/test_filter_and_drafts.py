from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_lead_filtering() -> None:
    client.post(
        "/api/leads",
        json={
            "nickname": "filter-user",
            "source_url": "https://www.douyin.com/video/777",
            "source_keyword": "装修",
            "platform": "douyin",
            "external_id": "filter-777",
            "note": "多少钱",
            "tags": ["hot"],
        },
    )
    response = client.get("/api/leads", params={"min_score": 30, "keyword": "装修"})
    assert response.status_code == 200
    assert any(item["nickname"] == "filter-user" for item in response.json())


def test_conversation_ai_drafts_are_saved() -> None:
    lead = client.post(
        "/api/leads",
        json={
            "nickname": "draft-user",
            "source_url": "https://www.douyin.com/video/778",
            "source_keyword": "获客",
            "platform": "douyin",
            "external_id": "draft-778",
        },
    ).json()
    conversation = client.post("/api/conversations", json={"lead_id": lead["id"], "account_name": "sales-2"}).json()

    drafts = client.post(
        f"/api/conversations/{conversation['id']}/drafts",
        json={"lead_nickname": "draft-user", "source_keyword": "获客", "user_note": "咨询"},
    )
    assert drafts.status_code == 200
    assert len(drafts.json()) == 3

    messages = client.get(f"/api/conversations/{conversation['id']}/messages")
    assert len(messages.json()) >= 3
