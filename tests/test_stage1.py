import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.domain.entities import AuditResult, AuditStatus, Product
from src.domain.services import ParentChildGrouper
from src.use_cases.sync_audited_brands import SyncAuditedBrandsUseCase


@pytest.mark.anyio
async def test_sync_audited_brands_is_dry_run_then_idempotent_apply(tmp_path: Path) -> None:
    repo = SQLiteProductRepository(tmp_path / "brand-sync.db")
    repo.save_product_and_audit_result(
        Product(product_id=1, title="iPhone", brand_name=None),
        AuditResult(product_id=1, status=AuditStatus.PENDING),
    )
    repo.save_product_and_audit_result(
        Product(product_id=2, title="Galaxy", brand_name="Samsung"),
        AuditResult(product_id=2, status=AuditStatus.PENDING),
    )
    calls: list[tuple[int, ...]] = []

    async def fetch(product_ids: Sequence[int]) -> dict[int, str | None]:
        calls.append(tuple(product_ids))
        return {1: " Apple ", 2: "Samsung"}

    use_case = SyncAuditedBrandsUseCase(
        get_current=repo.get_audited_product_brands,
        fetch_source=fetch,
        apply_updates=repo.update_audited_brands,
    )

    dry_run = await use_case.execute()
    applied = await use_case.execute(apply=True)
    repeated = await use_case.execute(apply=True)

    assert dry_run.changes == 1 and dry_run.updated == 0
    assert applied.changes == 1 and applied.updated == 1
    assert repeated.changes == 0 and repeated.updated == 0
    assert calls == [(1, 2), (1, 2), (1, 2)]
    synced = repo.get_product_by_id(1)
    assert synced is not None and synced.brand_name == "Apple"


class TestContentHash:

    def test_content_hash_deterministic_order_independence(self):
        specs_1 = {"диагональ": "6.1", "память": "128 гб", "цвет": "midnight"}
        specs_2 = {"цвет": "midnight", "диагональ": "6.1", "память": "128 гб"}
        title = "Смартфон Apple iPhone 13 128GB Midnight"

        hash_1 = Product.compute_content_hash(title, specs_1)
        hash_2 = Product.compute_content_hash(title, specs_2)

        assert hash_1 == hash_2
        assert len(hash_1) == 64

    def test_content_hash_case_and_whitespace_insensitivity(self):
        title_a = "  Apple iPhone 13 128GB  "
        title_b = "apple iphone 13 128gb"
        specs_a = {" Память ": " 128 GB "}
        specs_b = {"память": "128 gb"}

        hash_a = Product.compute_content_hash(title_a, specs_a)
        hash_b = Product.compute_content_hash(title_b, specs_b)

        assert hash_a == hash_b

    def test_content_hash_changes_on_spec_mutation(self):
        title = "Apple iPhone 13 128GB"
        specs_original = {"память": "128 гб", "диагональ": "6.1"}
        specs_modified = {"память": "256 гб", "диагональ": "6.1"}

        hash_orig = Product.compute_content_hash(title, specs_original)
        hash_mod = Product.compute_content_hash(title, specs_modified)

        assert hash_orig != hash_mod

    def test_product_auto_populates_hash_on_creation(self):
        product = Product(
            product_id=101,
            title="Apple iPhone 13 128GB Midnight",
            current_specs={"диагональ": "6.1", "память": "128 гб"}
        )

        assert product.content_hash != ""
        expected = Product.compute_content_hash(product.title, product.current_specs)
        assert product.content_hash == expected
        assert product.is_content_matching(expected) is True


