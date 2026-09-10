from fastapi import APIRouter, Depends

from app.core.dependencies import get_task_service
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from app.services.task_service import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskRead])
def list_tasks(service: TaskService = Depends(get_task_service)) -> list[TaskRead]:
    tasks = service.list_tasks()
    return [service.sync_live_task(task.id) if task.task_type == "media_crawler_live" else task for task in tasks]


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: str, service: TaskService = Depends(get_task_service)) -> TaskRead:
    return service.get_task(task_id)


@router.post("", response_model=TaskRead)
def create_task(payload: TaskCreate, service: TaskService = Depends(get_task_service)) -> TaskRead:
    return service.create_task(payload)


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(task_id: str, payload: TaskUpdate, service: TaskService = Depends(get_task_service)) -> TaskRead:
    return service.update_task(task_id, payload)


@router.post("/{task_id}/run", response_model=TaskRead)
def run_task(task_id: str, service: TaskService = Depends(get_task_service)) -> TaskRead:
    return service.run_task(task_id)


@router.post("/{task_id}/retry", response_model=TaskRead)
def retry_task(task_id: str, service: TaskService = Depends(get_task_service)) -> TaskRead:
    return service.retry_task(task_id)


@router.post("/{task_id}/cancel", response_model=TaskRead)
def cancel_task(task_id: str, service: TaskService = Depends(get_task_service)) -> TaskRead:
    task = service.get_task(task_id)
    if task.task_type == "media_crawler_live":
        return service.stop_live_task(task_id)
    return service.cancel_task(task_id)
