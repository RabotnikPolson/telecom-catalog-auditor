import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.search.serper_client import SerperClient
from src.adapters.search.vendor_direct import VendorDirectResolver
from src.domain.entities import Product
from src.use_cases.resolve_reference import ResolveReferenceUseCase


class TestVendorDirectResolver:

    def test_build_direct_url_al_style(self):
        resolver = VendorDirectResolver()
        url = resolver.build_direct_url("Al-Style", "63450")
        assert url == "https://www.al-style.kz/search/index.php?q=63450&s=%D0%9F%D0%BE%D0%B8%D1%81%D0%BA"

    def test_build_direct_url_alias_matching(self):
        resolver = VendorDirectResolver()
        url1 = resolver.build_direct_url("ТОО Vender1", "450760")
        assert url1 == "https://www.al-style.kz/search/index.php?q=450760&s=%D0%9F%D0%BE%D0%B8%D1%81%D0%BA"

        url2 = resolver.build_direct_url("Ал-Стайл", "998811")
        assert url2 == "https://www.al-style.kz/search/index.php?q=998811&s=%D0%9F%D0%BE%D0%B8%D1%81%D0%BA"

    def test_build_direct_url_unknown_vendor(self):
        resolver = VendorDirectResolver()
        assert resolver.build_direct_url("Unknown Vendor", "123") is None
        assert resolver.build_direct_url(None, "123") is None
        assert resolver.build_direct_url("Al-Style", "") is None

    @pytest.mark.anyio
    async def test_verify_url_availability_detection(self):
        resolver = VendorDirectResolver()

        mock_ok = httpx.Response(status_code=200, text="<html><body>Товар в наличии</body></html>")
        mock_404 = httpx.Response(status_code=404, text="Not Found")
        mock_not_found_text = httpx.Response(status_code=200, text="<html><body>К сожалению, товар не найден</body></html>")

        client = httpx.AsyncClient()
        with patch.object(client, "get", new=AsyncMock(return_value=mock_ok)):
            assert await resolver.verify_url_availability("https://al-style.kz/item", client=client) is True

        with patch.object(client, "get", new=AsyncMock(return_value=mock_404)):
            assert await resolver.verify_url_availability("https://al-style.kz/item", client=client) is False

        with patch.object(client, "get", new=AsyncMock(return_value=mock_not_found_text)):
            assert await resolver.verify_url_availability("https://al-style.kz/item", client=client) is False


class TestSerperClient:

    @pytest.mark.anyio
    async def test_search_and_filter_prioritizes_whitelist(self):
        client = SerperClient(api_key="test_dummy_key")
        mock_payload = {
            "organic": [
                {"title": "Spam OLX", "link": "https://olx.kz/item/123", "snippet": "cheap"},
                {"title": "Random Forum", "link": "https://forum.ru/topic/1", "snippet": "talk"},
                {"title": "Kaspi Store", "link": "https://kaspi.kz/shop/p/iphone-13-12345/", "snippet": "Apple iPhone"},
                {"title": "DNS Shop KZ", "link": "https://dns-shop.kz/product/555/", "snippet": "Apple"},
            ]
        }
        mock_response = httpx.Response(
            status_code=200,
            json=mock_payload,
            request=httpx.Request("POST", "https://google.serper.dev/search"),
        )

        with patch.object(client._get_client(), "post", new=AsyncMock(return_value=mock_response)):
            results = await client.search_and_filter("iphone 13")

        assert len(results) == 2
        links = [r["link"] for r in results]
        assert "https://kaspi.kz/shop/p/iphone-13-12345/" in links
        assert "https://dns-shop.kz/product/555/" in links
        assert "https://olx.kz/item/123" not in links

    @pytest.mark.anyio
    async def test_search_without_key_returns_empty_gracefully(self):
        client = SerperClient(api_key="")
        results = await client.search("anything")
        assert results == []


