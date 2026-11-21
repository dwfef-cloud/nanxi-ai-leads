from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_account_flow() -> None:
    # 契约：POST /api/accounts 必填 nickname（不是 name）
    created = client.post("/api/accounts", json={"nickname": "sales-1", "platform": "douyin", "notes": "main account"})
    assert created.status_code == 200
    account = created.json()
    assert account["nickname"] == "sales-1"

    # 契约：账号状态用 limit_status（healthy/warn/throttled40/safe_mode/banned），
    # 响应为 camelCase 的 limitStatus；旧的 status/risk_level 字段已废弃
    updated = client.patch(f"/api/accounts/{account['id']}", json={"limitStatus": "warn", "dailyLimit": 60})
    assert updated.status_code == 200
    assert updated.json()["limitStatus"] == "warn"
    assert updated.json()["dailyLimit"] == 60


def test_ai_draft() -> None:
    # /api/ai/draft 已升级为 SmartDraft（需 lead_id）；旧版按昵称出 3 条草稿的接口在 /draft-legacy
    response = client.post("/api/ai/draft-legacy", json={"lead_nickname": "阿强", "source_keyword": "装修", "user_note": "多少钱"})
    assert response.status_code == 200
    drafts = response.json()["drafts"]
    assert len(drafts) == 3