class TestParentChildGrouper:

    @pytest.fixture
    def grouper(self) -> ParentChildGrouper:
        return ParentChildGrouper()

    def test_iphone_color_normalization_and_memory_isolation(self, grouper: ParentChildGrouper):
        key_midnight = grouper.extract_master_key("Смартфон Apple iPhone 13 128GB Midnight")
        key_blue = grouper.extract_master_key("Apple iPhone 13 128GB Blue (Синий)")
        key_starlight = grouper.extract_master_key("Смартфон Apple iPhone 13 128 ГБ Сияющая звезда (slim box)")

        key_256gb = grouper.extract_master_key("Смартфон Apple iPhone 13 256GB Black")

        assert key_midnight == key_blue
        assert key_blue == key_starlight
        assert "128gb" in key_midnight

        assert key_midnight != key_256gb
        assert "256gb" in key_256gb

    def test_group_products_iphone_scenario(self, grouper: ParentChildGrouper):
        p1 = Product(
            product_id=1,
            title="Apple iPhone 13 128GB Midnight",
            vendor_name="Al-Style",
            vendor_sku="AL-IP13-128-MID",
            manufacturer_sku="MLPF3RM/A",
            current_specs={"память": "128 гб", "цвет": "midnight"}
        )
        p2 = Product(
            product_id=2,
            title="Apple iPhone 13 128GB Blue",
            vendor_name="Al-Style",
            vendor_sku="AL-IP13-128-BLU",
            manufacturer_sku=None,
            current_specs={"память": "128 гб", "цвет": "blue"}
        )
        p3 = Product(
            product_id=3,
            title="Apple iPhone 13 128GB Starlight",
            vendor_name=None,
            vendor_sku=None,
            manufacturer_sku=None,
            current_specs={"память": "128 гб", "цвет": "starlight"}
        )
        p4 = Product(
            product_id=4,
            title="Apple iPhone 13 256GB Black",
            vendor_name="Al-Style",
            vendor_sku="AL-IP13-256-BLK",
            manufacturer_sku="MLQ63RM/A",
            current_specs={"память": "256 гб", "цвет": "black"}
        )

        summary = grouper.group_products([p1, p2, p3, p4])

        assert summary.total_products == 4
        assert summary.master_count == 2
        assert summary.child_count == 2

        assert p1.is_master is True
        assert p1.parent_sku is None
        assert p1.master_key is not None
        assert p1.master_key.startswith("apple_iphone_13_128gb__specs_")

        assert p2.is_master is False
        assert p2.parent_sku == "MLPF3RM/A"
        assert p2.master_key == p1.master_key

        assert p3.is_master is False
        assert p3.parent_sku == "MLPF3RM/A"
        assert p3.master_key == p1.master_key

        assert p4.is_master is True
        assert p4.parent_sku is None
        assert p4.master_key is not None
        assert p4.master_key.startswith("apple_iphone_13_256gb__specs_")

    def test_standalone_region_is_neutral_but_factory_suffix_is_preserved(
        self, grouper: ParentChildGrouper
    ) -> None:
        specs = {"Память": "128 ГБ", "Связь": "5G"}
        regional_keys = {
            grouper.extract_master_key(title, specs)
            for title in ("Model X KZ", "Model X EU", "Model X DE", "Model X Global")
        }
        assert regional_keys == {"model_x_128gb"}
        assert grouper.extract_master_key("Model X", specs) == "model_x_128gb"
        assert grouper.extract_master_key("Model X ABC-123", specs) == "model_x_abc-123_128gb"
        assert grouper.extract_master_key("Model X HX9", specs) == "model_x_hx9_128gb"
        assert grouper.extract_master_key("Model X abc", specs) == "model_x_abc_128gb"

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("Model X 1 SIM", "Model X Dual SIM"),
            ("Model X eSIM", "Model X Dual SIM"),
            ("Router X 4G", "Router X 5G"),
            ("Router X V1", "Router X V2"),
        ],
    )
    def test_technical_variant_markers_remain_distinct(
        self, grouper: ParentChildGrouper, first: str, second: str
    ) -> None:
        assert grouper.extract_master_key(first) != grouper.extract_master_key(second)

    def test_spec_signature_is_stable_and_conservative(
        self, grouper: ParentChildGrouper
    ) -> None:
        first = {"Стандарт связи": "5G", "Память": "128 ГБ", "Цвет": "Black"}
        reordered = {"Цвет": "White", "Память": "128 ГБ", "Стандарт связи": "5G"}
        different = {"Память": "128 ГБ", "Стандарт связи": "4G", "Цвет": "Black"}

        products = [
            Product(product_id=201, title="Model X KZ", current_specs=first),
            Product(product_id=202, title="Model X EU", current_specs=reordered),
            Product(product_id=203, title="Model X Global", current_specs=different),
            Product(product_id=204, title="Model X", current_specs={}),
        ]

        summary = grouper.group_products(products)

        assert products[0].master_key == products[1].master_key
        assert products[0].master_key != products[2].master_key
        assert products[0].master_key != products[3].master_key
        assert summary.master_count == 3
        assert summary.child_count == 1
        assert all(
            product.master_key.startswith("model_x_128gb__specs_")
            for product in products[:3]
        )
        assert products[3].master_key == "model_x__specs_unknown"

    def test_master_key_is_stable_across_separate_batches(
        self, grouper: ParentChildGrouper
    ) -> None:
        first = Product(
            product_id=301,
            title="Model X KZ",
            current_specs={"Стандарт связи": "4G"},
        )
        second = Product(
            product_id=302,
            title="Model X EU",
            current_specs={"Стандарт связи": "5G"},
        )

        first_summary = grouper.group_products([first])
        seen_keys = set(first_summary.groups)
        second_summary = grouper.group_products([second])

        assert first.master_key == next(iter(first_summary.groups))
        assert second.master_key == next(iter(second_summary.groups))
        assert first.master_key != second.master_key
        assert second.master_key not in seen_keys


