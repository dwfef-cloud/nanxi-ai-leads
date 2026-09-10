from fastapi import APIRouter, Depends

from app.core.dependencies import get_business_service
from app.schemas.business import BusinessProfileRead, BusinessProfileUpdate
from app.services.business_service import BusinessService

router = APIRouter(prefix="/business", tags=["business"])


@router.get("/profile", response_model=BusinessProfileRead)
def get_profile(service: BusinessService = Depends(get_business_service)) -> BusinessProfileRead:
    return service.get_profile()


@router.put("/profile", response_model=BusinessProfileRead)
def update_profile(
    payload: BusinessProfileUpdate,
    service: BusinessService = Depends(get_business_service),
) -> BusinessProfileRead:
    return service.update_profile(payload)
