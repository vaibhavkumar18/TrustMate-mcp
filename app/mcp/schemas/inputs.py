from pydantic import BaseModel, Field, field_validator


class RankOffersInput(BaseModel):
    query: str = Field(min_length=1)
    budget: float = Field(gt=0, allow_inf_nan=False)
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217 currency code, e.g. USD")
    target_country: str = Field(
        min_length=2,
        max_length=2,
        description="ISO 3166-1 alpha-2 country code, e.g. US",
    )

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("target_country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        return value.strip().upper()


class SellerInput(BaseModel):
    seller: str = Field(min_length=1, description="Seller name or registered domain")
    url: str | None = Field(default=None, description="Official merchant website URL, not an affiliate purchase URL")
    target_country: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Optional target marketplace country (ISO 3166-1 alpha-2). Supply this to search business listings in the correct market, e.g. US or IN.",
    )

    @field_validator("target_country")
    @classmethod
    def normalize_target_country(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None
