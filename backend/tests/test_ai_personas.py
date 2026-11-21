"""AI 智能体（人设库）持久化 + 试跑配额：桥接契约与存储行为。

对应缺陷：页面点「保存配置」报「智能体未能写入本地数据库」——
save-ai-agents / delete-ai-agent 未接线的历史问题。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app import ai_personas
from app.api import ipc_bridge as bridge


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "lead_system.db"
    sqlite3.connect(path).close()
    return path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "bridge.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(bridge, "DB_PATH", path)
    with TestClient(bridge_server.app) as c:
        yield c, path


def _post(client, channel, args=None):
    resp = client.post(f"/ipc/{channel}", json={"args": args or []})
    assert resp.status_code == 200
    return resp.json()


def test_task_model_settings_persist_endpoint_without_returning_key(client):
    c, db = client
    result = _post(c, "save-task-model-settings", [{"base_url": "https://model.example/v1/",
                    "model": "model-one", "api_key": "fixture-private-key"}])
    assert result["data"]["success"]
    assert "fixture-private-key" not in str(result)
    assert ai_personas._llm_settings(db)["base_url"] == "https://model.example/v1"
    result = _post(c, "save-task-model-settings", [{"base_url": "https://model.example/v2",
                    "model": "model-two", "api_key": ""}])
    assert result["data"]["success"]
    assert ai_personas._llm_settings(db)["api_key"] == "fixture-private-key"
    public = _post(c, "get-task-model-settings")["data"]
    assert public["hasKey"] and public["model"] == "model-two"
    assert "api_key" not in public
    rejected = _post(c, "save-task-model-settings", [{"base_url": "https://secret@model.example/v1", "model": "m"}])
    assert not rejected["data"]["success"]


PERSONAS = [
    {"id": "p1", "name": "业务咨询助理", "role": "智能体开发",
     "goal": "我要找有真实需求的潜在客户", "prompt": "先听后说，别硬推"},
    {"id": "p2", "name": "内容助理", "role": "短视频运营", "goal": "涨粉"},
]


# ── 存储层 ─────────────────────────────────────────────────
def test_empty_db_returns_none_so_frontend_bootstraps(db):
    assert ai_personas.load_task_config(db) is None


def test_save_and_reload_personas(db):
    ok, count = ai_personas.save_personas(db, PERSONAS)
    assert ok and count == 2
    cfg = ai_personas.load_task_config(db)
    assert [p["id"] for p in cfg["personas"]] == ["p1", "p2"]
    assert cfg["personas"][0]["prompt"] == "先听后说，别硬推"


def test_persona_without_id_is_dropped(db):
    ok, count = ai_personas.save_personas(
        db, [{"name": "没人设id"}, {"id": "p1", "name": "正常"}])
    assert ok and count == 1


def test_task_config_merge_does_not_drop_personas(db):
    """前端后台自动保存的 task_config 不含 personas，合并时不能被抹掉。"""
    ai_personas.save_personas(db, PERSONAS)
    assert ai_personas.save_task_config(db, {"dmTemplates": ["你好"]})
    cfg = ai_personas.load_task_config(db)
    assert len(cfg["personas"]) == 2
    assert cfg["dmTemplates"] == ["你好"]


def test_delete_persona_clears_binding_and_sample(db):
    ai_personas.save_personas(db, PERSONAS)
    ai_personas.save_task_config(db, {
        "accountPersonas": {"acc-1": "p1", "acc-2": "p2"},
        "personaTestConfigs": {"p1": {"mode": "reply"}},
    })
    assert ai_personas.delete_persona(db, "p1")
    cfg = ai_personas.load_task_config(db)
    assert [p["id"] for p in cfg["personas"]] == ["p2"]
    assert cfg["accountPersonas"] == {"acc-2": "p2"}
    assert "p1" not in cfg["personaTestConfigs"]


def test_delete_unknown_persona_keeps_data(db):
    ai_personas.save_personas(db, PERSONAS)
    assert ai_personas.delete_persona(db, "nope")
    assert len(ai_personas.load_task_config(db)["personas"]) == 2


# ── 配额 ───────────────────────────────────────────────────
def test_quota_free_is_zero(db):
    q = ai_personas.persona_test_quota(db, {"isFree": True})
    assert q["limit"] == 0 and q["remaining"] == 0


def test_quota_trial_and_pro(db):
    assert ai_personas.persona_test_quota(db, {"isTrial": True})["limit"] == 20
    pro = ai_personas.persona_test_quota(db, {"isFree": False, "isTrial": False})
    assert pro["limit"] == 200 and pro["licenseType"] == "pro"
    # 授权信息缺失时按未激活处理（宁可不给次数，也不白送额度）
    assert ai_personas.persona_test_quota(db, {})["limit"] == 0


def test_quota_consume_decrements(db):
    q = ai_personas.persona_test_quota(db, {"isTrial": True})
    q2 = ai_personas._consume_quota(db, q)
    assert q2["used"] == 1 and q2["remaining"] == 19
    assert ai_personas.persona_test_quota(db, {"isTrial": True})["remaining"] == 19


# ── 试跑：无凭据必须如实失败，不伪造结果 ─────────────────────
def test_test_run_without_credentials_fails_honestly(db):
    res = ai_personas.run_persona_test(db, {"mode": "reply", "comment": "多少钱"},
                                       {"isTrial": True})
    assert res["success"] is False
    assert "模型凭据" in res["msg"]
    assert res["personaTestQuota"]["used"] == 0  # 失败不扣次数


# ── 桥接契约 ───────────────────────────────────────────────
def test_save_ai_agents_returns_strict_true(client):
    c, db_path = client
    packet = _post(c, "save-ai-agents", [PERSONAS])
    assert packet["data"] is True
    assert len(ai_personas.load_task_config(db_path)["personas"]) == 2


def test_delete_ai_agent_returns_strict_true(client):
    c, db_path = client
    _post(c, "save-ai-agents", [PERSONAS])
    assert _post(c, "delete-ai-agent", ["p1"])["data"] is True
    assert [p["id"] for p in ai_personas.load_task_config(db_path)["personas"]] == ["p2"]


def test_save_ai_agents_without_payload_does_not_write(client):
    c, db_path = client
    assert _post(c, "save-ai-agents", [])["data"] is False
    assert ai_personas.load_task_config(db_path) is None


def test_empty_task_config_does_not_pollute_db(client):
    c, db_path = client
    _post(c, "save-task-config", [])
    assert ai_personas.load_task_config(db_path) is None


def test_get_task_config_null_then_initialized(client):
    c, _ = client
    assert _post(c, "get-task-config", [{"includeStorageState": True}])["data"] is None
    _post(c, "save-ai-agents", [PERSONAS])
    data = _post(c, "get-task-config", [])["data"]
    assert data["aiInitialized"] is True
    assert len(data["config"]["personas"]) == 2


def test_persona_test_quota_channel_shape(client):
    c, _ = client
    data = _post(c, "get-persona-test-quota", [])["data"]
    assert data["success"] is True
    assert set(data["data"]) >= {"limit", "used", "remaining", "licenseType"}
