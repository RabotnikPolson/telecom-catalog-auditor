import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator, Sequence
from types import TracebackType
from typing import TypedDict

import aiomysql  # type: ignore[import-untyped]

from config.settings import Settings, get_settings
from src.domain.entities import AuditStatus, Product
from src.domain.exceptions import DomainError

logger = logging.getLogger(__name__)


class CategorySummaryDict(TypedDict):
    id: int
    name: str
    urlkey: str
    active_count: int
    total_count: int


class GroupSummaryDict(TypedDict):
    group_id: int
    group_name: str
    group_urlkey: str
    cat_id: int
    cat_name: str
    sub_id: int
    sub_name: str
    active_count: int
    total_count: int


class MySQLReaderError(DomainError):
    """Raised when an error occurs during MySQL catalog reading."""


class MySQLConfigurationError(MySQLReaderError):
    """Required connection settings are missing."""


class MySQLConnectionError(MySQLReaderError):
    """The catalog connection could not be opened or was lost."""


class MySQLQueryError(MySQLReaderError):
    """The catalog query could not be completed."""


class MySQLCatalogReader:
    """
    Read-only catalog reader directly connecting to the shop's MySQL database.
    
    Guarantees:
    - SET SESSION TRANSACTION READ ONLY (physical block of writes/alters)
    - SET SESSION TRANSACTION ISOLATION LEVEL READ UNCOMMITTED (zero lock contention)
    - Clean mapping to domain Product entities
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        connect_timeout: int = 10,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self.host = host or cfg.MYSQL_HOST
        self.port = port or cfg.MYSQL_PORT
        self.user = user or cfg.MYSQL_USER
        self.password = password or cfg.MYSQL_PASSWORD
        self.database = database or cfg.MYSQL_DATABASE
        self.connect_timeout = connect_timeout
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        """Initialize connection pool if not already active."""
        if self._pool is not None:
            return

        if not self.host or not self.user or not self.password:
            raise MySQLConfigurationError(
                "MySQL не настроен: необходимы MYSQL_HOST, MYSQL_USER и MYSQL_PASSWORD."
            )

        try:
            self._pool = await aiomysql.create_pool(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                db=self.database,
                charset="utf8mb4",
                autocommit=True,
                cursorclass=aiomysql.DictCursor,
                minsize=1,
                maxsize=5,
                connect_timeout=self.connect_timeout,
                pool_recycle=1800,
                init_command=(
                    "SET SESSION TRANSACTION READ ONLY; "
                    "SET SESSION TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;"
                ),
            )
            logger.info("Connected to MySQL catalog at %s:%s/%s (READ-ONLY)", self.host, self.port, self.database)
        except Exception as e:
            logger.error("Failed to connect to MySQL database: %s", e)
            raise MySQLConnectionError("Не удалось подключиться к MySQL.") from e

    async def close(self) -> None:
        """Close connection pool."""
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def __aenter__(self) -> "MySQLCatalogReader":
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    def _ensure_connected(self) -> aiomysql.Pool:
        if self._pool is None:
            raise MySQLReaderError("MySQLCatalogReader is not connected. Use 'async with reader:' or call 'await reader.connect()'")
        return self._pool

    async def _init_session(self, cur: aiomysql.Cursor) -> None:
        """Enforce strict read-only and uncommitted non-locking transactions."""
        await cur.execute("SET SESSION TRANSACTION READ ONLY;")
        await cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;")

    async def fetch_product_by_id(self, product_id: int) -> Product | None:
        """Fetch single product by products.id."""
        return await self._fetch_single_by_condition("p.id = %s", (product_id,))

    async def fetch_product_by_sku(self, shop_sku: str) -> Product | None:
        """Fetch single product by base_id (shop_sku)."""
        clean_sku = str(shop_sku).strip()
        prod = await self._fetch_single_by_condition("p.base_id = %s", (clean_sku,))
        if prod:
            return prod
        if clean_sku.isdigit():
            return await self.fetch_product_by_id(int(clean_sku))
        return None

    async def fetch_product(self, identifier: str | int) -> Product | None:
        """Fetch product by either SKU (base_id), barcode, or product_id."""
        clean_id = str(identifier).strip()
        if clean_id.isdigit():
            prod = await self.fetch_product_by_id(int(clean_id))
            if prod:
                return prod

        prod = await self._fetch_single_by_condition("p.base_id = %s", (clean_id,))
        if prod:
            return prod

        return await self._fetch_single_by_condition("p.barcode = %s", (clean_id,))

    async def fetch_brands_by_product_ids(
        self,
        product_ids: Sequence[int],
        batch_size: int = 500,
    ) -> dict[int, str | None]:
        unique_ids = tuple(dict.fromkeys(int(product_id) for product_id in product_ids))
        if not unique_ids:
            return {}
        placeholders = ",".join("%s" for _ in unique_ids)
        query = f"""
            SELECT p.id AS product_id, b.name AS brand_name
            FROM products p
            LEFT JOIN brand b ON p.brand_id = b.id
            WHERE p.id IN ({placeholders})
            ORDER BY p.id
        """
        pool = self._ensure_connected()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._init_session(cur)
                    await cur.execute(query, unique_ids)
                    brands: dict[int, str | None] = {}
                    while True:
                        rows = await cur.fetchmany(batch_size)
                        if not rows:
                            break
                        for row in rows:
                            raw_brand = row.get("brand_name")
                            brands[int(row["product_id"])] = (
                                str(raw_brand).strip() or None if raw_brand is not None else None
                            )
                    return brands
        except MySQLReaderError:
            raise
        except Exception as exc:
            raise MySQLQueryError("Не удалось получить бренды аудированных товаров.") from exc

    async def _fetch_single_by_condition(
        self, condition: str, params: Sequence[str | int]
    ) -> Product | None:
        pool = self._ensure_connected()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._init_session(cur)

                query = f"""
                    SELECT 
                        p.id as product_id,
                        p.base_id as shop_sku,
                        p.barcode,
                        p.name as title,
                        p.description as description,
                        b.name as brand_name
                    FROM products p
                    LEFT JOIN brand b ON p.brand_id = b.id
                    WHERE {condition}
                    LIMIT 1;
                """
                await cur.execute(query, tuple(params))
                row = await cur.fetchone()
                if not row:
                    return None

                pid = int(row["product_id"])
                specs = await self._fetch_specs_for_ids(cur, [pid])
                partners = await self._fetch_partners_for_ids(cur, [pid])
                img_counts = await self._fetch_images_for_ids(cur, [pid])

                partner_info = partners.get(pid, {})
                title = (row["title"] or "").strip() or f"Product #{pid}"
                vendor_name = partner_info.get("vendor_name")
                brand_name = (row.get("brand_name") or "").strip() or None

                return Product(
                    product_id=pid,
                    shop_sku=str(row["shop_sku"]).strip() if row["shop_sku"] else str(pid),
                    title=title,
                    barcode=str(row["barcode"]).strip() if row["barcode"] else None,
                    brand_name=brand_name,
                    vendor_name=vendor_name,
                    vendor_sku=partner_info.get("vendor_sku"),
                    manufacturer_sku=None,
                    description=row.get("description"),
                    images_count=img_counts.get(pid, 0),
                    current_specs=specs.get(pid, {}),
                    status=AuditStatus.PENDING,
                )

    async def _fetch_specs_for_ids(
        self, cur: aiomysql.Cursor, product_ids: list[int], chunk_size: int = 500
    ) -> dict[int, dict[str, str]]:
        """Batch fetch specifications for given product IDs using fetchmany."""
        if not product_ids:
            return {}

        format_strings = ",".join(["%s"] * len(product_ids))
        query = f"""
            SELECT 
                pa.product_id,
                a.name as attr_name,
                a.value as attr_value
            FROM product_attributes pa
            JOIN attributes a ON pa.attribute_id = a.id
            WHERE pa.product_id IN ({format_strings})
        """
        await cur.execute(query, tuple(product_ids))

        result: dict[int, dict[str, str]] = defaultdict(dict)
        while True:
            rows = await cur.fetchmany(chunk_size)
            if not rows:
                break
            for r in rows:
                pid = int(r["product_id"])
                name = (r["attr_name"] or "").strip()
                val = (r["attr_value"] or "").strip()
                if not name or not val:
                    continue

                if name in result[pid]:
                    result[pid][name] = f"{result[pid][name]}, {val}"
                else:
                    result[pid][name] = val

        return result

    async def _fetch_partners_for_ids(
        self, cur: aiomysql.Cursor, product_ids: list[int], chunk_size: int = 500
    ) -> dict[int, dict[str, str | None]]:
        """Batch fetch partner/vendor information for given product IDs using fetchmany."""
        if not product_ids:
            return {}

        format_strings = ",".join(["%s"] * len(product_ids))
        query = f"""
            SELECT 
                pp.product_id,
                part.name as vendor_name,
                pp.article_provider as vendor_sku,
                pp.enabled
            FROM partner_products pp
            LEFT JOIN partners part ON pp.partner_id = part.id
            WHERE pp.product_id IN ({format_strings})
            ORDER BY 
                CASE 
                    WHEN (INSTR(LOWER(part.name), 'vender1') > 0 OR INSTR(LOWER(part.name), 'al-style') > 0) AND pp.article_provider IS NOT NULL AND TRIM(pp.article_provider) != '' THEN 1 
                    ELSE 0 
                END DESC,
                pp.enabled DESC, 
                pp.id DESC
        """
        await cur.execute(query, tuple(product_ids))

        result: dict[int, dict[str, str | None]] = {}
        while True:
            rows = await cur.fetchmany(chunk_size)
            if not rows:
                break
            for r in rows:
                pid = int(r["product_id"])
                if pid not in result:
                    v_name = (r["vendor_name"] or "").strip() or None
                    v_sku = (r["vendor_sku"] or "").strip() or None
                    result[pid] = {
                        "vendor_name": v_name,
                        "vendor_sku": v_sku,
                    }

        return result

    async def _fetch_images_for_ids(
        self, cur: aiomysql.Cursor, product_ids: list[int], chunk_size: int = 500
    ) -> dict[int, int]:
        """Batch fetch images count from images table for given product IDs using fetchmany."""
        if not product_ids:
            return {}

        format_strings = ",".join(["%s"] * len(product_ids))
        query = f"""
            SELECT product_id, COUNT(*) as img_cnt
            FROM images
            WHERE product_id IN ({format_strings})
            GROUP BY product_id;
        """
        await cur.execute(query, tuple(product_ids))

        result: dict[int, int] = {}
        while True:
            rows = await cur.fetchmany(chunk_size)
            if not rows:
                break
            for r in rows:
                result[int(r["product_id"])] = int(r["img_cnt"])
        return result

    async def count_products(
        self,
        category_id: int | None = None,
        only_active: bool = True,
    ) -> int:
        """Count total products available in the database."""
        pool = self._ensure_connected()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._init_session(cur)
                if only_active:
                    if category_id is not None:
                        query = """
                            SELECT COUNT(DISTINCT p.id) as cnt
                            FROM products p
                            JOIN partner_products pp ON p.id = pp.product_id
                            WHERE pp.enabled = 1 AND p.category_id = %s;
                        """
                        await cur.execute(query, (category_id,))
                    else:
                        query = """
                            SELECT COUNT(DISTINCT pp.product_id) as cnt
                            FROM partner_products pp
                            WHERE pp.enabled = 1;
                        """
                        await cur.execute(query)
                else:
                    if category_id is not None:
                        await cur.execute("SELECT COUNT(*) as cnt FROM products WHERE category_id = %s;", (category_id,))
                    else:
                        await cur.execute("SELECT COUNT(*) as cnt FROM products;")
                row = await cur.fetchone()
                return int(row["cnt"]) if row else 0

    async def get_categories(self, only_active: bool = True) -> list[CategorySummaryDict]:
        """List main categories with active/total product counts."""
        pool = self._ensure_connected()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._init_session(cur)
                g = chr(96) + "groups" + chr(96)
                query = f"""
                    SELECT 
                        c.id, c.name, c.urlkey,
                        COUNT(DISTINCT CASE WHEN pp.enabled = 1 THEN p.id END) as active_count,
                        COUNT(DISTINCT p.id) as total_count
                    FROM categories c
                    LEFT JOIN sub_categories sc ON sc.categories_id = c.id
                    LEFT JOIN {g} g ON g.sub_categories_id = sc.id
                    LEFT JOIN products p ON p.category_id = g.id
                    LEFT JOIN partner_products pp ON p.id = pp.product_id
                    GROUP BY c.id, c.name, c.urlkey
                    {"HAVING active_count > 0" if only_active else ""}
                    ORDER BY active_count DESC;
                """
                await cur.execute(query)
                items: list[CategorySummaryDict] = []
                while True:
                    chunk = await cur.fetchmany(100)
                    if not chunk:
                        break
                    for r in chunk:
                        items.append(
                            {
                                "id": int(r["id"]),
                                "name": str(r["name"] or ""),
                                "urlkey": str(r["urlkey"] or ""),
                                "active_count": int(r["active_count"] or 0),
                                "total_count": int(r["total_count"] or 0),
                            }
                        )
                return items

    async def get_groups(
        self,
        main_category_id: int | None = None,
        search: str | None = None,
        only_active: bool = True,
    ) -> list[GroupSummaryDict]:
        """List product groups with active/total product counts."""
        pool = self._ensure_connected()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._init_session(cur)
                g = chr(96) + "groups" + chr(96)
                conditions = []
                params: list[str | int] = []
                if main_category_id is not None:
                    conditions.append("c.id = %s")
                    params.append(main_category_id)
                if search:
                    conditions.append("(g.name LIKE %s OR g.urlkey LIKE %s)")
                    params.extend([f"%{search}%", f"%{search}%"])

                where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
                query = f"""
                    SELECT 
                        g.id as group_id, g.name as group_name, g.urlkey as group_urlkey,
                        c.id as cat_id, c.name as cat_name,
                        sc.id as sub_id, sc.name as sub_name,
                        COUNT(DISTINCT CASE WHEN pp.enabled = 1 THEN p.id END) as active_count,
                        COUNT(DISTINCT p.id) as total_count
                    FROM {g} g
                    JOIN sub_categories sc ON g.sub_categories_id = sc.id
                    JOIN categories c ON sc.categories_id = c.id
                    LEFT JOIN products p ON p.category_id = g.id
                    LEFT JOIN partner_products pp ON p.id = pp.product_id
                    {where_clause}
                    GROUP BY g.id, g.name, g.urlkey, c.id, c.name, sc.id, sc.name
                    {"HAVING active_count > 0" if only_active else ""}
                    ORDER BY active_count DESC;
                """
                await cur.execute(query, tuple(params))
                items: list[GroupSummaryDict] = []
                while True:
                    chunk = await cur.fetchmany(200)
                    if not chunk:
                        break
                    for r in chunk:
                        items.append(
                            {
                                "group_id": int(r["group_id"]),
                                "group_name": str(r["group_name"] or ""),
                                "group_urlkey": str(r["group_urlkey"] or ""),
                                "cat_id": int(r["cat_id"]),
                                "cat_name": str(r["cat_name"] or ""),
                                "sub_id": int(r["sub_id"]),
                                "sub_name": str(r["sub_name"] or ""),
                                "active_count": int(r["active_count"] or 0),
                                "total_count": int(r["total_count"] or 0),
                            }
                        )
                return items

    async def stream_products(
        self,
        batch_size: int = 500,
        limit: int | None = None,
        offset: int = 0,
        last_seen_id: int = 0,
        category_id: int | None = None,
        main_category_id: int | None = None,
        only_active: bool = True,
    ) -> AsyncGenerator[list[Product]]:
        """Keep transport failures distinct from a successful empty selection."""
        if batch_size <= 0 or offset < 0 or (limit is not None and limit < 0):
            raise ValueError("batch_size must be positive; offset and limit must be nonnegative")
        stream = self._stream_products(
            batch_size, limit, offset, last_seen_id, category_id, main_category_id, only_active,
        )
        try:
            async for batch in stream:
                yield batch
        except MySQLReaderError:
            raise
        except (OSError, aiomysql.Error) as exc:
            code = exc.args[0] if exc.args else None
            if isinstance(exc, OSError) or code in (2003, 2006, 2013, 2055):
                raise MySQLConnectionError("Потеряно соединение с MySQL при чтении каталога.") from exc
            raise MySQLQueryError("Не удалось выполнить запрос каталога MySQL.") from exc
        finally:
            await stream.aclose()

    async def _stream_products(
        self,
        batch_size: int = 500,
        limit: int | None = None,
        offset: int = 0,
        last_seen_id: int = 0,
        category_id: int | None = None,
        main_category_id: int | None = None,
        only_active: bool = True,
    ) -> AsyncGenerator[list[Product]]:
        """
        Stream products in batches using O(N) Keyset Pagination (p.id > last_seen_id)
        with batch attribute fetching. Yields list[Product] per batch.
        """
        pool = self._ensure_connected()
        cursor_id = max(0, last_seen_id)
        initial_offset = max(0, offset) if cursor_id == 0 else 0
        total_yielded = 0

        while True:
            cur_batch_size = batch_size
            if limit is not None:
                remaining = limit - total_yielded
                if remaining <= 0:
                    break
                cur_batch_size = min(batch_size, remaining)

            where_conditions: list[str] = []
            params: list[str | int] = []

            if cursor_id > 0:
                where_conditions.append("p.id > %s")
                params.append(cursor_id)

            if only_active:
                where_conditions.append(
                    "EXISTS (SELECT 1 FROM partner_products pp WHERE pp.product_id = p.id AND pp.enabled = 1)"
                )

            if category_id is not None:
                where_conditions.append("p.category_id = %s")
                params.append(category_id)

            if main_category_id is not None:
                g = chr(96) + "groups" + chr(96)
                where_conditions.append(
                    f"p.category_id IN (SELECT g.id FROM {g} g JOIN sub_categories sc ON g.sub_categories_id = sc.id WHERE sc.categories_id = %s)"
                )
                params.append(main_category_id)

            where_clause = ""
            if where_conditions:
                where_clause = "WHERE " + " AND ".join(where_conditions)

            if initial_offset > 0:
                pagination_clause = "LIMIT %s OFFSET %s"
                params.extend([cur_batch_size, initial_offset])
                initial_offset = 0
            else:
                pagination_clause = "LIMIT %s"
                params.append(cur_batch_size)

            query = f"""
                SELECT 
                    p.id as product_id,
                    p.base_id as shop_sku,
                    p.barcode,
                    p.name as title,
                    p.description as description,
                    b.name as brand_name
                FROM products p
                LEFT JOIN brand b ON p.brand_id = b.id
                {where_clause}
                ORDER BY p.id ASC
                {pagination_clause};
            """

            async with asyncio.timeout(30), pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._init_session(cur)
                    await cur.execute(query, tuple(params))
                    rows = await cur.fetchmany(cur_batch_size)

                    if not rows:
                        break

                    pids = [int(r["product_id"]) for r in rows]
                    specs = await self._fetch_specs_for_ids(cur, pids, chunk_size=max(500, cur_batch_size * 10))
                    partners = await self._fetch_partners_for_ids(cur, pids, chunk_size=max(500, cur_batch_size * 4))
                    img_counts = await self._fetch_images_for_ids(cur, pids, chunk_size=max(500, cur_batch_size))

            cursor_id = int(rows[-1]["product_id"])
            batch_products: list[Product] = []
            for r in rows:
                pid = int(r["product_id"])
                title = (r["title"] or "").strip() or f"Product #{pid}"
                partner_info = partners.get(pid, {})
                vendor_name = partner_info.get("vendor_name")
                brand_name = (r.get("brand_name") or "").strip() or None

                prod = Product(
                    product_id=pid,
                    shop_sku=str(r["shop_sku"]).strip() if r["shop_sku"] else str(pid),
                    title=title,
                    barcode=str(r["barcode"]).strip() if r["barcode"] else None,
                    brand_name=brand_name,
                    vendor_name=vendor_name,
                    vendor_sku=partner_info.get("vendor_sku"),
                    manufacturer_sku=None,
                    description=r.get("description"),
                    images_count=img_counts.get(pid, 0),
                    current_specs=specs.get(pid, {}),
                    status=AuditStatus.PENDING,
                )
                batch_products.append(prod)

            yield batch_products
            total_yielded += len(batch_products)

            if len(rows) < cur_batch_size:
                break
