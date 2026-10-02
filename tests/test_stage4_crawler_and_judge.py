import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.adapters.crawler.web_crawler import CrawlResult, WebCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.llm.gemini_judge import GeminiJudge, LLMDiscrepancy, LLMJudgeOutput
from src.domain.entities import AuditResult, AuditStatus, DiscrepancyItem, Product
from src.use_cases.audit_product import AuditProductUseCase
from src.use_cases.resolve_reference import ResolvedReference, ResolveReferenceUseCase


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


class TestGeminiJudge:

    @pytest.mark.anyio
    async def test_judge_returns_error_when_no_api_key(
        self, sample_product: Product
    ) -> None:
        judge = GeminiJudge(api_key=None)
        judge.gemini_api_key = None
        judge.openrouter_api_key = None
        judge._client = None

        res = await judge.judge(
            product=sample_product,
            reference_url="https://example.com/item",
            external_markdown="# Item Specs",
        )
        assert res.status == AuditStatus.ERROR
        assert "is not configured" in (res.details or "")

    @pytest.mark.anyio
    async def test_judge_verified_scenario(self, sample_product: Product) -> None:
        judge = GeminiJudge(api_key="test-key")
        judge.openrouter_api_key = None
        judge._client = MagicMock()

        mock_output = LLMJudgeOutput(
            status="VERIFIED",
            confidence_score=0.98,
            matched_specs_count=3,
            total_specs_count=3,
            discrepancies=[],
            details="All 3 specs match reference",
        )
        mock_response = MagicMock()
        mock_response.text = mock_output.model_dump_json()

        mock_generate = AsyncMock(return_value=mock_response)
        judge._client.aio = MagicMock()
        judge._client.aio.models = MagicMock()
        judge._client.aio.models.generate_content = mock_generate

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
        judge = GeminiJudge(api_key="test-key")
        judge.openrouter_api_key = None
        judge._client = MagicMock()

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
        mock_response = MagicMock()
        mock_response.text = mock_output.model_dump_json()

        mock_generate = AsyncMock(return_value=mock_response)
        judge._client.aio = MagicMock()
        judge._client.aio.models = MagicMock()
        judge._client.aio.models.generate_content = mock_generate

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
        judge = GeminiJudge(api_key="test-key")
        judge.openrouter_api_key = None
        judge._client = MagicMock()

        mock_output = LLMJudgeOutput(
            status="MISSING_SPECS",
            confidence_score=0.92,
            matched_specs_count=0,
            total_specs_count=0,
            discrepancies=[],
            details="Shop catalog has 0 specs but reference has power, bluetooth, audio",
        )
        mock_response = MagicMock()
        mock_response.text = mock_output.model_dump_json()

        mock_generate = AsyncMock(return_value=mock_response)
        judge._client.aio = MagicMock()
        judge._client.aio.models = MagicMock()
        judge._client.aio.models.generate_content = mock_generate

        res = await judge.judge(
            product=empty_specs_product,
            reference_url="https://example.com/yandex",
            external_markdown="# Yandex Station\n* Power: 30W\n* Bluetooth: 5.0",
        )

        assert res.status == AuditStatus.MISSING_SPECS
        assert "Shop catalog has 0 specs" in (res.details or "")


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
