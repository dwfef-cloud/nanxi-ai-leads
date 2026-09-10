"""
合规域 Schema · 安全模式 / 暂停恢复 / 合规仪表盘
"""
from pydantic import BaseModel


class SafeModeEnterRequest(BaseModel):
    """进入安全模式请求体"""
    source: str = "manual"       # manual / r3 / health_score
    reason: str = ""


class SafeModeExitRequest(BaseModel):
    """退出安全模式请求体（人工确认）"""
    confirmed: bool = True
    note: str = ""


class ActionResponse(BaseModel):
    """通用操作响应"""
    ok: bool
    message: str = ""
