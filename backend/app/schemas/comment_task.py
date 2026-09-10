"""评论回复任务 Schema · 评论引流策略核心 · v1.0 契约对齐"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _Camel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


class CommentTaskCreate(_Camel):
    """创建评论回复任务（从高意向线索生成）"""
    lead_id: str
    comment_content: str = ""
    video_title: str = ""
    reply_content: str = ""
    account: str | None = None
    reply_script_id: str | None = None


class CommentTaskUpdate(_Camel):
    """更新任务状态（标记已回复/对方私信/对方追评）"""
    status: str | None = None  # pending/replied/user_replied/user_dm/ignored
    reply_content: str | None = None
    replied_at: datetime | None = None
    user_visited: bool | None = None
    user_dm: bool | None = None
    user_replied_comment: bool | None = None


class CommentTaskRead(_Camel):
    """评论回复任务响应"""
    id: str
    lead_id: str
    comment_content: str
    video_title: str
    reply_content: str
    status: str
    account: str | None = None
    replied_at: datetime | None = None
    user_visited: bool = False
    user_dm: bool = False
    user_replied_comment: bool = False
