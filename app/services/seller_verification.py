from app.domain.models import BusinessEvidence
from app.domain.interfaces import BusinessProvider


async def business_with_fallback(
    primary: BusinessProvider,
    fallback: BusinessProvider,
    seller_name: str,
    domain: str | None = None,
) -> BusinessEvidence:
    result = await primary.search_business(seller_name, domain)
    if result.provider.status in {"checked", "not_found"}:
        return result
    fallback_result = await fallback.search_business(seller_name, domain)
    if fallback_result.provider.status == "checked":
        return fallback_result
    return result
