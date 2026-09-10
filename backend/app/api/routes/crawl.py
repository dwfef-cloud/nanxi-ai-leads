"""
采集任务 API 路由
==================
MediaCrawler 采集对接模块 — 采集任务管理与结果入库。

端点：
  GET    /api/crawl/tasks              采集任务列表
  POST   /api/crawl/tasks              创建采集任务
  POST   /api/crawl/tasks/{id}/start   启动采集
  POST   /api/crawl/tasks/{id}/stop    停止采集
  GET    /api/crawl/tasks/{id}/status  采集状态
  DELETE /api/crawl/tasks/{id}         删除采集任务
  POST   /api/crawl/import              采集结果批量入库
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import get_crawl_service
from app.schemas.crawl import (
    CrawlImportRequest,
    CrawlImportResponse,
    CrawlTaskCreate,
    CrawlTaskRead,
    CrawlTaskStatus,
)
from app.services.crawl_service import CrawlService

router = APIRouter(prefix="/crawl", tags=["crawl"])


@router.get("/tasks", response_model=list[CrawlTaskRead])
def list_crawl_tasks(
    status: str | None = Query(None, description="按状态筛选: pending/running/completed/failed"),
    service: CrawlService = Depends(get_crawl_service),
) -> list:
    """采集任务列表"""
    return service.list_tasks(status=status)


@router.post("/tasks", response_model=CrawlTaskRead, status_code=201)
def create_crawl_task(
    payload: CrawlTaskCreate,
    service: CrawlService = Depends(get_crawl_service),
) -> dict:
    """创建采集任务

    支持三种采集入口（可组合）：
    - keyword: 搜索关键词
    - competitor_account: 对标账号
    - video_url: 指定视频URL
    """
    if not payload.keyword and not payload.competitor_account and not payload.video_url:
        raise HTTPException(
            status_code=400,
            detail="至少需要提供一个采集入口：keyword / competitor_account / video_url",
        )
    return service.create_task(payload)


@router.post("/tasks/{task_id}/start", response_model=CrawlTaskRead)
def start_crawl_task(
    task_id: str,
    service: CrawlService = Depends(get_crawl_service),
) -> dict:
    """启动采集任务"""
    try:
        return service.start_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"采集任务不存在: {task_id}")


@router.post("/tasks/{task_id}/stop", response_model=CrawlTaskRead)
def stop_crawl_task(
    task_id: str,
    service: CrawlService = Depends(get_crawl_service),
) -> dict:
    """停止采集任务"""
    try:
        return service.stop_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"采集任务不存在: {task_id}")


@router.get("/tasks/{task_id}/status", response_model=CrawlTaskStatus)
def get_crawl_task_status(
    task_id: str,
    service: CrawlService = Depends(get_crawl_service),
) -> dict:
    """采集任务状态（含进度）"""
    try:
        return service.get_status(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"采集任务不存在: {task_id}")


@router.delete("/tasks/{task_id}", status_code=204)
def delete_crawl_task(
    task_id: str,
    service: CrawlService = Depends(get_crawl_service),
) -> None:
    """删除采集任务"""
    try:
        service.delete_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"采集任务不存在: {task_id}")


@router.post("/import", response_model=CrawlImportResponse)
def import_crawl_results(
    payload: CrawlImportRequest,
    service: CrawlService = Depends(get_crawl_service),
) -> dict:
    """采集结果批量入库为线索

    MediaCrawler 输出的评论数据通过此端点自动入库：
    - 评论内容 → Lead.comment
    - 评论用户昵称 → Lead.nickname
    - source=own_comment 或 competitor
    - 自动评分（高意向关键词加权）
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="items 不能为空")
    return service.import_comments(payload)
