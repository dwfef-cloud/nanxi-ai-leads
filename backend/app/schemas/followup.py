"""今日跟进 · Schema 定义

严格对照 api-contract.md 第三节 3.2。
响应字段使用 camelCase（customerId / customerName）。
"""
from pydantic import BaseModel, ConfigDict, Field


class FollowUpCreate(BaseModel):
    """新建跟进待办 · 关联客户而非线索"""
    customer_id: str | None = None
    customer_name: str = ""
    type: str = ""
    text: str = ""
    due: str = ""
    overdue: bool = False


class FollowUpComplete(BaseModel):
    """标记完成 · 可选备注"""
    note: str | None = None


class FollowUpRead(BaseModel):
    """跟进待办详情 · camelCase 输出"""
    model_config = ConfigDict(from_attributes=True)

    id: str
    customerId: str | None = Field(validation_alias="customer_id", default=None)
    customerName: str = Field(validation_alias="customer_name")
    type: str
    text: str
    due: str
    overdue: bool
    done: bool
