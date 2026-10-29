from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.db.schema import get_sqlite_connection, init_db
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.llm.description_evaluator import (
    build_description_prompt_section,
    clean_html_description,
    parse_description_audit,
)
from src.domain.entities import (
    AuditResult,
    AuditStatus,
    DescriptionDiscrepancyItem,
    Product,
)
from src.use_cases.audit_product import AuditProductUseCase, evaluate_photos_status
from src.use_cases.resolve_reference import ResolvedReference


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
    p1 = Product(product_id=6001, title="Camera Model A", images_count=0, images_count_known=True)
    p2 = Product(product_id=6002, title="Camera Model B", images_count=3, images_count_known=True)

    mock_resolver = MagicMock()
    mock_crawler = MagicMock()
    mock_judge = MagicMock()
    mock_judge.matches_model = AsyncMock(return_value=True)
    mock_crawler.crawl = AsyncMock(return_value=MagicMock(
        success=True, markdown="Camera model reference", images_count=4,
        title="Camera", status_code=200,
    ))

    use_case = AuditProductUseCase(
        product_repo=repo,
        reference_resolver=mock_resolver,
        crawler=mock_crawler,
        judge=mock_judge,
    )

    res1 = await use_case.audit_product(p1, photos_only=True, reference_url="https://example.com/a")
    assert res1.images_status == "NO_IMAGES"
    assert res1.status == AuditStatus.PENDING
    assert "photos" in res1.audit_parts

    res2 = await use_case.audit_product(p2, photos_only=True, reference_url="https://example.com/b")
    assert res2.images_status == "LESS_THAN_REFERENCE"
    assert res2.status == AuditStatus.PENDING
    assert "specs" not in res2.audit_parts

    # Verify both products were saved JIT into products table to satisfy FK
    assert repo.get_product_by_id(6001) is not None
    assert repo.get_product_by_id(6002) is not None

    # Manual URLs bypass search but still require a crawl and model match.
    assert not mock_resolver.execute.called
    assert mock_crawler.crawl.await_count == 2
    assert mock_judge.matches_model.await_count == 2
    assert not mock_judge.judge.called


@pytest.mark.anyio
async def test_photos_only_failure_without_full_audit_stays_pending(tmp_path: Path) -> None:
    repo = SQLiteProductRepository(tmp_path / "photo-failure.db")
    product = Product(
        product_id=6003,
        title="Camera without known image count",
        images_count=0,
        images_count_known=False,
    )

    result = await AuditProductUseCase(repo, MagicMock(), MagicMock(), MagicMock()).audit_product(
        product,
        photos_only=True,
    )

    assert result.status == AuditStatus.PENDING
    assert result.confidence_score == 0
    assert result.photos_error == "Нет достоверного количества фото магазина."
    assert "specs" not in result.audit_parts
    assert "description" not in result.audit_parts


@pytest.mark.anyio
async def test_full_failure_replaces_invalid_legacy_verified_zero(tmp_path: Path) -> None:
    repo = SQLiteProductRepository(tmp_path / "legacy-verified.db")
    product = Product(product_id=6004, title="Xiaomi 12 Pro")
    repo.save_product_and_audit_result(
        product,
        AuditResult(
            product_id=product.product_id,
            status=AuditStatus.VERIFIED,
            confidence_score=0,
            details="Invalid legacy photos-only result",
            audit_parts={},
        ),
    )
    resolver = MagicMock()
    resolver.execute = AsyncMock(
        return_value=ResolvedReference(
            product_id=product.product_id,
            status="NOT_FOUND",
            scenario_applied="NONE",
            candidate_urls=[],
            reference_url=None,
            search_trace=[],
            source_type=None,
            query_used=None,
        )
    )

    result = await AuditProductUseCase(repo, resolver, MagicMock(), MagicMock()).audit_product(
        product,
        force_refresh=True,
    )

    persisted = repo.get_latest_audit_result(product.product_id)
    assert result.status == AuditStatus.NOT_FOUND
    assert persisted is not None and persisted.status == AuditStatus.NOT_FOUND
    assert persisted.details != "Invalid legacy photos-only result"


