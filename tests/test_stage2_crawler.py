import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from src.adapters.catalog.telecom_crawler import (
    CatalogItemPreview,
    CategoryPageResult,
    ProductDetailResult,
    TelecomShopCrawler,
)
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.domain.entities import AuditStatus, Product
from src.use_cases.crawl_shop_catalog import CrawlShopCatalogUseCase


SAMPLE_CATEGORIES_JSON = [
    {
        "id": 1,
        "name": "Смартфоны и аксессуары",
        "urlkey": "smartfony-i-aksessuary",
        "enabled": 1,
        "children": [
            {
                "id": 27,
                "name": "Телефоны",
                "urlkey": "telefony",
                "enabled": 1,
                "get_groups": [
                    {"id": 143, "name": "Мобильные телефоны", "urlkey": "mobilnye-telefony", "enabled": 1},
                    {"id": 144, "name": "Смартфоны", "urlkey": "smartfony", "enabled": 1},
                    {"id": 145, "name": "Неактивная подгруппа", "urlkey": "disabled-group", "enabled": 0},
                ],
            },
            {
                "id": 28,
                "name": "Стационарные телефоны",
                "urlkey": "stacionarnye-telefony",
                "enabled": 1,
                "get_groups": [],
            },
        ],
    },
    {
        "id": 2,
        "name": "Отключенная категория",
        "urlkey": "disabled-cat",
        "enabled": 0,
        "children": [],
    },
]

SAMPLE_CATALOGUE_HTML = """
<html>
<body>
    <div class="card">
        <a href="https://shop.telecom.kz/product/1001?product_unit=apple-iphone-13-128gb-black" class="slider-link">
            <h3 class="card-order__title">
                <a href="https://shop.telecom.kz/product/1001?product_unit=apple-iphone-13-128gb-black">Смартфон Apple iPhone 13 128GB Black</a>
            </h3>
        </a>
        <button onclick="addToBasket(this, 'Смартфон Apple iPhone 13 128GB Black', 450123, 350000, null, null, null, 1)" data-id="1001"></button>
    </div>
    <div class="card">
        <a href="/product/1002?product_unit=samsung-galaxy-s23-256gb" class="slider-link">
            <h3 class="card-order__title">
                <a href="/product/1002?product_unit=samsung-galaxy-s23-256gb">Смартфон Samsung Galaxy S23 256GB</a>
            </h3>
        </a>
    </div>
    <ul class="pagination">
        <li class="page-item active"><span class="page-link">1</span></li>
        <li class="page-item"><a class="page-link" href="/catalogue/smartfony?page=2">2</a></li>
        <li class="page-item"><a class="page-link" href="/catalogue/smartfony?page=3">3</a></li>
    </ul>
</body>
</html>
"""

SAMPLE_PRODUCT_HTML = """
<html>
<head><title>Смартфон Apple iPhone 13 128GB Midnight | shop.telecom.kz</title></head>
<body>
    <h1 class="product-info__title">Смартфон Apple iPhone 13 128GB Midnight</h1>
    <p class="product-info__links vendor-code">Артикул: 450123</p>
    <dl class="specifications-block__list">
        <dt class="specifications-block__spec-term">
            <span class="specifications-block__spec-term-text">Производитель</span>
        </dt>
        <dd class="specifications-block__spec-definition">Apple</dd>

        <dt class="specifications-block__spec-term">
            <span class="specifications-block__spec-term-text">Модель</span>
        </dt>
        <dd class="specifications-block__spec-definition">MLPF3RM/A</dd>

        <dt class="specifications-block__spec-term">
            <span class="specifications-block__spec-term-text">Объем встроенной памяти</span>
        </dt>
        <dd class="specifications-block__spec-definition">128 ГБ</dd>

        <dt class="specifications-block__spec-term">
            <span class="specifications-block__spec-term-text">Цвет</span>
        </dt>
        <dd class="specifications-block__spec-definition">Midnight</dd>
    </dl>
</body>
</html>
"""


