"""互动模块第 2 周离线验证（DESIGN-DOC.md §6 D8-D14 验收项）。

全部离线：OfflineDriver 不启动浏览器、不发起平台请求；
AI 生成走注入 transport，不调用真实 LLM、不需要 API key。
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.engagement.ai_comment import AICommentGenerator
from app.engagement.engine import EngineManager, OfflineDriver
from app.engagement.humanize import (
    comment_cooldown_s, input_box_point, keystroke_delay_ms,
    probe_interval_s, submit_pause_s,
)
from app.engagement.quotas import QuotaLedger, QuotaPolicy
from app.engagement.workers import CommentWorker, LikeWorker, WorkerDeps
from app.execution.params import ENGAGEMENT
from app.execution.scorer import Provider
from app.execution.store import ExecutionStore
from app.execution.write_safety import WriteStatus


@pytest.fixture()
def env(tmp_path: Path):
    store = ExecutionStore(tmp_path / "t.db")
    store.initialize()
    ledger = QuotaLedger(store, QuotaPolicy(like_daily_limit=3, comment_daily_limit=2))
    drivers: dict[int, OfflineDriver] = {}
    engine = EngineManager(tmp_path / "profiles",
                           driver_factory=lambda: OfflineDriver())
    deps = WorkerDeps(store=store, ledger=ledger, engine=engine)
    return store, ledger, engine, deps, drivers


def _candidate(store: ExecutionStore, *, score: int = 90, text: str = "想学这个副业在哪报名") -> dict:
    cid = store.insert_candidate({
        "video_url": "https://www.douyin.com/video/1001",
        "nickname": "路人甲",
        "comment_text": text,
        "intent_score": score,
        "intent_tags": ["副业"],
        "source_keyword": "副业",
    })
    rows = {int(r["id"]): r for r in store.list_candidates()}
    return rows[cid]


def _gen_ok(transport_response: str = '{"choices":[{"message":{"content":"{\\"text\\": \\"这个思路讲得很细，收藏了\\"}"}}]}'):
    p = Provider("test", "http://unit.test", "m", "UNIT_KEY")
    import os
    os.environ["UNIT_KEY"] = "k"
    return AICommentGenerator(providers=(p,), transport=lambda *a, **k: transport_response)


# ── D8 引擎：profile 隔离 + 租约 ────────────────────────────
class TestEngine:
    def test_双账号交替执行互不串_profile(self, env):
        _, _, engine, _, _ = env
        now = 1_000_000
        l1 = engine.acquire(1, now=now)
        l2 = engine.acquire(2, now=now)
        assert l1.profile_dir != l2.profile_dir
        assert l1.profile_dir.name == "1" and l2.profile_dir.name == "2"

    def test_同账号租约互斥(self, env):
        _, _, engine, _, _ = env
        engine.acquire(1, now=1_000_000)
        with pytest.raises(RuntimeError):
            engine.acquire(1, now=1_000_001)

    def test_崩溃租约心跳超时自动回收(self, env):
        _, _, engine, _, _ = env
        engine.acquire(1, now=1_000_000)
        engine.heartbeat(1, now=1_000_029)
        assert engine.lease_of(1, now=1_000_059) is not None
        # 心跳 30s 超时 → 回收，可重新租约
        assert engine.lease_of(1, now=1_000_060) is None
        engine.acquire(1, now=1_000_061)
        assert engine.lease_of(1, now=1_000_062) is not None

    def test_release后可重新租约(self, env):
        _, _, engine, _, _ = env
        engine.acquire(1, now=1_000_000)
        assert engine.release(1) is True
        engine.acquire(1, now=1_000_001)


# ── D11 humanize：§5.1 参数落界 ─────────────────────────────
class TestHumanize:
    def test_逐字延迟落在80到200ms(self):
        rng = random.Random(7)
        for _ in range(500):
            v = keystroke_delay_ms(rng)
            assert ENGAGEMENT.keystroke_delay_min_ms <= v <= ENGAGEMENT.keystroke_delay_max_ms

    def test_提交停顿落在20到27s(self):
        rng = random.Random(7)
        for _ in range(500):
            assert ENGAGEMENT.ai_submit_pause_min_s <= submit_pause_s(rng) <= ENGAGEMENT.ai_submit_pause_max_s

    def test_冷却落在300到600s(self):
        rng = random.Random(7)
        for _ in range(500):
            assert ENGAGEMENT.comment_cooldown_min_s <= comment_cooldown_s(rng) <= ENGAGEMENT.comment_cooldown_max_s

    def test_探测间隔落在12到13s(self):
        rng = random.Random(7)
        for _ in range(200):
            assert ENGAGEMENT.comment_probe_min_s <= probe_interval_s(rng) <= ENGAGEMENT.comment_probe_max_s

    def test_输入框坐标每次实时计算不缓存(self):
        rng = random.Random(42)
        pts = {input_box_point((100, 200, 400, 60), rng) for _ in range(50)}
        assert len(pts) > 1  # 同一 rect 多次调用产生不同坐标
        for x, y in pts:
            assert 100 <= x < 500 and 200 <= y < 260


# ── D9 配额账本：快照对账 + 限额闸门 ────────────────────────
class TestQuotas:
    def test_本地记账与剩余额度(self, env):
        _, ledger, _, _, _ = env
        ledger.consume(1, "like", now=1_000_000)
        ledger.consume(1, "like", now=1_000_001)
        assert ledger.used(1, "like", now=1_000_002) == 2
        assert ledger.remaining(1, "like", now=1_000_002) == 1
        ok, _ = ledger.can_do(1, "like", now=1_000_003)
        assert ok
        ok, why = ledger.can_do(1, "like", n=2, now=1_000_003)
        assert not ok and "quota_exhausted" in why

    def test_平台观测值覆盖校准_只增不减delta(self, env):
        _, ledger, _, _, _ = env
        ledger.set_platform_baseline(1, "like", 100, now=1_000_000)
        ledger.consume(1, "like", now=1_000_001)
        assert ledger.used(1, "like", now=1_000_002) == 1
        # 平台侧观测到 105（本地只记了 1）→ 对账修正为 5
        delta = ledger.record_platform_observation(1, "like", 105, now=1_000_003)
        assert delta == 5 and ledger.used(1, "like", now=1_000_004) == 5
        # 观测值倒退（105→103）不得把 used 缩回去
        ledger.record_platform_observation(1, "like", 103, now=1_000_005)
        assert ledger.used(1, "like", now=1_000_006) == 3

    def test_dm限额取官方锁定值(self, env):
        _, ledger, _, _, _ = env
        from app.execution.params import DM_LIMITS
        assert ledger.policy.limit_of("dm") == DM_LIMITS.per_day_people == 100

    def test_跨天清零(self, env):
        _, ledger, _, _, _ = env
        day0 = 1_754_000_000  # 固定锚点，跨天用偏移
        ledger.consume(1, "like", now=day0)
        assert ledger.used(1, "like", now=day0 + 86_400) == 0


# ── D9 like_worker：三查 + 配额耗尽自动停 ───────────────────
class TestLikeWorker:
    def test_成功点赞并记账(self, env):
        store, _, engine, deps, drivers = env
        now = 1_000_000
        lease = engine.acquire(1, now=now)
        drivers[1] = lease.driver
        c = _candidate(store)
        r = LikeWorker(deps).run_one(c, 1, now=now + 1)
        assert r.status == "sent_ok"
        with store.connect() as conn:
            used = conn.execute(
                "SELECT snapshot FROM account_quotas WHERE account_id=1 AND metric='like'"
            ).fetchone()
        assert used["snapshot"] == 1

    def test_配额耗尽自动停(self, env):
        store, _, engine, deps, _ = env
        now = 1_000_000
        engine.acquire(1, now=now)
        worker = LikeWorker(deps)
        results = [worker.run_one(_candidate(store, text=f"评论文本{i}"), 1, now=now + 1 + i)
                   for i in range(5)]  # 限额 3
        statuses = [r.status for r in results]
        assert statuses[:3] == ["sent_ok"] * 3
        assert statuses[3] == "skipped_quota"

    def test_风险事件拦截(self, env):
        store, _, engine, deps, _ = env
        deps.risk_check = lambda aid: ["风控警告"]
        engine.acquire(1, now=1_000_000)
        r = LikeWorker(deps).run_one(_candidate(store), 1, now=1_000_001)
        assert r.status == "skipped_risk"

    def test_无租约不动作(self, env):
        store, _, _, deps, _ = env
        r = LikeWorker(deps).run_one(_candidate(store), 1, now=1_000_000)
        assert r.status == "skipped_no_lease"

    def test_重复点赞被幂等闸门拦截(self, env):
        store, _, engine, deps, _ = env
        now = 1_000_000
        engine.acquire(1, now=now)
        worker = LikeWorker(deps)
        c = _candidate(store)
        assert worker.run_one(c, 1, now=now + 1).status == "sent_ok"
        r = worker.run_one(c, 1, now=now + 2)
        assert r.status == "skipped_dup"


# ── D10+D12 comment_worker：AI 生成 + uncertain 语义 ────────
class TestCommentWorker:
    def test_成功评论_落库_记账_冷却生效(self, env):
        store, _, engine, deps, _ = env
        now = 1_000_000
        engine.acquire(1, now=now)
        worker = CommentWorker(deps, generator=_gen_ok())
        r = worker.run_one(_candidate(store), 1, now=now + 1)
        assert r.status == "sent_ok"
        assert r.content
        with store.connect() as conn:
            log = conn.execute("SELECT status,content FROM comment_send_log").fetchone()
        assert log["status"] == "sent_ok" and log["content"] == r.content
        assert worker.cooldown_remaining(1, now=now + 2) > 0
        # 冷却期内再评 → skipped
        c2 = _candidate(store, text="另一条评论的内容")
        assert worker.run_one(c2, 1, now=now + 3).status == "skipped_cooldown"

    def test_AI生成失败跳过_不用模板兜底(self, env):
        store, _, engine, deps, _ = env
        engine.acquire(1, now=1_000_000)
        p = Provider("test", "http://unit.test", "m", "UNIT_KEY")
        gen = AICommentGenerator(providers=(p,), transport=lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
        r = CommentWorker(deps, generator=gen).run_one(_candidate(store), 1, now=1_000_001)
        assert r.status == "skipped_no_ai"

    def test_uncertain不重试且触发告警(self, env):
        store, _, engine, deps, _ = env
        alerts: list[tuple[str, str]] = []
        deps.alert = lambda code, msg: alerts.append((code, msg))
        now = 1_000_000
        engine.acquire(1, now=now)
        # 驱动返回 5xx → uncertain
        worker = CommentWorker(deps, generator=_gen_ok())
        lease = engine.lease_of(1, now=now + 1)
        lease.driver.results = [(502, None)]
        c = _candidate(store)
        r = worker.run_one(c, 1, now=now + 1)
        assert r.status == "uncertain"
        assert alerts and "comment_uncertain" == alerts[0][0]
        # 立即重试 → 被闸门拦截，不再发出
        r2 = worker.run_one(c, 1, now=now + 2)
        assert r2.status == "skipped_dup"
        # 人工确认「未发出」→ 允许重试
        worker.confirm_uncertain(1, int(c["id"]), sent=False)
        lease.driver.results = [(200, {"ok": True})]
        r3 = worker.run_one(c, 1, now=now + 3)
        assert r3.status == "sent_ok"

    def test_uncertain后人工确认已发_永久占用(self, env):
        store, _, engine, deps, _ = env
        now = 1_000_000
        engine.acquire(1, now=now)
        worker = CommentWorker(deps, generator=_gen_ok())
        lease = engine.lease_of(1, now=now + 1)
        lease.driver.results = [(200, "")]  # 2xx 空响应 → uncertain
        c = _candidate(store)
        assert worker.run_one(c, 1, now=now + 1).status == "uncertain"
        worker.confirm_uncertain(1, int(c["id"]), sent=True)
        lease.driver.results = [(200, {"ok": True})]
        r = worker.run_one(c, 1, now=now + 2)
        assert r.status == "skipped_dup"


# ── D13 AI 评论生成：persona + 广告法自检 ───────────────────
class TestAIComment:
    def test_禁用词拦截(self):
        for bad in ("全网最低价带你飞哦", "加微信 xxx123 领资料", "这是最好的选择", "国家级行业标准"):
            ok, why = AICommentGenerator.check(bad)
            assert not ok and why.startswith("banned")

    def test_形态校验(self):
        assert not AICommentGenerator.check("哈哈")[0]          # 过短
        assert not AICommentGenerator.check("啊" * 50)[0]        # 过长
        assert not AICommentGenerator.check("！！！？？")[0]     # 纯符号
        ok, _ = AICommentGenerator.check("这个运镜细节讲得真细")
        assert ok

    def test_双供应商切换(self):
        p1 = Provider("a", "http://a.test", "m", "K1")
        p2 = Provider("b", "http://b.test", "m", "K2")
        import os
        os.environ["K1"] = "k"
        os.environ["K2"] = "k"
        calls: list[str] = []

        def transport(url, payload, headers, timeout):
            calls.append(url)
            if "a.test" in url:
                raise TimeoutError()
            return '{"choices":[{"message":{"content":"{\\"text\\": \\"拍得挺有生活气息的\\"}"}}]}'

        r = AICommentGenerator(providers=(p1, p2), transport=transport).generate(comment_text="不错")
        assert r is not None and r.provider == "b" and "a:TimeoutError" in r.attempts

    def test_全部供应商失败返回None(self):
        p = Provider("a", "http://a.test", "m", "K3")
        import os
        os.environ["K3"] = "k"
        gen = AICommentGenerator(providers=(p,), transport=lambda *a, **k: (_ for _ in ()).throw(ConnectionError()))
        assert gen.generate(comment_text="想学") is None


# ── D14 集成：候选→筛选→互动全流程（离线） ──────────────────
class TestIntegrationFlow:
    def test_筛选高分候选到点赞评论闭环(self, env):
        store, ledger, engine, deps, _ = env
        now = 1_000_000
        engine.acquire(1, now=now)
        # 模拟筛选产出：高分入池
        hi = _candidate(store, score=90)
        _candidate(store, score=30, text="哈哈哈哈哈哈")
        pool = [r for r in store.list_candidates(min_score=70)]
        assert len(pool) == 1
        # 点赞 → 评论
        like = LikeWorker(deps).run_one(hi, 1, now=now + 1)
        comment = CommentWorker(deps, generator=_gen_ok()).run_one(hi, 1, now=now + 2)
        assert like.status == "sent_ok" and comment.status == "sent_ok"
        assert ledger.used(1, "like", now=now + 3) == 1
        assert ledger.used(1, "comment", now=now + 3) == 1
        with store.connect() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM comment_send_log WHERE status='sent_ok'").fetchone()
        assert n["c"] == 1


# ── REST 接口冒烟 ────────────────────────────────────────────
class TestApi:
    def test_接口冒烟(self, env, tmp_path: Path, monkeypatch):
        from app.engagement import api as eng_api
        db = tmp_path / "api.db"
        store = ExecutionStore(db)
        store.initialize()
        monkeypatch.setenv("EXECUTION_DB_PATH", str(db))
        monkeypatch.setenv("PROFILE_ROOT", str(tmp_path / "profiles"))
        # 用离线驱动替换生产驱动工厂
        monkeypatch.setattr(eng_api, "_ENGINE", EngineManager(tmp_path / "profiles", driver_factory=OfflineDriver))
        client = TestClient(eng_api.standalone_app)

        store.insert_candidate({
            "video_url": "https://www.douyin.com/video/2002",
            "nickname": "乙", "comment_text": "请问可以带带我吗",
            "intent_score": 85, "intent_tags": ["求带"], "source_keyword": "副业",
        })
        cid = store.list_candidates()[0]["id"]

        r = client.post("/api/engagement/lease/acquire", json={"account_id": 9})
        assert r.status_code == 200
        r = client.get("/api/engagement/lease/status")
        assert r.json()["leases"][0]["account_id"] == 9
        r = client.post("/api/engagement/quotas/9/baseline",
                        json={"metric": "comment", "platform_total": 0})
        assert r.status_code == 200
        r = client.post("/api/engagement/comment", json={"account_id": 9, "candidate_ids": [cid]})
        body = r.json()
        assert body["success"] and body["results"][0]["status"] in {"sent_ok", "uncertain", "sent_fail", "skipped_no_ai"}
        r = client.get("/api/engagement/quotas/9")
        assert {q["metric"] for q in r.json()["quotas"]} == {"read", "like", "comment", "dm"}
        r = client.get("/api/engagement/pacing")
        assert r.json()["pacing"]["keystroke_delay_ms"] == [80, 200]

    def test_租约冲突返回409(self, tmp_path: Path):
        from app.engagement import api as eng_api
        eng_api._ENGINE = EngineManager(tmp_path / "p", driver_factory=OfflineDriver)
        client = TestClient(eng_api.standalone_app)
        client.post("/api/engagement/lease/acquire", json={"account_id": 5})
        assert client.post("/api/engagement/lease/acquire", json={"account_id": 5}).status_code == 409
