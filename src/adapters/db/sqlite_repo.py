import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from src.domain.entities import (
    AuditResult,
    AuditStatus,
    DescriptionDiscrepancyItem,
    DiscrepancyItem,
    MissingSpecItem,
    Product,
)

from .schema import get_sqlite_connection, init_db

DEFAULT_CACHE_TTL_DAYS: int = 14
DEFAULT_CACHE_MAX_RECORDS: int = 30000


class CrawlerStateDict(TypedDict):
    category_key: str
    last_page: int
    total_pages: int
    is_completed: bool
    updated_at: str


class SQLiteProductRepository:

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        init_db(self.db_path)
        self.prune_expired_cache()

    def _row_to_product(self, row: sqlite3.Row) -> Product:
        specs_dict = json.loads(row["current_specs"]) if row["current_specs"] else {}
        created_at = (
            datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else datetime.now(UTC)
        )
        updated_at = (
            datetime.fromisoformat(row["updated_at"])
            if row["updated_at"]
            else datetime.now(UTC)
        )
        barcode = row["barcode"] if "barcode" in row.keys() else None
        shop_sku = row["shop_sku"] if "shop_sku" in row.keys() else None
        description = row["description"] if "description" in row.keys() else None
        images_count = int(row["images_count"]) if "images_count" in row.keys() and row["images_count"] is not None else 0

        return Product(
            product_id=row["product_id"],
            shop_sku=shop_sku,
            title=row["title"],
            barcode=barcode,
            vendor_name=row["vendor_name"],
            vendor_sku=row["vendor_sku"],
            manufacturer_sku=row["manufacturer_sku"],
            description=description,
            images_count=images_count,
            current_specs=specs_dict,
            content_hash=row["content_hash"],
            parent_sku=row["parent_sku"],
            master_key=row["master_key"],
            is_master=bool(row["is_master"]),
            status=AuditStatus(row["status"]),
            created_at=created_at,
            updated_at=updated_at,
        )

    def save_product(self, product: Product) -> Product:
        with get_sqlite_connection(self.db_path) as conn:
            self._execute_upsert_product(conn, product)
            conn.commit()
        return product

    def batch_upsert(self, products: Sequence[Product]) -> int:
        if not products:
            return 0

        sql = """
        INSERT INTO products (
            product_id, shop_sku, title, barcode, vendor_name, vendor_sku, manufacturer_sku,
            description, images_count,
            current_specs, content_hash, parent_sku, master_key, is_master,
            status, created_at, updated_at
        ) VALUES (
            :product_id, :shop_sku, :title, :barcode, :vendor_name, :vendor_sku, :manufacturer_sku,
            :description, :images_count,
            :current_specs, :content_hash, :parent_sku, :master_key, :is_master,
            :status, :created_at, :updated_at
        )
        ON CONFLICT(product_id) DO UPDATE SET
            shop_sku = excluded.shop_sku,
            title = excluded.title,
            barcode = excluded.barcode,
            vendor_name = excluded.vendor_name,
            vendor_sku = excluded.vendor_sku,
            manufacturer_sku = excluded.manufacturer_sku,
            description = excluded.description,
            images_count = excluded.images_count,
            current_specs = excluded.current_specs,
            content_hash = excluded.content_hash,
            parent_sku = excluded.parent_sku,
            master_key = excluded.master_key,
            is_master = excluded.is_master,
            status = CASE 
                WHEN products.content_hash = excluded.content_hash THEN products.status 
                ELSE excluded.status 
            END,
            updated_at = excluded.updated_at;
        """

        records = [
            {
                "product_id": p.product_id,
                "shop_sku": p.shop_sku,
                "title": p.title,
                "barcode": p.barcode,
                "vendor_name": p.vendor_name,
                "vendor_sku": p.vendor_sku,
                "manufacturer_sku": p.manufacturer_sku,
                "description": p.description,
                "images_count": p.images_count,
                "current_specs": json.dumps(p.current_specs, ensure_ascii=False),
                "content_hash": p.content_hash or p.recalculate_content_hash(),
                "parent_sku": p.parent_sku,
                "master_key": p.master_key,
                "is_master": 1 if p.is_master else 0,
                "status": p.status.value,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
            }
            for p in products
        ]

        with get_sqlite_connection(self.db_path) as conn:
            conn.executemany(sql, records)
            conn.commit()

        return len(records)

    def get_product_by_id(self, product_id: int) -> Product | None:
        sql = "SELECT * FROM products WHERE product_id = ?;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (product_id,))
            row = cursor.fetchone()
            return self._row_to_product(row) if row else None

    def get_products_by_ids(self, product_ids: Sequence[int]) -> dict[int, Product]:
        if not product_ids:
            return {}
        unique_ids = list({int(pid) for pid in product_ids})
        placeholders = ",".join("?" for _ in unique_ids)
        sql = f"SELECT * FROM products WHERE product_id IN ({placeholders});"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, unique_ids)
            rows = cursor.fetchall()
            result: dict[int, Product] = {}
            for row in rows:
                p = self._row_to_product(row)
                if p and p.product_id:
                    result[p.product_id] = p
            return result

    def get_product_by_shop_sku(self, shop_sku: str) -> Product | None:
        sql = "SELECT * FROM products WHERE shop_sku = ? LIMIT 1;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (str(shop_sku).strip(),))
            row = cursor.fetchone()
            return self._row_to_product(row) if row else None

    def find_product(self, identifier: str | int) -> Product | None:
        clean_id = str(identifier).strip()
        by_sku = self.get_product_by_shop_sku(clean_id)
        if by_sku:
            return by_sku
        if clean_id.isdigit():
            by_id = self.get_product_by_id(int(clean_id))
            if by_id:
                return by_id
        return None

    def get_by_content_hash(self, content_hash: str) -> Product | None:
        sql = "SELECT * FROM products WHERE content_hash = ? LIMIT 1;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (content_hash,))
            row = cursor.fetchone()
            return self._row_to_product(row) if row else None

    def get_by_barcode(self, barcode: str) -> Product | None:
        sql = "SELECT * FROM products WHERE barcode = ? LIMIT 1;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (barcode,))
            row = cursor.fetchone()
            return self._row_to_product(row) if row else None


    def get_by_master_key(self, master_key: str) -> list[Product]:
        sql = "SELECT * FROM products WHERE master_key = ? ORDER BY is_master DESC, product_id ASC;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (master_key,))
            return [self._row_to_product(row) for row in cursor.fetchall()]

    def get_all_products(self, limit: int = 100, offset: int = 0) -> list[Product]:
        sql = "SELECT * FROM products ORDER BY product_id ASC LIMIT ? OFFSET ?;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (limit, offset))
            return [self._row_to_product(row) for row in cursor.fetchall()]

    def get_auditable_products(self, limit: int = 100, offset: int = 0) -> list[Product]:
        sql = "SELECT * FROM products WHERE shop_sku IS NOT NULL ORDER BY product_id ASC LIMIT ? OFFSET ?;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (limit, offset))
            real_products = [self._row_to_product(row) for row in cursor.fetchall()]
            if real_products:
                return real_products
        return self.get_all_products(limit=limit, offset=offset)

    def count_products(self) -> int:
        sql = "SELECT COUNT(*) FROM products;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql)
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_audited_product_ids(self) -> set[int]:
        sql = "SELECT DISTINCT product_id FROM audit_results;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql)
            return {int(row[0]) for row in cursor.fetchall()}

    def get_audited_master_keys(self) -> set[str]:
        self.backfill_master_keys()
        sql = """
            SELECT DISTINCT p.master_key 
            FROM audit_results a
            JOIN products p ON a.product_id = p.product_id
            WHERE p.master_key IS NOT NULL AND p.master_key != '';
        """
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql)
            return {str(row[0]) for row in cursor.fetchall() if row[0]}

    def backfill_master_keys(self) -> int:
        from src.domain.services import ParentChildGrouper
        grouper = ParentChildGrouper()
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT product_id, title, current_specs FROM products WHERE master_key IS NULL OR master_key = '';"
            )
            rows = cursor.fetchall()
            if not rows:
                return 0
            updates = []
            for row in rows:
                p_id, title, specs_raw = row[0], row[1], row[2]
                try:
                    specs = json.loads(specs_raw) if specs_raw else {}
                except Exception:
                    specs = {}
                try:
                    m_key = grouper.extract_master_key(title, specs)
                except Exception:
                    m_key = f"pid_{p_id}"
                updates.append((m_key or f"pid_{p_id}", p_id))
            if updates:
                conn.executemany("UPDATE products SET master_key = ? WHERE product_id = ?;", updates)
                conn.commit()
            return len(updates)

    def save_url_cache(
        self,
        query_hash: str,
        resolved_url: str,
        source_type: str,
        ttl_days: int = DEFAULT_CACHE_TTL_DAYS,
        expires_at: str | None = None,
    ) -> None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        if expires_at is None:
            expires_at = (now_dt + timedelta(days=ttl_days)).isoformat()
        sql = """
        INSERT INTO url_cache (query_hash, resolved_url, source_type, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(query_hash) DO UPDATE SET
            resolved_url = excluded.resolved_url,
            source_type = excluded.source_type,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at;
        """
        with get_sqlite_connection(self.db_path) as conn:
            conn.execute(sql, (query_hash, resolved_url, source_type, now, expires_at))
            conn.commit()

    def get_url_cache(self, query_hash: str) -> str | None:
        now = datetime.now(UTC).isoformat()
        sql = "SELECT resolved_url FROM url_cache WHERE query_hash = ? AND (expires_at IS NULL OR expires_at > ?);"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (query_hash, now))
            row = cursor.fetchone()
            return row["resolved_url"] if row else None

    def delete_url_cache_by_url(self, resolved_url: str) -> None:
        sql = "DELETE FROM url_cache WHERE resolved_url = ?;"
        with get_sqlite_connection(self.db_path) as conn:
            conn.execute(sql, (resolved_url,))
            conn.commit()

    def save_specs_cache(
        self,
        resolved_url: str,
        specs_json: str,
        raw_html_hash: str | None = None,
        status_code: int = 200,
        ttl_days: int = DEFAULT_CACHE_TTL_DAYS,
        expires_at: str | None = None,
    ) -> None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        if expires_at is None:
            expires_at = (now_dt + timedelta(days=ttl_days)).isoformat()
        sql = """
        INSERT INTO specs_cache (resolved_url, specs_json, fetched_at, raw_html_hash, status_code, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(resolved_url) DO UPDATE SET
            specs_json = excluded.specs_json,
            fetched_at = excluded.fetched_at,
            raw_html_hash = excluded.raw_html_hash,
            status_code = excluded.status_code,
            expires_at = excluded.expires_at;
        """
        with get_sqlite_connection(self.db_path) as conn:
            conn.execute(sql, (resolved_url, specs_json, now, raw_html_hash, status_code, expires_at))
            conn.commit()

    def get_specs_cache(self, resolved_url: str) -> str | None:
        now = datetime.now(UTC).isoformat()
        sql = "SELECT specs_json FROM specs_cache WHERE resolved_url = ? AND (expires_at IS NULL OR expires_at > ?);"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (resolved_url, now))
            row = cursor.fetchone()
            return row["specs_json"] if row else None

    def delete_specs_cache(self, resolved_url: str) -> None:
        sql = "DELETE FROM specs_cache WHERE resolved_url = ?;"
        with get_sqlite_connection(self.db_path) as conn:
            conn.execute(sql, (resolved_url,))
            conn.commit()

    def prune_expired_cache(self, max_records: int = DEFAULT_CACHE_MAX_RECORDS) -> dict[str, int]:
        """
        Prunes expired cache entries and enforces a hard FIFO capacity cap (max_records).
        Returns a dictionary with pruned count statistics.
        """
        now = datetime.now(UTC).isoformat()
        stats = {
            "url_cache_expired": 0,
            "url_cache_fifo": 0,
            "specs_cache_expired": 0,
            "specs_cache_fifo": 0,
        }
        with get_sqlite_connection(self.db_path) as conn:
            # 1. Prune time-expired records
            cur = conn.execute("DELETE FROM url_cache WHERE expires_at IS NOT NULL AND expires_at <= ?;", (now,))
            stats["url_cache_expired"] = cur.rowcount

            cur = conn.execute("DELETE FROM specs_cache WHERE expires_at IS NOT NULL AND expires_at <= ?;", (now,))
            stats["specs_cache_expired"] = cur.rowcount

            # 2. Enforce FIFO Cap on url_cache
            count_url = conn.execute("SELECT COUNT(*) FROM url_cache;").fetchone()[0]
            if count_url > max_records:
                excess_url = count_url - max_records
                cur = conn.execute(
                    """
                    DELETE FROM url_cache 
                    WHERE query_hash IN (
                        SELECT query_hash FROM url_cache ORDER BY created_at ASC LIMIT ?
                    );
                    """,
                    (excess_url,),
                )
                stats["url_cache_fifo"] = cur.rowcount

            # 3. Enforce FIFO Cap on specs_cache
            count_specs = conn.execute("SELECT COUNT(*) FROM specs_cache;").fetchone()[0]
            if count_specs > max_records:
                excess_specs = count_specs - max_records
                cur = conn.execute(
                    """
                    DELETE FROM specs_cache 
                    WHERE resolved_url IN (
                        SELECT resolved_url FROM specs_cache ORDER BY fetched_at ASC LIMIT ?
                    );
                    """,
                    (excess_specs,),
                )
                stats["specs_cache_fifo"] = cur.rowcount

            conn.commit()

        return stats

    def check_wal_mode(self) -> bool:
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute("PRAGMA journal_mode;")
            mode = cursor.fetchone()[0]
            return str(mode).lower() == "wal"

    def update_crawler_state(
        self,
        category_key: str,
        last_page: int,
        total_pages: int,
        is_completed: bool = False,
    ) -> None:
        sql = """
        INSERT INTO crawler_state (category_key, last_page, total_pages, is_completed, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(category_key) DO UPDATE SET
            last_page = excluded.last_page,
            total_pages = excluded.total_pages,
            is_completed = excluded.is_completed,
            updated_at = excluded.updated_at;
        """
        now = datetime.now(UTC).isoformat()
        with get_sqlite_connection(self.db_path) as conn:
            conn.execute(
                sql,
                (category_key, last_page, total_pages, 1 if is_completed else 0, now),
            )
            conn.commit()

    def get_crawler_state(self, category_key: str) -> CrawlerStateDict | None:
        sql = "SELECT * FROM crawler_state WHERE category_key = ?;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (category_key,))
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "category_key": str(row["category_key"]),
                "last_page": int(row["last_page"]),
                "total_pages": int(row["total_pages"]),
                "is_completed": bool(row["is_completed"]),
                "updated_at": str(row["updated_at"]),
            }

    def reset_crawler_state(self, category_key: str | None = None) -> None:
        with get_sqlite_connection(self.db_path) as conn:
            if category_key:
                conn.execute("DELETE FROM crawler_state WHERE category_key = ?;", (category_key,))
            else:
                conn.execute("DELETE FROM crawler_state;")
            conn.commit()

    def get_all_crawler_states(self) -> list[CrawlerStateDict]:
        sql = "SELECT * FROM crawler_state ORDER BY updated_at DESC;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql)
            return [
                {
                    "category_key": str(row["category_key"]),
                    "last_page": int(row["last_page"]),
                    "total_pages": int(row["total_pages"]),
                    "is_completed": bool(row["is_completed"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in cursor.fetchall()
            ]

    def _row_to_audit_result(self, row: sqlite3.Row) -> AuditResult:
        disc_raw = row["discrepancies_json"]
        discrepancies = [
            DiscrepancyItem(**item) for item in json.loads(disc_raw)
        ] if disc_raw else []
        missing_raw = row["missing_specs_json"] if "missing_specs_json" in row.keys() else None
        missing_specs = [
            MissingSpecItem(**item) for item in json.loads(missing_raw)
        ] if missing_raw else []
        audited_at = (
            datetime.fromisoformat(row["audited_at"])
            if row["audited_at"]
            else datetime.now(UTC)
        )
        keys = row.keys()
        images_count = int(row["images_count"]) if "images_count" in keys and row["images_count"] is not None else 0
        ref_images_count = int(row["reference_images_count"]) if "reference_images_count" in keys and row["reference_images_count"] is not None else 0
        images_status = str(row["images_status"]) if "images_status" in keys and row["images_status"] else "OK"
        desc_disc_raw = row["description_discrepancies_json"] if "description_discrepancies_json" in keys else None
        description_discrepancies = [
            DescriptionDiscrepancyItem(**item) for item in json.loads(desc_disc_raw)
        ] if desc_disc_raw else []
        description_quality = str(row["description_quality"]) if "description_quality" in keys and row["description_quality"] else "GOOD"

        trace_raw = row["search_trace_json"] if "search_trace_json" in keys else None
        search_trace = [str(x) for x in json.loads(trace_raw)] if trace_raw else []
        parts_raw = row["audit_parts_json"] if "audit_parts_json" in keys else None
        audit_parts = json.loads(parts_raw) if parts_raw else {}
        return AuditResult(
            product_id=row["product_id"],
            status=AuditStatus(row["status"]),
            confidence_score=row["confidence_score"],
            reference_url=row["reference_url"],
            discrepancies=discrepancies,
            missing_specs=missing_specs,
            matched_specs_count=row["matched_specs_count"],
            total_specs_count=row["total_specs_count"],
            audited_at=audited_at,
            details=row["details"],
            search_trace=search_trace,
            execution_time_sec=row["execution_time_sec"] if "execution_time_sec" in keys and row["execution_time_sec"] is not None else 0.0,
            crawler_time_sec=row["crawler_time_sec"] if "crawler_time_sec" in keys and row["crawler_time_sec"] is not None else 0.0,
            llm_time_sec=row["llm_time_sec"] if "llm_time_sec" in keys and row["llm_time_sec"] is not None else 0.0,
            input_tokens=row["input_tokens"] if "input_tokens" in keys and row["input_tokens"] is not None else 0,
            output_tokens=row["output_tokens"] if "output_tokens" in keys and row["output_tokens"] is not None else 0,
            estimated_cost_usd=row["estimated_cost_usd"] if "estimated_cost_usd" in keys and row["estimated_cost_usd"] is not None else 0.0,
            images_count=images_count,
            reference_images_count=ref_images_count,
            images_status=images_status,
            description_discrepancies=description_discrepancies,
            description_quality=description_quality,
            audit_parts=audit_parts,
        )

    def _execute_upsert_product(self, conn: sqlite3.Connection, product: Product) -> None:
        sql = """
        INSERT INTO products (
            product_id, shop_sku, title, barcode, vendor_name, vendor_sku, manufacturer_sku,
            description, images_count,
            current_specs, content_hash, parent_sku, master_key, is_master,
            status, created_at, updated_at
        ) VALUES (
            :product_id, :shop_sku, :title, :barcode, :vendor_name, :vendor_sku, :manufacturer_sku,
            :description, :images_count,
            :current_specs, :content_hash, :parent_sku, :master_key, :is_master,
            :status, :created_at, :updated_at
        )
        ON CONFLICT(product_id) DO UPDATE SET
            shop_sku = excluded.shop_sku,
            title = excluded.title,
            barcode = excluded.barcode,
            vendor_name = excluded.vendor_name,
            vendor_sku = excluded.vendor_sku,
            manufacturer_sku = excluded.manufacturer_sku,
            description = excluded.description,
            images_count = excluded.images_count,
            current_specs = excluded.current_specs,
            content_hash = excluded.content_hash,
            parent_sku = excluded.parent_sku,
            master_key = excluded.master_key,
            is_master = excluded.is_master,
            status = CASE 
                WHEN products.content_hash = excluded.content_hash THEN products.status 
                ELSE excluded.status 
            END,
            updated_at = excluded.updated_at;
        """
        payload = {
            "product_id": product.product_id,
            "shop_sku": product.shop_sku,
            "title": product.title,
            "barcode": product.barcode,
            "vendor_name": product.vendor_name,
            "vendor_sku": product.vendor_sku,
            "manufacturer_sku": product.manufacturer_sku,
            "description": product.description,
            "images_count": product.images_count,
            "current_specs": json.dumps(product.current_specs, ensure_ascii=False),
            "content_hash": product.content_hash or product.recalculate_content_hash(),
            "parent_sku": product.parent_sku,
            "master_key": product.master_key,
            "is_master": 1 if product.is_master else 0,
            "status": product.status.value,
            "created_at": product.created_at.isoformat(),
            "updated_at": product.updated_at.isoformat(),
        }
        conn.execute(sql, payload)

    def _execute_upsert_audit_result(self, conn: sqlite3.Connection, result: AuditResult) -> int:
        sql = """
        INSERT INTO audit_results (
            product_id, status, confidence_score, reference_url,
            discrepancies_json, missing_specs_json, matched_specs_count, total_specs_count,
            images_count, reference_images_count, images_status,
            description_discrepancies_json, description_quality,
            audited_at, details, execution_time_sec, crawler_time_sec, llm_time_sec,
            input_tokens, output_tokens, estimated_cost_usd, search_trace_json, audit_parts_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_id) DO UPDATE SET
            status = excluded.status,
            confidence_score = excluded.confidence_score,
            reference_url = excluded.reference_url,
            discrepancies_json = excluded.discrepancies_json,
            missing_specs_json = excluded.missing_specs_json,
            matched_specs_count = excluded.matched_specs_count,
            total_specs_count = excluded.total_specs_count,
            images_count = excluded.images_count,
            reference_images_count = excluded.reference_images_count,
            images_status = excluded.images_status,
            description_discrepancies_json = excluded.description_discrepancies_json,
            description_quality = excluded.description_quality,
            audited_at = excluded.audited_at,
            details = excluded.details,
            execution_time_sec = excluded.execution_time_sec,
            crawler_time_sec = excluded.crawler_time_sec,
            llm_time_sec = excluded.llm_time_sec,
            input_tokens = excluded.input_tokens,
            output_tokens = excluded.output_tokens,
            estimated_cost_usd = excluded.estimated_cost_usd,
            search_trace_json = excluded.search_trace_json,
            audit_parts_json = excluded.audit_parts_json
        RETURNING audit_id;
        """
        disc_json = json.dumps(
            [d.model_dump() for d in result.discrepancies],
            ensure_ascii=False
        )
        missing_json = json.dumps(
            [m.model_dump() for m in result.missing_specs],
            ensure_ascii=False
        )
        desc_disc_json = json.dumps(
            [d.model_dump() for d in result.description_discrepancies],
            ensure_ascii=False
        ) if result.description_discrepancies else None
        trace_json = json.dumps(result.search_trace, ensure_ascii=False) if result.search_trace else None
        now_iso = result.audited_at.isoformat()
        cursor = conn.execute(
            sql,
            (
                result.product_id,
                result.status.value,
                result.confidence_score,
                result.reference_url,
                disc_json,
                missing_json,
                result.matched_specs_count,
                result.total_specs_count,
                result.images_count,
                result.reference_images_count,
                result.images_status,
                desc_disc_json,
                result.description_quality,
                now_iso,
                result.details,
                result.execution_time_sec,
                result.crawler_time_sec,
                result.llm_time_sec,
                result.input_tokens,
                result.output_tokens,
                result.estimated_cost_usd,
                trace_json,
                json.dumps(result.audit_parts, ensure_ascii=False) if result.audit_parts else None,
            ),
        )
        row = cursor.fetchone()
        audit_id = int(row[0]) if row else 0
        conn.execute(
            "UPDATE products SET status = ?, updated_at = ? WHERE product_id = ?;",
            (result.status.value, now_iso, result.product_id),
        )
        return audit_id

    def save_audit_result(self, result: AuditResult) -> int:
        with get_sqlite_connection(self.db_path) as conn:
            audit_id = self._execute_upsert_audit_result(conn, result)
            conn.commit()
            return audit_id

    def save_product_and_audit_result(self, product: Product, result: AuditResult) -> int:
        with get_sqlite_connection(self.db_path) as conn:
            self._execute_upsert_product(conn, product)
            audit_id = self._execute_upsert_audit_result(conn, result)
            conn.commit()
            return audit_id

    def get_audit_results_by_product_id(self, product_id: int) -> list[AuditResult]:
        sql = "SELECT * FROM audit_results WHERE product_id = ? ORDER BY audit_id DESC;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (product_id,))
            return [self._row_to_audit_result(row) for row in cursor.fetchall()]

    def get_latest_audit_result(self, product_id: int) -> AuditResult | None:
        sql = "SELECT * FROM audit_results WHERE product_id = ? ORDER BY audit_id DESC LIMIT 1;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (product_id,))
            row = cursor.fetchone()
            return self._row_to_audit_result(row) if row else None

    def get_latest_audit_results_for_ids(self, product_ids: Sequence[int]) -> dict[int, AuditResult]:
        results: dict[int, AuditResult] = {}
        with get_sqlite_connection(self.db_path) as conn:
            for start in range(0, len(product_ids), 900):
                chunk = product_ids[start:start + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM audit_results WHERE product_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                results.update({int(row["product_id"]): self._row_to_audit_result(row) for row in rows})
        return results


