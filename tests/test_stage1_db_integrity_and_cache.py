import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.adapters.db.schema import get_sqlite_connection, init_db
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.domain.entities import AuditResult, AuditStatus, DiscrepancyItem, Product


@pytest.fixture
def temp_repo(tmp_path: Path) -> SQLiteProductRepository:
    db_file = tmp_path / "test_stage1.db"
    return SQLiteProductRepository(db_file)


def test_status_preservation_on_sync(temp_repo: SQLiteProductRepository) -> None:
    p = Product(
        product_id=101,
        title="Test Router AX3000",
        shop_sku="SKU-101",
        current_specs={"speed": "3000Mbps"},
        status=AuditStatus.VERIFIED,
    )
    temp_repo.save_product(p)
    saved = temp_repo.get_product_by_id(101)
    assert saved is not None
    assert saved.status == AuditStatus.VERIFIED

    # Re-sync with exact same specs (content_hash matches) but incoming status is PENDING
    incoming_same = Product(
        product_id=101,
        title="Test Router AX3000",
        shop_sku="SKU-101",
        current_specs={"speed": "3000Mbps"},
        status=AuditStatus.PENDING,
    )
    temp_repo.save_product(incoming_same)
    after_resync = temp_repo.get_product_by_id(101)
    assert after_resync is not None
    assert after_resync.status == AuditStatus.VERIFIED  # PRESERVED!

    # Re-sync with modified specs (content_hash changed)
    incoming_changed = Product(
        product_id=101,
        title="Test Router AX3000 V2",
        shop_sku="SKU-101",
        current_specs={"speed": "5400Mbps"},
        status=AuditStatus.PENDING,
    )
    temp_repo.save_product(incoming_changed)
    after_change = temp_repo.get_product_by_id(101)
    assert after_change is not None
    assert after_change.status == AuditStatus.PENDING  # RESET!


def test_batch_upsert_status_preservation(temp_repo: SQLiteProductRepository) -> None:
    p1 = Product(product_id=201, title="Switch 1", current_specs={"ports": "8"}, status=AuditStatus.VERIFIED)
    p2 = Product(product_id=202, title="Switch 2", current_specs={"ports": "16"}, status=AuditStatus.MISMATCH)
    temp_repo.batch_upsert([p1, p2])

    # Re-sync batch with PENDING status
    resync_p1 = Product(product_id=201, title="Switch 1", current_specs={"ports": "8"}, status=AuditStatus.PENDING)
    resync_p2_changed = Product(product_id=202, title="Switch 2 NEW", current_specs={"ports": "24"}, status=AuditStatus.PENDING)
    temp_repo.batch_upsert([resync_p1, resync_p2_changed])

    res1 = temp_repo.get_product_by_id(201)
    res2 = temp_repo.get_product_by_id(202)
    assert res1 is not None and res1.status == AuditStatus.VERIFIED
    assert res2 is not None and res2.status == AuditStatus.PENDING


