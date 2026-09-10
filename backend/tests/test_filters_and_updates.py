from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_lead_filter_and_update_note_tags() -> None:
    lead = client.post(
        "/api/leads",
        json={
            "nickname": "update-user",
            "source_url": "https://www.douyin.com/video/1234",
            "source_keyword": "投放",
            "platform": "douyin",
            "external_id": "update-1234",
            "note": "报价",
            "tags": ["old"],
        },
    ).json()

    filtered = client.get("/api/leads", params={"keyword": "投放", "min_score": 30})
    assert filtered.status_code == 200
    assert any(item["id"] == lead["id"] for item in filtered.json())

    updated = client.patch(
        f"/api/leads/{lead['id']}",
        json={"note": "新的备注", "tags": ["new", "hot"]},
    )
    assert updated.status_code == 200
    data = updated.json()
    assert data["note"] == "新的备注"
    assert data["tags"] == ["new", "hot"]
