import json
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "TRUSTMATE-MCP"
    app_env: str = "development"
    mock: bool = True
    port: int = 8000
    log_level: str = "INFO"
    database_url: str | None = None
    database_url_unpooled: str | None = None
    mcp_allowed_hosts: str = "localhost:8000,127.0.0.1:8000"
    # Public Render services must set a strong token before APP_ENV=production.
    mcp_bearer_token: str | None = None
    product_provider: str = "channel3"
    business_provider: str = "foursquare"
    business_fallback_provider: str = "openstreetmap"
    reputation_provider: str = "urlhaus"
    domain_provider: str = "rdap"
    website_provider: str = "native"
    llm_provider: str = "openrouter"
    channel3_api_key: str | None = None
    channel3_base_url: str = "https://api.trychannel3.com"
    channel3_timeout_seconds: float = Field(default=10.0, gt=0)
    channel3_max_retries: int = Field(default=2, ge=0, le=3)
    foursquare_api_key: str | None = None
    foursquare_base_url: str = "https://places-api.foursquare.com"
    foursquare_api_version: str = "2025-06-17"
    foursquare_timeout_seconds: float = Field(default=10.0, gt=0)
    foursquare_max_retries: int = Field(default=2, ge=0, le=3)
    foursquare_premium_enabled: bool = False
    osm_base_url: str = "https://nominatim.openstreetmap.org"
    osm_user_agent: str = "TRUSTMATE/0.1"
    osm_contact_email: str | None = None
    osm_timeout_seconds: float = Field(default=10.0, gt=0)
    osm_cache_ttl_seconds: int = Field(default=3600, ge=0)
    rdap_bootstrap_url: str = "https://data.iana.org/rdap/dns.json"
    rdap_timeout_seconds: float = Field(default=10.0, gt=0)
    rdap_cache_ttl_seconds: int = Field(default=3600, ge=0)
    rdap_live_test: bool = False
    website_timeout_seconds: float = Field(default=10.0, gt=0)
    website_max_pages: int = Field(default=4, ge=1, le=5)
    # Seller reviews are optional. Trustpilot's global Data Solutions service
    # reviews require Insights API entitlement; never silently use POI reviews.
    review_provider: str = "none"
    trustpilot_api_key: str | None = None
    trustpilot_base_url: str = "https://datasolutions.trustpilot.com/v1"
    trustpilot_timeout_seconds: float = Field(default=10.0, gt=0)
    trustpilot_max_review_pages: int = Field(default=5, ge=1, le=10)
    # URLhaus remains fail-closed unless the operator explicitly enables both
    # authorization and live-test flags and provides an Auth-Key.
    urlhaus_auth_key: str | None = None
    urlhaus_authorized: bool = False
    urlhaus_live_test: bool = False
    urlhaus_base_url: str = "https://urlhaus-api.abuse.ch"
    urlhaus_timeout_seconds: float = Field(default=10.0, gt=0)
    urlhaus_max_retries: int = Field(default=1, ge=0, le=3)

    # Optional canonical seller-domain -> physical locality map. This is a
    # discovery hint for Foursquare, never identity proof. Example:
    # {"selfridges.com": "London, United Kingdom"}
    seller_location_hints_json: str | None = None
    openrouter_api_key: str | None = None
    openrouter_model: str = "openai/gpt-oss-20b:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_explainer_enabled: bool = False
    openrouter_timeout_seconds: float = Field(default=15.0, gt=0)
    openrouter_max_retries: int = Field(default=2, ge=0, le=3)
    # SQLite assessment/explanation cache. Render mounts a paid persistent disk
    # at /var/data; local development uses ./data.
    sqlite_path: str = "./data/trustmate.sqlite"
    cache_ttl_days: int = Field(default=3, ge=1, le=30)
    cache_ttl_hours: int = Field(default=72, ge=1)
    cache_max_entries: int = Field(default=256, ge=1, le=10000)

    @property
    def seller_location_hints(self) -> dict[str, str]:
        """Parse the optional seller-locality override map from environment JSON."""
        if not self.seller_location_hints_json:
            return {}
        try:
            value = json.loads(self.seller_location_hints_json)
        except json.JSONDecodeError as exc:
            raise ValueError("SELLER_LOCATION_HINTS_JSON must be a valid JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("SELLER_LOCATION_HINTS_JSON must be a JSON object")
        cleaned: dict[str, str] = {}
        for domain, locality in value.items():
            if not isinstance(domain, str) or not isinstance(locality, str):
                raise ValueError("Seller locality map keys and values must be strings")
            domain = domain.strip().lower().removeprefix("www.").rstrip(".")
            locality = locality.strip()
            if domain and locality:
                cleaned[domain] = locality
        return cleaned

    @property
    def allowed_hosts(self) -> list[str]:
        return [host.strip() for host in self.mcp_allowed_hosts.split(",") if host.strip()]

    def sqlalchemy_url(self, *, migration: bool = False) -> str | None:
        url = self.database_url_unpooled if migration else self.database_url
        if not url:
            return None
        parts = urlsplit(url)
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parts.query)
                if key not in {"channel_binding", "sslmode"}
            ]
        )
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
        return clean_url.replace("postgresql://", "postgresql+asyncpg://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()