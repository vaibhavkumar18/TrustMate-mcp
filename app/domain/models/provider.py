from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

ProviderStatus = Literal["checked", "not_found", "unverified", "error"]
EvidenceStatus = Literal["ok", "warn", "bad", "unverified"]


class RetentionPolicy(BaseModel):
    cacheable: bool
    max_ttl_hours: int | None = None
    allowed_fields: list[str] = Field(default_factory=list)
    attribution: str | None = None


class ProviderResult(BaseModel):
    provider_code: str
    capability: str
    status: ProviderStatus
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = Field(default_factory=dict)
    source_reference: str | None = None
    cache_policy: RetentionPolicy
    error_code: str | None = None
    error_message: str | None = None


class VerificationEvidence(BaseModel):
    provider_code: str
    status: EvidenceStatus
    detail: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_reference: str | None = None


class SellerIdentity(BaseModel):
    display_name: str
    seller_key: str
    normalized_domain: str | None = None
    canonical_url: str | None = None
    identity_status: Literal["domain_matched", "provider_matched", "name_only", "ambiguous"]
    original_url: str | None = None


class Offer(BaseModel):
    id: str
    product_id: str
    title: str
    price: float
    currency: str
    url: str
    seller: SellerIdentity
    variant: dict[str, str] = Field(default_factory=dict)


class Product(BaseModel):
    id: str
    title: str
    model: str | None = None
    variant: dict[str, str] = Field(default_factory=dict)


class ProductSearchResult(BaseModel):
    provider: ProviderResult
    products: list[Product] = Field(default_factory=list)
    offers: list[Offer] = Field(default_factory=list)
    next_page_token: str | None = None


class ReviewEvidence(BaseModel):
    # "place" ratings describe a physical POI, not the retailer's online
    # service history. Only scope="seller" can contribute to TRUSTMATE's
    # Reviews factor. Provider metadata is optional for backwards compatibility
    # with existing business-directory adapters and tests.
    scope: Literal["seller", "place", "unknown"] = "unknown"
    rating: float | None = Field(default=None, ge=0, le=5)
    # For Trustpilot, this is the number of verified service reviews actually
    # inspected by the adapter, not the profile's lifetime total.
    review_count: int | None = Field(default=None, ge=0)
    total_review_count: int | None = Field(default=None, ge=0)
    sample_complete: bool = True
    snippets: list[str] = Field(default_factory=list)
    latest_review_at: datetime | None = None
    provider: ProviderResult | None = None


class BusinessEvidence(BaseModel):
    provider: ProviderResult
    business_name: str | None = None
    address: str | None = None
    provider_place_id: str | None = None
    match_confidence: Literal["high", "medium", "low"] | None = None
    reviews: ReviewEvidence | None = None
    attribution: str | None = None
    telephone: str | None = None
    email: str | None = None
    official_website: str | None = None
    unresolved_flags: list[str] = Field(default_factory=list)


class DomainEvidence(BaseModel):
    provider: ProviderResult
    domain: str
    creation_date: datetime | None = None
    registrar: str | None = None
    expiration_date: datetime | None = None
    reregistration_date: datetime | None = None
    last_changed_date: datetime | None = None
    status: list[str] = Field(default_factory=list)
    nameservers_present: bool | None = None
    dnssec_enabled: bool | None = None


class WebsiteEvidence(BaseModel):
    provider: ProviderResult
    https_reachable: bool | None = None
    tls_valid: bool | None = None
    tls_expires_at: datetime | None = None
    contact_present: bool | None = None
    contact_email_present: bool | None = None
    contact_phone_present: bool | None = None
    contact_address_present: bool | None = None
    return_policy_present: bool | None = None
    refund_policy_present: bool | None = None
    shipping_policy_present: bool | None = None
    terms_present: bool | None = None


class ReputationEvidence(BaseModel):
    provider: ProviderResult
    is_malicious: bool | None = None
    threat_type: str | None = None


class LLMExplanation(BaseModel):
    provider: ProviderResult
    summary: str


class SellerEvidence(BaseModel):
    identity: SellerIdentity
    business: BusinessEvidence
    domain: DomainEvidence
    website: WebsiteEvidence
    reputation: ReputationEvidence
    # Keep seller service reviews separate from directory/POI records. The
    # business.reviews field remains supported for legacy providers, but
    # physical-place reviews are never promoted to seller scope.
    reviews: ReviewEvidence | None = None
    errors: list[str] = Field(default_factory=list)
