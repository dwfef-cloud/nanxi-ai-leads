from datetime import datetime, timezone

from app.models.domain import AudienceProfile, BusinessProfile, ProductKnowledge
from app.repositories.memory import MemoryRepository
from app.schemas.business import BusinessProfileUpdate


class BusinessService:
    def __init__(self, repo: MemoryRepository) -> None:
        self._repo = repo

    def get_profile(self) -> BusinessProfile:
        return self._repo.get_business_profile()

    def update_profile(self, payload: BusinessProfileUpdate) -> BusinessProfile:
        updated_at = datetime.now(timezone.utc)
        profile = BusinessProfile(**payload.model_dump(), updated_at=updated_at)
        current_product = self._repo.get_product_knowledge()
        self._repo.save_product_knowledge(ProductKnowledge(
            product_name=payload.product,
            target_customers=payload.target_customer,
            price_range=payload.price_range,
            description=current_product.description,
            selling_points=current_product.selling_points,
            faq=current_product.faq,
            forbidden_claims=current_product.forbidden_claims,
            updated_at=updated_at,
        ))
        current_audience = self._repo.get_audience_profile()
        self._repo.save_audience_profile(AudienceProfile(
            industry=payload.industry,
            region=payload.service_area,
            name=current_audience.name,
            needs=current_audience.needs,
            pain_points=current_audience.pain_points,
            intent_keywords=current_audience.intent_keywords,
            excluded_keywords=current_audience.excluded_keywords,
            updated_at=updated_at,
        ))
        return self._repo.save_business_profile(profile)
