from app.integrations.mediacrawler_importer import adapt_douyin_comments
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_comment_import_filters_intent_and_excluded_words() -> None:
    leads = adapt_douyin_comments(
        [
            {"comment_id": "1", "content": "想了解装修报价"},
            {"comment_id": "1", "content": "想了解装修报价"},
            {"comment_id": "2", "content": "招聘装修师傅"},
            {"comment_id": "3", "content": "这个视频不错"},
        ], "装修", "task-1", "报价,怎么联系", "招聘",
    )
    assert len(leads) == 1
    assert leads[0].external_id == "douyin-comment-1"


def test_outreach_plan_and_confirmed_action() -> None:
    lead = client.post("/api/leads", json={"nickname": "评论用户", "source_url": "https://douyin.com/video/1", "source_keyword": "装修", "note": "想了解报价"}).json()
    plan = client.get(f"/api/leads/{lead['id']}/outreach-plan")
    assert plan.status_code == 200
    assert [step["id"] for step in plan.json()["steps"]] == ["comment_reply", "private_message", "wechat_guide"]
    response = client.post(f"/api/leads/{lead['id']}/outreach-execute", json={"step": "comment_reply", "content": "可以，方便了解一下你的需求吗？", "confirmed": True})
    assert response.status_code == 200
    assert response.json()["status"] == "recorded"
