import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
import httpx
from pydantic import BaseModel, Field, model_validator

from config.settings import get_settings
from src.domain.entities import AuditResult, AuditStatus, DiscrepancyItem, MissingSpecItem, Product
from .prompts import AUDIT_SYSTEM_PROMPT, build_audit_user_prompt

logger = logging.getLogger(__name__)


class LLMDiscrepancy(BaseModel):
    spec_name: str = Field(default="")
    shop_value: str = Field(default="")
    reference_value: str = Field(default="")
    proof_quote: str = Field(default="")
    contradiction_reason: str = Field(default="")
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


def _normalize_tech_val(val: str) -> str:
    """Normalizes standard metric units and punctuation for strict equivalence checking."""
    v = val.lower()
    # Normalize multipliers / counts: e.g. 4x / 4х -> 4
    v = re.sub(r"(\d+)\s*[xх]\s*", r"\1 ", v)
    v = re.sub(r"\bmah\b", "мач", v)
    v = re.sub(r"\bgb\b", "гб", v)
    v = re.sub(r"\btb\b", "тб", v)
    return re.sub(r"[\s\-_/+,.:;()]+", "", v)


def _check_metric_unit_equivalence(val1: str, val2: str) -> bool:
    """Checks numerical equivalence across standard metric prefixes (e.g. 2500 Мбит/с vs 2.5 Гбит/с, 1000 МГц vs 1 ГГц)."""
    v1 = val1.lower().replace(",", ".")
    v2 = val2.lower().replace(",", ".")

    patterns = [
        # (unit1_pattern, unit2_pattern, multiplier)
        (r"(\d+(?:\.\d+)?)\s*(?:мбит|mbps)", r"(\d+(?:\.\d+)?)\s*(?:гбит|gbps)", 1000.0),
        (r"(\d+(?:\.\d+)?)\s*(?:мгц|mhz)", r"(\d+(?:\.\d+)?)\s*(?:ггц|ghz)", 1000.0),
        (r"(\d+(?:\.\d+)?)\s*(?:мб|mb)", r"(\d+(?:\.\d+)?)\s*(?:гб|gb)", 1000.0),
        (r"(\d+(?:\.\d+)?)\s*(?:гб|gb)", r"(\d+(?:\.\d+)?)\s*(?:тб|tb)", 1000.0),
    ]

    for p1, p2, mult in patterns:
        m1 = re.search(p1, v1)
        m2 = re.search(p2, v2)
        if m1 and m2:
            n1 = float(m1.group(1))
            n2 = float(m2.group(1))
            if abs(n1 - (n2 * mult)) < 1e-3:
                return True

        # Check reverse
        m1_rev = re.search(p2, v1)
        m2_rev = re.search(p1, v2)
        if m1_rev and m2_rev:
            n1 = float(m1_rev.group(1))
            n2 = float(m2_rev.group(1))
            if abs((n1 * mult) - n2) < 1e-3:
                return True

    return False


def _is_equivalent_value(val1: str, val2: str) -> bool:
    """Checks if two specification values are strictly identical (ignoring case, whitespace, separators) or equivalent across units."""
    if not val1 or not val2:
        return False
    norm1 = _normalize_tech_val(val1)
    norm2 = _normalize_tech_val(val2)
    if norm1 == norm2:
        return True
    return _check_metric_unit_equivalence(val1, val2)


