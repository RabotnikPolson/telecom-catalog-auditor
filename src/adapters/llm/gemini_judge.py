import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from google import genai
from google.genai import types
import httpx
from pydantic import BaseModel, Field, model_validator

from config.settings import get_settings
from src.domain.entities import AuditResult, AuditStatus, DiscrepancyItem, MissingSpecItem, Product
from .prompts import AUDIT_SYSTEM_PROMPT, build_audit_user_prompt


class LLMDiscrepancy(BaseModel):
    spec_name: str = Field(default="")
    shop_value: str = Field(default="")
    reference_value: str = Field(default="")
    proof_quote: str = Field(default="")
    severity: str = Field(default="warning")

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "shop_value" not in data or not data["shop_value"]:
                for alt in ("store_value", "current_value", "our_value", "shop_val", "value"):
                    if alt in data and data[alt]:
                        data["shop_value"] = str(data[alt])
                        break
            if "reference_value" not in data or not data["reference_value"]:
                for alt in ("ref_value", "external_value", "reference_val"):
                    if alt in data and data[alt]:
                        data["reference_value"] = str(data[alt])
                        break
        return data


class LLMMissingSpec(BaseModel):
    spec_name: str = Field(default="")
    reference_value: str = Field(default="")
    proof_quote: str = Field(default="")

    @model_validator(mode="before")
    @classmethod
    def normalize_missing(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "reference_value" not in data or not data["reference_value"]:
                for alt in ("ref_value", "external_value", "value"):
                    if alt in data and data[alt]:
                        data["reference_value"] = str(data[alt])
                        break
        return data


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
        settings = get_settings()
        self.openrouter_api_key = (
            os.environ.get("OPENROUTER_API_KEY")
            or settings.OPENROUTER_API_KEY
        )
        self.openrouter_model = (
            os.environ.get("OPENROUTER_MODEL")
            or settings.OPENROUTER_MODEL
            or "google/gemini-2.5-flash"
        )
        self.gemini_api_key = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or settings.GEMINI_API_KEY
        )
        self.model = model
        self._client: genai.Client | None = None
        if self.gemini_api_key:
            try:
                self._client = genai.Client(api_key=self.gemini_api_key)
            except Exception:
                self._client = None

    async def clean_search_query(self, title: str, vendor_name: str | None = None) -> str:
        """
        Extracts clean Brand + Model query without noise (colors, device category, advertising slogans).
        E.g.: 'Смартфон Apple iPhone 17 Pro Max 256Gb оранжевый MFYN4HX/A' -> 'Apple iPhone 17 Pro Max 256GB'
        """
        clean_title = title.strip()
        system_msg = (
            "Ты поисковый ассистент каталога электроники в Казахстане. "
            "Получив сырое название товара с витрины магазина, сформируй краткий, идеальный поисковый запрос (Бренд + Модель + ключевая модификация, например объем памяти или версия). "
            "Удали цвета (синий, оранжевый, белый и т.д.), категорию устройства (смартфон, умная колонка, настольная лампа, батарейка, mesh-система), "
            "маркетинговые фразы (до ~425 кв.м., с гибкой ножкой, 2-pack) и внутренние складские артикулы. "
            "Ответь ТОЛЬКО очищенной поисковой фразой на одной строке без кавычек, знаков препинания и пояснений."
        )
        user_msg = f"Название товара: {clean_title}"
        if vendor_name and "склад" not in vendor_name.lower():
            user_msg += f"\nБренд: {vendor_name}"

        try:
            if self.openrouter_api_key:
                payload = {
                    "model": self.openrouter_model,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 100,
                }
                headers = {
                    "Authorization": f"Bearer {self.openrouter_api_key}",
                    "HTTP-Referer": "https://shop.telecom.kz",
                    "X-Title": "Telecom Catalog Auditor",
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        result = data["choices"][0]["message"]["content"].strip().strip('"').strip("'")
                        if result:
                            return result
            elif self._client and self.gemini_api_key:
                response = await self._client.aio.models.generate_content(
                    model=self.model,
                    contents=f"{system_msg}\n\n{user_msg}",
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                if response.text:
                    result = response.text.strip().strip('"').strip("'")
                    if result:
                        return result
        except Exception:
            pass

        return clean_title

    async def judge(
        self,
        product: Product,
        reference_url: str,
        external_markdown: str,
    ) -> AuditResult:
        if not self.openrouter_api_key and (not self._client or not self.gemini_api_key):
            return AuditResult(
                product_id=product.product_id,
                status=AuditStatus.ERROR,
                confidence_score=0.0,
                reference_url=reference_url,
                discrepancies=[],
                matched_specs_count=0,
                total_specs_count=len(product.current_specs),
                details="API key is not configured (neither OPENROUTER_API_KEY nor GEMINI_API_KEY is available)",
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

        t_llm_start = time.perf_counter()
        try:
            raw_text = "{}"
            input_tokens = 0
            output_tokens = 0

            if self.openrouter_api_key:
                payload = {
                    "model": self.openrouter_model,
                    "messages": [
                        {"role": "system", "content": AUDIT_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                    "max_tokens": 4000,
                }
                headers = {
                    "Authorization": f"Bearer {self.openrouter_api_key}",
                    "HTTP-Referer": "https://shop.telecom.kz",
                    "X-Title": "Telecom Catalog Auditor",
                }
                async with httpx.AsyncClient(timeout=45.0) as client:
                    resp = await client.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
                    if resp.status_code != 200:
                        raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text}")
                    data = resp.json()
                    raw_text = data["choices"][0]["message"]["content"] or "{}"
                    usage = data.get("usage", {})
                    input_tokens = usage.get("prompt_tokens", 0) or 0
                    output_tokens = usage.get("completion_tokens", 0) or 0
            else:
                config = types.GenerateContentConfig(
                    system_instruction=AUDIT_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=LLMJudgeOutput,
                    temperature=0.1,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                )
                assert self._client is not None
                response = await self._client.aio.models.generate_content(
                    model=self.model,
                    contents=user_prompt,
                    config=config,
                )
                raw_text = response.text or "{}"
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                    output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

            llm_time_sec = time.perf_counter() - t_llm_start
            cost_usd = (input_tokens * 0.00000010) + (output_tokens * 0.00000040)

            clean_json_str = raw_text.strip()
            if clean_json_str.startswith("```"):
                clean_json_str = re.sub(r"^```(?:json)?\s*", "", clean_json_str, flags=re.I)
                clean_json_str = re.sub(r"\s*```$", "", clean_json_str)

            output = LLMJudgeOutput.model_validate_json(clean_json_str)

            status_str = output.status.strip().upper()
            try:
                audit_status = AuditStatus(status_str)
            except ValueError:
                audit_status = (
                    AuditStatus.MISMATCH
                    if output.discrepancies
                    else AuditStatus.VERIFIED
                )

            # Filter out false-positive discrepancies where reference has placeholder/no data
            placeholder_markers = ("нет данных", "не указано", "отсутствует", "-", "n/a", "none", "null")
            valid_discrepancies = []
            for d in output.discrepancies:
                ref_val = (d.reference_value or "").strip().lower()
                shop_val = (d.shop_value or "").strip().lower()
                quote_val = (d.proof_quote or "").strip().lower()
                if ref_val in placeholder_markers or quote_val in placeholder_markers:
                    continue
                # If shop_value is missing or placeholder, this is actually a missing spec, not a discrepancy!
                if not shop_val or shop_val in ("нет", "отсутствует", "не указано", "n/a", "none"):
                    output.missing_specs.append(
                        LLMMissingSpec(
                            spec_name=d.spec_name,
                            reference_value=d.reference_value,
                            proof_quote=d.proof_quote,
                        )
                    )
                    continue
                valid_discrepancies.append(d)

            if not valid_discrepancies and audit_status == AuditStatus.MISMATCH:
                audit_status = AuditStatus.VERIFIED
                output.details = f"Все заявленные характеристики витрины подтверждены эталоном; выявлено {len(output.missing_specs)} характеристик для обогащения."

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
                for d in valid_discrepancies
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
                llm_time_sec=round(llm_time_sec, 2),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=round(cost_usd, 6),
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