class TestSQLiteRepository:

    @pytest.fixture
    def test_repo(self, tmp_path: Path) -> SQLiteProductRepository:
        db_file = tmp_path / "test_catalog_audit.db"
        return SQLiteProductRepository(db_file)

    def test_wal_mode_is_active(self, test_repo: SQLiteProductRepository):
        assert test_repo.check_wal_mode() is True

    def test_save_and_retrieve_product(self, test_repo: SQLiteProductRepository):
        product = Product(
            product_id=5001,
            title="Samsung Galaxy S23 8/256GB Phantom Black",
            brand_name="Samsung",
            vendor_name="Al-Style",
            vendor_sku="AL-SAM-S23-256",
            manufacturer_sku="SM-S911BZKDCAZ",
            current_specs={"экран": "6.1", "память": "256 гб", "ram": "8 гб"},
            status=AuditStatus.PENDING
        )

        saved = test_repo.save_product(product)
        assert saved.product_id == 5001

        retrieved = test_repo.get_product_by_id(5001)
        assert retrieved is not None
        assert retrieved.title == "Samsung Galaxy S23 8/256GB Phantom Black"
        assert retrieved.brand_name == "Samsung"
        assert retrieved.vendor_sku == "AL-SAM-S23-256"
        assert retrieved.manufacturer_sku == "SM-S911BZKDCAZ"
        assert retrieved.current_specs["память"] == "256 гб"
        assert retrieved.content_hash == product.content_hash

    def test_get_by_content_hash_for_skip(self, test_repo: SQLiteProductRepository):
        product = Product(
            product_id=5002,
            title="Xiaomi Redmi Note 12 128GB Blue",
            current_specs={"память": "128 гб"}
        )
        test_repo.save_product(product)

        found = test_repo.get_by_content_hash(product.content_hash)
        assert found is not None
        assert found.product_id == 5002

        fake_hash = "0" * 64
        assert test_repo.get_by_content_hash(fake_hash) is None

    def test_batch_upsert(self, test_repo: SQLiteProductRepository):
        products = [
            Product(
                product_id=6000 + i,
                title=f"Test Device {i} 128GB",
                current_specs={"index": str(i)}
            )
            for i in range(10)
        ]

        count = test_repo.batch_upsert(products)
        assert count == 10
        assert test_repo.count_products() == 10

        fetched = test_repo.get_all_products(limit=5)
        assert len(fetched) == 5

    def test_two_layer_cache(self, test_repo: SQLiteProductRepository):
        query_hash = hashlib.sha256(b"iphone 13 128gb").hexdigest()
        url = "https://shop.telecom.kz/product/iphone-13-128gb"
        test_repo.save_url_cache(query_hash, url, source_type="vendor_direct")

        cached_url = test_repo.get_url_cache(query_hash)
        assert cached_url == url
        assert test_repo.get_url_cache("unknown_hash") is None

        sample_specs = '{"screen": "6.1", "cpu": "A15 Bionic", "ram": "4GB"}'
        test_repo.save_specs_cache(url, sample_specs)

        cached_specs = test_repo.get_specs_cache(url)
        assert cached_specs == sample_specs
        assert test_repo.get_specs_cache("https://example.com/not-cached") is None