@pytest.mark.anyio
async def test_photos_only_preserves_previous_findings_and_retries_missing_reference_count(tmp_path: Path) -> None:
    repo = SQLiteProductRepository(tmp_path / "partial.db")
    product = Product(product_id=6110, title="Router RX10", images_count=3, images_count_known=True)
    old = AuditResult(
        product_id=6110,
        status=AuditStatus.MISMATCH,
        details="Specification mismatch",
        reference_url="https://example.com/rx10",
        audit_parts={"specs": product.audit_input_hash("specs")},
    )
    repo.save_product_and_audit_result(product, old)
    crawler = MagicMock()
    crawler.crawl = AsyncMock(return_value=MagicMock(
        success=True, markdown="Router RX10", images_count=5,
        title="RX10", status_code=200,
    ))
    judge = MagicMock()
    judge.matches_model = AsyncMock(return_value=True)
    use_case = AuditProductUseCase(repo, MagicMock(), crawler, judge)

    updated = await use_case.audit_product(product, photos_only=True, reference_url="https://example.com/rx10")
    assert updated.status == AuditStatus.MISMATCH
    assert updated.details == "Specification mismatch"
    assert updated.audit_parts["specs"] == old.audit_parts["specs"]
    assert updated.audit_parts["photos"] == product.audit_input_hash("photos", old.reference_url)
    assert repo.get_latest_audit_result(6110).images_status == "LESS_THAN_REFERENCE"

    crawler.crawl = AsyncMock(return_value=MagicMock(
        success=True, markdown="Router RX10", images_count=0,
        title="RX10", status_code=200,
    ))
    failed = await use_case.audit_product(product, photos_only=True, reference_url="https://example.com/rx10", force_refresh=True)
    assert failed.photos_error == "У подходящего эталона нет количества фото."
    after_failure = repo.get_latest_audit_result(6110)
    assert after_failure.images_status == updated.images_status
    assert after_failure.details == old.details
    assert "photos" not in after_failure.audit_parts
    assert after_failure.audit_parts["specs"] == old.audit_parts["specs"]


def test_audit_part_hashes_change_only_with_relevant_inputs() -> None:
    original = Product(product_id=7001, title="Router RX10", description="Old", images_count=3)
    changed = original.model_copy(update={"description": "New", "images_count": 4})
    assert original.audit_input_hash("specs") == changed.audit_input_hash("specs")
    assert original.audit_input_hash("description") != changed.audit_input_hash("description")
    assert original.audit_input_hash("photos", "https://example.com/rx10") != changed.audit_input_hash("photos", "https://example.com/rx10")
    assert original.audit_input_hash("photos", "https://example.com/a") != original.audit_input_hash("photos", "https://example.com/b")


@pytest.mark.anyio
async def test_photo_source_does_not_relabel_existing_spec_evidence(tmp_path: Path) -> None:
    from src.entrypoints.cli import _audit_is_current

    repo = SQLiteProductRepository(tmp_path / "photo_source.db")
    product = Product(product_id=7100, title="Router RX10", images_count=1, images_count_known=True)
    old = AuditResult(
        product_id=7100,
        status=AuditStatus.MISMATCH,
        reference_url="https://example.com/spec-source",
        details="Spec evidence from first source",
        audit_parts={"specs": product.audit_input_hash("specs")},
    )
    repo.save_product_and_audit_result(product, old)
    crawler = MagicMock()
    crawler.crawl = AsyncMock(return_value=MagicMock(
        success=True, markdown="Router RX10", images_count=2,
        title="RX10", status_code=200,
    ))
    judge = MagicMock()
    judge.matches_model = AsyncMock(return_value=True)
    use_case = AuditProductUseCase(repo, MagicMock(), crawler, judge)
    result = await use_case.audit_product(product, photos_only=True, reference_url="https://example.com/photo-source")
    assert result.reference_url == old.reference_url
    assert result.audit_parts["photos_url"] == "https://example.com/photo-source"
    assert _audit_is_current(product, result, photos_only=True, skip_desc=False)


