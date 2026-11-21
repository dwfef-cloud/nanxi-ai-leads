"""M1 编排器 REST 接口（D22，DESIGN-DOC §M1 规格）：

    POST /api/pipeline/start           启动（或断点续跑）一条全流水线
    GET  /api/pipeline/{run_id}/status 查询阶段/统计
    POST /api/pipeline/{run_id}/stop   请求停止（协作式，阶段边界/条目间生效）

独立 router，不改南溪现有 api/router.py。挂载：bridge_server.py。
合规：engage/dm 执行器默认不注入 → 阶段 blocked，不触发真实平台写动作；
真实链路须先过 TEST-CHECKLIST B 组验证后再接线。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.execution.api import _OfflineReader, _store
from app.execution.orchestrator import Orchestrator
from app.execution.pipeline import Pipeline
from app.execution.scorer import IntentScorer
from app.execution.store import ExecutionStore

pipeline_router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class PipelineStartRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    video_urls: list[str] = Field(default_factory=list)
    source_keyword: str = ""
    max_comments_per_video: int = 50
    min_score: int | None = None
    resume: bool = True


class _RunRegistry:
    """进程内运行注册表：同一 run_id 不允许并发；stop 用协作式标志。

    注意：注册表是进程内的，断点续跑的正确性不依赖它 —— 崩溃重启后
    progress 数据在 SQLite（pipeline_progress / pipeline_runs），可安全续跑。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_flags: dict[str, list] = {}
        self._alive: dict[str, bool] = {}

    def register_stop(self, run_id: str) -> list:
        with self._lock:
            flags = self._stop_flags.setdefault(run_id, [])
            self._alive[run_id] = True
            return flags

    def bind(self, run_id: str, flag: list) -> None:
        with self._lock:
            self._stop_flags[run_id] = flag

    def finish(self, run_id: str) -> None:
        with self._lock:
            self._alive[run_id] = False

    def stop(self, run_id: str) -> None:
        with self._lock:
            for f in self._stop_flags.get(run_id, []):
                f.append(True)

    def is_alive(self, run_id: str) -> bool:
        with self._lock:
            return self._alive.get(run_id, False)


_REGISTRY = _RunRegistry()


def build_orchestrator(store: ExecutionStore | None = None) -> Orchestrator:
    store = store or _store()
    pipeline = Pipeline(store=store, reader=_OfflineReader(), scorer=IntentScorer())
    return Orchestrator(store=store, pipeline=pipeline)


@pipeline_router.post("/start")
def start_pipeline(req: PipelineStartRequest) -> dict[str, Any]:
    orch = build_orchestrator()
    if orch.store.get_run(req.run_id) is None:
        orch.store.create_run(req.run_id)
    if _REGISTRY.is_alive(req.run_id):
        raise HTTPException(status_code=409, detail=f"run_id 已在运行: {req.run_id}")

    flag: list = []
    _REGISTRY.bind(req.run_id, flag)

    def task() -> None:
        try:
            orch.run(
                req.run_id,
                req.video_urls,
                min_score=req.min_score,
                source_keyword=req.source_keyword,
                max_comments_per_video=req.max_comments_per_video,
                resume=req.resume,
                stop_flag=lambda: bool(flag),
            )
        finally:
            _REGISTRY.finish(req.run_id)

    t = threading.Thread(target=task, name=f"pipeline:{req.run_id}", daemon=True)
    t.start()
    return {"success": True, "run_id": req.run_id, "status": "running"}


@pipeline_router.get("/{run_id}/status")
def pipeline_status(run_id: str) -> dict[str, Any]:
    orch = build_orchestrator()
    run = orch.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"未知 run_id: {run_id}")
    import json
    try:
        stats = json.loads(run["stats"] or "{}")
    except ValueError:
        stats = {}
    return {
        "success": True,
        "run_id": run_id,
        "stage": run["stage"],
        "thread_alive": _REGISTRY.is_alive(run_id),
        "stats": stats,
    }


@pipeline_router.post("/{run_id}/stop")
def stop_pipeline(run_id: str) -> dict[str, Any]:
    _REGISTRY.stop(run_id)
    return {"success": True, "run_id": run_id, "status": "stopping"}


def mount(app: Any) -> None:
    app.include_router(pipeline_router)
