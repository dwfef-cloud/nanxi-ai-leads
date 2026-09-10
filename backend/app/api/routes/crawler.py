from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_task_service
from app.integrations.mediacrawler_client import MediaCrawlerUnavailable
from datetime import datetime, timezone

from app.schemas.crawler import AcquisitionStartRequest, LiveAcquisitionRequest
from app.schemas.task import TaskCreate, TaskRead
from app.services.task_service import TaskService

router = APIRouter(prefix="/crawler", tags=["crawler"])


@router.post("/import-demo", response_model=TaskRead)
def create_import_demo_task(service: TaskService = Depends(get_task_service)) -> TaskRead:
    return service.create_task_from_data(
        "media_crawler_import",
        {
            "source": [
                {
                    "nickname": "demo-user-1",
                    "source_url": "https://www.douyin.com/video/111",
                    "source_keyword": "装修",
                    "platform": "douyin",
                    "external_id": "demo-111",
                    "tags": ["comment"],
                    "note": "多少钱",
                },
                {
                    "nickname": "demo-user-2",
                    "source_url": "https://www.douyin.com/video/222",
                    "source_keyword": "获客",
                    "platform": "douyin",
                    "external_id": "demo-222",
                    "tags": ["search"],
                    "note": "怎么做",
                },
            ]
        },
    )


@router.post("/import-file", response_model=TaskRead)
def create_import_file_task(payload: TaskCreate, service: TaskService = Depends(get_task_service)) -> TaskRead:
    return service.create_task(payload)


@router.post("/start", response_model=TaskRead)
def start_acquisition(payload: AcquisitionStartRequest, service: TaskService = Depends(get_task_service)) -> TaskRead:
    task = service.create_task_from_data(
        "media_crawler_import",
        {
            "keyword": payload.keyword,
            "count": payload.count,
            "source_mode": payload.source_mode,
            "note": payload.note,
            "tag": payload.tag,
            "raw_text": payload.raw_text,
            "file_path": payload.file_path,
        },
    )
    if payload.auto_run:
        return service.run_task(task.id)
    return task


@router.post("/live/start", response_model=TaskRead)
def start_live_acquisition(payload: LiveAcquisitionRequest, service: TaskService = Depends(get_task_service)) -> TaskRead:
    task = service.create_task_from_data(
        "media_crawler_live",
        {
            "keyword": payload.keyword,
            "intent_keywords": payload.intent_keywords,
            "excluded_keywords": payload.excluded_keywords,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "crawler_config": {
                "platform": "dy",
                "login_type": payload.login_type,
                "crawler_type": "search",
                "keywords": payload.keyword,
                "start_page": 1,
                "enable_comments": True,
                "enable_sub_comments": False,
                "save_option": "json",
                "headless": payload.headless,
                "max_notes_count": payload.count,
                "max_comments_count": payload.max_comments_count,
            },
        },
    )
    return service.run_task(task.id)


@router.get("/live/status")
def live_status(service: TaskService = Depends(get_task_service)) -> dict:
    try:
        return service.media_crawler_status()
    except MediaCrawlerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/live/logs")
def live_logs(service: TaskService = Depends(get_task_service)) -> dict:
    try:
        return {"logs": service.media_crawler_logs()}
    except MediaCrawlerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
