"""执行层第 1 周离线验证（DESIGN-DOC.md §6 D1-D7 验收项）。

全部离线：不启动浏览器、不发起平台请求、不调用真实 LLM。
回归样本仅读取公开评论文本用于噪声判定，不落库、不触达（§0.5 红线）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.execution import compliance, params
from app.execution.api import standalone_app
from app.execution.pipeline import Pipeline
from app.execution.read_model import assert_visible_comment
from app.execution.rules import classify, filter_batch
from app.execution.scorer import (
    IntentScorer, Provider, parse_score_response,
)
from app.execution.screen_reader import ScreenReader, SelectorInvalid, load_selectors
from app.execution.store import ExecutionStore
from app.execution.write_safety import WriteSafetyGate, WriteStatus, classify_response

SAMPLE_PATH = Path(
    os.getenv(
        "LEADS_SAMPLE_PATH",
        r"D:\workbuddy\3、获客系统\阿里山获客\阿里山AI获客-源资源\_backend\live\leads-export.json",
    )
)


# ── §5 参数锁定 ───────────────────────────────────────────────
def test_params_locked_values():
    expected = {
        "ttl_days": 90,
        "read.max_items_per_second": 3,
        "engage.comment_cooldown_min_s": 300,
        "engage.comment_cooldown_max_s": 600,
        "engage.ai_submit_pause_min_s": 20,
        "engage.ai_submit_pause_max_s": 27,
        "nurture.like_probability": 0.10,
        "nurture.open_comments_probability": 0.32,
        "nurture.comments_browse_limit": 20,
        "dm.non_follower_per_person": 3,
        "dm.per_day_people": 100,
        "dm.per_hour_people": 40,
        "filter.intent_threshold": 70,
        "filter.llm_timeout_s": 10,
    }
    assert params.validate_locked(expected) == []
    assert params.READ_MIN_INTERVAL_MS == 334
    assert params.ENABLE_HUMANIZED_PACING is False


# ── 合规护栏（D5 验收：插入含 uid 的记录被拒绝）────────────────
@pytest.mark.parametrize("field", [
    "uid", "sec_uid", "webcastUid", "comment_user_id", "raw_page",
    "ipLocation", "authorProfileUrl", "messageId", "collectedFields",
    "user_secuid", "some_uid_ref", "cookie",
])
def test_uid_fields_rejected(field):
    with pytest.raises(compliance.ComplianceViolation):
        compliance.validate_candidate_payload({"video_url": "https://x", field: "1"})


def test_minimal_set_only():
    compliance.validate_candidate_payload({
        "video_url": "https://www.douyin.com/video/1",
        "nickname": "张三",
        "comment_text": "怎么合作",
    })
    with pytest.raises(compliance.ComplianceViolation):
        compliance.validate_candidate_payload({"video_url": "https://x", "phone": "138"})


def test_ci_scan_detects_uid_insert(tmp_path):
    bad = tmp_path / "bad.py"
    # 拼接构造，避免夹具字面量被生产代码的 CI 扫描误判为真实 INSERT
    bad.write_text(
        "conn.execute(" + '"INSERT INTO lead_candidates '
        '(video_url, ' + 'uid' + ', comment_text) VALUES (?,?,?)")',
        encoding="utf-8",
    )
    findings = compliance.scan_source_for_uid_fields([bad])
    assert any("uid" in f for f in findings)

    good = tmp_path / "good.py"
    good.write_text(
        'conn.execute("INSERT INTO lead_candidates (video_url, nickname, comment_text) VALUES (?,?,?)")',
        encoding="utf-8",
    )
    assert compliance.scan_source_for_uid_fields([good]) == []


def test_interface_data_rejected():
    with pytest.raises(compliance.ComplianceViolation):
        assert_visible_comment({"nickname": "a", "text": "hi", "uid": "123"})
    with pytest.raises(compliance.ComplianceViolation):
        assert_visible_comment({"nickname": "123456789", "text": "hi"})
    node = assert_visible_comment({"nickname": "小明", "text": "想了解报价"})
    assert node.text == "想了解报价"


# ── 数据层 ────────────────────────────────────────────────────
@pytest.fixture
def store(tmp_path):
    s = ExecutionStore(tmp_path / "exec.db")
    s.initialize()
    return s


def test_schema_tables(store):
    with store.connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"lead_candidates", "pipeline_runs", "comment_send_log",
            "account_quotas", "nurture_sessions"} <= names


def test_insert_dedup_and_ttl(store):
    payload = {"video_url": "https://x/video/1", "nickname": "a",
               "comment_text": "怎么合作", "intent_score": 82}
    first = store.insert_candidate(payload, now=1_000_000)
    assert first is not None
    assert store.insert_candidate(payload, now=1_000_100) is None
    rows = store.list_candidates()
    assert len(rows) == 1
    assert rows[0]["expire_at"] - rows[0]["created_at"] == params.TTL_SECONDS
    assert rows[0]["intent_tags"] is None or isinstance(rows[0]["intent_tags"], str)
    # 服务端强制 TTL，调用方传时间不生效
    with pytest.raises(compliance.ComplianceViolation):
        store.insert_candidate({**payload, "expire_at": 99999999999})


def test_purge_expired(store):
    store.insert_candidate({"video_url": "https://x/1", "comment_text": "a"}, now=100)
    store.insert_candidate({"video_url": "https://x/2", "comment_text": "b"},
                           now=100 + params.TTL_SECONDS + 10)
    assert store.purge_expired(now=100 + params.TTL_SECONDS + 5) == 1
    assert len(store.list_candidates()) == 1


def test_pipeline_run_resume(store):
    store.create_run("run_1", now=100)
    store.set_stage("run_1", "filter", {"collected": 7}, now=110)
    row = store.get_run("run_1")
    assert row["stage"] == "filter"
    assert json.loads(row["stats"])["collected"] == 7
    store.set_stage("run_1", "done", {"promoted": 3}, now=120)
    assert store.get_run("run_1")["finished_at"] == 120
    with pytest.raises(ValueError):
        store.set_stage("run_1", "bogus")


def test_quota_snapshot_delta(store):
    store.upsert_quota(1, "2026-11-13", "dm", baseline=10, snapshot=25)
    assert store.quota_delta(1, "2026-11-13", "dm") == 15
    store.upsert_quota(1, "2026-11-13", "dm", baseline=30, snapshot=25)
    assert store.quota_delta(1, "2026-11-13", "dm") == 0
    with pytest.raises(ValueError):
        store.upsert_quota(1, "2026-11-13", "bogus", 0, 0)


def test_send_log_and_nurture(store):
    with pytest.raises(ValueError):
        store.log_send(candidate_id=1, content="x", status="maybe")
    store.log_send(candidate_id=1, content="x", status="uncertain", now=1)
    store.log_nurture({"likes": 2, "videos_watched": 10}, now=1)
    with pytest.raises(ValueError):
        store.log_nurture({"uid": 3})


def test_migrate_leads_idempotent(store):
    with store.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY)")
    assert set(store.migrate_leads()) == {"intent_score", "source_candidate_id"}
    assert store.migrate_leads() == []


# ── M3 规则层（D6 验收：噪声 100% 过滤）────────────────────────
@pytest.mark.parametrize("text", ["111", "11", "1", "1111111", "!!!", "。。。",
                                  "[表情或表情包]", "https://www.douyin.com/video/123",
                                  "111已关注", "哈哈哈哈", ""])
def test_noise_cases(text):
    assert classify(text).is_noise, f"应判噪声: {text!r}"


def test_signal_cases():
    strong = classify("博主这个怎么合作，能私聊吗")
    assert not strong.is_noise and strong.base_score == 75
    assert "咨询" in strong.tags
    assert classify("干货收藏了").base_score == 55
    assert classify("今天天气不错啊朋友们").base_score == 30


def test_real_sample_noise_regression():
    """用阿里山导出的 698 条真实样本回归：'111' 类噪声必须 100% 过滤。"""
    if not SAMPLE_PATH.is_file():
        pytest.skip("回归样本不存在，跳过（CI 可用 LEADS_SAMPLE_PATH 指定）")
    data = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    texts = [str(item.get("content") or "") for item in data["leads"]]
    assert len(texts) == 698
    kept, noise = filter_batch(texts)
    digit_like = [t for t in texts if t.strip() and t.strip().replace("1", "").strip() == ""
                  or t.strip().startswith("111")]
    leaked = [t for t in digit_like if any(t == original for original, _ in kept)]
    assert not leaked, f"「111」类噪声泄漏: {leaked[:10]}"
    assert sum(noise.values()) >= 540, noise
    print(f"\n[回归] 698 条样本 → 噪声 {sum(noise.values())} 条，保留 {len(kept)} 条，分布 {noise}")


# ── M3 模型层（D7 验收：断网降级可用且标记 ai_degraded）────────
def test_parse_score_response():
    assert parse_score_response('{"score": 88, "tags": ["咨询"], "reason": "明确问价"}')[0] == 88
    wrapped = {"choices": [{"message": {"content": '{"score": 71, "tags": ["求带"]}'}}]}
    assert parse_score_response(json.dumps(wrapped))[0] == 71
    assert parse_score_response("```json\n{\"score\": 55}\n```")[0] == 55
    assert parse_score_response("not json") is None
    assert parse_score_response('{"score": 999}')[0] == 100


def _providers():
    return (
        Provider("p1", "https://p1.invalid/v1", "m1", "TEST_KEY_P1"),
        Provider("p2", "https://p2.invalid/v1", "m2", "TEST_KEY_P2"),
    )


def test_scorer_requires_two_providers():
    with pytest.raises(ValueError):
        IntentScorer(providers=_providers()[:1])


def test_scorer_degrades_when_all_fail(monkeypatch):
    monkeypatch.setenv("TEST_KEY_P1", "k1")
    monkeypatch.setenv("TEST_KEY_P2", "k2")

    def failing(*args, **kwargs):
        raise TimeoutError("simulated")

    scorer = IntentScorer(providers=_providers(), transport=failing)
    verdict = classify("怎么合作，能私聊吗")
    result = scorer.score("怎么合作，能私聊吗", verdict)
    assert result.ai_degraded is True
    assert result.intent_score == verdict.base_score == 75
    assert result.provider is None
    assert len(result.attempts) == 2
    assert not scorer.should_promote(scorer.score("今天天气不错", classify("今天天气不错")))


def test_scorer_fails_over_to_second_provider(monkeypatch):
    monkeypatch.setenv("TEST_KEY_P1", "k1")
    monkeypatch.setenv("TEST_KEY_P2", "k2")
    calls = []
    timeouts = []
    auth_headers = []

    def transport(url, payload, headers, timeout):
        calls.append(url)
        timeouts.append(timeout)
        auth_headers.append(headers.get("Authorization"))
        if "p1" in url:
            raise TimeoutError("primary down")
        return '{"score": 91, "tags": ["咨询"], "reason": "明确索取联系方式"}'

    scorer = IntentScorer(providers=_providers(), transport=transport)
    result = scorer.score("怎么合作", classify("怎么合作"))
    assert result.ai_degraded is False
    assert result.provider == "p2"
    assert result.intent_score == 91
    assert scorer.should_promote(result)
    assert len(calls) == 2
    assert timeouts == [params.FILTER.llm_timeout_s] * 2
    assert auth_headers == ["Bearer k1", "Bearer k2"]


def timeout_ok(values):
    return all(v == params.FILTER.llm_timeout_s for v in values)


def test_scorer_no_key_degrades(monkeypatch):
    monkeypatch.delenv("TEST_KEY_P1", raising=False)
    monkeypatch.delenv("TEST_KEY_P2", raising=False)
    scorer = IntentScorer(providers=_providers(), transport=lambda *a: pytest.fail("不应调用"))
    result = scorer.score("求带", classify("求带"))
    assert result.ai_degraded and all("no_key" in a for a in result.attempts)
    assert scorer.score("111", classify("111")).intent_score == 0


# ── M2 读屏引擎（D2-D4 验收：选择器失效降级不崩）──────────────
class FakePage:
    def __init__(self, nodes=None, fail_selector=None):
        self.nodes = nodes or []
        self.fail_selector = fail_selector
        self.goto_urls = []
        self.scrolls = []
        self.waits = []

    def goto(self, url):
        self.goto_urls.append(url)

    def scroll_by(self, pixels):
        self.scrolls.append(pixels)

    def wait_ms(self, ms):
        self.waits.append(ms)

    def visible_nodes(self, selector, field_map):
        if self.fail_selector and self.fail_selector in selector:
            raise RuntimeError("selector not found")
        return [dict(n) for n in self.nodes]


BASE_SELECTORS = {
    "comment_item": "#comment-item",
    "comment_text": ".text",
    "comment_nickname": ".name",
    "comment_time": ".time",
    "search_card": ".card",
    "search_title": ".title",
    "search_author": ".author",
    "search_link": "a[href*='/video/']",
}


def _reader(page, **kw):
    return ScreenReader(page, selectors=dict(BASE_SELECTORS), sleeper=lambda s: None, **kw)


def test_screen_reader_reads_visible_text():
    page = FakePage([
        {"nickname": "小A", "text": "这个怎么合作", "time_text": "3天前"},
        {"nickname": "小B", "text": "111", "time_text": "昨天"},
    ])
    reader = _reader(page)
    comments = reader.read_video_comments("https://www.douyin.com/video/1", max_n=10)
    assert len(comments) == 2
    assert comments[0].nickname == "小A"
    assert page.goto_urls == ["https://www.douyin.com/video/1"]
    assert reader.stats.nodes_kept == 2
    assert all(w >= params.READ_MIN_INTERVAL_MS for w in page.waits)


def test_screen_reader_rejects_interface_fields():
    page = FakePage([
        {"nickname": "小A", "text": "hi", "uid": "987654321"},
        {"nickname": "小B", "text": "怎么报名"},
    ])
    reader = _reader(page)
    comments = reader.read_video_comments("https://www.douyin.com/video/1", max_n=10)
    assert len(comments) == 1
    assert reader.stats.rejected == 1


def test_selector_failure_degrades_with_alert():
    alerts = []
    page = FakePage([{"nickname": "a", "text": "b"}], fail_selector="#comment-item")
    reader = _reader(page, alert=lambda code, msg: alerts.append(code))
    assert reader.read_video_comments("https://x/video/1") == []
    assert alerts == ["SELECTOR_INVALID"]
    assert "SELECTOR_INVALID" in reader.stats.alerts[0]


def test_selector_config_governance(tmp_path):
    cfg = tmp_path / "selectors.yaml"
    cfg.write_text(json.dumps({**BASE_SELECTORS, "comment_item": ""}), encoding="utf-8")
    with pytest.raises(SelectorInvalid):
        load_selectors(cfg)
    assert load_selectors()["comment_item"]


def test_search_seeds_only():
    page = FakePage([
        {"title": "AI 副业", "author": "博主",
         "video_url": "https://www.douyin.com/video/7700"},
        {"title": "bad", "author": "x", "video_url": "javascript:void(0)"},
    ])
    seeds = _reader(page).read_search_results("副业")
    assert len(seeds) == 1
    assert seeds[0].video_url.endswith("7700")


# ── M7 写入安全 ───────────────────────────────────────────────
@pytest.mark.parametrize("kwargs,expected", [
    ({"status_code": 200, "body": {"ok": True}}, WriteStatus.SENT_OK),
    ({"status_code": 400, "body": "err"}, WriteStatus.SENT_FAIL),
    ({"status_code": 200, "body": ""}, WriteStatus.UNCERTAIN),
    ({"status_code": 500, "body": ""}, WriteStatus.UNCERTAIN),
    ({"error": TimeoutError()}, WriteStatus.UNCERTAIN),
    ({"status_code": None}, WriteStatus.UNCERTAIN),
])
def test_write_classify(kwargs, expected):
    assert classify_response(**kwargs).status == expected


def test_uncertain_stops_retry():
    gate = WriteSafetyGate()
    assert gate.can_attempt("k")[0]
    gate.record("k", classify_response(status_code=200, body=""))
    allowed, reason = gate.can_attempt("k")
    assert not allowed and reason == "uncertain_pending_manual"
    assert gate.state("k") == "uncertain"
    gate.confirm_manual("k", sent=False)
    assert gate.can_attempt("k")[0]


# ── 端到端离线流水线 ──────────────────────────────────────────
def test_pipeline_end_to_end(store, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    page = FakePage([
        {"nickname": "小A", "text": "怎么合作，能私聊吗"},
        {"nickname": "小B", "text": "111"},
        {"nickname": "小C", "text": "[表情或表情包]"},
        {"nickname": "小D", "text": "干货收藏了"},
    ])
    reader = _reader(page)
    promoted = []
    pipeline = Pipeline(
        store=store,
        reader=reader,
        scorer=IntentScorer(transport=lambda *a: pytest.fail("不应调用 LLM")),
        lead_sink=type("Sink", (), {"promote": lambda self, cid, score: promoted.append(cid)})(),
    )
    report = pipeline.run("run_e2e", ["https://www.douyin.com/video/1"],
                          source_keyword="副业", max_comments_per_video=10)
    assert report.collected == 4
    assert report.noise_filtered == 2
    assert report.promoted == 1  # 只有强意向 75 分过阈值
    assert report.degraded == 2
    assert store.get_run("run_e2e")["stage"] == "done"
    rows = store.list_candidates()
    assert len(rows) == 2
    assert all("uid" not in r for r in rows)
    assert promoted


# ── API 冒烟 ──────────────────────────────────────────────────
def test_api_health_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTION_DB_PATH", str(tmp_path / "api.db"))
    client = TestClient(standalone_app)
    assert client.get("/api/execution/health").json()["ok"] is True
    assert client.get("/api/execution/pipeline/nope/status").status_code == 404
    r = client.post("/api/execution/pipeline/start", json={"run_id": "r1", "video_urls": []})
    assert r.status_code == 200
    status = client.get("/api/execution/pipeline/r1/status").json()
    assert status["stage"] in {"collect", "filter", "done", "stopped"}
    assert client.post("/api/execution/pipeline/r1/stop").json()["success"] is True
    assert client.post("/api/execution/purge").json()["deleted"] == 0
