"""
采集任务 · Pydantic Schemas
=============================
与 CrawlTask 领域模型对齐，用于 API 请求/响应校验。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CrawlTaskCreate(BaseModel):
    """创建采集任务"""
    name: str = Field(default="", max_length=200)
    crawl_type: Literal["comment", "search", "profile"] = "comment"
    keyword: str = ""
    competitor_account: str = ""
    video_url: str = ""
    source: Literal["own_comment", "competitor"] = "own_comment"
    intent_keywords: str = ""
    excluded_keywords: str = ""
    max_comments: int = Field(default=100, ge=1, le=10000)


class CrawlTaskRead(BaseModel):
    """采集任务详情"""
    id: str
    name: str
    crawl_type: str
    keyword: str
    competitor_account: str
    video_url: str
    source: str
    intent_keywords: str
    excluded_keywords: str
    max_comments: int
    status: str
    collected_count: int
    imported_count: int
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CrawlTaskStatus(BaseModel):
    """采集任务状态响应"""
    id: str
    status: str
    collected_count: int
    imported_count: int
    max_comments: int
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class CrawlImportItem(BaseModel):
    """单条采集评论数据（MediaCrawler 输出格式）"""
    nickname: str = ""
    content: str = ""                          # 评论内容
    comment_id: str = ""                       # 评论唯一ID（去重用）
    aweme_id: str = ""                         # 视频ID
    video_title: str = ""                      # 视频标题
    source_url: str = ""                       # 来源URL
    user_id: str = ""                          # 用户ID
    create_time: str = ""                      # 评论时间


class CrawlImportRequest(BaseModel):
    """采集结果批量入库请求"""
    task_id: str = ""
    source: Literal["own_comment", "competitor"] = "own_comment"
    keyword: str = ""
    items: list[CrawlImportItem] = Field(default_factory=list)


class CrawlImportResponse(BaseModel):
    """采集结果入库响应"""
    imported: int
    skipped: int
    task_id: str
    leads: list[str] = Field(default_factory=list)  # 新入库的 lead id 列表
