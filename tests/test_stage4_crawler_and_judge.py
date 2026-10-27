from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.adapters.crawler.web_crawler import CrawlResult, WebCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.llm.openai_judge import LLMDiscrepancy, LLMJudgeOutput, LLMMissingSpec, OpenAIJudge
from src.domain.entities import AuditResult, AuditStatus, DiscrepancyItem, Product
from src.domain.exceptions import DomainError
from src.entrypoints.cli import _audit_is_current
from src.use_cases.audit_product import AuditProductUseCase
from src.use_cases.resolve_reference import ResolvedReference


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def temp_db(tmp_path: Path) -> SQLiteProductRepository:
    db_file = tmp_path / "test_stage4.db"
    return SQLiteProductRepository(db_file)


@pytest.fixture
def sample_product() -> Product:
    return Product(
        product_id=1001,
        shop_sku="SKU-1001",
        title="Аккумулятор Camelion UB-AA2200",
        barcode="849198020366",
        vendor_name="Al-Style",
        vendor_sku="63450",
        current_specs={
            "Емкость": "2200 мАч",
            "Тип": "Ni-MH",
            "Напряжение": "1.2 В",
        },
    )


def test_incremental_skip_requires_all_parts_for_same_product() -> None:
    first = Product(
        product_id=1001, title="Router RX10", description="Description",
        images_count=3, images_count_known=True,
    )
    second = first.model_copy(update={"product_id": 1002})
    report = AuditResult(
        product_id=1001,
        status=AuditStatus.VERIFIED,
        reference_url="https://example.com/rx10",
        audit_parts={
            "specs": first.audit_input_hash("specs"),
            "description": first.audit_input_hash("description"),
            "photos": first.audit_input_hash("photos", "https://example.com/rx10"),
        },
    )
    assert _audit_is_current(first, report, photos_only=False, skip_desc=False)
    assert not _audit_is_current(second, report, photos_only=False, skip_desc=False)
    assert not _audit_is_current(first.model_copy(update={"images_count_known": False}), report, True, False)
    assert not _audit_is_current(first, report.model_copy(update={"audit_parts": {}}), False, False)
    assert not _audit_is_current(first.model_copy(update={"description": "Changed"}), report, False, False)
    assert _audit_is_current(first.model_copy(update={"description": "Changed"}), report, True, False)


class TestWebCrawler:

    @pytest.mark.anyio
    async def test_crawler_inline_html(self) -> None:
        async with WebCrawler(headless=True) as crawler:
            html = """
            <html>
                <head><title>Test Battery</title></head>
                <body>
                    <h1>Camelion Battery 2200</h1>
                    <table>
                        <tr><th>Параметр</th><th>Значение</th></tr>
                        <tr><td>Емкость</td><td>2200 мАч</td></tr>
                    </table>
                    <dl>
                        <dt>Форм-фактор</dt>
                        <dd>AA</dd>
                    </dl>
                    <div>Описание товара аккумулятора</div>
                </body>
            </html>
            """
            data_url = f"data:text/html;charset=utf-8,{html}"
            res = await crawler.crawl(data_url)

            assert res.success is True
            assert "Camelion Battery 2200" in res.title
            assert "Specifications (Tables)" in res.markdown
            assert "Емкость" in res.markdown
            assert "2200 мАч" in res.markdown

    @pytest.mark.anyio
    async def test_crawler_handles_invalid_url_gracefully(self) -> None:
        async with WebCrawler(headless=True) as crawler:
            res = await crawler.crawl("http://non-existent-domain-12345xyz.local")
            assert res.success is False
            assert res.error is not None


def _make_openai_resp(
    content: str,
    status_code: int = 200,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cached_tokens: int = 0,
) -> httpx.Response:
    data = {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }
    return httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )


class TestOpenAIJudge:

    @pytest.mark.anyio
    async def test_model_verification_distinguishes_rejection_from_failure(self, sample_product: Product) -> None:
        judge = OpenAIJudge(api_key="test-key")
        try:
            response = _make_openai_resp('{"same_model": false}')
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response):
                assert not await judge.matches_model(sample_product, "https://example.com/item", "Another model")
            malformed = _make_openai_resp('{"other": true}')
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=malformed):
                with pytest.raises(DomainError, match="same_model"):
                    await judge.matches_model(sample_product, "https://example.com/item", "Unknown")
            judge.api_key = None
            with pytest.raises(DomainError, match="OPENAI_API_KEY"):
                await judge.matches_model(sample_product, "https://example.com/item", "Unknown")
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_model_verification_receives_specs_and_shared_variant_rules(
        self, sample_product: Product
    ) -> None:
        judge = OpenAIJudge(api_key="test-key")
        response = _make_openai_resp('{"same_model": true}')
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)) as post:
                assert await judge.matches_model(
                    sample_product,
                    "https://example.com/item",
                    "Camelion UB-AA2200 exact reference",
                )
            messages = post.call_args.kwargs["json"]["messages"]
            assert "SIM/eSIM" in messages[0]["content"]
            assert "4G versus 5G" in messages[0]["content"]
            assert '"Емкость": "2200 мАч"' in messages[1]["content"]
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("cached_tokens", "expected_cost"),
        [(400_000, 0.164), (0, 0.20), (1_000_000, 0.11)],
    )
    async def test_judge_estimated_cost_uses_cached_input_rate(
        self, sample_product: Product, cached_tokens: int, expected_cost: float
    ) -> None:
        judge = OpenAIJudge(api_key="test-key")
        mock_output = LLMJudgeOutput(
            status="VERIFIED",
            confidence_score=0.98,
            matched_specs_count=3,
            total_specs_count=3,
            discrepancies=[],
            details="All specs match reference",
        )
        mock_resp = _make_openai_resp(
            mock_output.model_dump_json(),
            prompt_tokens=1_000_000,
            completion_tokens=200_000,
            cached_tokens=cached_tokens,
        )

        try:
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
                result = await judge.judge(
                    product=sample_product,
                    reference_url="https://example.com/item",
                    external_markdown="* Емкость: 2200 мАч",
                )

            assert result.status == AuditStatus.VERIFIED
            assert result.input_tokens == 1_000_000
            assert result.output_tokens == 200_000
            assert result.estimated_cost_usd == pytest.approx(expected_cost)
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_judge_returns_error_when_no_api_key(
        self, sample_product: Product
    ) -> None:
        judge = OpenAIJudge(api_key=None)
        judge.api_key = None

        res = await judge.judge(
            product=sample_product,
            reference_url="https://example.com/item",
            external_markdown="# Item Specs",
        )
        assert res.status == AuditStatus.ERROR
        assert "OPENAI_API_KEY is not configured" in (res.details or "")

    @pytest.mark.anyio
    async def test_judge_verified_scenario(self, sample_product: Product) -> None:
        judge = OpenAIJudge(api_key="test-key")

        mock_output = LLMJudgeOutput(
            status="VERIFIED",
            confidence_score=0.98,
            matched_specs_count=3,
            total_specs_count=3,
            discrepancies=[],
            details="All 3 specs match reference",
        )
        mock_resp = _make_openai_resp(mock_output.model_dump_json())

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            res = await judge.judge(
                product=sample_product,
                reference_url="https://example.com/item",
                external_markdown="* Емкость: 2200 мАч\n* Напряжение: 1.2 В",
            )

        assert res.status == AuditStatus.VERIFIED
        assert res.confidence_score == 0.98
        assert res.matched_specs_count == 3
        assert len(res.discrepancies) == 0

    @pytest.mark.anyio
    async def test_judge_mismatch_scenario_with_proof_quote(
        self, sample_product: Product
    ) -> None:
        judge = OpenAIJudge(api_key="test-key")

        mock_output = LLMJudgeOutput(
            status="MISMATCH",
            confidence_score=0.95,
            matched_specs_count=2,
            total_specs_count=3,
            discrepancies=[
                LLMDiscrepancy(
                    spec_name="Емкость",
                    shop_value="2200 мАч",
                    reference_value="2000 мАч",
                    proof_quote="Емкость: 2000 мАч",
                    severity="critical",
                )
            ],
            details="Found 1 mismatch in capacity",
        )
        mock_resp = _make_openai_resp(mock_output.model_dump_json())

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            res = await judge.judge(
                product=sample_product,
                reference_url="https://example.com/item",
                external_markdown="* Емкость: 2000 мАч",
            )

        assert res.status == AuditStatus.MISMATCH
        assert len(res.discrepancies) == 1
        disc = res.discrepancies[0]
        assert disc.spec_name == "Емкость"
        assert disc.shop_value == "2200 мАч"
        assert disc.reference_value == "2000 мАч"
        assert disc.proof_quote == "Емкость: 2000 мАч"
        assert disc.severity == "critical"
        assert disc.source_url == "https://example.com/item"

    @pytest.mark.anyio
    async def test_judge_missing_specs_scenario_6(self) -> None:
        empty_specs_product = Product(
            product_id=1002,
            title="Умная колонка Яндекс Станция",
            shop_sku="YANDEX-1",
            current_specs={},
        )
        judge = OpenAIJudge(api_key="test-key")

        mock_output = LLMJudgeOutput(
            status="MISSING_SPECS",
            confidence_score=0.92,
            matched_specs_count=0,
            total_specs_count=0,
            discrepancies=[],
            details="Shop catalog has 0 specs but reference has power, bluetooth, audio",
        )
        mock_resp = _make_openai_resp(mock_output.model_dump_json())

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            res = await judge.judge(
                product=empty_specs_product,
                reference_url="https://example.com/yandex",
                external_markdown="# Yandex Station\n* Power: 30W\n* Bluetooth: 5.0",
            )

        assert res.status == AuditStatus.MISSING_SPECS
        assert "Shop catalog has 0 specs" in (res.details or "")

    @pytest.mark.anyio
    async def test_judge_numeric_vs_prose_guard_and_equivalence(self, sample_product: Product) -> None:
        from src.adapters.llm.openai_judge import _is_equivalent_value

        # Test SI metric unit equivalence (Mbps vs Gbps, MHz vs GHz)
        assert _is_equivalent_value("2500 Мбит/с", "2.5 Гбит/с") is True
        assert _is_equivalent_value("1000 МГц", "1 ГГц") is True
        assert _is_equivalent_value("1000 ГБ", "1 ТБ") is True

        judge = OpenAIJudge(api_key="test-key")

        # LLM returned false discrepancy that violates Mutual Exclusion or Numeric vs Prose
        mock_output = LLMJudgeOutput(
            status="MISMATCH",
            confidence_score=0.90,
            matched_specs_count=5,
            total_specs_count=7,
            discrepancies=[
                LLMDiscrepancy(
                    spec_name="Радиус действия внутри помещения",
                    shop_value="425 кв.м.",
                    reference_value="Более широкое покрытие всего дома",
                    proof_quote="Более широкое покрытие всего дома",
                    contradiction_reason="",
                    severity="warning",
                ),
                LLMDiscrepancy(
                    spec_name="Особенности",
                    shop_value="LED-экран с часами",
                    reference_value="Часы есть",
                    proof_quote="Часы есть",
                    contradiction_reason="Нет противоречия, эталон подтверждает наличие часов",
                    severity="warning",
                ),
            ],
            missing_specs=[
                LLMMissingSpec(
                    spec_name="Производительность маршрутизатора",
                    reference_value="Очень высокая",
                    proof_quote="Очень высокая производительность",
                ),
                LLMMissingSpec(
                    spec_name="Стандарт Wi-Fi",
                    reference_value="Wi-Fi 7",
                    proof_quote="Поддержка Wi-Fi 7",
                ),
            ],
            details="Found mismatches",
        )
        mock_resp = _make_openai_resp(mock_output.model_dump_json())

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            res = await judge.judge(
                product=sample_product,
                reference_url="https://example.com/item",
                external_markdown=(
                    "Более широкое покрытие всего дома\nЧасы есть\n"
                    "Очень высокая производительность\nПоддержка Wi-Fi 7"
                ),
            )

        # Both false discrepancies must be discarded, so status flips to VERIFIED
        assert res.status == AuditStatus.VERIFIED
        assert len(res.discrepancies) == 0

        # Subjective missing spec "Очень высокая" must be discarded, factual "Wi-Fi 7" kept
        assert len(res.missing_specs) == 1
        assert res.missing_specs[0].spec_name == "Стандарт Wi-Fi"
        assert res.missing_specs[0].reference_value == "Wi-Fi 7"

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("title", "spec_name", "reference_value", "quote"),
        [
            ("Смартфон Model X", "NFC", "нет", "NFC не поддерживается"),
            ("Роутер Model R", "Поддержка PoE", "отсутствует", "PoE отсутствует"),
            ("Стиральная машина Model W", "Сушка", "нет", "Функция сушки недоступна"),
        ],
    )
    async def test_directly_proven_negative_missing_spec_is_kept_for_different_categories(
        self, title: str, spec_name: str, reference_value: str, quote: str
    ) -> None:
        product = Product(product_id=2001, title=title, current_specs={})
        judge = OpenAIJudge(api_key="test-key")
        output = LLMJudgeOutput(
            status="MISSING_SPECS",
            confidence_score=0.9,
            missing_specs=[
                LLMMissingSpec(
                    spec_name=spec_name,
                    reference_value=reference_value,
                    proof_quote=quote,
                )
            ],
            details="Подтверждена важная отсутствующая характеристика.",
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(product, "https://example.com/item", quote)
            assert result.status == AuditStatus.MISSING_SPECS
            assert len(result.missing_specs) == 1
            assert result.missing_specs[0].reference_value == reference_value
            assert result.missing_specs[0].proof_quote == quote
            assert result.missing_specs[0].source_url == "https://example.com/item"
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_missing_spec_requires_quote_from_transmitted_reference(self) -> None:
        product = Product(product_id=2002, title="Model X", current_specs={})
        judge = OpenAIJudge(api_key="test-key")
        output = LLMJudgeOutput(
            status="MISSING_SPECS",
            missing_specs=[
                LLMMissingSpec(
                    spec_name="NFC",
                    reference_value="нет",
                    proof_quote="NFC не поддерживается",
                )
            ],
            details="Missing NFC",
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(product, "https://example.com/item", "NFC поддерживается")
            assert result.status == AuditStatus.ERROR
            assert result.missing_specs == []
            assert "проверяемых цитат" in (result.details or "")
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_verified_with_only_invented_missing_spec_becomes_error(self) -> None:
        product = Product(product_id=2006, title="Model X", current_specs={"Память": "128 ГБ"})
        judge = OpenAIJudge(api_key="test-key")
        output = LLMJudgeOutput(
            status="VERIFIED",
            matched_specs_count=1,
            missing_specs=[
                LLMMissingSpec(
                    spec_name="NFC",
                    reference_value="нет",
                    proof_quote="NFC не поддерживается",
                )
            ],
            details="Verified",
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(product, "https://example.com/item", "Память: 128 ГБ")
            assert result.status == AuditStatus.ERROR
            assert result.missing_specs == []
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_placeholder_quote_cannot_support_verified_result(self) -> None:
        product = Product(product_id=2008, title="Model X", current_specs={})
        judge = OpenAIJudge(api_key="test-key")
        output = LLMJudgeOutput(
            status="VERIFIED",
            missing_specs=[
                LLMMissingSpec(
                    spec_name="NFC",
                    reference_value="нет",
                    proof_quote="нет данных",
                )
            ],
            details="Verified",
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(
                    product,
                    "https://example.com/item",
                    "NFC не поддерживается",
                )
            assert result.status == AuditStatus.ERROR
            assert result.missing_specs == []
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_filtered_subjective_missing_spec_does_not_leave_empty_missing_status(
        self,
    ) -> None:
        product = Product(product_id=2009, title="Model X", current_specs={})
        judge = OpenAIJudge(api_key="test-key")
        output = LLMJudgeOutput(
            status="MISSING_SPECS",
            missing_specs=[
                LLMMissingSpec(
                    spec_name="Качество",
                    reference_value="очень высокое",
                    proof_quote="Очень высокое качество",
                )
            ],
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(
                    product,
                    "https://example.com/item",
                    "Очень высокое качество",
                )
            assert result.status == AuditStatus.VERIFIED
            assert result.missing_specs == []
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_valid_evidence_survives_alongside_invented_missing_spec(self) -> None:
        product = Product(product_id=2007, title="Model X", current_specs={})
        judge = OpenAIJudge(api_key="test-key")
        output = LLMJudgeOutput(
            status="MISSING_SPECS",
            missing_specs=[
                LLMMissingSpec(
                    spec_name="Wi-Fi",
                    reference_value="Wi-Fi 6",
                    proof_quote="Поддерживается Wi-Fi 6",
                ),
                LLMMissingSpec(
                    spec_name="NFC",
                    reference_value="нет",
                    proof_quote="NFC не поддерживается",
                ),
            ],
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(
                    product,
                    "https://example.com/item",
                    "Поддерживается Wi-Fi 6",
                )
            assert result.status == AuditStatus.MISSING_SPECS
            assert [item.spec_name for item in result.missing_specs] == ["Wi-Fi"]
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_quote_normalization_and_exact_existing_spec_name_filter(self) -> None:
        product = Product(
            product_id=2003,
            title="Model X",
            current_specs={"  NFC  ": "нет"},
        )
        judge = OpenAIJudge(api_key="test-key")
        output = LLMJudgeOutput(
            status="VERIFIED",
            matched_specs_count=1,
            missing_specs=[
                LLMMissingSpec(
                    spec_name="nfc",
                    reference_value="нет",
                    proof_quote="NFC не поддерживается",
                )
            ],
            details="Verified",
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(
                    product,
                    "https://example.com/item",
                    "NFC\u00a0  не поддерживается",
                )
            assert result.status == AuditStatus.VERIFIED
            assert result.missing_specs == []
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_negative_shop_value_is_a_real_value_not_a_missing_spec(self) -> None:
        product = Product(product_id=2004, title="Model X", current_specs={"NFC": "нет"})
        judge = OpenAIJudge(api_key="test-key")
        output = LLMJudgeOutput(
            status="MISMATCH",
            discrepancies=[
                LLMDiscrepancy(
                    spec_name="NFC",
                    shop_value="нет",
                    reference_value="есть",
                    proof_quote="NFC поддерживается",
                    contradiction_reason="Функция не может одновременно отсутствовать и поддерживаться.",
                )
            ],
            details="Mismatch",
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(product, "https://example.com/item", "NFC поддерживается")
            assert result.status == AuditStatus.MISMATCH
            assert len(result.discrepancies) == 1
            assert result.missing_specs == []
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_quote_after_transmitted_25k_boundary_is_rejected(self) -> None:
        product = Product(product_id=2005, title="Model X", current_specs={})
        judge = OpenAIJudge(api_key="test-key")
        quote = "NFC не поддерживается"
        output = LLMJudgeOutput(
            status="MISSING_SPECS",
            missing_specs=[LLMMissingSpec(spec_name="NFC", reference_value="нет", proof_quote=quote)],
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(
                    product,
                    "https://example.com/item",
                    ("x" * 25_000) + quote,
                )
            assert result.status == AuditStatus.ERROR
            assert result.missing_specs == []
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_unverifiable_discrepancy_does_not_become_verified(
        self, sample_product: Product
    ) -> None:
        judge = OpenAIJudge(api_key="test-key")
        output = LLMJudgeOutput(
            status="MISMATCH",
            discrepancies=[
                LLMDiscrepancy(
                    spec_name="Емкость",
                    shop_value="2200 мАч",
                    reference_value="2000 мАч",
                    proof_quote="Емкость: 2000 мАч",
                    contradiction_reason="Значения взаимоисключающие.",
                )
            ],
        )
        response = _make_openai_resp(output.model_dump_json())
        try:
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
                result = await judge.judge(
                    sample_product,
                    "https://example.com/item",
                    "Емкость: 2200 мАч",
                )
            assert result.status == AuditStatus.ERROR
            assert result.discrepancies == []
        finally:
            await judge.aclose()

    @pytest.mark.anyio
    async def test_judge_openai_retry_on_transient_error(self, sample_product: Product) -> None:
        judge = OpenAIJudge(api_key="test-key")

        mock_output = LLMJudgeOutput(
            status="VERIFIED",
            confidence_score=0.99,
            matched_specs_count=3,
            total_specs_count=3,
            discrepancies=[],
            details="All specs match after retry",
        )
        err_resp = _make_openai_resp("", status_code=503)
        ok_resp = _make_openai_resp(mock_output.model_dump_json(), status_code=200)

        # First request returns 503, second returns 200 OK
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=[err_resp, ok_resp]), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            res = await judge.judge(
                product=sample_product,
                reference_url="https://example.com/item",
                external_markdown="Specs",
            )
            assert res.status == AuditStatus.VERIFIED
            assert res.confidence_score == 0.99

    @pytest.mark.anyio
    async def test_extract_core_product_name_openai(self) -> None:
        judge = OpenAIJudge(api_key="test-key")
        mock_resp = _make_openai_resp("Apple iPhone 17 Pro Max 256GB")

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            name = await judge.extract_core_product_name("Смартфон Apple iPhone 17 Pro Max 256Gb оранжевый MFYN4HX/A")
            assert name == "Apple iPhone 17 Pro Max 256GB"

    def test_contradiction_reason_extra_forbid_domain_compliance(self) -> None:
        llm_d = LLMDiscrepancy(
            spec_name="Цвет",
            shop_value="Черный",
            reference_value="Белый",
            proof_quote="Цвет: белый",
            contradiction_reason="Объект не может быть одновременно белым и черным",
            severity="critical",
        )
        assert llm_d.contradiction_reason == "Объект не может быть одновременно белым и черным"

        # Domain entity DiscrepancyItem has extra="forbid"
        domain_d = DiscrepancyItem(
            spec_name=llm_d.spec_name,
            shop_value=llm_d.shop_value,
            reference_value=llm_d.reference_value,
            proof_quote=llm_d.proof_quote,
            source_url="https://example.com",
            severity=llm_d.severity,
        )
        assert domain_d.spec_name == "Цвет"

        # Ensuring extra="forbid" raises ValidationError if contradiction_reason is leaked into domain
        with pytest.raises(Exception):
            DiscrepancyItem(
                spec_name=llm_d.spec_name,
                shop_value=llm_d.shop_value,
                reference_value=llm_d.reference_value,
                proof_quote=llm_d.proof_quote,
                source_url="https://example.com",
                severity=llm_d.severity,
                contradiction_reason=llm_d.contradiction_reason,  # type: ignore[call-arg]
            )


# Backwards compatibility alias for test suite
TestGeminiJudge = TestOpenAIJudge


class TestAuditProductUseCase:

    @pytest.mark.anyio
    async def test_full_audit_lifecycle_with_cache_hit(
        self, temp_db: SQLiteProductRepository, sample_product: Product
    ) -> None:
        temp_db.save_product(sample_product)

        mock_resolver = MagicMock()
        mock_resolver.execute = AsyncMock(
            return_value=ResolvedReference(
                product_id=sample_product.product_id,
                reference_url="https://www.al-style.kz/item/63450",
                source_type="VENDOR_DIRECT",
                query_used="63450",
                scenario_applied="SCENARIO_1",
                status="RESOLVED",
                is_cached=False,
            )
        )

        mock_crawler = MagicMock()
        mock_crawler.crawl = AsyncMock(
            return_value=CrawlResult(
                url="https://www.al-style.kz/item/63450",
                title="Camelion UB-AA2200",
                markdown="* Емкость: 2200 мАч\n* Напряжение: 1.2 В",
                status_code=200,
                success=True,
            )
        )

        mock_judge = MagicMock()
        mock_judge.judge = AsyncMock(
            return_value=AuditResult(
                product_id=sample_product.product_id,
                status=AuditStatus.VERIFIED,
                confidence_score=0.99,
                reference_url="https://www.al-style.kz/item/63450",
                discrepancies=[],
                matched_specs_count=3,
                total_specs_count=3,
                details="Verified specs",
            )
        )

        use_case = AuditProductUseCase(
            product_repo=temp_db,
            reference_resolver=mock_resolver,
            crawler=mock_crawler,
            judge=mock_judge,
        )

        res1 = await use_case.audit_product(sample_product)
        assert res1.status == AuditStatus.VERIFIED
        assert mock_crawler.crawl.call_count == 1

        cached_specs = temp_db.get_specs_cache("https://www.al-style.kz/item/63450")
        assert cached_specs is not None
        assert "Camelion UB-AA2200" in cached_specs

        updated_prod = temp_db.get_product_by_id(sample_product.product_id)
        assert updated_prod is not None
        assert updated_prod.status == AuditStatus.VERIFIED

        latest_audit = temp_db.get_latest_audit_result(sample_product.product_id)
        assert latest_audit is not None
        assert latest_audit.status == AuditStatus.VERIFIED
        assert latest_audit.confidence_score == 0.99

        res2 = await use_case.audit_product(sample_product)
        assert res2.status == AuditStatus.VERIFIED
        assert mock_crawler.crawl.call_count == 1

    @pytest.mark.anyio
    async def test_audit_unresolved_reference_sets_not_found(
        self, temp_db: SQLiteProductRepository, sample_product: Product
    ) -> None:
        temp_db.save_product(sample_product)

        mock_resolver = MagicMock()
        mock_resolver.execute = AsyncMock(return_value=None)

        mock_crawler = MagicMock()
        mock_judge = MagicMock()

        use_case = AuditProductUseCase(
            product_repo=temp_db,
            reference_resolver=mock_resolver,
            crawler=mock_crawler,
            judge=mock_judge,
        )

        res = await use_case.audit_product(sample_product)
        assert res.status == AuditStatus.NOT_FOUND
        assert res.reference_url is None
        assert mock_crawler.crawl.call_count == 0

        updated_prod = temp_db.get_product_by_id(sample_product.product_id)
        assert updated_prod is not None
        assert updated_prod.status == AuditStatus.NOT_FOUND

    @pytest.mark.anyio
    async def test_audit_crawler_failure_sets_error(
        self, temp_db: SQLiteProductRepository, sample_product: Product
    ) -> None:
        temp_db.save_product(sample_product)

        mock_resolver = MagicMock()
        mock_resolver.execute = AsyncMock(
            return_value=ResolvedReference(
                product_id=sample_product.product_id,
                reference_url="https://bad-site.local",
                source_type="VENDOR_DIRECT",
                query_used=None,
                scenario_applied="SCENARIO_1",
                status="RESOLVED",
            )
        )

        mock_crawler = MagicMock()
        mock_crawler.crawl = AsyncMock(
            return_value=CrawlResult(
                url="https://bad-site.local",
                markdown="",
                success=False,
                error="Timeout connecting to host",
            )
        )

        mock_judge = MagicMock()

        use_case = AuditProductUseCase(
            product_repo=temp_db,
            reference_resolver=mock_resolver,
            crawler=mock_crawler,
            judge=mock_judge,
        )

        res = await use_case.audit_product(sample_product)
        assert res.status == AuditStatus.ERROR
        assert "Crawler error: Timeout" in (res.details or "")

        updated_prod = temp_db.get_product_by_id(sample_product.product_id)
        assert updated_prod is not None
        assert updated_prod.status == AuditStatus.ERROR

    @pytest.mark.anyio
    async def test_audit_batch_concurrency(
        self, temp_db: SQLiteProductRepository
    ) -> None:
        prods = [
            Product(product_id=2001, title="Item 1"),
            Product(product_id=2002, title="Item 2"),
            Product(product_id=2003, title="Item 3"),
        ]
        for p in prods:
            temp_db.save_product(p)

        mock_resolver = MagicMock()
        mock_resolver.execute = AsyncMock(
            return_value=ResolvedReference(
                product_id=2001,
                reference_url="https://example.com/ref",
                source_type="BARCODE_SEARCH",
                query_used=None,
                scenario_applied="SCENARIO_2",
                status="RESOLVED",
            )
        )

        mock_crawler = MagicMock()
        mock_crawler.crawl = AsyncMock(
            return_value=CrawlResult(
                url="https://example.com/ref",
                markdown="# Item",
                success=True,
            )
        )

        mock_judge = MagicMock()
        mock_judge.judge = AsyncMock(
            return_value=AuditResult(
                product_id=2001,
                status=AuditStatus.VERIFIED,
                confidence_score=1.0,
            )
        )

        use_case = AuditProductUseCase(
            product_repo=temp_db,
            reference_resolver=mock_resolver,
            crawler=mock_crawler,
            judge=mock_judge,
        )

        results = await use_case.audit_batch(prods, concurrency=2)
        assert len(results) == 3
        for r in results:
            assert r.status == AuditStatus.VERIFIED

    @pytest.mark.anyio
    async def test_audit_escalates_to_step2_when_step1_pinpoint_rejected(
        self, temp_db: SQLiteProductRepository
    ) -> None:
        laptop = Product(product_id=3001, title="MacBook Air 13 M4 (A3240)")
        temp_db.save_product(laptop)

        mock_resolver = MagicMock()
        mock_resolver.execute = AsyncMock(
            return_value=ResolvedReference(
                product_id=3001,
                reference_url="https://kaspi.kz/shop/p/case-1",
                source_type="CODE_PINPOINT",
                query_used="A3240",
                candidate_urls=[
                    "https://kaspi.kz/shop/p/case-1",
                    "https://kaspi.kz/shop/p/case-2",
                    "https://kaspi.kz/shop/p/case-3",
                ],
                scenario_applied="SCENARIO_4",
                status="FOUND",
            )
        )
        mock_resolver.resolve_marketing_fallback = AsyncMock(
            return_value=ResolvedReference(
                product_id=3001,
                reference_url="https://kaspi.kz/shop/p/macbook-real",
                source_type="MODEL_SEARCH",
                query_used="Apple MacBook Air 13 M4",
                candidate_urls=["https://kaspi.kz/shop/p/macbook-real"],
                scenario_applied="SCENARIO_4",
                status="FOUND",
            )
        )

        crawled_urls = []

        async def fake_crawl(url: str):
            crawled_urls.append(url)
            return CrawlResult(url=url, markdown="# Specs", success=True, status_code=200)

        mock_crawler = MagicMock()
        mock_crawler.crawl = AsyncMock(side_effect=fake_crawl)

        async def fake_judge(product, reference_url, external_markdown):
            if "case" in reference_url:
                return AuditResult(
                    product_id=product.product_id,
                    status=AuditStatus.NOT_FOUND,
                    confidence_score=1.0,
                    reference_url=reference_url,
                    details="Accessory, not a laptop",
                )
            return AuditResult(
                product_id=product.product_id,
                status=AuditStatus.VERIFIED,
                confidence_score=0.99,
                reference_url=reference_url,
                details="Exact MacBook match",
            )

        mock_judge = MagicMock()
        mock_judge.judge = AsyncMock(side_effect=fake_judge)

        use_case = AuditProductUseCase(
            product_repo=temp_db,
            reference_resolver=mock_resolver,
            crawler=mock_crawler,
            judge=mock_judge,
        )

        res = await use_case.audit_product(laptop, force_refresh=True)

        assert res.status == AuditStatus.VERIFIED
        assert res.reference_url == "https://kaspi.kz/shop/p/macbook-real"
        # Step 1 tested at most 2 candidates
        assert "https://kaspi.kz/shop/p/case-1" in crawled_urls
        assert "https://kaspi.kz/shop/p/case-2" in crawled_urls
        assert "https://kaspi.kz/shop/p/case-3" not in crawled_urls  # Third candidate skipped!
        # Step 2 was escalated to and tested
        assert "https://kaspi.kz/shop/p/macbook-real" in crawled_urls
        assert mock_resolver.resolve_marketing_fallback.called is True
        # Verify trace contains escalation marker
        trace_str = " ".join(res.search_trace or [])
        assert "[Escalate]" in trace_str

