"""D23 账号池 v2 REST 接口：

    GET  /api/engagement/pool                 全账号状态视图
    GET  /api/engagement/pool/{account_id}   单账号状态 + 事件
    POST /api/engagement/pool/{account_id}/events  上报事件（状态机驱动）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.engagement.account_pool import AccountPool
from app.paths import DATA_DIR as _DATA_DIR

pool_router = APIRouter(prefix="/api/engagement/pool", tags=["account-pool"])

_EVENT_KINDS = {"captcha", "send_uncertain", "send_fail_403",
                "manual_suspend", "manual_recover"}


def _pool() -> AccountPool:
    db = os.getenv("ACCOUNT_POOL_DB")
    path = Path(db) if db else _DATA_DIR / "account_pool.db"
    return AccountPool(path)


class EventRequest(BaseModel):
    kind: str = Field(min_length=1)
    detail: str = ""


@pool_router.get("")
def all_states() -> dict[str, Any]:
    return {"success": True, "accounts": _pool().states()}


@pool_router.get("/{account_id}")
def one_state(account_id: str) -> dict[str, Any]:
    p = _pool()
    state = p.state_of(account_id)
    if state == "healthy" and not p.events_of(account_id, limit=1):
        raise HTTPException(status_code=404, detail=f"未知账号: {account_id}")
    return {"success": True, "account_id": account_id, "state": state,
            "events": p.events_of(account_id)}


@pool_router.post("/{account_id}/events")
def report_event(account_id: str, req: EventRequest) -> dict[str, Any]:
    if req.kind not in _EVENT_KINDS:
        raise HTTPException(status_code=422, detail=f"kind 须为 {_EVENT_KINDS} 之一")
    return {"success": True, **_pool().report_event(account_id, req.kind, detail=req.detail)}