class TestTelecomCrawlerAdapter:

    @pytest.mark.anyio
    async def test_get_leaf_categories_extraction(self):
        crawler = TelecomShopCrawler()
        mock_response = httpx.Response(
            status_code=200,
            json=SAMPLE_CATEGORIES_JSON,
            request=httpx.Request("GET", "https://shop.telecom.kz/getAllCategories"),
        )

        with patch.object(crawler._get_client(), "get", new=AsyncMock(return_value=mock_response)):
            leaves = await crawler.get_leaf_categories()

        assert len(leaves) == 3

        leaf_keys = [leaf.category_key for leaf in leaves]
        assert "smartfony-i-aksessuary/telefony/mobilnye-telefony" in leaf_keys
        assert "smartfony-i-aksessuary/telefony/smartfony" in leaf_keys
        assert "smartfony-i-aksessuary/stacionarnye-telefony" in leaf_keys
        assert "disabled-group" not in " ".join(leaf_keys)
        assert "disabled-cat" not in " ".join(leaf_keys)

    @pytest.mark.anyio
    async def test_crawl_category_page_parsing(self):
        crawler = TelecomShopCrawler(delay_min=0.0, delay_max=0.0)
        mock_response = httpx.Response(
            status_code=200,
            text=SAMPLE_CATALOGUE_HTML,
            request=httpx.Request("GET", "https://shop.telecom.kz/catalogue/test?page=1"),
        )

        with patch.object(crawler._get_client(), "get", new=AsyncMock(return_value=mock_response)):
            result = await crawler.crawl_category_page("smartfony", page=1)

        assert result.category_path == "smartfony"
        assert result.page == 1
        assert result.total_pages == 3
        assert result.has_next is True
        assert len(result.items) == 2

        item1 = result.items[0]
        assert item1.product_id == 1001
        assert item1.slug == "apple-iphone-13-128gb-black"
        assert item1.title == "Смартфон Apple iPhone 13 128GB Black"
        assert item1.vendor_sku == "450123"

        item2 = result.items[1]
        assert item2.product_id == 1002
        assert item2.slug == "samsung-galaxy-s23-256gb"

    @pytest.mark.anyio
    async def test_fetch_product_details_parsing(self):
        crawler = TelecomShopCrawler(delay_min=0.0, delay_max=0.0)
        mock_response = httpx.Response(
            status_code=200,
            text=SAMPLE_PRODUCT_HTML,
            request=httpx.Request("GET", "https://shop.telecom.kz/product/1001"),
        )

        with patch.object(crawler._get_client(), "get", new=AsyncMock(return_value=mock_response)):
            details = await crawler.fetch_product_details(1001)

        assert details.product_id == 1001
        assert details.title == "Смартфон Apple iPhone 13 128GB Midnight"
        assert details.vendor_sku == "450123"
        assert details.manufacturer_sku == "MLPF3RM/A"
        assert details.vendor_name == "Apple"
        assert details.current_specs["Объем встроенной памяти"] == "128 ГБ"
        assert details.current_specs["Цвет"] == "Midnight"


class TestCrawlerStateRepository:

    @pytest.fixture
    def test_repo(self, tmp_path: Path) -> SQLiteProductRepository:
        db_file = tmp_path / "test_crawler_state.db"
        return SQLiteProductRepository(db_file)

    def test_crawler_state_lifecycle(self, test_repo: SQLiteProductRepository):
        cat_key = "smartfony-i-aksessuary/telefony/smartfony"

        initial_state = test_repo.get_crawler_state(cat_key)
        assert initial_state is None

        test_repo.update_crawler_state(cat_key, last_page=3, total_pages=10, is_completed=False)

        state = test_repo.get_crawler_state(cat_key)
        assert state is not None
        assert state["category_key"] == cat_key
        assert state["last_page"] == 3
        assert state["total_pages"] == 10
        assert state["is_completed"] is False

        test_repo.update_crawler_state(cat_key, last_page=10, total_pages=10, is_completed=True)
        updated_state = test_repo.get_crawler_state(cat_key)
        assert updated_state["last_page"] == 10
        assert updated_state["is_completed"] is True

        all_states = test_repo.get_all_crawler_states()
        assert len(all_states) == 1

        test_repo.reset_crawler_state(cat_key)
        assert test_repo.get_crawler_state(cat_key) is None


