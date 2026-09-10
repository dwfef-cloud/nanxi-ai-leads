"""私信发送执行器 Schema · P4-D 契约"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Camel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


class SendRequest(_Camel):
    """发送单条私信请求"""
    content: str | None = None  # 可选：不传则用默认话术


class BatchSendRequest(_Camel):
    """批量发送请求"""
    count: int = Field(default=10, ge=1, le=200)


class SendResultOut(_Camel):
    """单条发送结果 / 发送记录"""
    lead_id: str
    status: str
    account_id: str | None = None
    message: str = ""


class SendRecordOut(_Camel):
    """发送结果列表项（含留痕字段）"""
    id: str
    lead_id: str
    account_id: str | None = None
    content: str = ""
    status: str
    message: str = ""
    sender_mode: str = "mock"
    created_at: datetime


class BatchSendResponse(_Camel):
    """批量发送响应（含后台任务实时进度）"""
    task_id: str | None = None
    running: bool = False
    total: int = 0
    done: int = 0
    success: int = 0
    failed: int = 0
    throttled: int = 0
    results: list[SendResultOut] = Field(default_factory=list)


class SenderConfig(_Camel):
    """发送器配置"""
    mode: str = "mock"               # mock / cdp
    min_interval: int = 30           # 最小发送间隔（秒）
    max_interval: int = 60           # 最大发送间隔（秒）
    daily_limit: int = 50            # 单账号每日发送上限


class SenderConfigUpdate(_Camel):
    """更新发送器配置（全部可选）"""
    mode: str | None = None
    min_interval: int | None = None
    max_interval: int | None = None
    daily_limit: int | None = None


class SenderTestOut(_Camel):
    """测试发送结果"""
    status: str
    message: str = ""
    sender_mode: str = "mock"
