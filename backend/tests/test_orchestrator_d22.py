"""D22 M1 编排器测试：全流水线 + 断点续跑 + 合规闸门 + REST 接口。

验收标准（DESIGN-DOC D22）：杀进程重启后从断点继续。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.execution.orchestrator import Orchestrator
from app.execution.pipeline import Pipeline
from app.execution.scorer import IntentScorer
from app.execution.store import ExecutionStore


@dataclass
class FakeComment:
    nickname: str
    text: str


class FakeReader:
    """可统计调用次数的离线读屏 fake。"""

    def __init__(self, videos: dict[str, list[FakeComment]]) -> None:
        self.videos = videos
        self.calls: list[str] = []

    def read_video_comments(self, video_url: str, max_n: int = 50) -> list:
        self.calls.append(video_url)
        return self.videos.get(video_url, [])


@pytest.fixture()
def env(tmp_path):
    store = ExecutionStore(tmp_path / "exec.db")
    store.initialize()

    def make(reader: FakeReader | None = None) -> tuple[Orchestrator, ExecutionStore]:
        r = reader or FakeReader({})
        p = Pipeline(store=store, reader=r, scorer=IntentScorer())
        return Orchestrator(store=store, pipeline=p), store

    return make, store


class TestFullPipeline:
    def test_五阶段全流程_注入执行器(self, env):
        make, store = env
        reader = FakeReader({
            "https://x/v1": [FakeComment("小A", "这个怎么合作"), FakeComment("小B", "111")],
            "https://x/v2": [FakeComment("小C", "多少钱怎么买")],
        })
        orch, store = make(reader)
        engaged: list[int] = []
        dmed: list[int] = []
        result = orch.run(
            "run-full", ["https://x/v1", "https://x/v2"],
            engage_fn=lambda c: (engaged.append(c["id"]) or {"status": "sent_ok"}),
            dm_fn=lambda c: (dmed.append(c["id"]) or {"status": "sent_ok"}),
        )
        assert result["stage"] == "done"
        assert store.get_run("run-full")["stage"] == "done"
        assert len(engaged) > 0 and len(dmed) == len(engaged)  # DM 目标与互动一致
        assert result["stats"]["engage_ok"] == len(engaged)
        assert result["stats"]["dm_ok"] == len(dmed)

    def test_未注入执行器_合规闸门跳过真实动作(self, env):
        make, store = env
        reader = FakeReader({"https://x/v1": [FakeComment("小A", "怎么合作")]})
        orch, _ = make(reader)
        result = orch.run("run-gate", ["https://x/v1"])
        # 合规：无执行器 → blocked 记录在案，但流水线照常走到 done
        assert result["stage"] == "done"
        assert "no_handler" in str(result["stats"].get("engage_blocked", ""))
        assert "no_handler" in str(result["stats"].get("dm_blocked", ""))


class TestCrashResume:
    def test_collect阶段杀进程重启_从断点继续(self, env):
        make, store = env
        urls = [f"https://x/v{i}" for i in range(1, 4)]
        reader = FakeReader({
            urls[0]: [FakeComment("小A", "怎么合作")],
            urls[1]: [FakeComment("小B", "多少钱")],
            urls[2]: [FakeComment("小C", "合作意向")],
        })
        orch, store = make(reader)
        # 第一轮：处理完 v1 后"崩溃"（stop_flag 生效）
        state = {"n": 0}

        def stop_after_first() -> bool:
            state["n"] += 1
            return state["n"] > 1  # 第二个视频迭代时停

        r1 = orch.run("run-crash", urls, stop_flag=stop_after_first)
        assert r1["stage"] == "stopped"
        assert len(reader.calls) == 1  # 只读了 v1

        # "重启"：全新 Orchestrator 实例（同 DB），模拟杀进程后拉起
        orch2, _ = make(reader)
        r2 = orch2.run("run-crash", urls,
                       engage_fn=lambda c: {"status": "sent_ok"},
                       dm_fn=lambda c: {"status": "sent_ok"})
        assert r2["stage"] == "done"
        assert len(reader.calls) == 3  # v1 断点跳过，只补 v2/v3
        # v1 的候选不重复入库（唯一约束 + 断点跳过双保险）
        r3 = orch2.run("run-crash", urls)
        assert r3["stage"] == "done"  # 全部条目 done，直接幂等收尾

    def test_engage阶段逐候选断点_失败不重试不阻塞(self, env):
        make, store = env
        reader = FakeReader({"https://x/v1": [
            FakeComment("小A", "怎么合作"), FakeComment("小B", "多少钱"), FakeComment("小C", "合作")]})
        orch, store = make(reader)

        calls: list[int] = []

        def engage(cand):
            calls.append(cand["id"])
            if cand["nickname"] == "小B":
                raise RuntimeError("模拟崩溃点")
            return {"status": "sent_ok"}

        r1 = orch.run("run-engage", ["https://x/v1"], engage_fn=engage,
                      dm_fn=lambda c: {"status": "sent_ok"})
        assert r1["stage"] == "done"
        assert r1["stats"]["engage_fail"] == 1  # 小B 失败
        assert r1["stats"]["engage_ok"] == len(calls) - 1

        # 重启续跑：已 done 的条目不再调用（fail 也已落 checkpoint，不重试）
        n_before = len(calls)
        r2 = orch.run("run-engage", ["https://x/v1"], engage_fn=engage,
                      dm_fn=lambda c: {"status": "sent_ok"})
        assert len(calls) == n_before  # 零重复调用
        assert r2["stats"]["engage_fail"] == 1

    def test_uncertain不重试_重启后跳过(self, env):
        make, store = env
        reader = FakeReader({"https://x/v1": [FakeComment("小A", "怎么合作")]})
        orch, store = make(reader)
        n = {"c": 0}

        def engage(cand):
            n["c"] += 1
            return {"status": "uncertain"}

        r1 = orch.run("run-unc", ["https://x/v1"], engage_fn=engage,
                      dm_fn=lambda c: {"status": "sent_ok"})
        assert r1["stats"]["engage_uncertain"] == 1
        orch.run("run-unc", ["https://x/v1"], engage_fn=engage,
                 dm_fn=lambda c: {"status": "sent_ok"})
        assert n["c"] == 1  # uncertain 语义：不重试


class TestRestApi:
    def test_start_status_stop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EXECUTION_DB_PATH", str(tmp_path / "api.db"))
        from app.api import pipeline_api
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(pipeline_api.pipeline_router)
        client = TestClient(app)
        P = "/api/pipeline"
        # 离线 reader：start 后 collect 空跑 → blocked → done
        resp = client.post(f"{P}/start", json={
            "run_id": "api-run", "video_urls": [], "source_keyword": "test"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        # 等线程结束
        for _ in range(50):
            st = client.get(f"{P}/api-run/status").json()
            if st["stage"] in {"done", "stopped"} and not st["thread_alive"]:
                break
            time.sleep(0.05)
        st = client.get(f"{P}/api-run/status").json()
        assert st["success"] is True
        assert st["stage"] == "done"
        assert "no_handler" in str(st["stats"].get("engage_blocked", ""))
        # stop 幂等
        assert client.post(f"{P}/api-run/stop").json()["success"] is True
        assert client.get(f"{P}/unknown/status").status_code == 404
