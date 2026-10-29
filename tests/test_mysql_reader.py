import pytest

from config.settings import get_settings
from src.adapters.catalog.mysql_reader import MySQLCatalogReader


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
        assert product.brand_name is not None
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

@pytest.mark.anyio
async def test_mysql_configuration_and_connection_errors_are_distinct(monkeypatch):
    from unittest.mock import AsyncMock

    from config.settings import Settings
    from src.adapters.catalog.mysql_reader import MySQLConfigurationError, MySQLConnectionError

    missing = MySQLCatalogReader(settings=Settings(MYSQL_HOST=None, MYSQL_USER=None, MYSQL_PASSWORD=None, _env_file=None))
    pool = AsyncMock(side_effect=OSError("isolated network failure"))
    monkeypatch.setattr("src.adapters.catalog.mysql_reader.aiomysql.create_pool", pool)
    with pytest.raises(MySQLConfigurationError, match="не настроен"):
        await missing.connect()
    pool.assert_not_awaited()
    configured = MySQLCatalogReader(host="example.invalid", user="test", password="test")
    with pytest.raises(MySQLConnectionError, match="подключиться") as caught:
        await configured.connect()
    assert isinstance(caught.value.__cause__, OSError)
    await configured.close()


@pytest.mark.anyio
@pytest.mark.parametrize("connection_lost", [False, True])
async def test_mysql_stream_distinguishes_query_failure_from_connection_loss(monkeypatch, connection_lost):
    import aiomysql

    from src.adapters.catalog.mysql_reader import MySQLConnectionError, MySQLQueryError
    from src.domain.entities import Product

    reader = MySQLCatalogReader()
    error = aiomysql.OperationalError(2013 if connection_lost else 1054, "isolated failure")
    closed = []

    async def failing_stream(*args):
        try:
            yield [Product(product_id=1, title="First batch")]
            raise error
        finally:
            closed.append(True)

    monkeypatch.setattr(reader, "_stream_products", failing_stream)
    stream = reader.stream_products(batch_size=1)
    assert (await anext(stream))[0].product_id == 1
    with pytest.raises(MySQLConnectionError if connection_lost else MySQLQueryError) as caught:
        await anext(stream)
    assert caught.value.__cause__ is error
    assert closed == [True]


@pytest.mark.anyio
async def test_mysql_keyset_keeps_filters_and_releases_connection_before_yield(monkeypatch):
    from unittest.mock import AsyncMock

    from src.domain.entities import Product

    reader = MySQLCatalogReader()
    queries = []
    acquired = []
    row_sets = [[{"product_id": 5, "shop_sku": "5", "title": "First", "barcode": None, "description": None}],
                [{"product_id": 9, "shop_sku": "9", "title": "Second", "barcode": None, "description": None}], []]

    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, query, params=()):
            if "SELECT" in query:
                queries.append((query, params))

        async def fetchmany(self, size):
            assert size == 1
            return row_sets.pop(0)

    class Connection:
        def cursor(self):
            return Cursor()

    class Acquisition:
        async def __aenter__(self):
            acquired.append(True)
            return Connection()

        async def __aexit__(self, *args):
            acquired.pop()

    class Pool:
        def acquire(self):
            return Acquisition()

    monkeypatch.setattr(reader, "_ensure_connected", lambda: Pool())
    monkeypatch.setattr(reader, "_fetch_specs_for_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(reader, "_fetch_partners_for_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(reader, "_fetch_images_for_ids", AsyncMock(return_value={}))
    seen: list[Product] = []
    async for batch in reader.stream_products(batch_size=1, offset=2, category_id=78, main_category_id=11):
        assert acquired == []
        seen.extend(batch)
    assert [p.product_id for p in seen] == [5, 9]
    assert [params for _, params in queries] == [(78, 11, 1, 2), (5, 78, 11, 1), (9, 78, 11, 1)]
    assert all("p.category_id = %s" in query and "sc.categories_id = %s" in query for query, _ in queries)
    assert "OFFSET" not in queries[1][0]


@pytest.mark.anyio
async def test_brand_lookup_uses_one_parameterized_query_and_fetchmany(monkeypatch):
    reader = MySQLCatalogReader()
    select_calls = []
    row_sets = [
        [
            {"product_id": 1, "brand_name": " Apple "},
            {"product_id": 2, "brand_name": None},
        ],
        [],
    ]

    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, query, params=()):
            if "SELECT p.id AS product_id" in query:
                select_calls.append((query, params))

        async def fetchmany(self, size):
            assert size == 2
            return row_sets.pop(0)

    class Connection:
        def cursor(self):
            return Cursor()

    class Acquisition:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return False

    class Pool:
        def acquire(self):
            return Acquisition()

    monkeypatch.setattr(reader, "_ensure_connected", lambda: Pool())

    result = await reader.fetch_brands_by_product_ids([1, 2, 1], batch_size=2)

    assert result == {1: "Apple", 2: None}
    assert len(select_calls) == 1
    assert select_calls[0][1] == (1, 2)
