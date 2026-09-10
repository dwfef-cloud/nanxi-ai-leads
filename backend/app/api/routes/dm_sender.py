"""私信发送执行器路由 · P4-D

端点：
  POST /api/dm/send/{lead_id}        发送一条私信
  POST /api/dm/send-batch            批量发送（后台线程）
  POST /api/dm/send-batch/stop       停止批量发送
  GET  /api/dm/send-batch/status     批量发送进度
  GET  /api/dm/send-results          发送结果列表
  POST /api/dm/sender/test           测试发送器
  GET  /api/dm/sender/config         发送器配置
  PUT  /api/dm/sender/config         更新配置
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_dm_sender_service
from app.schemas.dm_sender import (
    BatchSendRequest,
    BatchSendResponse,
    SenderConfig,
    SenderConfigUpdate,
    SenderTestOut,
    SendRecordOut,
    SendRequest,
    SendResultOut,
)
from app.services.dm_sender_service import DmSenderService

router = APIRouter(prefix="/dm", tags=["dm-sender"])


@router.post("/send/{lead_id}", response_model=SendResultOut)
def send_one(
    lead_id: str,
    payload: SendRequest | None = None,
    service: DmSenderService = Depends(get_dm_sender_service),
) -> SendResultOut:
    content = payload.content if payload else None
    try:
        out = service.send_one(lead_id, content=content)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Lead not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SendResultOut(**out)


@router.post("/send-batch", response_model=BatchSendResponse)
def send_batch(
    payload: BatchSendRequest,
    service: DmSenderService = Depends(get_dm_sender_service),
) -> BatchSendResponse:
    try:
        out = service.send_batch(count=payload.count)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return BatchSendResponse(**out)


@router.post("/send-batch/stop", response_model=BatchSendResponse)
def stop_batch(service: DmSenderService = Depends(get_dm_sender_service)) -> BatchSendResponse:
    return BatchSendResponse(**service.stop_batch())


@router.get("/send-batch/status", response_model=BatchSendResponse)
def batch_status(service: DmSenderService = Depends(get_dm_sender_service)) -> BatchSendResponse:
    return BatchSendResponse(**service.get_batch_status())


@router.get("/send-results", response_model=list[SendRecordOut])
def list_results(
    limit: int = 50,
    service: DmSenderService = Depends(get_dm_sender_service),
) -> list[SendRecordOut]:
    return [SendRecordOut(**r) for r in service.list_results(limit=limit)]


@router.post("/sender/test", response_model=SenderTestOut)
def test_sender(service: DmSenderService = Depends(get_dm_sender_service)) -> SenderTestOut:
    return SenderTestOut(**service.test_send())


@router.get("/sender/config", response_model=SenderConfig)
def get_config(service: DmSenderService = Depends(get_dm_sender_service)) -> SenderConfig:
    return SenderConfig(**service.get_config())


@router.put("/sender/config", response_model=SenderConfig)
def update_config(
    payload: SenderConfigUpdate,
    service: DmSenderService = Depends(get_dm_sender_service),
) -> SenderConfig:
    try:
        cfg = service.update_config(
            mode=payload.mode,
            min_interval=payload.min_interval,
            max_interval=payload.max_interval,
            daily_limit=payload.daily_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SenderConfig(**cfg)
