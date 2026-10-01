from datetime import datetime, timezone
import os
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from config.settings import get_settings
from src.domain.entities import AuditResult, AuditStatus, DiscrepancyItem, MissingSpecItem, Product
from .prompts import AUDIT_SYSTEM_PROMPT, build_audit_user_prompt


class LLMDiscrepancy(BaseModel):
    spec_name: str = Field(...)
    shop_value: str = Field(...)
    reference_value: str = Field(...)
    proof_quote: str = Field(...)
    severity: str = Field(default="warning")


class LLMMissingSpec(BaseModel):
    spec_name: str = Field(...)
    reference_value: str = Field(...)
    proof_quote: str = Field(...)


class LLMJudgeOutput(BaseModel):
    status: str = Field(...)
    confidence_score: float = Field(default=0.0)
    matched_specs_count: int = Field(default=0)
    total_specs_count: int = Field(default=0)
    discrepancies: list[LLMDiscrepancy] = Field(default_factory=list)
    missing_specs: list[LLMMissingSpec] = Field(default_factory=list)
    details: str = Field(default="")


class GeminiJudge:

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or get_settings().GEMINI_API_KEY
        )
        self.model = model
        self._client: genai.Client | None = None
        if self.api_key:
            self._client = genai.Client(api_key=self.api_key)

    async def judge(
        self,
        product: Product,
        reference_url: str,
        external_markdown: str,
    ) -> AuditResult:
        if not self._client or not self.api_key:
            return AuditResult(
                product_id=product.product_id,
                status=AuditStatus.ERROR,
                confidence_score=0.0,
                reference_url=reference_url,
                discrepancies=[],
                matched_specs_count=0,
                total_specs_count=len(product.current_specs),
                details="GEMINI_API_KEY is not configured",
            )

        user_prompt = build_audit_user_prompt(
            product_title=product.title,
            shop_sku=product.shop_sku,
            barcode=product.barcode,
            vendor_sku=product.vendor_sku,
            current_specs=product.current_specs,
            reference_url=reference_url,
            external_markdown=external_markdown,
        )

        config = types.GenerateContentConfig(
            system_instruction=AUDIT_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=LLMJudgeOutput,
            temperature=0.1,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=user_prompt,
                config=config,
            )

            raw_text = response.text or "{}"
            output = LLMJudgeOutput.model_validate_json(raw_text)

            status_str = output.status.strip().upper()
            try:
                audit_status = AuditStatus(status_str)
            except ValueError:
                audit_status = (
                    AuditStatus.MISMATCH
                    if output.discrepancies
                    else AuditStatus.VERIFIED
                )

            discrepancy_items = [
                DiscrepancyItem(
                    spec_name=d.spec_name,
                    shop_value=d.shop_value,
                    reference_value=d.reference_value,
                    proof_quote=d.proof_quote,
                    source_url=reference_url,
                    severity=(
                        d.severity
                        if d.severity in ("critical", "warning")
                        else "warning"
                    ),
                )
                for d in output.discrepancies
            ]

            missing_items = [
                MissingSpecItem(
                    spec_name=m.spec_name,
                    reference_value=m.reference_value,
                    proof_quote=m.proof_quote,
                    source_url=reference_url,
                )
                for m in output.missing_specs
            ]

            return AuditResult(
                product_id=product.product_id,
                status=audit_status,
                confidence_score=max(0.0, min(1.0, output.confidence_score)),
                reference_url=reference_url,
                discrepancies=discrepancy_items,
                missing_specs=missing_items,
                matched_specs_count=output.matched_specs_count,
                total_specs_count=output.total_specs_count or len(product.current_specs),
                audited_at=datetime.now(timezone.utc),
                details=output.details,
            )
        except Exception as e:
            return AuditResult(
                product_id=product.product_id,
                status=AuditStatus.ERROR,
                confidence_score=0.0,
                reference_url=reference_url,
                discrepancies=[],
                matched_specs_count=0,
                total_specs_count=len(product.current_specs),
                details=f"LLM Judge error: {e}",
            )
