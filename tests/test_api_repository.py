from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.adapters.db.sqlite_repo import (
    AuditListFilters,
    SQLiteAuditReadRepository,
    SQLiteProductRepository,
)
from src.domain.entities import AuditResult, AuditStatus, DiscrepancyItem, Product


def _seed(db_path: Path) -> SQLiteAuditReadRepository:
    writer = SQLiteProductRepository(db_path)
    now = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
    for index, status in enumerate((AuditStatus.VERIFIED, AuditStatus.MISMATCH, AuditStatus.ERROR), start=1):
        product = Product(
            product_id=index,
            shop_sku=f"SKU-{index}",
            title=f"Product {index}",
            brand_name="Apple" if index < 3 else "Samsung",
            vendor_name="Поставщик А" if index < 3 else "Поставщик Б",
            current_specs={"Spec": str(index)},
        )
        discrepancies = []
        if status == AuditStatus.MISMATCH:
            discrepancies = [
                DiscrepancyItem(
                    spec_name="Spec",
                    shop_value="old",
                    reference_value="new",
                    proof_quote="Spec: new",
                    source_url="https://example.com/product",
                )
            ]
        result = AuditResult(
            product_id=index,
            status=status,
            confidence_score=index / 10,
            discrepancies=discrepancies,
            audited_at=now + timedelta(minutes=index),
            audit_parts={"specs": f"sha256:{index}"},
        )
        writer.save_product_and_audit_result(product, result)
    return SQLiteAuditReadRepository(db_path)


def test_repository_paginates_and_sorts_with_stable_tie_breaker(tmp_path: Path) -> None:
    repository = _seed(tmp_path / "repository.db")

    page = repository.get_paginated_results(
        AuditListFilters(page=1, per_page=25, sort="confidence_score", direction="desc")
    )

    assert page.total_items == 3
    assert [item.product_id for item in page.items] == [3, 2, 1]


def test_repository_filters_status_attention_search_vendor_and_dates(tmp_path: Path) -> None:
    repository = _seed(tmp_path / "filters.db")

    result = repository.get_paginated_results(
        AuditListFilters(
            statuses=(AuditStatus.MISMATCH,),
            attention_required=True,
            query="SKU-2",
            brand="Apple",
            vendor="поставщик а",
            audited_from=datetime(2026, 10, 27, 8, 1, tzinfo=UTC),
            audited_to=datetime(2026, 10, 27, 8, 3, tzinfo=UTC),
        )
    )

    assert result.total_items == 1
    assert result.items[0].product_id == 2
    assert SQLiteAuditReadRepository.attention_required(result.items[0]) is True


def test_repository_numeric_search_matches_product_id(tmp_path: Path) -> None:
    repository = _seed(tmp_path / "numeric.db")

    result = repository.get_paginated_results(AuditListFilters(query="3"))

    assert result.total_items == 1
    assert result.items[0].product_id == 3


def test_repository_detail_and_missing_product(tmp_path: Path) -> None:
    repository = _seed(tmp_path / "detail.db")

    assert repository.get_audit_card_details(2) is not None
    assert repository.get_audit_card_details(999) is None


def test_repository_lists_unique_trimmed_audited_vendors(tmp_path: Path) -> None:
    db_path = tmp_path / "vendors.db"
    repository = _seed(db_path)
    writer = SQLiteProductRepository(db_path)
    writer.save_product_and_audit_result(
        Product(product_id=4, title="Product 4", vendor_name="  поставщик а  "),
        AuditResult(product_id=4, status=AuditStatus.VERIFIED),
    )
    writer.save_product_and_audit_result(
        Product(product_id=5, title="Product 5", vendor_name="   "),
        AuditResult(product_id=5, status=AuditStatus.VERIFIED),
    )

    assert repository.list_audited_vendors() == ("Поставщик А", "Поставщик Б")


def test_repository_lists_and_filters_structured_brands(tmp_path: Path) -> None:
    repository = _seed(tmp_path / "brands.db")

    assert repository.list_audited_brands() == ("Apple", "Samsung")
    page = repository.get_paginated_results(AuditListFilters(brand="Apple"))
    assert page.total_items == 2
    assert {item.product_id for item in page.items} == {1, 2}
    assert {item.brand_name for item in page.items} == {"Apple"}


def test_brand_metadata_update_only_touches_audited_products(tmp_path: Path) -> None:
    db_path = tmp_path / "brand-update.db"
    _seed(db_path)
    writer = SQLiteProductRepository(db_path)
    writer.save_product(Product(product_id=99, title="Not audited", brand_name="Old"))

    changed = writer.update_audited_brands({1: "Apple Inc.", 99: "Should not change"})

    audited = writer.get_product_by_id(1)
    unaudited = writer.get_product_by_id(99)
    assert changed == 1
    assert audited is not None and audited.brand_name == "Apple Inc."
    assert unaudited is not None and unaudited.brand_name == "Old"
