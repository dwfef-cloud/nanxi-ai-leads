"""M1 对外接口 —— POST /api/pipeline/start · GET /api/pipeline/{run_id}/status · POST /stop

本模块是独立 router，不改动南溪现有 `app/api/router.py`。
接入方式（挂载一行，见交付说明）：
    from app.execution.api import execution_router
    api_router.include_router(execution_router)

也可单独起一个 app 做离线验证：
    uvicorn app.execution.api:standalone_app --port 8898
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.execution.params import FILTER
from app.execution.pipeline import Pipeline
from app.execution.scorer import IntentScorer
from app.execution.screen_reader import ScreenReader
from app.paths import DATA_DIR as _DATA_DIR
from app.execution.store import ExecutionStore

execution_router = APIRouter(prefix="/api/execution", tags=["execution"])


class PipelineStartRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=120)
    video_urls: list[str] = Field(default_factory=list, max_length=100)
    source_keyword: str = ""
    max_comments_per_video: int = Field(default=50, ge=1, le=200)
    resume: bool = True


class RunState:
    """内存态任务表。生产应换 redis / DB，此处保持零依赖。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop: set[str] = set()
        self._last_error: dict[str, str] = {}

    def start(self, run_id: str, target, args: tuple) -> bool:
        with self._lock:
            if run_id in self._threads and self._threads[run_id].is_alive():
                return False
            self._stop.discard(run_id)
            self._last_error.pop(run_id, None)

            def runner() -> None:
                try:
                    target(*args)
                except Exception as exc:  # 线程内异常落表，不静默吞掉
                    with self._lock:
                        self._last_error[run_id] = f"{type(exc).__name__}: {exc}"

            thread = threading.Thread(target=runner, name=f"pipeline-{run_id}", daemon=True)
            self._threads[run_id] = thread
            thread.start()
            return True

    def stop(self, run_id: str) -> None:
        with self._lock:
            self._stop.add(run_id)

    def is_stopped(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._stop

    def status(self, run_id: str) -> str:
        with self._lock:
            if run_id in self._last_error:
                return "error"
            thread = self._threads.get(run_id)
            if thread is None:
                return "idle"
            return "running" if thread.is_alive() else "finished"

    def error(self, run_id: str) -> str | None:
        with self._lock:
            return self._last_error.get(run_id)


_STATE = RunState()


def _store() -> ExecutionStore:
    db = os.getenv("EXECUTION_DB_PATH")
    path = Path(db) if db else _DATA_DIR / "execution.db"
    store = ExecutionStore(path)
    store.initialize()
    return store


def build_pipeline(reader: ScreenReader | None = None) -> Pipeline:
    """离线模式下 reader 为 None → 只读空数据，不触发任何平台请求。"""
    store = _store()
    if reader is None:
        reader = _OfflineReader()
    return Pipeline(
        store=store,
        reader=reader,
        scorer=IntentScorer(),
    )


class _OfflineReader:
    """无浏览器时的空实现：保证接口可用但不越合规边界。"""

    def read_video_comments(self, video_url: str, max_n: int = 50) -> list:
        return []


@execution_router.get("/health")
def health() -> dict[str, Any]:
    store = _store()
    return {
        "ok": True,
        "db": str(store.db_path),
        "intent_threshold": FILTER.intent_threshold,
        "browser_attached": False,
        "note": "未挂载浏览器读屏引擎，collect 阶段为空跑；不产生任何平台请求",
    }


@execution_router.post("/pipeline/start")
def start_pipeline(req: PipelineStartRequest) -> dict[str, Any]:
    pipeline = build_pipeline()
    if pipeline.store.get_run(req.run_id) is None:
        pipeline.store.create_run(req.run_id)

    def task() -> None:
        pipeline.run(
            req.run_id,
            req.video_urls,
            source_keyword=req.source_keyword,
            max_comments_per_video=req.max_comments_per_video,
            resume=req.resume,
            stop_flag=lambda: _STATE.is_stopped(req.run_id),
        )

    started = _STATE.start(req.run_id, task, ())
    if not started:
        raise HTTPException(status_code=409, detail=f"run_id 已在运行: {req.run_id}")
    return {"success": True, "run_id": req.run_id, "status": "running"}


@execution_router.get("/pipeline/{run_id}/status")
def pipeline_status(run_id: str) -> dict[str, Any]:
    store = _store()
    run = store.get_run(run_id)
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
        "thread": _STATE.status(run_id),
        "error": _STATE.error(run_id),
        "stats": stats,
    }


@execution_router.post("/pipeline/{run_id}/stop")
def stop_pipeline(run_id: str) -> dict[str, Any]:
    _STATE.stop(run_id)
    return {"success": True, "run_id": run_id, "status": "stopping"}


@execution_router.post("/purge")
def purge() -> dict[str, Any]:
    store = _store()
    deleted = store.purge_expired()
    return {"success": True, "deleted": deleted}


def create_standalone_app() -> FastAPI:
    app = FastAPI(title="Nanxi Execution Layer", version="0.1.0-w1")
    app.include_router(execution_router)
    return app


standalone_app = create_standalone_app()
