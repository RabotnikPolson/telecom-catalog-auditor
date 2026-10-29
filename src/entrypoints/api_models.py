from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.domain.entities import AuditStatus


class ApiModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CoverageStatus(str, Enum):
    CHECKED = "CHECKED"
    NOT_CHECKED = "NOT_CHECKED"
    UNKNOWN = "UNKNOWN"


class AuditListQuery(ApiModel):
    page: int = Field(default=1, ge=1)
    per_page: int = 25
    status: str | None = None
    attention_required: bool | None = None
    q: str | None = Field(default=None, min_length=1, max_length=100)
    brand: str | None = Field(default=None, min_length=1, max_length=100)
    vendor: str | None = Field(default=None, min_length=1, max_length=100)
    audited_from: datetime | None = None
    audited_to: datetime | None = None
    sort: Literal["audited_at", "confidence_score", "discrepancies_count", "title"] = "audited_at"
    direction: Literal["asc", "desc"] = "desc"

    @field_validator("per_page")
    @classmethod
    def validate_per_page(cls, value: int) -> int:
        if value not in (25, 50, 100):
            raise ValueError("per_page must be 25, 50 or 100")
        return value

    @field_validator("q", "brand", "vendor", mode="before")
    @classmethod
    def trim_optional_search(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("status", mode="before")
    @classmethod
    def trim_status(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def validate_query(self) -> "AuditListQuery":
        if self.audited_from and self.audited_to:
            audited_from = (
                self.audited_from.replace(tzinfo=UTC)
                if self.audited_from.tzinfo is None
                else self.audited_from.astimezone(UTC)
            )
            audited_to = (
                self.audited_to.replace(tzinfo=UTC)
                if self.audited_to.tzinfo is None
                else self.audited_to.astimezone(UTC)
            )
            if audited_to < audited_from:
                raise ValueError("audited_to must be greater than or equal to audited_from")
        if self.status is not None:
            values = [item.strip() for item in self.status.split(",") if item.strip()]
            if not values:
                raise ValueError("status must contain at least one audit status")
            allowed = {status.value for status in AuditStatus}
            invalid = sorted(set(values) - allowed)
            if invalid:
                raise ValueError(f"Unsupported audit status: {', '.join(invalid)}")
        return self

    def parsed_statuses(self) -> tuple[AuditStatus, ...]:
        if self.status is None:
            return ()
        return tuple(AuditStatus(item.strip()) for item in self.status.split(",") if item.strip())


class CountsResponse(ApiModel):
    discrepancies: int = Field(ge=0)
    missing_specs: int = Field(ge=0)
    description_discrepancies: int = Field(ge=0)
    matched_specs: int = Field(ge=0)
    total_specs: int = Field(ge=0)


class PhotosResponse(ApiModel):
    coverage: CoverageStatus
    shop_count: int = Field(ge=0)
    reference_count: int = Field(ge=0)
    status: str


class DescriptionSummaryResponse(ApiModel):
    coverage: CoverageStatus
    quality: str


class AuditListItemResponse(ApiModel):
    audit_id: int = Field(gt=0)
    audit_revision: str
    product_id: int = Field(gt=0)
    shop_sku: str | None
    barcode: str | None
    title: str
    brand_name: str | None
    vendor_name: str | None
    vendor_sku: str | None
    audit_status: AuditStatus
    attention_required: bool
    confidence_score: float = Field(ge=0.0, le=1.0)
    reference_url: str | None
    counts: CountsResponse
    photos: PhotosResponse
    description: DescriptionSummaryResponse
    audited_at: datetime


class PaginationResponse(ApiModel):
    page: int = Field(ge=1)
    per_page: int = Field(gt=0)
    total_items: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    has_previous: bool
    has_next: bool


class AuditListResponse(ApiModel):
    api_version: Literal["1.0"] = "1.0"
    request_id: str
    items: list[AuditListItemResponse]
    pagination: PaginationResponse


class VendorListResponse(ApiModel):
    api_version: Literal["1.0"] = "1.0"
    request_id: str
    items: list[str]


class BrandListResponse(ApiModel):
    api_version: Literal["1.0"] = "1.0"
    request_id: str
    items: list[str]


class ProductResponse(ApiModel):
    product_id: int = Field(gt=0)
    shop_sku: str | None
    barcode: str | None
    title: str
    brand_name: str | None
    vendor_name: str | None
    vendor_sku: str | None
    manufacturer_sku: str | None


class DiscrepancyResponse(ApiModel):
    finding_id: str
    spec_name: str
    shop_value: str
    reference_value: str
    proof_quote: str
    source_url: str
    severity: str


class MissingSpecResponse(ApiModel):
    finding_id: str
    spec_name: str
    reference_value: str
    proof_quote: str
    source_url: str


class SpecsResponse(ApiModel):
    coverage: CoverageStatus
    matched_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    discrepancies: list[DiscrepancyResponse]
    missing_specs: list[MissingSpecResponse]


class DescriptionDiscrepancyResponse(ApiModel):
    finding_id: str
    claim_in_description: str
    factual_truth: str
    contradiction_type: str
    severity: str


class DescriptionDetailResponse(ApiModel):
    coverage: CoverageStatus
    quality: str
    discrepancies: list[DescriptionDiscrepancyResponse]


class AuditDetailResponse(ApiModel):
    audit_id: int = Field(gt=0)
    audit_revision: str
    audit_status: AuditStatus
    attention_required: bool
    confidence_score: float = Field(ge=0.0, le=1.0)
    reference_url: str | None
    reference_title: str | None
    reference_sku: str | None
    audited_at: datetime
    specs: SpecsResponse
    photos: PhotosResponse
    description: DescriptionDetailResponse


class AuditCardResponse(ApiModel):
    api_version: Literal["1.0"] = "1.0"
    request_id: str
    product: ProductResponse
    audit: AuditDetailResponse


class ErrorFieldResponse(ApiModel):
    field: str
    message: str


class ErrorBodyResponse(ApiModel):
    code: str
    message: str
    fields: list[ErrorFieldResponse] = Field(default_factory=list)


class ErrorResponse(ApiModel):
    api_version: Literal["1.0"] = "1.0"
    request_id: str
    error: ErrorBodyResponse
