from typing import Protocol

from app.domain.models import (
    BusinessEvidence, DomainEvidence, LLMExplanation, ProductSearchResult,
    ReputationEvidence, ReviewEvidence, WebsiteEvidence,
)


class ProductProvider(Protocol):
    async def search(
        self,
        query: str,
        budget: float,
        target_country: str,
        currency: str,
        page_token: str | None = None,
    ) -> ProductSearchResult: ...


class BusinessProvider(Protocol):
    async def search_business(
        self, seller_name: str, domain: str | None = None, location_hint: str | None = None
    ) -> BusinessEvidence: ...


class ReputationProvider(Protocol):
    async def check_url(self, url: str) -> ReputationEvidence: ...


class DomainProvider(Protocol):
    async def lookup_domain(self, domain: str) -> DomainEvidence: ...


class ReviewProvider(Protocol):
    async def lookup_seller_reviews(
        self, domain: str, target_country: str | None = None
    ) -> ReviewEvidence: ...


class WebsiteProvider(Protocol):
    async def inspect(self, url: str) -> WebsiteEvidence: ...


class LLMProvider(Protocol):
    async def generate_explanation(
        self, evidence_summary: dict[str, object], review_snippets: list[str]
    ) -> LLMExplanation: ...
