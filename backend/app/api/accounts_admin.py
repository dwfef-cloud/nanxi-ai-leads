"""账号池管理接口（/accounts/api/*）—— 多选 / 全选 / 批量删除 / 状态复位。

为什么单独做一个页面
--------------------
壳（ui/）是编译产物，往它渲染的表格里插复选框会被重新渲染冲掉；
因此这里提供一个只读业务库的轻量管理面，直接复用同一套设计 token，
用来清理测试账号、重名账号与无登录态的历史账号。
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import RedirectResponse

from app.api import account_pool, ipc_bridge

router = APIRouter()


@router.get("/accounts", include_in_schema=False)
def accounts_redirect() -> RedirectResponse:
    return RedirectResponse(url="/accounts/", status_code=307)


@router.get("/accounts/api/list")
def accounts_list(keyword: str = "", only: str = "") -> dict:
    return account_pool.list_pool(ipc_bridge.DB_PATH, keyword=keyword, only=only)


@router.post("/accounts/api/delete")
def accounts_delete(payload: dict = Body(default={})) -> dict:
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        return {"success": False, "error": "请提供要删除的账号 id 列表"}
    return account_pool.delete_accounts(ipc_bridge.DB_PATH, ids)


@router.post("/accounts/api/reset")
def accounts_reset(payload: dict = Body(default={})) -> dict:
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        return {"success": False, "error": "请提供要复位的账号 id 列表"}
    return account_pool.reset_status(ipc_bridge.DB_PATH, ids)
