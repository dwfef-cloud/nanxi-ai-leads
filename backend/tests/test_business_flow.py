from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_business_profile_can_be_saved_and_read() -> None:
    payload = {
        "industry": "装修",
        "product": "全屋装修",
        "service_area": "深圳",
        "target_customer": "准备装修的新房业主",
        "price_range": "8万-30万",
        "conversion_goal": "添加微信",
        "tone": "专业、真诚",
    }
    saved = client.put("/api/business/profile", json=payload)
    assert saved.status_code == 200
    assert saved.json()["industry"] == "装修"
    assert client.get("/api/business/profile").json()["target_customer"] == "准备装修的新房业主"


def test_lead_followup_fields_and_daily_metrics() -> None:
    lead = client.post(
        "/api/leads",
        json={
            "nickname": "business-flow-user",
            "source_url": "https://www.douyin.com/video/business-flow",
            "source_keyword": "装修",
            "platform": "douyin",
            "external_id": "business-flow-1",
            "note": "多少钱",
        },
    ).json()
    followup_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    updated = client.patch(
        f"/api/leads/{lead['id']}",
        json={
            "customer_need": "想了解价格和交付周期",
            "next_followup_at": followup_at,
            "status": "follow_up",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["customer_need"] == "想了解价格和交付周期"
    overview = client.get("/api/overview").json()
    assert overview["pending_followups"] >= 1
