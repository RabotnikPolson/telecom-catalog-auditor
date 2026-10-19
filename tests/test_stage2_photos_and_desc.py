from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.db.schema import init_db, get_sqlite_connection
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.llm.description_evaluator import (
    clean_html_description,
    build_description_prompt_section,
    parse_description_audit,
)
from src.domain.entities import (
    AuditResult,
    AuditStatus,
    DescriptionDiscrepancyItem,
    Product,
)
from src.use_cases.audit_product import AuditProductUseCase, evaluate_photos_status


def test_clean_html_description() -> None:
    dirty = """
    <p><b>Мощный роутер</b> Wi-Fi 6 с поддержкой <i>Mesh-сетей</i>.</p><br/>
    <div>Подходит для больших квартир &amp; домов.&nbsp;Порты: 4x 1Gbps.</div>
    """
    clean = clean_html_description(dirty)
    assert "<p>" not in clean
    assert "<b>" not in clean
    assert "<br/>" not in clean
    assert "&amp;" not in clean
    assert "&nbsp;" not in clean
    assert "Мощный роутер Wi-Fi 6 с поддержкой Mesh-сетей." in clean
    assert "Подходит для больших квартир & домов. Порты: 4x 1Gbps." in clean


def test_build_description_prompt_section() -> None:
    # Empty / too short
    assert build_description_prompt_section(None) == ""
    assert build_description_prompt_section("<p>Коротко</p>") == ""

    # Normal description
    section = build_description_prompt_section("<p>Качественный коммутатор с поддержкой PoE+ 24 порта.</p>")
    assert "## SHOP PRODUCT DESCRIPTION" in section
    assert "Качественный коммутатор с поддержкой PoE+ 24 порта." in section


def test_evaluate_photos_status_b2b_and_retail() -> None:
    # 1. No images on store -> CRITICAL NO_IMAGES
    assert evaluate_photos_status(shop_count=0, reference_count=0) == "NO_IMAGES"
    assert evaluate_photos_status(shop_count=0, reference_count=5) == "NO_IMAGES"

    # 2. B2B Case: SFP module or cable with exactly 1 photo in reference and 1 in store -> OK
    assert evaluate_photos_status(shop_count=1, reference_count=1) == "OK"

    # 3. Store has less than reference -> LESS_THAN_REFERENCE
    assert evaluate_photos_status(shop_count=2, reference_count=4) == "LESS_THAN_REFERENCE"
    assert evaluate_photos_status(shop_count=1, reference_count=2) == "LESS_THAN_REFERENCE"

    # 4. Standard consumer electronics (3+ photos)
    assert evaluate_photos_status(shop_count=3, reference_count=3) == "OK"
    assert evaluate_photos_status(shop_count=5, reference_count=3) == "OK"
    assert evaluate_photos_status(shop_count=4, reference_count=0) == "OK"


def test_parse_description_audit() -> None:
    conflicts = [
        {
            "claim_in_description": "поддержка питания PoE+ на 24 портах",
            "factual_truth": "Стандартные порты без PoE, только 2 порта Uplink",
        }
    ]
    phantom = [
        {
            "claim_in_description": "встроенный модуль Wi-Fi 6",
            "factual_truth": "Устройство проводное, беспроводной модуль отсутствует",
        }
    ]

    desc_sample = "Коммутатор с поддержкой питания PoE+ на 24 портах и встроенный модуль Wi-Fi 6."
    items, quality = parse_description_audit(
        raw_conflicts=conflicts,
        raw_phantom=phantom,
        raw_quality="RICH_MARKETING",
        original_description=desc_sample,
    )

    assert len(items) == 2
    assert items[0].contradiction_type == "CONFLICT_WITH_SPECS"
    assert items[0].claim_in_description == "поддержка питания PoE+ на 24 портах"
    assert items[1].contradiction_type == "PHANTOM_FEATURE"
    assert items[1].claim_in_description == "встроенный модуль Wi-Fi 6"
    assert quality == "RICH_MARKETING"


def test_sqlite_persistence_of_photos_and_description(tmp_path: Path) -> None:
    db_file = tmp_path / "test_stage2.db"
    repo = SQLiteProductRepository(db_file)

    p = Product(
        product_id=5001,
        title="Router Pro",
        description="<p>Отличный гигабитный роутер.</p>",
        images_count=4,
    )
    repo.save_product(p)

    loaded = repo.get_product_by_id(5001)
    assert loaded is not None
    assert loaded.description == "<p>Отличный гигабитный роутер.</p>"
    assert loaded.images_count == 4

    desc_items = [
        DescriptionDiscrepancyItem(
            claim_in_description="гигабитный роутер",
            factual_truth="порты 100 Мбит/с",
            contradiction_type="CONFLICT_WITH_SPECS",
        )
    ]
    res = AuditResult(
        product_id=5001,
        status=AuditStatus.MISMATCH,
        confidence_score=0.95,
        images_count=4,
        reference_images_count=6,
        images_status="LESS_THAN_REFERENCE",
        description_discrepancies=desc_items,
        description_quality="RICH_MARKETING",
        matched_specs_count=5,
        total_specs_count=6,
    )
    repo.save_audit_result(res)

    saved_res = repo.get_latest_audit_result(5001)
    assert saved_res is not None
    assert saved_res.images_count == 4
    assert saved_res.reference_images_count == 6
    assert saved_res.images_status == "LESS_THAN_REFERENCE"
    assert len(saved_res.description_discrepancies) == 1
    assert saved_res.description_discrepancies[0].claim_in_description == "гигабитный роутер"
    assert saved_res.description_quality == "RICH_MARKETING"


def test_idempotent_sqlite_migration(tmp_path: Path) -> None:
    db_file = tmp_path / "test_idempotent.db"
    # Call init_db three times in a row — must never raise OperationalError: duplicate column name
    init_db(db_file)
    init_db(db_file)
    init_db(db_file)

    with get_sqlite_connection(db_file) as conn:
        a_cols = [r["name"] for r in conn.execute("PRAGMA table_info(audit_results);").fetchall()]
        assert "images_count" in a_cols
        assert "reference_images_count" in a_cols
        assert "images_status" in a_cols
        assert "description_discrepancies_json" in a_cols
        assert "description_quality" in a_cols


@pytest.mark.anyio
async def test_photos_only_mode_in_use_case(tmp_path: Path) -> None:
    db_file = tmp_path / "test_photos_only.db"
    repo = SQLiteProductRepository(db_file)

    # Note: Products are NOT pre-saved into repo — tests JIT persistence contract
    p1 = Product(product_id=6001, title="Camera with 0 photos", images_count=0)
    p2 = Product(product_id=6002, title="Camera with 3 photos", images_count=3)

    mock_resolver = MagicMock()
    mock_crawler = MagicMock()
    mock_judge = MagicMock()

    use_case = AuditProductUseCase(
        product_repo=repo,
        reference_resolver=mock_resolver,
        crawler=mock_crawler,
        judge=mock_judge,
    )

    res1 = await use_case.audit_product(p1, photos_only=True)
    assert res1.images_status == "NO_IMAGES"
    assert res1.status == AuditStatus.MISMATCH

    res2 = await use_case.audit_product(p2, photos_only=True)
    assert res2.images_status == "OK"
    assert res2.status == AuditStatus.VERIFIED

    # Verify both products were saved JIT into products table to satisfy FK
    assert repo.get_product_by_id(6001) is not None
    assert repo.get_product_by_id(6002) is not None

    # Zero calls to external resolver/crawler/judge were made!
    assert not mock_resolver.execute.called
    assert not mock_crawler.crawl.called
    assert not mock_judge.judge.called
