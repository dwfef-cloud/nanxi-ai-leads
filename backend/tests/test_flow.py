from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_followup_and_conversation_flow() -> None:
    lead = client.post(
        "/api/leads",
        json={
            "nickname": "flow-user",
            "source_url": "https://www.douyin.com/video/321",
            "source_keyword": "咨询",
            "platform": "douyin",
            "external_id": "aweme-flow",
        },
    ).json()

    followup = client.post("/api/followups", json={"lead_id": lead["id"], "event_type": "contacted", "content": "first touch"})
    assert followup.status_code == 200

    conversation = client.post("/api/conversations", json={"lead_id": lead["id"], "account_name": "sales-1"}).json()
    assert conversation["lead_id"] == lead["id"]

    message = client.post(
        "/api/conversations/messages",
        json={"conversation_id": conversation["id"], "sender": "human", "content": "你好", "is_ai_generated": False},
    )
    assert message.status_code == 200