class TestResolveReferenceUseCase:

    @pytest.fixture
    def test_repo(self, tmp_path: Path) -> SQLiteProductRepository:
        db_file = tmp_path / "test_resolve.db"
        return SQLiteProductRepository(db_file)

    @pytest.mark.anyio
    async def test_scenario_1_direct_vendor(self, test_repo: SQLiteProductRepository):
        product = Product(
            product_id=101,
            title="Кабель Al-Style Type-C 1m",
            vendor_name="Al-Style",
            vendor_sku="AL-CABLE-01",
        )
        use_case = ResolveReferenceUseCase(
            repository=test_repo,
            vendor_resolver=VendorDirectResolver(),
            serper_client=SerperClient(api_key="mock"),
            verify_direct_urls=False,
        )

        res = await use_case.execute(product)
        assert res.status == "FOUND"
        assert res.scenario_applied == "SCENARIO_1_VENDOR_DIRECT"
        assert res.source_type == "VENDOR_DIRECT"
        assert res.reference_url == "https://www.al-style.kz/search/index.php?q=AL-CABLE-01&s=%D0%9F%D0%BE%D0%B8%D1%81%D0%BA"
        assert res.is_cached is False

        res_cached = await use_case.execute(product)
        assert res_cached.status == "FOUND"
        assert res_cached.is_cached is True
        assert res_cached.source_type == "CACHE"

    @pytest.mark.anyio
    async def test_scenario_2_vendor_fallback_on_unavailable(self, test_repo: SQLiteProductRepository):
        product = Product(
            product_id=102,
            title="Аккумулятор Camelion UB-AA2200",
            vendor_name="Al-Style",
            vendor_sku="63450",
            barcode="849198020366",
        )
        vendor_resolver = VendorDirectResolver()
        serper_client = SerperClient(api_key="mock")

        mock_search_results = [
            {"title": "Kaspi Camelion", "link": "https://kaspi.kz/shop/p/camelion-102/"}
        ]

        with patch.object(vendor_resolver, "verify_url_availability", new=AsyncMock(return_value=False)):
            with patch.object(serper_client, "search_and_filter", new=AsyncMock(return_value=mock_search_results)):
                use_case = ResolveReferenceUseCase(
                    repository=test_repo,
                    vendor_resolver=vendor_resolver,
                    serper_client=serper_client,
                    verify_direct_urls=True,
                )
                res = await use_case.execute(product)

        assert res.status == "FOUND"
        assert res.scenario_applied == "SCENARIO_2_VENDOR_FALLBACK"
        assert res.reference_url == "https://kaspi.kz/shop/p/camelion-102/"
        assert "Camelion" in res.query_used

    @pytest.mark.anyio
    async def test_scenario_3_vendor_in_list_without_sku(self, test_repo: SQLiteProductRepository):
        product = Product(
            product_id=103,
            title="Роутер TP-Link Archer C6",
            vendor_name="Al-Style",
            vendor_sku=None,
            manufacturer_sku="Archer C6",
        )
        serper_client = SerperClient(api_key="mock")
        mock_search_results = [
            {"title": "DNS TP-Link", "link": "https://dns-shop.kz/product/c6/"}
        ]

        with patch.object(serper_client, "search_and_filter", new=AsyncMock(return_value=mock_search_results)):
            use_case = ResolveReferenceUseCase(
                repository=test_repo,
                vendor_resolver=VendorDirectResolver(),
                serper_client=serper_client,
            )
            res = await use_case.execute(product)

        assert res.status == "FOUND"
        assert res.scenario_applied == "SCENARIO_3_VENDOR_NO_SKU"
        assert res.reference_url == "https://dns-shop.kz/product/c6/"

    @pytest.mark.anyio
    async def test_scenario_4_unknown_vendor_injects_sku(self, test_repo: SQLiteProductRepository):
        product = Product(
            product_id=104,
            title="Наушники Hoco M1",
            vendor_name="ТОО Ромашка",
            vendor_sku="HOC-M1-BLK",
            manufacturer_sku=None,
        )
        serper_client = SerperClient(api_key="mock")
        mock_search_results = [
            {"title": "Kaspi Hoco M1", "link": "https://kaspi.kz/shop/p/hoco-m1/"}
        ]

        with patch.object(serper_client, "search_and_filter", new=AsyncMock(return_value=mock_search_results)) as mock_call:
            use_case = ResolveReferenceUseCase(
                repository=test_repo,
                vendor_resolver=VendorDirectResolver(),
                serper_client=serper_client,
            )
            res = await use_case.execute(product)
            mock_call.assert_called_once()
            called_query = mock_call.call_args[1]["query"]
            assert "HOC-M1-BLK" in called_query

        assert res.status == "FOUND"
        assert res.scenario_applied == "SCENARIO_4_EXTERNAL_SEARCH"
        assert res.source_type == "PROVIDER_SKU_SEARCH"
        assert res.reference_url == "https://kaspi.kz/shop/p/hoco-m1/"

    @pytest.mark.anyio
    async def test_scenario_5_nameless_generic_product_skipped(self, test_repo: SQLiteProductRepository):
        product = Product(
            product_id=105,
            title="Чехол силиконовый прозрачный",
            vendor_name=None,
            vendor_sku=None,
            manufacturer_sku=None,
            barcode=None,
            current_specs={},
        )
        use_case = ResolveReferenceUseCase(
            repository=test_repo,
            vendor_resolver=VendorDirectResolver(),
            serper_client=SerperClient(api_key="mock"),
        )
        res = await use_case.execute(product)

        assert res.status == "INSUFFICIENT_DATA"
        assert res.scenario_applied == "SCENARIO_5_INSUFFICIENT_DATA"
        assert res.reference_url is None

    @pytest.mark.anyio
    async def test_samsung_galaxy_not_treated_as_nameless(self, test_repo: SQLiteProductRepository):
        product = Product(
            product_id=106,
            title="Samsung Galaxy S23 256GB Phantom Black",
            vendor_name=None,
            vendor_sku=None,
            manufacturer_sku=None,
            barcode=None,
        )
        serper_client = SerperClient(api_key="mock")
        mock_search_results = [
            {"title": "Samsung KZ", "link": "https://samsung.com/kz/smartphones/galaxy-s23/"}
        ]

        with patch.object(serper_client, "search_and_filter", new=AsyncMock(return_value=mock_search_results)):
            use_case = ResolveReferenceUseCase(
                repository=test_repo,
                vendor_resolver=VendorDirectResolver(),
                serper_client=serper_client,
            )
            res = await use_case.execute(product)

        assert res.status == "FOUND"
        assert res.scenario_applied == "SCENARIO_4_EXTERNAL_SEARCH"
        assert res.reference_url == "https://samsung.com/kz/smartphones/galaxy-s23/"
