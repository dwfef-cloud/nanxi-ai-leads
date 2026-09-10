from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_account_flow() -> None:
    created = client.post("/api/accounts", json={"name": "sales-1", "platform": "douyin", "notes": "main account"})
    assert created.status_code == 200
    account = created.json()
    assert account["name"] == "sales-1"

    updated = client.patch(f"/api/accounts/{account['id']}", json={"status": "active", "daily_send_count": 3, "risk_level": 1})
    assert updated.status_code == 200
    assert updated.json()["status"] == "active"


def test_ai_draft() -> None:
    response = client.post("/api/ai/draft", json={"lead_nickname": "阿强", "source_keyword": "装修", "user_note": "多少钱"})
    assert response.status_code == 200
    drafts = response.json()["drafts"]
    assert len(drafts) == 3
