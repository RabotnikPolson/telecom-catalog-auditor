from datetime import datetime, timezone
from enum import Enum
import hashlib
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AuditStatus(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"


class SpecItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)
    unit: str | None = Field(default=None)

    def to_normalized_string(self) -> str:
        val = f"{self.value} {self.unit}".strip() if self.unit else self.value.strip()
        return f"{self.name.strip().lower()}:{val.lower()}"


class DiscrepancyItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_name: str = Field(...)
    shop_value: str = Field(...)
    reference_value: str = Field(...)
    proof_quote: str = Field(...)
    source_url: str = Field(...)
    severity: str = Field(default="warning")


class AuditResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(...)
    status: AuditStatus = Field(...)
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    reference_url: str | None = Field(default=None)
    discrepancies: list[DiscrepancyItem] = Field(default_factory=list)
    matched_specs_count: int = Field(default=0, ge=0)
    total_specs_count: int = Field(default=0, ge=0)
    audited_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    details: str | None = Field(default=None)


class Product(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    product_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=1)
    vendor_name: str | None = Field(default=None)
    vendor_sku: str | None = Field(default=None)
    manufacturer_sku: str | None = Field(default=None)
    current_specs: dict[str, str] = Field(default_factory=dict)
    content_hash: str = Field(default="")
    parent_sku: str | None = Field(default=None)
    master_key: str | None = Field(default=None)
    is_master: bool = Field(default=False)
    status: AuditStatus = Field(default=AuditStatus.PENDING)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator("title")
    @classmethod
    def validate_title_non_empty(cls, v: str) -> str:
        clean = v.strip()
        if not clean:
            raise ValueError("Product title cannot be empty or blank")
        return clean

    @staticmethod
    def compute_content_hash(title: str, current_specs: dict[str, str]) -> str:
        clean_title = title.strip().lower()
        sorted_pairs = [
            f"{str(k).strip().lower()}:{str(v).strip().lower()}"
            for k, v in sorted(current_specs.items())
        ]
        payload = f"{clean_title}|" + "|".join(sorted_pairs)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @model_validator(mode="after")
    def populate_content_hash_if_empty(self) -> "Product":
        calculated = self.compute_content_hash(self.title, self.current_specs)
        if not self.content_hash:
            self.content_hash = calculated
        return self

    def recalculate_content_hash(self) -> str:
        self.content_hash = self.compute_content_hash(self.title, self.current_specs)
        self.updated_at = datetime.now(timezone.utc)
        return self.content_hash

    def is_content_matching(self, target_hash: str) -> bool:
        current = self.compute_content_hash(self.title, self.current_specs)
        return current == target_hash

    @property
    def sku_completeness_score(self) -> int:
        score = 0
        if self.manufacturer_sku and self.manufacturer_sku.strip():
            score += 3
        if self.vendor_sku and self.vendor_sku.strip():
            score += 2
        if self.vendor_name and self.vendor_name.strip():
            score += 1
        return score
