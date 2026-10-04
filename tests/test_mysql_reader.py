import pytest
from src.adapters.catalog.mysql_reader import MySQLCatalogReader
from config.settings import get_settings


@pytest.mark.anyio
async def test_mysql_reader_live_connection():
    settings = get_settings()
    if not settings.MYSQL_HOST:
        pytest.skip("MySQL credentials not configured in settings")

    async with MySQLCatalogReader() as reader:
        count = await reader.count_products()
        assert count > 0, "Products count in MySQL should be greater than zero"


@pytest.mark.anyio
async def test_mysql_reader_fetch_by_sku_and_specs():
    settings = get_settings()
    if not settings.MYSQL_HOST:
        pytest.skip("MySQL credentials not configured in settings")

    async with MySQLCatalogReader() as reader:
        # SKU 468555 is Deco BE85 Mesh-system
        product = await reader.fetch_product_by_sku("468555")
        assert product is not None, "Product with SKU 468555 should exist"
        assert product.product_id == 90363
        assert "TP-Link" in product.title or "Tp-Link" in product.title
        assert product.barcode == "4897098686928"
        assert len(product.current_specs) > 0, "Product should have specs loaded from MySQL"
        assert product.content_hash != "", "Content hash should be auto-computed"


@pytest.mark.anyio
async def test_mysql_reader_streaming():
    settings = get_settings()
    if not settings.MYSQL_HOST:
        pytest.skip("MySQL credentials not configured in settings")

    async with MySQLCatalogReader() as reader:
        batches: list[list] = []
        async for batch in reader.stream_products(batch_size=5, limit=10):
            batches.append(batch)
            assert len(batch) <= 5
            for p in batch:
                assert p.product_id > 0
                assert len(p.title) > 0

        assert len(batches) >= 1
        total_streamed = sum(len(b) for b in batches)
        assert total_streamed == 10