class OpenAIJudge:
    """
    Autonomous arbiter based directly on OpenAI API (default model: gpt-6-luna).
    Uses strict zero-shot Natural Language Inference (NLI) logic without product-specific hardcodes.
    """

    API_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = (
            api_key
            or os.environ.get("OPENAI_API_KEY")
            or settings.OPENAI_API_KEY
        )
        self.model = (
            model
            or os.environ.get("OPENAI_MODEL")
            or settings.OPENAI_MODEL
            or "gpt-6-luna"
        )
        self.reasoning_effort = (
            reasoning_effort
            or os.environ.get("OPENAI_REASONING_EFFORT")
            or settings.OPENAI_REASONING_EFFORT
            or "low"
        )

    async def clean_search_query(
        self, title: str, vendor_name: str | None = None
    ) -> tuple[str, str | None]:
        """
        Extracts clean Brand + Model query without noise and detects manufacturer brand.
        Returns: (clean_query: str, brand: str | None)
        """
        clean_title = title.strip()
        if not self.api_key:
            return clean_title, None

        system_msg = (
            "Ты поисковый ассистент каталога электроники. Твоя задача — извлечь из сырого названия товара "
            "истинный бренд производителя и краткую поисковую строку по формуле: [Бренд] + [Модель] + [Аппаратная модификация (память, процессор, ревизия, если есть)].\n"
            "ПРАВИЛА:\n"
            "1. В поле 'brand' укажи только имя бренда производителя (например, TP-Link, Apple, Xiaomi, Keenetic, Яндекс, Samsung, Camelion, D-Link).\n"
            "2. В поле 'clean_query' укажи очищенную поисковую строку. Удали начальное общее наименование категории (смартфон, ноутбук, беспроводной роутер, настольная лампа), цвет, рекламные лозунги и упаковочный шум.\n"
            "3. Ответь СТРОГО в формате JSON:\n"
            '{"brand": "...", "clean_query": "..."}'
        )
        user_msg = f"Название товара: {clean_title}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
            "reasoning_effort": self.reasoning_effort,
            "max_completion_tokens": 300,
            # Note: temperature is intentionally omitted for reasoning models (gpt-6-luna)
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            for attempt in range(3):
                try:
                    resp = await client.post(self.API_URL, json=payload, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        raw_content = str(data["choices"][0]["message"]["content"]).strip()
                        return self._safe_parse_query_metadata(raw_content, clean_title)
                    elif resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    else:
                        break
                except Exception as exc:
                    if attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    logger.warning(f"clean_search_query exception: {exc}")
                    break

        return clean_title, None

    @staticmethod
    def _safe_parse_query_metadata(raw_content: str, default_title: str) -> tuple[str, str | None]:
        """
        Safely parses LLM response into (clean_query, brand).
        Gracefully handles JSON objects, markdown fences, and plain string fallbacks.
        """
        clean = raw_content.strip()
        if not clean:
            return default_title, None

        # 1. Attempt JSON parsing (direct or regex extracted)
        try:
            json_str = clean
            if "{" in clean and "}" in clean:
                m = re.search(r"\{[\s\S]*\}", clean)
                if m:
                    json_str = m.group(0)
            parsed = json.loads(json_str)
            if isinstance(parsed, dict):
                clean_q = str(parsed.get("clean_query") or "").strip().strip('"').strip("'")
                brand = str(parsed.get("brand") or "").strip().strip('"').strip("'") or None
                if clean_q:
                    return clean_q, brand
        except Exception:
            pass

        # 2. Fallback to plain text string (e.g. from mock tests or unstructured LLM output)
        first_line = clean.splitlines()[0].strip().strip('"').strip("'")
        if first_line:
            tokens = first_line.split()
            fallback_brand = tokens[0] if tokens else None
            return first_line, fallback_brand

        return default_title, None

    async def extract_core_product_name(self, title: str, vendor_name: str | None = None) -> str:
        """
        Extracts clean Brand + Model query without noise.
        Strictly returns str as required.
        """
        res = await self.clean_search_query(title=title, vendor_name=vendor_name)
        if isinstance(res, tuple):
            return str(res[0])
        return str(res)

    async def judge(
        self,
        product: Product,
        reference_url: str,
        external_markdown: str,
    ) -> AuditResult:
        """
        Compares store product specifications against reference markdown using gpt-6-luna.
        Returns formal AuditResult domain entity.
        """
        if not self.api_key:
            return AuditResult(
                product_id=product.product_id,
                status=AuditStatus.ERROR,
                confidence_score=0.0,
                reference_url=reference_url,
                discrepancies=[],
                matched_specs_count=0,
                total_specs_count=len(product.current_specs),
                details="OPENAI_API_KEY is not configured",
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

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": AUDIT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "reasoning_effort": self.reasoning_effort,
            "max_completion_tokens": 3000,
            # Note: temperature is intentionally omitted for reasoning models (gpt-6-luna)
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        t_llm_start = time.perf_counter()
        raw_text = "{}"
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                for attempt in range(3):
                    try:
                        resp = await client.post(self.API_URL, json=payload, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            raw_text = data["choices"][0]["message"]["content"] or "{}"
                            usage = data.get("usage", {})
                            input_tokens = usage.get("prompt_tokens", 0) or 0
                            output_tokens = usage.get("completion_tokens", 0) or 0
                            cached_tokens = (
                                usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0
                            )
                            break
                        elif resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                            await asyncio.sleep(2.0 * (attempt + 1))
                            continue
                        else:
                            resp.raise_for_status()
                    except Exception as req_err:
                        if attempt < 2:
                            await asyncio.sleep(2.0 * (attempt + 1))
                            continue
                        raise req_err

            llm_time_sec = time.perf_counter() - t_llm_start

            # gpt-6-luna pricing:
            # uncached input: $0.10 / 1M ($0.00000010)
            # cached input:   $0.05 / 1M ($0.00000005)
            # completion:     $0.50 / 1M ($0.00000050)
            uncached_tokens = max(0, input_tokens - cached_tokens)
            cost_usd = (
                (uncached_tokens * 0.00000010)
                + (cached_tokens * 0.00000005)
                + (output_tokens * 0.00000050)
            )

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
            valid_discrepancies: list[LLMDiscrepancy] = []
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

                # Discard if values are semantically identical (e.g. Ethernet + SFP vs Ethernet + SFP)
                if _is_equivalent_value(d.shop_value, d.reference_value):
                    continue

                # Numeric vs Prose Guard: if shop_value specifies a quantitative number, but the reference
                # value and proof quote contain no digits at all (e.g. '425 кв.м.' vs 'Более широкое покрытие всего дома'),
                # this is qualitative advertising prose in reference, NOT a genuine numeric discrepancy.
                if re.search(r"\d", shop_val) and not re.search(r"\d", ref_val) and not re.search(r"\d", quote_val):
                    continue

                # Mutual Exclusion Guard: if contradiction_reason states that values do not contradict or are compatible
                if hasattr(d, "contradiction_reason") and d.contradiction_reason:
                    reason_clean = d.contradiction_reason.strip().lower()
                    if any(phrase in reason_clean for phrase in (
                        "нет противоречия", "не противоречит", "не исключают", 
                        "могут существовать", "могут сосуществовать", "совместимы",
                        "является подтверждением", "подтверждает", "одно и то же",
                    )):
                        continue

                valid_discrepancies.append(d)

            # Filter out excluded categories from missing_specs (warranty, lifespan/service life, meta-sections)
            excluded_missing_keywords = (
                "гаранти",          # гарантия продавца / производителя, гарантийный срок
                "срок эксплуат",    # срок эксплуатации
                "срок служб",       # срок службы
                "warranty",
                "service life",
                "особенност",       # особенности (рекламный блок)
                "преимуществ",      # преимущества (рекламный блок)
                "наград",           # награды / значки
            )
            # Subjective Adjectives Filter: exclude qualitative marketing badges without factual technical content
            subjective_adjective_markers = (
                "очень высок", "высок", "низк", "средн", "базов",
                "отличн", "хорош", "стильн", "компактн", "улучшен",
                "надежн", "high", "low", "medium", "basic", "premium", "good",
            )
            filtered_missing: list[LLMMissingSpec] = []
            for m in output.missing_specs:
                m_name = (m.spec_name or "").strip().lower()
                m_val = (m.reference_value or "").strip().lower()
                if not m_name or not m_val:
                    continue
                if any(kw in m_name for kw in excluded_missing_keywords):
                    continue
                # If value has no numbers and is just a short subjective evaluation
                if not re.search(r"\d", m_val):
                    val_tokens = m_val.split()
                    if len(val_tokens) <= 3 and any(
                        any(m_val.startswith(marker) or marker in t for marker in subjective_adjective_markers)
                        for t in val_tokens
                    ):
                        continue
                filtered_missing.append(m)
            output.missing_specs = filtered_missing

            if not valid_discrepancies and audit_status == AuditStatus.MISMATCH:
                audit_status = AuditStatus.VERIFIED
                output.details = f"Все заявленные характеристики витрины подтверждены эталоном; выявлено {len(output.missing_specs)} характеристик для обогащения."
            elif valid_discrepancies and len(valid_discrepancies) < len(output.discrepancies):
                spec_names = ", ".join(f"'{d.spec_name}'" for d in valid_discrepancies[:3])
                output.details = f"Обнаружено расхождение по характеристикам: {spec_names}; выявлено {len(output.missing_specs)} недостающих характеристик."

            # Construct domain DiscrepancyItem entities (strictly respecting extra="forbid")
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


# Backwards compatibility alias
GeminiJudge = OpenAIJudge
