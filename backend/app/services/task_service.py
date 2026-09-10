from app.integrations.crawler_job import MediaCrawlerJob
from app.integrations.mediacrawler_client import MediaCrawlerClient
from app.integrations.mediacrawler_importer import adapt_douyin_comments
from app.models.domain import Task, now_utc
from app.repositories.memory import MemoryRepository
from app.schemas.task import TaskCreate, TaskUpdate
from app.services.lead_service import LeadService


class TaskService:
    def __init__(self, repo: MemoryRepository) -> None:
        self._repo = repo
        self._lead_service = LeadService(repo)
        self._crawler_job = MediaCrawlerJob()
        self._media_crawler = MediaCrawlerClient()

    def list_tasks(self) -> list[Task]:
        return self._repo.list_tasks()

    def get_task(self, task_id: str) -> Task:
        return self._repo.get_task(task_id)

    def create_task(self, payload: TaskCreate) -> Task:
        task = Task(task_type=payload.task_type, payload=payload.payload)
        return self._repo.save_task(task)

    def create_task_from_data(self, task_type: str, payload: dict) -> Task:
        return self._repo.save_task(Task(task_type=task_type, payload=payload))

    def update_task(self, task_id: str, payload: TaskUpdate) -> Task:
        task = next(item for item in self._repo.list_tasks() if item.id == task_id)
        if payload.status is not None:
            task.status = payload.status  # type: ignore[assignment]
        if payload.error_message is not None:
            task.error_message = payload.error_message
        task.updated_at = now_utc()
        return self._repo.save_task(task)

    def run_task(self, task_id: str) -> Task:
        task = next(item for item in self._repo.list_tasks() if item.id == task_id)
        task.status = "running"
        task.error_message = ""
        task.updated_at = now_utc()
        self._repo.save_task(task)

        try:
            if task.task_type == "media_crawler_live":
                self._media_crawler.start(task.payload["crawler_config"])
                return self._repo.save_task(task)
            if task.task_type == "media_crawler_import":
                result = self._crawler_job.run(task.payload)
                self._lead_service.import_external_leads(result.leads)
                task.status = "done"
            else:
                task.status = "failed"
                task.error_message = f"unsupported task type: {task.task_type}"
        except Exception as exc:
            task.status = "failed"
            task.error_message = str(exc)
        task.updated_at = now_utc()

        return self._repo.save_task(task)

    def sync_live_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task.task_type != "media_crawler_live" or task.status != "running":
            return task
        try:
            status = self._media_crawler.status()
            task.payload["crawler_status"] = status
            if status.get("status") == "running":
                return self._repo.save_task(task)
            records = self._media_crawler.comments_since(task.payload["started_at"], task.payload["keyword"])
            leads = adapt_douyin_comments(
                records, task.payload["keyword"], task.id,
                task.payload.get("intent_keywords", ""), task.payload.get("excluded_keywords", ""),
            )
            self._lead_service.import_external_leads(leads)
            task.status = "done"
            task.payload["imported_count"] = len(leads)
        except Exception as exc:
            task.status = "failed"
            task.error_message = str(exc)
        task.updated_at = now_utc()
        return self._repo.save_task(task)

    def media_crawler_status(self) -> dict:
        return self._media_crawler.status()

    def media_crawler_logs(self) -> list[dict]:
        return self._media_crawler.logs()

    def stop_live_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task.task_type == "media_crawler_live" and task.status == "running":
            self._media_crawler.stop()
            task.status = "paused"
            task.error_message = "已停止 MediaCrawler 采集"
            task.updated_at = now_utc()
        return self._repo.save_task(task)

    def retry_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        task.status = "pending"
        task.error_message = ""
        task.updated_at = now_utc()
        self._repo.save_task(task)
        return self.run_task(task_id)

    def cancel_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task.status in {"done", "failed"}:
            return task
        task.status = "paused"
        task.error_message = "已由用户取消"
        task.updated_at = now_utc()
        return self._repo.save_task(task)
