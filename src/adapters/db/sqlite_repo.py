from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Sequence, Union
from src.domain.entities import AuditStatus, Product
from .schema import get_sqlite_connection, init_db


class SQLiteProductRepository:

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = Path(db_path)
        init_db(self.db_path)

    def _row_to_product(self, row: sqlite3.Row) -> Product:
        specs_dict = json.loads(row["current_specs"]) if row["current_specs"] else {}
        created_at = (
            datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else datetime.now(timezone.utc)
        )
        updated_at = (
            datetime.fromisoformat(row["updated_at"])
            if row["updated_at"]
            else datetime.now(timezone.utc)
        )

        return Product(
            product_id=row["product_id"],
            title=row["title"],
            vendor_name=row["vendor_name"],
            vendor_sku=row["vendor_sku"],
            manufacturer_sku=row["manufacturer_sku"],
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
        sql = """
        INSERT INTO products (
            product_id, title, vendor_name, vendor_sku, manufacturer_sku,
            current_specs, content_hash, parent_sku, master_key, is_master,
            status, created_at, updated_at
        ) VALUES (
            :product_id, :title, :vendor_name, :vendor_sku, :manufacturer_sku,
            :current_specs, :content_hash, :parent_sku, :master_key, :is_master,
            :status, :created_at, :updated_at
        )
        ON CONFLICT(product_id) DO UPDATE SET
            title = excluded.title,
            vendor_name = excluded.vendor_name,
            vendor_sku = excluded.vendor_sku,
            manufacturer_sku = excluded.manufacturer_sku,
            current_specs = excluded.current_specs,
            content_hash = excluded.content_hash,
            parent_sku = excluded.parent_sku,
            master_key = excluded.master_key,
            is_master = excluded.is_master,
            status = excluded.status,
            updated_at = excluded.updated_at;
        """
        payload = {
            "product_id": product.product_id,
            "title": product.title,
            "vendor_name": product.vendor_name,
            "vendor_sku": product.vendor_sku,
            "manufacturer_sku": product.manufacturer_sku,
            "current_specs": json.dumps(product.current_specs, ensure_ascii=False),
            "content_hash": product.content_hash or product.recalculate_content_hash(),
            "parent_sku": product.parent_sku,
            "master_key": product.master_key,
            "is_master": 1 if product.is_master else 0,
            "status": product.status.value,
            "created_at": product.created_at.isoformat(),
            "updated_at": product.updated_at.isoformat(),
        }

        with get_sqlite_connection(self.db_path) as conn:
            conn.execute(sql, payload)
            conn.commit()

        return product

    def batch_upsert(self, products: Sequence[Product]) -> int:
        if not products:
            return 0

        sql = """
        INSERT INTO products (
            product_id, title, vendor_name, vendor_sku, manufacturer_sku,
            current_specs, content_hash, parent_sku, master_key, is_master,
            status, created_at, updated_at
        ) VALUES (
            :product_id, :title, :vendor_name, :vendor_sku, :manufacturer_sku,
            :current_specs, :content_hash, :parent_sku, :master_key, :is_master,
            :status, :created_at, :updated_at
        )
        ON CONFLICT(product_id) DO UPDATE SET
            title = excluded.title,
            vendor_name = excluded.vendor_name,
            vendor_sku = excluded.vendor_sku,
            manufacturer_sku = excluded.manufacturer_sku,
            current_specs = excluded.current_specs,
            content_hash = excluded.content_hash,
            parent_sku = excluded.parent_sku,
            master_key = excluded.master_key,
            is_master = excluded.is_master,
            status = excluded.status,
            updated_at = excluded.updated_at;
        """

        records = [
            {
                "product_id": p.product_id,
                "title": p.title,
                "vendor_name": p.vendor_name,
                "vendor_sku": p.vendor_sku,
                "manufacturer_sku": p.manufacturer_sku,
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

    def get_by_content_hash(self, content_hash: str) -> Product | None:
        sql = "SELECT * FROM products WHERE content_hash = ? LIMIT 1;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (content_hash,))
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

    def count_products(self) -> int:
        sql = "SELECT COUNT(*) FROM products;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql)
            return cursor.fetchone()[0]

    def save_url_cache(
        self,
        query_hash: str,
        resolved_url: str,
        source_type: str,
        expires_at: str | None = None
    ) -> None:
        sql = """
        INSERT INTO url_cache (query_hash, resolved_url, source_type, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(query_hash) DO UPDATE SET
            resolved_url = excluded.resolved_url,
            source_type = excluded.source_type,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at;
        """
        now = datetime.now(timezone.utc).isoformat()
        with get_sqlite_connection(self.db_path) as conn:
            conn.execute(sql, (query_hash, resolved_url, source_type, now, expires_at))
            conn.commit()

    def get_url_cache(self, query_hash: str) -> str | None:
        sql = "SELECT resolved_url FROM url_cache WHERE query_hash = ?;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (query_hash,))
            row = cursor.fetchone()
            return row["resolved_url"] if row else None

    def save_specs_cache(
        self,
        resolved_url: str,
        specs_json: str,
        raw_html_hash: str | None = None,
        status_code: int = 200
    ) -> None:
        sql = """
        INSERT INTO specs_cache (resolved_url, specs_json, fetched_at, raw_html_hash, status_code)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(resolved_url) DO UPDATE SET
            specs_json = excluded.specs_json,
            fetched_at = excluded.fetched_at,
            raw_html_hash = excluded.raw_html_hash,
            status_code = excluded.status_code;
        """
        now = datetime.now(timezone.utc).isoformat()
        with get_sqlite_connection(self.db_path) as conn:
            conn.execute(sql, (resolved_url, specs_json, now, raw_html_hash, status_code))
            conn.commit()

    def get_specs_cache(self, resolved_url: str) -> str | None:
        sql = "SELECT specs_json FROM specs_cache WHERE resolved_url = ?;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (resolved_url,))
            row = cursor.fetchone()
            return row["specs_json"] if row else None

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
        now = datetime.now(timezone.utc).isoformat()
        with get_sqlite_connection(self.db_path) as conn:
            conn.execute(
                sql,
                (category_key, last_page, total_pages, 1 if is_completed else 0, now),
            )
            conn.commit()

    def get_crawler_state(self, category_key: str) -> dict | None:
        sql = "SELECT * FROM crawler_state WHERE category_key = ?;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql, (category_key,))
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "category_key": row["category_key"],
                "last_page": row["last_page"],
                "total_pages": row["total_pages"],
                "is_completed": bool(row["is_completed"]),
                "updated_at": row["updated_at"],
            }

    def reset_crawler_state(self, category_key: str | None = None) -> None:
        with get_sqlite_connection(self.db_path) as conn:
            if category_key:
                conn.execute("DELETE FROM crawler_state WHERE category_key = ?;", (category_key,))
            else:
                conn.execute("DELETE FROM crawler_state;")
            conn.commit()

    def get_all_crawler_states(self) -> list[dict]:
        sql = "SELECT * FROM crawler_state ORDER BY updated_at DESC;"
        with get_sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(sql)
            return [
                {
                    "category_key": row["category_key"],
                    "last_page": row["last_page"],
                    "total_pages": row["total_pages"],
                    "is_completed": bool(row["is_completed"]),
                    "updated_at": row["updated_at"],
                }
                for row in cursor.fetchall()
            ]