@pytest.mark.anyio
async def test_legacy_full_report_keeps_its_reference_after_photo_run(tmp_path: Path) -> None:
    repo = SQLiteProductRepository(tmp_path / "legacy_full_photo.db")
    product = Product(product_id=7150, title="Router RX10", images_count=0, images_count_known=True)
    previous = AuditResult(
        product_id=7150,
        status=AuditStatus.MISMATCH,
        reference_url="https://example.com/old-spec-evidence",
        details="Old specification discrepancy",
        audit_parts={},
    )
    repo.save_product_and_audit_result(product, previous)
    crawler = MagicMock()
    crawler.crawl = AsyncMock(return_value=MagicMock(
        success=True, markdown="Router RX10", images_count=1,
        title="RX10", status_code=200,
    ))
    judge = MagicMock()
    judge.matches_model = AsyncMock(return_value=True)
    result = await AuditProductUseCase(repo, MagicMock(), crawler, judge).audit_product(
        product, photos_only=True, reference_url="https://example.com/photo-evidence",
    )
    assert result.reference_url == previous.reference_url
    assert result.details == previous.details
    assert result.images_status == "NO_IMAGES"
    assert result.reference_images_count == 1
    assert result.audit_parts["photos_url"] == "https://example.com/photo-evidence"
    persisted = repo.get_latest_audit_result(7150)
    assert persisted.reference_url == previous.reference_url
    assert persisted.audit_parts["photos"] == product.audit_input_hash("photos", "https://example.com/photo-evidence")


@pytest.mark.anyio
async def test_model_check_failure_is_reported_without_replacing_findings(tmp_path: Path) -> None:
    repo = SQLiteProductRepository(tmp_path / "model_error.db")
    product = Product(product_id=7200, title="Router RX10", images_count=1, images_count_known=True)
    old = AuditResult(product_id=7200, status=AuditStatus.MISMATCH, details="Existing finding")
    repo.save_product_and_audit_result(product, old)
    crawler = MagicMock()
    crawler.crawl = AsyncMock(return_value=MagicMock(
        success=True, markdown="Router RX10", images_count=2,
        title="RX10", status_code=200,
    ))
    judge = MagicMock()
    judge.matches_model = AsyncMock(side_effect=RuntimeError("API unavailable"))
    result = await AuditProductUseCase(repo, MagicMock(), crawler, judge).audit_product(
        product, photos_only=True, reference_url="https://example.com/photo-source",
    )
    assert "API unavailable" in (result.photos_error or "")
    assert repo.get_latest_audit_result(7200).details == "Existing finding"


@pytest.mark.anyio
async def test_full_run_with_unknown_reference_count_preserves_previous_photo_result(tmp_path: Path) -> None:
    repo = SQLiteProductRepository(tmp_path / "photo_then_full.db")
    product = Product(product_id=7300, title="Router RX10", images_count=1, images_count_known=True)
    photo_source = "https://example.com/photos"
    old = AuditResult(
        product_id=7300, status=AuditStatus.PENDING,
        reference_url=photo_source, images_count=1, reference_images_count=3,
        images_status="LESS_THAN_REFERENCE",
        audit_parts={
            "photos": product.audit_input_hash("photos", photo_source),
            "photos_url": photo_source,
        },
    )
    repo.save_product_and_audit_result(product, old)
    crawler = MagicMock()
    crawler.crawl = AsyncMock(return_value=MagicMock(
        success=True, markdown="Router RX10 specs", images_count=0,
        title="RX10", status_code=200,
    ))
    judge = MagicMock()
    judge.judge = AsyncMock(return_value=AuditResult(
        product_id=7300, status=AuditStatus.VERIFIED,
        reference_url="https://example.com/specs", details="Specs confirmed",
    ))
    result = await AuditProductUseCase(repo, MagicMock(), crawler, judge).audit_product(
        product, reference_url="https://example.com/specs",
    )
    assert result.reference_url == "https://example.com/specs"
    assert result.images_status == "LESS_THAN_REFERENCE"
    assert result.reference_images_count == 3
    assert result.audit_parts["photos_url"] == photo_source
    assert result.audit_parts["photos"] == old.audit_parts["photos"]
    assert result.audit_parts["specs"] == product.audit_input_hash("specs")
