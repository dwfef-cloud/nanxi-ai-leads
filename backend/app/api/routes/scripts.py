"""话术库路由 · GET/POST /api/scripts, 变体管理, 模板列表"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_script_service
from app.schemas.script import (
    ScriptCreate, ScriptRead, ScriptTemplateRead,
    VariantCreate, VariantRead, VariantUpdate,
)
from app.services.script_service import ScriptService

router = APIRouter(prefix="/scripts", tags=["scripts"])


@router.get("", response_model=list[ScriptRead], response_model_by_alias=True)
def list_scripts(
    category: str | None = None,
    service: ScriptService = Depends(get_script_service),
) -> list[ScriptRead]:
    """话术列表，支持 category 过滤"""
    return service.list_scripts(category=category)


@router.get("/templates", response_model=list[ScriptTemplateRead], response_model_by_alias=True)
def list_templates(
    service: ScriptService = Depends(get_script_service),
) -> list[ScriptTemplateRead]:
    """行业话术模板包"""
    return service.list_templates()


@router.post("", response_model=ScriptRead, response_model_by_alias=True)
def create_script(
    payload: ScriptCreate,
    service: ScriptService = Depends(get_script_service),
) -> ScriptRead:
    """新建话术"""
    return service.create_script(payload)


@router.post("/{script_id}/variants", response_model=VariantRead, response_model_by_alias=True)
def add_variant(
    script_id: str,
    payload: VariantCreate,
    service: ScriptService = Depends(get_script_service),
) -> VariantRead:
    """新增话术变体"""
    try:
        return service.add_variant(script_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{script_id}/variants/{variant_id}", response_model=VariantRead, response_model_by_alias=True)
def update_variant(
    script_id: str,
    variant_id: str,
    payload: VariantUpdate,
    service: ScriptService = Depends(get_script_service),
) -> VariantRead:
    """更新变体（权重/状态/文本）。R2 命中时自动切换 switched_off"""
    try:
        return service.update_variant(script_id, variant_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
