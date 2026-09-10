from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_analytics_report_has_business_dimensions() -> None:
    client.post("/api/demo/seed-report-data")
    response = client.get("/api/analytics/report")

    assert response.status_code == 200
    report = response.json()
    assert report["summary"]["leads"] >= 12
    assert {"leads", "qualified", "replied", "follow_up", "won"} <= report["funnel"].keys()
    assert report["sources"]
    assert report["regions"]
    assert report["daily"]
    assert report["rfm"]
    assert set(report["emotions"]) == {"积极明确", "观望比较", "低意向"}
    assert report["emotion_basis"]
    assert report["pain_points"]
    assert "cumulative_rate" in report["pain_points"][0]
    assert len(report["insights"]) == 3
    assert report["path"][0]["from"] == "内容线索"
    assert report["cohorts"]
    assert {"date", "users", "d1", "d3", "d7"} <= report["cohorts"][0].keys()
    assert report["cohorts"][0]["basis"] in {"基于用户行为事件", "当前阶段代理留存"}
    limited = client.get("/api/analytics/report?days=7").json()
    assert limited["period_days"] == 7


def test_behavior_events_can_be_recorded() -> None:
    lead = client.get("/api/leads").json()[0]
    response = client.post("/api/behavior-events", json={"lead_id": lead["id"], "event_type": "comment", "content": "想了解报价"})
    assert response.status_code == 200
    assert response.json()["event_type"] == "comment"
    report = client.get("/api/analytics/report").json()
    assert report["event_counts"]["comment"] >= 1
    assert {"recency", "frequency", "monetary"} <= report["rfm"][0].keys()


def test_behavior_event_requires_existing_lead() -> None:
    response = client.post("/api/behavior-events", json={"lead_id": "missing", "event_type": "comment"})
    assert response.status_code == 404
