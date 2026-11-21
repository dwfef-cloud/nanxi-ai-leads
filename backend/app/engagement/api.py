"""互动模块对外 REST 接口（独立 router，不动南溪现有路由）。

    POST /api/engagement/like                 {account_id, candidate_ids}
    POST /api/engagement/comment              {account_id, candidate_ids}
    POST /api/engagement/lease/acquire        {account_id}
    POST /api/engagement/lease/heartbeat      {account_id}
    POST /api/engagement/lease/release        {account_id}
    GET  /api/engagement/lease/status
    GET  /api/engagement/quotas/{account_id}
    POST /api/engagement/quotas/{account_id}/baseline   {metric, platform_total}
    POST /api/engagement/quotas/{account_id}/observation{metric, platform_total}
    POST /api/engagement/confirm              {account_id, candidate_id, sent}
    GET  /api/engagement/pacing               §5.1 参数快照（对账用）

数据库与执行层共用（EXECUTION_DB_PATH 环境变量可覆盖，默认 backend/data/lead_system.db）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.engagement.ai_comment import AICommentGenerator
from app.engagement.engine import EngineManager
from app.engagement.humanize import pacing_profile
from app.engagement.quotas import QuotaLedger
from app.engagement.workers import CommentWorker, LikeWorker, WorkerDeps
from app.execution.store import ExecutionStore
from app.paths import DATA_DIR

engagement_router = APIRouter(prefix="/api/engagement", tags=["engagement"])

_DEFAULT_DB = DATA_DIR / "lead_system.db"
_DEFAULT_PROFILES = DATA_DIR / "profiles"

_alerts: list[dict[str, str]] = []
_ENGINE: EngineManager | None = None
_LEDGER: QuotaLedger | None = None
_COMMENT: CommentWorker | None = None
_LIKE_WORKER: LikeWorker | None = None


def _store() -> ExecutionStore:
    db_path = Path(os.getenv("EXECUTION_DB_PATH", str(_DEFAULT_DB)))
    store = ExecutionStore(db_path)
    store.initialize()
    return store


def _engine() -> EngineManager:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = EngineManager(os.getenv("PROFILE_ROOT", str(_DEFAULT_PROFILES)))
    return _ENGINE


def _ledger() -> QuotaLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = QuotaLedger(_store())
    return _LEDGER


def _comment_worker() -> CommentWorker:
    global _COMMENT
    if _COMMENT is None:
        _COMMENT = CommentWorker(WorkerDeps(
            store=_store(), ledger=_ledger(), engine=_engine(),
            alert=lambda code, msg: _alerts.append({"code": code, "message": msg}),
        ))
    return _COMMENT


def _like_worker() -> LikeWorker:
    global _LIKE_WORKER
    if _LIKE_WORKER is None:
        _LIKE_WORKER = LikeWorker(WorkerDeps(
            store=_store(), ledger=_ledger(), engine=_engine(),
            gate=_comment_worker().deps.gate,
            alert=_comment_worker().deps.alert,
        ))
    return _LIKE_WORKER


class CandidateIdsRequest(BaseModel):
    account_id: int = Field(ge=1)
    candidate_ids: list[int] = Field(min_length=1, max_length=50)


class LeaseRequest(BaseModel):
    account_id: int = Field(ge=1)


class BaselineRequest(BaseModel):
    metric: str = Field(pattern="^(read|like|comment|dm)$")
    platform_total: int = Field(ge=0)


class ConfirmRequest(BaseModel):
    account_id: int = Field(ge=1)
    candidate_id: int = Field(ge=1)
    sent: bool


def _candidates(store: ExecutionStore, ids: list[int]) -> list[dict]:
    rows = {int(r["id"]): r for r in store.list_candidates()}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise HTTPException(404, f"候选不存在: {missing[:5]}")
    return [rows[i] for i in ids]


@engagement_router.post("/like")
def like(req: CandidateIdsRequest) -> dict[str, Any]:
    store = _store()
    worker = _like_worker()
    out = [worker.run_one(c, req.account_id).__dict__ for c in _candidates(store, req.candidate_ids)]
    return {"success": True, "results": out}


@engagement_router.post("/comment")
def comment(req: CandidateIdsRequest) -> dict[str, Any]:
    store = _store()
    worker = _comment_worker()
    out = [worker.run_one(c, req.account_id).__dict__ for c in _candidates(store, req.candidate_ids)]
    return {"success": True, "results": out}


@engagement_router.post("/lease/acquire")
def lease_acquire(req: LeaseRequest) -> dict[str, Any]:
    try:
        lease = _engine().acquire(req.account_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"success": True, "profile_dir": str(lease.profile_dir)}


@engagement_router.post("/lease/heartbeat")
def lease_heartbeat(req: LeaseRequest) -> dict[str, Any]:
    ok = _engine().heartbeat(req.account_id)
    if not ok:
        raise HTTPException(404, "租约不存在或已被回收")
    return {"success": True}


@engagement_router.post("/lease/release")
def lease_release(req: LeaseRequest) -> dict[str, Any]:
    return {"success": _engine().release(req.account_id)}


@engagement_router.get("/lease/status")
def lease_status() -> dict[str, Any]:
    return {"success": True, "leases": _engine().active_leases()}


@engagement_router.get("/quotas/{account_id}")
def quotas(account_id: int) -> dict[str, Any]:
    ledger = QuotaLedger(_store())
    return {"success": True,
            "quotas": [s.__dict__ for s in ledger.snapshot_all(account_id)]}


@engagement_router.post("/quotas/{account_id}/baseline")
def quotas_baseline(account_id: int, req: BaselineRequest) -> dict[str, Any]:
    QuotaLedger(_store()).set_platform_baseline(account_id, req.metric, req.platform_total)
    return {"success": True}


@engagement_router.post("/quotas/{account_id}/observation")
def quotas_observation(account_id: int, req: BaselineRequest) -> dict[str, Any]:
    delta = QuotaLedger(_store()).record_platform_observation(
        account_id, req.metric, req.platform_total)
    return {"success": True, "used": delta}


@engagement_router.post("/confirm")
def confirm(req: ConfirmRequest) -> dict[str, Any]:
    _comment_worker().confirm_uncertain(req.account_id, req.candidate_id, sent=req.sent)
    return {"success": True}


@engagement_router.get("/pacing")
def pacing() -> dict[str, Any]:
    return {"success": True, "pacing": pacing_profile()}


@engagement_router.get("/alerts")
def alerts() -> dict[str, Any]:
    return {"success": True, "alerts": _alerts}


def create_standalone_app() -> FastAPI:
    app = FastAPI(title="Nanxi Engagement Layer", version="0.1.0-w2")
    app.include_router(engagement_router)
    return app


standalone_app = create_standalone_app()
