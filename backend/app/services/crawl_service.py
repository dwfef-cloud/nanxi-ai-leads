"""
采集服务 · CrawlService
========================
负责采集任务的 CRUD、启动/停止、以及采集结果入库为线索。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

from app.models.domain import CrawlTask, Lead, now_utc
from app.repositories.base import Repository
from app.schemas.crawl import CrawlImportItem, CrawlImportRequest, CrawlTaskCreate
from app.schemas.lead import LeadCreate


HIGH_INTENT_KEYWORDS = [
    "多少钱", "价格", "报价", "怎么收费", "费用",
    "怎么做", "联系", "微信", "vx", "加微",
    "推荐", "求", "私", "咨询", "合作",
    "想买", "需要", "感兴趣", "了解一下", "详细",
]

BUSINESS_KEYWORDS = [
    "装修", "获客", "投放", "咨询", "合作",
    "翻新", "改造", "定制", "设计", "施工",
    "引流", "推广", "营销", "运营", "代运营",
]


class CrawlService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._processes: dict[str, subprocess.Popen] = {}

    def list_tasks(self, status: str | None = None) -> list[CrawlTask]:
        return self._repo.list_crawl_tasks(status=status)

    def get_task(self, task_id: str) -> CrawlTask:
        return self._repo.get_crawl_task(task_id)

    def create_task(self, payload: CrawlTaskCreate) -> CrawlTask:
        task = CrawlTask(
            name=payload.name or self._generate_task_name(payload),
            crawl_type=payload.crawl_type,
            keyword=payload.keyword,
            competitor_account=payload.competitor_account,
            video_url=payload.video_url,
            source=payload.source,
            intent_keywords=payload.intent_keywords,
            excluded_keywords=payload.excluded_keywords,
            max_comments=payload.max_comments,
        )
        return self._repo.save_crawl_task(task)

    def delete_task(self, task_id: str) -> None:
        self._stop_process(task_id)
        self._repo.delete_crawl_task(task_id)

    def start_task(self, task_id: str) -> CrawlTask:
        task = self._repo.get_crawl_task(task_id)
        if task.status == "running":
            return task
        task.status = "running"
        task.error_message = ""
        task.started_at = now_utc()
        task.finished_at = None
        task.updated_at = now_utc()
        self._repo.save_crawl_task(task)
        self._launch_mediacrawler(task)
        return task

    def stop_task(self, task_id: str) -> CrawlTask:
        task = self._repo.get_crawl_task(task_id)
        self._stop_process(task_id)
        if task.status == "running":
            task.status = "completed"
            task.finished_at = now_utc()
            task.error_message = "已手动停止"
            task.updated_at = now_utc()
            self._repo.save_crawl_task(task)
        return task

    def get_status(self, task_id: str) -> CrawlTask:
        task = self._repo.get_crawl_task(task_id)
        proc = self._processes.get(task_id)
        if proc is not None and proc.poll() is not None:
            returncode = proc.returncode
            if task.status == "running":
                if returncode == 0:
                    task.status = "completed"
                else:
                    task.status = "failed"
                    task.error_message = f"MediaCrawler 进程退出码: {returncode}"
                task.finished_at = now_utc()
                task.updated_at = now_utc()
                self._repo.save_crawl_task(task)
            del self._processes[task_id]
        return task

    def import_comments(self, payload: CrawlImportRequest) -> dict:
        task_id = payload.task_id
        imported = 0
        skipped = 0
        lead_ids: list[str] = []

        task: CrawlTask | None = None
        if task_id:
            try:
                task = self._repo.get_crawl_task(task_id)
            except KeyError:
                task = None

        intent_keywords = self._parse_keywords(
            task.intent_keywords if task else payload.keyword
        )
        excluded_keywords = self._parse_keywords(
            task.excluded_keywords if task else ""
        )
        seen_comment_ids: set[str] = set()

        for item in payload.items:
            content = (item.content or "").strip()
            nickname = (item.nickname or "抖音用户").strip()
            comment_id = (item.comment_id or "").strip()

            if not content:
                skipped += 1
                continue

            content_lower = content.lower()
            if any(kw in content_lower for kw in excluded_keywords):
                skipped += 1
                continue

            if comment_id and comment_id in seen_comment_ids:
                skipped += 1
                continue
            if comment_id:
                seen_comment_ids.add(comment_id)

            external_id = f"douyin-comment-{comment_id}" if comment_id else ""
            source_url = item.source_url
            if not source_url and item.aweme_id:
                source_url = f"https://www.douyin.com/video/{item.aweme_id}"

            score = self._score_comment(content, nickname, intent_keywords)
            intent_level = "A" if score >= 70 else "B" if score >= 40 else "C"
            intent_tag = "high" if score >= 60 else "mid" if score >= 30 else "low"

            tags = ["mediacrawler", "douyin-comment"]
            if task_id:
                tags.append(f"crawl-{task_id}")
            if score >= 60:
                tags.append("high_intent")

            lead_create = LeadCreate(
                nickname=nickname,
                source=payload.source,
                comment=content,
                video=item.video_title,
                source_url=source_url,
                source_keyword=payload.keyword or (task.keyword if task else ""),
                platform="douyin",
                external_id=external_id,
                tags=tags,
                note=content,
                account=None,
            )

            lead = self._create_or_get_lead(lead_create, score, intent_level, intent_tag)
            if lead is not None:
                lead_ids.append(lead.id)
                imported += 1
            else:
                skipped += 1

        if task_id and task is not None:
            task.collected_count = len(payload.items)
            task.imported_count = task.imported_count + imported
            task.updated_at = now_utc()
            self._repo.save_crawl_task(task)

        return {
            "imported": imported,
            "skipped": skipped,
            "task_id": task_id,
            "leads": lead_ids,
        }

    def _create_or_get_lead(
        self, payload: LeadCreate, score: int, intent_level: str, intent_tag: str
    ) -> Lead | None:
        if payload.external_id:
            existing = self._repo.find_lead_by_external(payload.platform, payload.external_id)
            if existing is not None:
                return existing

        lead = Lead(
            nickname=payload.nickname,
            source=payload.source,
            comment=payload.comment,
            video=payload.video,
            source_url=payload.source_url,
            source_keyword=payload.source_keyword,
            platform=payload.platform,
            external_id=payload.external_id,
            tags=payload.tags,
            note=payload.note,
            score=score,
            intent_level=intent_level,
            intent=intent_tag,
            hue=hash(payload.nickname) % 360,
        )
        return self._repo.save_lead(lead)

    def _score_comment(self, content: str, nickname: str, intent_keywords: list[str]) -> int:
        score = 0
        text = f"{content} {nickname}".lower()
        high_hits = sum(1 for kw in HIGH_INTENT_KEYWORDS if kw in text)
        score += min(high_hits * 15, 45)
        biz_hits = sum(1 for kw in BUSINESS_KEYWORDS if kw in text)
        score += min(biz_hits * 10, 30)
        custom_hits = sum(1 for kw in intent_keywords if kw and kw in text)
        score += min(custom_hits * 12, 25)
        if len(content) > 20:
            score += 5
        if len(content) > 50:
            score += 5
        return min(score, 100)

    def _parse_keywords(self, raw: str) -> list[str]:
        if not raw:
            return []
        return [kw.strip().lower() for kw in raw.replace("，", ",").split(",") if kw.strip()]

    def _generate_task_name(self, payload: CrawlTaskCreate) -> str:
        parts = []
        if payload.keyword:
            parts.append(f"关键词:{payload.keyword}")
        if payload.competitor_account:
            parts.append(f"对标:{payload.competitor_account}")
        if payload.video_url:
            parts.append("指定视频")
        if not parts:
            parts.append("评论采集")
        return " · ".join(parts)

    def _launch_mediacrawler(self, task: CrawlTask) -> None:
        try:
            mediacrawler_root = os.getenv(
                "MEDIA_CRAWLER_ROOT",
                r"D:\24\MediaCrawler-main (1)\MediaCrawler-main",
            )
            venv_python = os.getenv(
                "MEDIA_CRAWLER_VENV",
                r"D:\.venv\mediacrawler\Scripts\python.exe",
            )

            if not os.path.isdir(mediacrawler_root):
                task.error_message = f"MediaCrawler 目录不存在: {mediacrawler_root}"
                task.status = "failed"
                task.updated_at = now_utc()
                self._repo.save_crawl_task(task)
                return

            if not os.path.isfile(venv_python):
                task.error_message = (
                    f"MediaCrawler 虚拟环境未就绪: {venv_python}。"
                    f"请先执行 uv pip install 安装依赖。"
                )
                task.status = "failed"
                task.updated_at = now_utc()
                self._repo.save_crawl_task(task)
                return

            env = {
                **os.environ,
                "CRAWL_TASK_ID": task.id,
                "CRAWL_KEYWORD": task.keyword,
                "CRAWL_TYPE": task.crawl_type,
                "CRAWL_MAX_COMMENTS": str(task.max_comments),
                "CRAWL_IMPORT_URL": "http://127.0.0.1:8000/api/crawl/import",
            }

            proc = subprocess.Popen(
                [
                    venv_python, "-m", "uvicorn", "api.main:app",
                    "--host", "127.0.0.1", "--port", "8080",
                ],
                cwd=mediacrawler_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            self._processes[task.id] = proc
        except Exception as exc:
            task.error_message = f"MediaCrawler 启动失败（已降级为手动导入模式）: {exc}"
            task.updated_at = now_utc()
            self._repo.save_crawl_task(task)

    def _stop_process(self, task_id: str) -> None:
        proc = self._processes.pop(task_id, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