class TestCrawlShopCatalogUseCase:

    @pytest.fixture
    def test_repo(self, tmp_path: Path) -> SQLiteProductRepository:
        db_file = tmp_path / "test_orchestrator.db"
        return SQLiteProductRepository(db_file)

    @pytest.mark.anyio
    async def test_crawl_saves_new_products_as_pending(self, test_repo: SQLiteProductRepository):
        mock_crawler = AsyncMock(spec=TelecomShopCrawler)
        mock_crawler.crawl_category_page.return_value = CategoryPageResult(
            category_path="smartfony",
            page=1,
            total_pages=1,
            has_next=False,
            items=[
                CatalogItemPreview(
                    product_id=2001,
                    slug="iphone-13",
                    title="Apple iPhone 13 128GB",
                    detail_url="https://shop.telecom.kz/product/2001",
                )
            ],
        )
        mock_crawler.fetch_product_details.return_value = ProductDetailResult(
            product_id=2001,
            title="Apple iPhone 13 128GB Midnight",
            vendor_name="Apple",
            vendor_sku="SKU-2001",
            manufacturer_sku="MLPF3RM/A",
            current_specs={"память": "128 гб", "цвет": "midnight"},
            detail_url="https://shop.telecom.kz/product/2001",
        )

        use_case = CrawlShopCatalogUseCase(crawler=mock_crawler, repository=test_repo)
        stats = await use_case.execute(category_path="smartfony")

        assert stats.categories_processed == 1
        assert stats.pages_crawled == 1
        assert stats.products_seen == 1
        assert stats.products_added == 1
        assert stats.products_skipped == 0

        saved = test_repo.get_product_by_id(2001)
        assert saved is not None
        assert saved.status == AuditStatus.PENDING
        assert saved.title == "Apple iPhone 13 128GB Midnight"
        assert saved.vendor_sku == "SKU-2001"
        assert saved.content_hash == Product.compute_content_hash(
            saved.title, {"память": "128 гб", "цвет": "midnight"}
        )

        state = test_repo.get_crawler_state("smartfony")
        assert state is not None
        assert state["is_completed"] is True

    @pytest.mark.anyio
    async def test_skip_verified_product_with_identical_hash(self, test_repo: SQLiteProductRepository):
        specs = {"память": "128 гб"}
        title = "Apple iPhone 13 128GB"
        existing_verified = Product(
            product_id=3001,
            title=title,
            current_specs=specs,
            status=AuditStatus.VERIFIED,
        )
        test_repo.save_product(existing_verified)

        mock_crawler = AsyncMock(spec=TelecomShopCrawler)
        mock_crawler.crawl_category_page.return_value = CategoryPageResult(
            category_path="smartfony",
            page=1,
            total_pages=1,
            has_next=False,
            items=[
                CatalogItemPreview(
                    product_id=3001,
                    slug="iphone-13",
                    title=title,
                    detail_url="https://shop.telecom.kz/product/3001",
                )
            ],
        )
        mock_crawler.fetch_product_details.return_value = ProductDetailResult(
            product_id=3001,
            title=title,
            vendor_name=None,
            vendor_sku=None,
            manufacturer_sku=None,
            current_specs=specs,
            detail_url="https://shop.telecom.kz/product/3001",
        )

        use_case = CrawlShopCatalogUseCase(crawler=mock_crawler, repository=test_repo)
        stats = await use_case.execute(category_path="smartfony")

        assert stats.products_seen == 1
        assert stats.products_skipped == 1
        assert stats.products_added == 0
        assert stats.products_updated == 0

        current_in_db = test_repo.get_product_by_id(3001)
        assert current_in_db.status == AuditStatus.VERIFIED

    @pytest.mark.anyio
    async def test_update_product_to_pending_when_specs_change(self, test_repo: SQLiteProductRepository):
        existing = Product(
            product_id=4001,
            title="Xiaomi 12 128GB",
            current_specs={"память": "128 гб"},
            status=AuditStatus.VERIFIED,
        )
        test_repo.save_product(existing)

        mock_crawler = AsyncMock(spec=TelecomShopCrawler)
        mock_crawler.crawl_category_page.return_value = CategoryPageResult(
            category_path="smartfony",
            page=1,
            total_pages=1,
            has_next=False,
            items=[
                CatalogItemPreview(
                    product_id=4001,
                    slug="xiaomi-12",
                    title="Xiaomi 12 128GB",
                    detail_url="https://shop.telecom.kz/product/4001",
                )
            ],
        )
        mock_crawler.fetch_product_details.return_value = ProductDetailResult(
            product_id=4001,
            title="Xiaomi 12 128GB",
            vendor_name="Xiaomi",
            vendor_sku="MI-12",
            manufacturer_sku=None,
            current_specs={"память": "128 гб", "аккумулятор": "4500 мач"},
            detail_url="https://shop.telecom.kz/product/4001",
        )

        use_case = CrawlShopCatalogUseCase(crawler=mock_crawler, repository=test_repo)
        stats = await use_case.execute(category_path="smartfony")

        assert stats.products_seen == 1
        assert stats.products_updated == 1
        assert stats.products_added == 0

        updated_in_db = test_repo.get_product_by_id(4001)
        assert updated_in_db.status == AuditStatus.PENDING
        assert "аккумулятор" in updated_in_db.current_specs
