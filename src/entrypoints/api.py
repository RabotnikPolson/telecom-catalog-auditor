import hashlib
import json
import logging
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from math import ceil
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.responses import Response

from config.settings import Settings, get_settings
from src.adapters.db.sqlite_repo import (
    AuditListFilters,
    AuditReadRecord,
    SQLiteAuditReadRepository,
)
from src.entrypoints.api_models import (
    AuditCardResponse,
    AuditDetailResponse,
    AuditListItemResponse,
    AuditListQuery,
    AuditListResponse,
    BrandListResponse,
    CountsResponse,
    CoverageStatus,
    DescriptionDetailResponse,
    DescriptionDiscrepancyResponse,
    DescriptionSummaryResponse,
    DiscrepancyResponse,
    ErrorBodyResponse,
    ErrorFieldResponse,
    ErrorResponse,
    MissingSpecResponse,
    PaginationResponse,
    PhotosResponse,
    ProductResponse,
    SpecsResponse,
    VendorListResponse,
)

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else uuid4().hex


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    fields: list[ErrorFieldResponse] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        request_id=_request_id(request),
        error=ErrorBodyResponse(code=code, message=message, fields=fields or []),
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coverage(record: AuditReadRecord, part: str) -> CoverageStatus:
    return CoverageStatus.CHECKED if part in record.audit_parts else CoverageStatus.UNKNOWN


def _audit_revision(record: AuditReadRecord) -> str:
    snapshot = {
        "audit_id": record.audit_id,
        "product_id": record.product_id,
        "audit_status": record.audit_status.value,
        "confidence_score": record.confidence_score,
        "reference_url": record.reference_url,
        "reference_title": record.reference_title,
        "reference_sku": record.reference_sku,
        "discrepancies": [item.model_dump(mode="json") for item in record.discrepancies],
        "missing_specs": [item.model_dump(mode="json") for item in record.missing_specs],
        "matched_specs_count": record.matched_specs_count,
        "total_specs_count": record.total_specs_count,
        "images_count": record.images_count,
        "reference_images_count": record.reference_images_count,
        "images_status": record.images_status,
        "description_discrepancies": [
            item.model_dump(mode="json") for item in record.description_discrepancies
        ],
        "description_quality": record.description_quality,
        "audited_at": _as_utc(record.audited_at).isoformat(),
        "coverage": sorted(record.audit_parts),
    }
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finding_id(revision: str, kind: str, index: int, item: dict[str, object]) -> str:
    canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload = f"{revision}|{kind}|{index}|{canonical}"
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _photos(record: AuditReadRecord) -> PhotosResponse:
    return PhotosResponse(
        coverage=_coverage(record, "photos"),
        shop_count=record.images_count,
        reference_count=record.reference_images_count,
        status=record.images_status,
    )


def _list_item(record: AuditReadRecord) -> AuditListItemResponse:
    return AuditListItemResponse(
        audit_id=record.audit_id,
        audit_revision=_audit_revision(record),
        product_id=record.product_id,
        shop_sku=record.shop_sku,
        barcode=record.barcode,
        title=record.title,
        brand_name=record.brand_name,
        vendor_name=record.vendor_name,
        vendor_sku=record.vendor_sku,
        audit_status=record.audit_status,
        attention_required=SQLiteAuditReadRepository.attention_required(record),
        confidence_score=record.confidence_score,
        reference_url=record.reference_url,
        counts=CountsResponse(
            discrepancies=len(record.discrepancies),
            missing_specs=len(record.missing_specs),
            description_discrepancies=len(record.description_discrepancies),
            matched_specs=record.matched_specs_count,
            total_specs=record.total_specs_count,
        ),
        photos=_photos(record),
        description=DescriptionSummaryResponse(
            coverage=_coverage(record, "description"),
            quality=record.description_quality,
        ),
        audited_at=_as_utc(record.audited_at),
    )


def _detail(record: AuditReadRecord) -> AuditCardResponse:
    revision = _audit_revision(record)
    discrepancies = []
    for index, discrepancy_item in enumerate(record.discrepancies):
        item_data = discrepancy_item.model_dump(mode="json")
        discrepancies.append(
            DiscrepancyResponse.model_validate(
                {
                    "finding_id": _finding_id(revision, "discrepancy", index, item_data),
                    **item_data,
                }
            )
        )

    missing_specs = []
    for index, missing_item in enumerate(record.missing_specs):
        item_data = missing_item.model_dump(mode="json")
        missing_specs.append(
            MissingSpecResponse.model_validate(
                {
                    "finding_id": _finding_id(revision, "missing_spec", index, item_data),
                    **item_data,
                }
            )
        )

    description_discrepancies = []
    for index, description_item in enumerate(record.description_discrepancies):
        item_data = description_item.model_dump(mode="json")
        description_discrepancies.append(
            DescriptionDiscrepancyResponse.model_validate(
                {
                    "finding_id": _finding_id(revision, "description_discrepancy", index, item_data),
                    **item_data,
                }
            )
        )

    return AuditCardResponse(
        request_id="",
        product=ProductResponse(
            product_id=record.product_id,
            shop_sku=record.shop_sku,
            barcode=record.barcode,
            title=record.title,
            brand_name=record.brand_name,
            vendor_name=record.vendor_name,
            vendor_sku=record.vendor_sku,
            manufacturer_sku=record.manufacturer_sku,
        ),
        audit=AuditDetailResponse(
            audit_id=record.audit_id,
            audit_revision=revision,
            audit_status=record.audit_status,
            attention_required=SQLiteAuditReadRepository.attention_required(record),
            confidence_score=record.confidence_score,
            reference_url=record.reference_url,
            reference_title=record.reference_title,
            reference_sku=record.reference_sku,
            audited_at=_as_utc(record.audited_at),
            specs=SpecsResponse(
                coverage=_coverage(record, "specs"),
                matched_count=record.matched_specs_count,
                total_count=record.total_specs_count,
                discrepancies=discrepancies,
                missing_specs=missing_specs,
            ),
            photos=_photos(record),
            description=DescriptionDetailResponse(
                coverage=_coverage(record, "description"),
                quality=record.description_quality,
                discrepancies=description_discrepancies,
            ),
        ),
    )