def test_audit_results_deduplication_upsert(temp_repo: SQLiteProductRepository) -> None:
    p = Product(product_id=301, title="Camera Pro", current_specs={})
    temp_repo.save_product(p)

    res1 = AuditResult(
        product_id=301,
        status=AuditStatus.MISMATCH,
        confidence_score=0.8,
        details="First run verdict",
        discrepancies=[
            DiscrepancyItem(
                spec_name="res",
                shop_value="1080p",
                reference_value="4K",
                proof_quote="Supports 4K UHD video output",
                source_url="https://vendor.kz/spec",
            )
        ],
    )
    audit_id_1 = temp_repo.save_audit_result(res1)
    assert audit_id_1 > 0

    # Repeat audit for same product
    res2 = AuditResult(
        product_id=301,
        status=AuditStatus.VERIFIED,
        confidence_score=0.98,
        details="Second run verified",
    )
    audit_id_2 = temp_repo.save_audit_result(res2)
    assert audit_id_2 > 0

    with get_sqlite_connection(temp_repo.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM audit_results WHERE product_id = 301;").fetchone()[0]
        assert count == 1  # Strictly 1 result!

    latest = temp_repo.get_latest_audit_result(301)
    assert latest is not None
    assert latest.status == AuditStatus.VERIFIED
    assert latest.details == "Second run verified"
    assert latest.confidence_score == 0.98


def test_cache_ttl_and_expiration(temp_repo: SQLiteProductRepository) -> None:
    # 1. URL cache expired
    past_iso = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    future_iso = (datetime.now(UTC) + timedelta(days=5)).isoformat()

    temp_repo.save_url_cache("hash_expired", "https://old.kz", "TEST", expires_at=past_iso)
    temp_repo.save_url_cache("hash_valid", "https://new.kz", "TEST", expires_at=future_iso)

    assert temp_repo.get_url_cache("hash_expired") is None
    assert temp_repo.get_url_cache("hash_valid") == "https://new.kz"

    # 2. Specs cache expired
    temp_repo.save_specs_cache("https://old.kz", '{"test": 1}', expires_at=past_iso)
    temp_repo.save_specs_cache("https://new.kz", '{"test": 2}', expires_at=future_iso)

    assert temp_repo.get_specs_cache("https://old.kz") is None
    assert temp_repo.get_specs_cache("https://new.kz") == '{"test": 2}'


def test_fifo_capacity_and_pruning(temp_repo: SQLiteProductRepository) -> None:
    now_dt = datetime.now(UTC)
    past_iso = (now_dt - timedelta(days=1)).isoformat()

    # Insert 1 expired item
    temp_repo.save_url_cache("hash_exp", "https://exp.kz", "TEST", expires_at=past_iso)
    temp_repo.save_specs_cache("https://exp.kz", "{}", expires_at=past_iso)

    # Insert 10 valid items (expiring tomorrow)
    for i in range(10):
        t_iso = (now_dt + timedelta(days=1, minutes=i)).isoformat()
        temp_repo.save_url_cache(f"hash_{i}", f"https://site.kz/{i}", "TEST", expires_at=t_iso)
        temp_repo.save_specs_cache(f"https://site.kz/{i}", json.dumps({"idx": i}), expires_at=t_iso)

    # Prune with max_records=4
    stats = temp_repo.prune_expired_cache(max_records=4)
    assert stats["url_cache_expired"] == 1
    assert stats["specs_cache_expired"] == 1
    assert stats["url_cache_fifo"] == 6  # 10 remaining - 4 limit = 6 deleted
    assert stats["specs_cache_fifo"] == 6

    with get_sqlite_connection(temp_repo.db_path) as conn:
        url_count = conn.execute("SELECT COUNT(*) FROM url_cache;").fetchone()[0]
        specs_count = conn.execute("SELECT COUNT(*) FROM specs_cache;").fetchone()[0]
        assert url_count == 4
        assert specs_count == 4


def test_sqlite_connection_closes_on_context_exit(temp_repo: SQLiteProductRepository) -> None:
    with get_sqlite_connection(temp_repo.db_path) as conn:
        row = conn.execute("SELECT 1;").fetchone()
        assert row[0] == 1

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1;")


def test_atomic_jit_save_product_and_audit_result(temp_repo: SQLiteProductRepository) -> None:
    p = Product(
        product_id=9901,
        title="JIT Atomic Test Router",
        shop_sku="JIT-9901",
        current_specs={"wifi": "Wi-Fi 7"},
    )
    res = AuditResult(
        product_id=9901,
        status=AuditStatus.VERIFIED,
        confidence_score=0.97,
        details="JIT saved atomically",
    )
    audit_id = temp_repo.save_product_and_audit_result(p, res)
    assert audit_id > 0

    saved_prod = temp_repo.get_product_by_id(9901)
    assert saved_prod is not None
    assert saved_prod.status == AuditStatus.VERIFIED

    saved_res = temp_repo.get_latest_audit_result(9901)
    assert saved_res is not None
    assert saved_res.status == AuditStatus.VERIFIED


def test_cache_indexes_and_user_version_exist(temp_repo: SQLiteProductRepository) -> None:
    with get_sqlite_connection(temp_repo.db_path) as conn:
        ver = conn.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == 6

        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index';"
        ).fetchall()
        idx_names = {r[0] for r in idx_rows}
        assert "idx_url_cache_resolved_url" in idx_names
        assert "idx_url_cache_created_at" in idx_names
        assert "idx_specs_cache_fetched_at" in idx_names
        assert "idx_audit_results_product_unique" in idx_names
        assert "idx_products_brand_name" in idx_names

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(audit_results);").fetchall()
        }
        assert {"reference_title", "reference_sku"} <= columns


def test_v3_audit_migration_keeps_report_and_unique_index(tmp_path: Path) -> None:
    db_file = tmp_path / "version_three.db"
    init_db(db_file)
    repo = SQLiteProductRepository(db_file)
    product = Product(product_id=9201, title="Legacy router")
    original = AuditResult(product_id=9201, status=AuditStatus.MISMATCH, details="Keep this finding")
    repo.save_product_and_audit_result(product, original)
    with get_sqlite_connection(db_file) as conn:
        conn.execute("ALTER TABLE audit_results DROP COLUMN audit_parts_json")
        conn.execute("PRAGMA user_version = 3")
    init_db(db_file)
    init_db(db_file)
    with get_sqlite_connection(db_file) as conn:
        row = conn.execute("SELECT details, audit_parts_json FROM audit_results WHERE product_id = 9201").fetchone()
        index = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_audit_results_product_unique'").fetchone()
        assert row["details"] == "Keep this finding"
        assert row["audit_parts_json"] is None
        assert index is not None


def test_legacy_schema_without_inline_unique_migrates_and_upserts(tmp_path: Path) -> None:
    db_file = tmp_path / "legacy_catalog_audit.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            shop_sku TEXT,
            title TEXT NOT NULL,
            barcode TEXT,
            vendor_name TEXT,
            vendor_sku TEXT,
            manufacturer_sku TEXT,
            description TEXT,
            images_count INTEGER NOT NULL DEFAULT 0,
            current_specs TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            parent_sku TEXT,
            master_key TEXT,
            is_master INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE audit_results (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            confidence_score REAL NOT NULL,
            reference_url TEXT,
            discrepancies_json TEXT,
            missing_specs_json TEXT,
            matched_specs_count INTEGER NOT NULL,
            total_specs_count INTEGER NOT NULL,
            audited_at TEXT NOT NULL,
            details TEXT,
            FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
        );
        PRAGMA user_version = 2;
        """
    )
    conn.commit()
    conn.close()

    repo = SQLiteProductRepository(db_file)
    prod = Product(
        product_id=3180,
        title="Defender Forsage GTR",
        shop_sku="3180",
        current_specs={"Диаметр руля": "24 см"},
    )
    res1 = AuditResult(
        product_id=3180,
        status=AuditStatus.MISMATCH,
        confidence_score=0.99,
        details="First audit on legacy schema",
        search_trace=["[Step 1] Tested candidate"],
    )
    aid1 = repo.save_product_and_audit_result(prod, res1)
    assert aid1 > 0

    res2 = AuditResult(
        product_id=3180,
        status=AuditStatus.VERIFIED,
        confidence_score=0.98,
        details="Updated audit on legacy schema",
        search_trace=["[Step 1] Re-verified"],
    )
    aid2 = repo.save_product_and_audit_result(prod, res2)
    assert aid2 == aid1

    latest = repo.get_latest_audit_result(3180)
    assert latest is not None
    assert latest.status == AuditStatus.VERIFIED
    assert latest.search_trace == ["[Step 1] Re-verified"]


