import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.domain.entities import (
    AuditResult,
    AuditStatus,
    DescriptionDiscrepancyItem,
    DiscrepancyItem,
    MissingSpecItem,
    Product,
)
from src.entrypoints.api import create_app

TOKEN = "stage6a-test-token"


@pytest.fixture
def api_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "audit_api.db"
    repo = SQLiteProductRepository(db_path)
    product = Product(
        product_id=38059,
        shop_sku="10459",
        barcode="8806094901234",
        title="Samsung Galaxy A54",
        brand_name="Samsung",
        vendor_name="Samsung",
        vendor_sku="SM-A546ELVD",
        current_specs={"RAM": "6 GB"},
    )
    result = AuditResult(
        product_id=product.product_id,
        status=AuditStatus.MISMATCH,
        confidence_score=0.95,
        reference_url="https://kaspi.kz/shop/p/example",
        discrepancies=[
            DiscrepancyItem(
                spec_name="RAM",
                shop_value="6 GB",
                reference_value="8 GB",
                proof_quote="RAM: 8 GB",
                source_url="https://kaspi.kz/shop/p/example",
                severity="critical",
            )
        ],
        missing_specs=[
            MissingSpecItem(
                spec_name="Protection",
                reference_value="IP67",
                proof_quote="Protection: IP67",
                source_url="https://kaspi.kz/shop/p/example",
            )
        ],
        matched_specs_count=3,
        total_specs_count=5,
        images_count=4,
        reference_images_count=4,
        images_status="OK",
        description_discrepancies=[
            DescriptionDiscrepancyItem(
                claim_in_description="60 Hz",
                factual_truth="120 Hz",
            )
        ],
        description_quality="WARNING",
        audited_at=datetime(2026, 10, 21, 14, 30, tzinfo=UTC),
        audit_parts={"specs": "sha256:specs", "description": "sha256:description", "photos": "sha256:photos"},
    )
    repo.save_product_and_audit_result(product, result)
    return db_path


@pytest.fixture
def client(api_db: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        DATABASE_PATH=str(api_db),
        AUDIT_API_TOKEN=TOKEN,
    )
    return TestClient(create_app(settings=settings))


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_list_requires_service_token(client: TestClient) -> None:
    response = client.get("/api/v1/audit/products")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_vendors_require_token_and_return_contract(client: TestClient) -> None:
    unauthorized = client.get("/api/v1/audit/vendors")
    response = client.get("/api/v1/audit/vendors", headers=auth_headers())

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "api_version": "1.0",
        "request_id": response.headers["X-Request-ID"],
        "items": ["Samsung"],
    }


def test_brands_require_token_and_filter_products(client: TestClient) -> None:
    unauthorized = client.get("/api/v1/audit/brands")
    brands = client.get("/api/v1/audit/brands", headers=auth_headers())
    products = client.get(
        "/api/v1/audit/products",
        params={"brand": "Samsung"},
        headers=auth_headers(),
    )

    assert unauthorized.status_code == 401
    assert brands.status_code == 200
    assert brands.json()["items"] == ["Samsung"]
    assert products.status_code == 200
    assert products.json()["pagination"]["total_items"] == 1
    assert products.json()["items"][0]["brand_name"] == "Samsung"


def test_list_contract_filters_and_forbidden_fields(client: TestClient) -> None:
    response = client.get(
        "/api/v1/audit/products",
        params={
            "status": "MISMATCH,ERROR",
            "attention_required": "true",
            "q": "10459",
            "per_page": "25",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_version"] == "1.0"
    assert payload["pagination"] == {
        "page": 1,
        "per_page": 25,
        "total_items": 1,
        "total_pages": 1,
        "has_previous": False,
        "has_next": False,
    }
    item = payload["items"][0]
    assert item["product_id"] == 38059
    assert item["shop_sku"] == "10459"
    assert item["audit_status"] == "MISMATCH"
    assert item["attention_required"] is True
    assert item["counts"]["discrepancies"] == 1
    assert item["photos"]["coverage"] == "CHECKED"
    serialized = response.text
    for forbidden in (
        "search_trace",
        "input_tokens",
        "output_tokens",
        "estimated_cost_usd",
        "execution_time_sec",
        "crawler_time_sec",
        "llm_time_sec",
        "audit_parts",
        "content_hash",
    ):
        assert forbidden not in serialized


def test_detail_has_stable_revision_and_finding_ids(client: TestClient) -> None:
    first = client.get("/api/v1/audit/products/38059", headers=auth_headers())
    second = client.get("/api/v1/audit/products/38059", headers=auth_headers())

    assert first.status_code == 200
    assert second.status_code == 200
    first_audit = first.json()["audit"]
    second_audit = second.json()["audit"]
    assert first_audit["audit_revision"] == second_audit["audit_revision"]
    assert first_audit["specs"]["discrepancies"][0]["finding_id"] == (
        second_audit["specs"]["discrepancies"][0]["finding_id"]
    )
    assert first_audit["description"]["discrepancies"][0]["finding_id"].startswith("sha256:")
    assert first.json()["product"]["vendor_sku"] == "SM-A546ELVD"
    assert first.json()["product"]["brand_name"] == "Samsung"
    assert "shop_url" not in first.text


def test_old_row_without_audit_parts_reports_unknown(api_db: Path) -> None:
    settings = Settings(_env_file=None, DATABASE_PATH=str(api_db), AUDIT_API_TOKEN=TOKEN)
    client = TestClient(create_app(settings=settings))
    response = client.get("/api/v1/audit/products/38059", headers=auth_headers())
    assert response.status_code == 200

    # Re-save the audit as a legacy-style row with no coverage metadata.
    repo = SQLiteProductRepository(api_db)
    current = repo.get_latest_audit_result(38059)
    assert current is not None
    repo.save_audit_result(current.model_copy(update={"audit_parts": {}}))

    legacy = client.get("/api/v1/audit/products/38059", headers=auth_headers())
    audit = legacy.json()["audit"]
    assert audit["specs"]["coverage"] == "UNKNOWN"
    assert audit["photos"]["coverage"] == "UNKNOWN"
    assert audit["description"]["coverage"] == "UNKNOWN"


def test_validation_and_not_found_use_error_envelope(client: TestClient) -> None:
    invalid = client.get(
        "/api/v1/audit/products",
        params={"status": "RESOLVED", "unknown": "value"},
        headers=auth_headers(),
    )
    missing = client.get("/api/v1/audit/products/999999", headers=auth_headers())

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "PRODUCT_AUDIT_NOT_FOUND"


def test_list_rejects_unsupported_per_page(client: TestClient) -> None:
    response = client.get(
        "/api/v1/audit/products",
        params={"per_page": "30"},
        headers=auth_headers(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["fields"] == [
        {"field": "per_page", "message": "Value error, per_page must be 25, 50 or 100"}
    ]


def test_get_requests_do_not_modify_sqlite_file(api_db: Path, client: TestClient) -> None:
    before = hashlib.sha256(api_db.read_bytes()).hexdigest()

    assert client.get("/api/v1/audit/products", headers=auth_headers()).status_code == 200
    assert client.get("/api/v1/audit/products/38059", headers=auth_headers()).status_code == 200
    assert client.get("/api/v1/audit/vendors", headers=auth_headers()).status_code == 200
    assert client.get("/api/v1/audit/brands", headers=auth_headers()).status_code == 200

    after = hashlib.sha256(api_db.read_bytes()).hexdigest()
    assert after == before
