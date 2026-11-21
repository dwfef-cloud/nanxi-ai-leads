"""Fixed-sample offline API. No account IDs, URLs, scripts or live-mode flags accepted."""
from pathlib import Path
from typing import Literal
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.execution.offline_service import OfflineRunService
from app.paths import DATA_DIR

router = APIRouter(prefix="/api/offline", tags=["offline"])
_service = None
_service_lock = threading.Lock()


class RunOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: Literal["all", "leadgen", "nurture"] = "all"
    scenario: Literal["success", "uncertain", "declined"] = "success"
    sample: Literal["A", "B"] = "A"


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = OfflineRunService(Path(DATA_DIR) / "offline" / "runs.db")
        return _service


@router.on_event("shutdown")
def stop_workers():
    if _service is not None:
        _service.shutdown()


def same_origin(request: Request):
    # Prevent a third-party web page from starting/stopping local runs through a
    # simple cross-origin POST. The desktop page uses this API on the same origin.
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "请从软件内打开离线测试")


@router.get("/runs")
def list_runs():
    return {"mode": "offline", "runs": get_service().list()}


@router.post("/runs", status_code=201)
def start_run(options: RunOptions, request: Request):
    same_origin(request)
    try:
        return get_service().start(**options.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    item = get_service().get(run_id)
    if item is None:
        raise HTTPException(404, "离线测试记录不存在")
    return item


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: str, request: Request):
    same_origin(request)
    try:
        return get_service().stop(run_id)
    except KeyError as exc:
        raise HTTPException(404, "离线测试记录不存在") from exc