def create_app(
    settings: Settings | None = None,
    repository: SQLiteAuditReadRepository | None = None,
) -> FastAPI:
    config = settings or get_settings()
    secret_value = config.AUDIT_API_TOKEN.get_secret_value() if config.AUDIT_API_TOKEN else ""
    if not secret_value:
        raise RuntimeError("AUDIT_API_TOKEN must be configured before starting the audit API")

    read_repository = repository or SQLiteAuditReadRepository(config.db_file_path)
    read_repository.check_health()
    bearer_scheme = HTTPBearer(auto_error=False)
    app = FastAPI(
        title="Telecom Catalog Audit API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def attach_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def require_service_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    ) -> None:
        supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
        if not supplied or not secrets.compare_digest(supplied, secret_value):
            raise ApiError(401, "UNAUTHORIZED", "Ошибка аутентификации сервиса.")

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            ErrorFieldResponse(
                field=".".join(str(part) for part in error["loc"] if part not in ("query", "path")),
                message=str(error["msg"]),
            )
            for error in exc.errors()
        ]
        return _error_response(
            request,
            422,
            "VALIDATION_ERROR",
            "Запрос не прошёл валидацию.",
            fields,
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_error_handler(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        logger.warning("Audit SQLite unavailable for request %s: %s", _request_id(request), exc)
        return _error_response(
            request,
            503,
            "AUDIT_STORE_UNAVAILABLE",
            "Хранилище результатов аудита временно недоступно.",
        )

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Audit API request %s failed: %s", _request_id(request), exc)
        return _error_response(request, 500, "INTERNAL_ERROR", "Внутренняя ошибка сервиса.")

    @app.get(
        "/api/v1/audit/vendors",
        response_model=VendorListResponse,
        dependencies=[Depends(require_service_token)],
    )
    def list_vendors(request: Request) -> VendorListResponse:
        return VendorListResponse(
            request_id=_request_id(request),
            items=list(read_repository.list_audited_vendors()),
        )

    @app.get(
        "/api/v1/audit/brands",
        response_model=BrandListResponse,
        dependencies=[Depends(require_service_token)],
    )
    def list_brands(request: Request) -> BrandListResponse:
        return BrandListResponse(
            request_id=_request_id(request),
            items=list(read_repository.list_audited_brands()),
        )

    @app.get(
        "/api/v1/audit/products",
        response_model=AuditListResponse,
        dependencies=[Depends(require_service_token)],
    )
    def list_products(request: Request, query: Annotated[AuditListQuery, Query()]) -> AuditListResponse:
        filters = AuditListFilters(
            page=query.page,
            per_page=query.per_page,
            statuses=query.parsed_statuses(),
            attention_required=query.attention_required,
            query=query.q,
            brand=query.brand,
            vendor=query.vendor,
            audited_from=_as_utc(query.audited_from) if query.audited_from else None,
            audited_to=_as_utc(query.audited_to) if query.audited_to else None,
            sort=query.sort,
            direction=query.direction,
        )
        page = read_repository.get_paginated_results(filters)
        total_pages = ceil(page.total_items / page.per_page) if page.total_items else 0
        return AuditListResponse(
            request_id=_request_id(request),
            items=[_list_item(item) for item in page.items],
            pagination=PaginationResponse(
                page=page.page,
                per_page=page.per_page,
                total_items=page.total_items,
                total_pages=total_pages,
                has_previous=page.page > 1,
                has_next=page.page < total_pages,
            ),
        )

    @app.get(
        "/api/v1/audit/products/{product_id}",
        response_model=AuditCardResponse,
        dependencies=[Depends(require_service_token)],
    )
    def product_details(request: Request, product_id: int) -> AuditCardResponse:
        if product_id <= 0:
            raise ApiError(422, "VALIDATION_ERROR", "product_id должен быть больше нуля.")
        record = read_repository.get_audit_card_details(product_id)
        if record is None:
            raise ApiError(404, "PRODUCT_AUDIT_NOT_FOUND", "Результат аудита товара не найден.")
        response = _detail(record)
        return response.model_copy(update={"request_id": _request_id(request)})

    return app
