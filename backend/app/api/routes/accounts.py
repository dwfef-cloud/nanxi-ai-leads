"""抖音账号路由 · v1.0 契约对齐

GET/POST/PATCH /api/accounts
含健康分五维因子、限流状态机、日频配置。
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_account_service
from app.schemas.account import AccountCreate, AccountRead, AccountUpdate
from app.services.account_service import AccountService

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountRead], response_model_by_alias=True)
def list_accounts(
    service: AccountService = Depends(get_account_service),
) -> list[AccountRead]:
    """账号列表（含健康分和限流状态）"""
    return service.list_accounts()


@router.get("/{account_id}", response_model=AccountRead, response_model_by_alias=True)
def get_account(
    account_id: str,
    service: AccountService = Depends(get_account_service),
) -> AccountRead:
    try:
        return service.get_account(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc


@router.post("", response_model=AccountRead, response_model_by_alias=True)
def create_account(
    payload: AccountCreate,
    service: AccountService = Depends(get_account_service),
) -> AccountRead:
    """新增账号"""
    return service.create_account(payload)


@router.patch("/{account_id}", response_model=AccountRead, response_model_by_alias=True)
def update_account(
    account_id: str,
    payload: AccountUpdate,
    service: AccountService = Depends(get_account_service),
) -> AccountRead:
    """更新账号（状态/备注/健康分因子/日频配置）"""
    try:
        return service.update_account(account_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc
