from collections import defaultdict
from collections.abc import AsyncIterator
import logging
from typing import Any
import aiomysql

from config.settings import Settings, get_settings
from src.domain.entities import AuditStatus, Product
from src.domain.exceptions import DomainError

logger = logging.getLogger(__name__)


class MySQLReaderError(DomainError):
    """Raised when an error occurs during MySQL catalog reading."""
    pass


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
            raise MySQLReaderError(
                "MySQL credentials not configured. Please set MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD in .env"
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
            )
            logger.info("Connected to MySQL catalog at %s:%s/%s (READ-ONLY)", self.host, self.port, self.database)
        except Exception as e:
            logger.error("Failed to connect to MySQL database: %s", e)
            raise MySQLReaderError(f"Database connection error: {e}") from e

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
        exc_tb: Any | None,
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
        # First try exact match on base_id
        prod = await self._fetch_single_by_condition("p.base_id = %s", (clean_sku,))
        if prod:
            return prod
        # Fallback to ID if numeric
        if clean_sku.isdigit():
            return await self.fetch_product_by_id(int(clean_sku))
        return None

    async def fetch_product(self, identifier: str | int) -> Product | None:
        """Fetch product by either SKU (base_id), barcode, or product_id."""
        clean_id = str(identifier).strip()
        if clean_id.isdigit():
            # Try by product_id first
            prod = await self.fetch_product_by_id(int(clean_id))
            if prod:
                return prod

        # Try by shop_sku (base_id)
        prod = await self.fetch_product_by_sku(clean_id)
        if prod:
            return prod

        # Try by barcode
        return await self._fetch_single_by_condition("p.barcode = %s", (clean_id,))

    async def _fetch_single_by_condition(self, condition: str, params: tuple[Any, ...]) -> Product | None:
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
                        b.name as brand_name
                    FROM products p
                    LEFT JOIN brand b ON p.brand_id = b.id
                    WHERE {condition}
                    LIMIT 1;
                """
                await cur.execute(query, params)
                row = await cur.fetchone()
                if not row:
                    return None

                pid = row["product_id"]
                specs = await self._fetch_specs_for_ids(cur, [pid])
                partners = await self._fetch_partners_for_ids(cur, [pid])

                partner_info = partners.get(pid, {})
                title = (row["title"] or "").strip() or f"Product #{pid}"
                vendor_name = partner_info.get("vendor_name")
                brand_name = (row.get("brand_name") or "").strip() or None
                if (not vendor_name or "склад" in vendor_name.lower()) and brand_name:
                    vendor_name = brand_name

                return Product(
                    product_id=pid,
                    shop_sku=str(row["shop_sku"]).strip() if row["shop_sku"] else str(pid),
                    title=title,
                    barcode=str(row["barcode"]).strip() if row["barcode"] else None,
                    vendor_name=vendor_name,
                    vendor_sku=partner_info.get("vendor_sku"),
                    manufacturer_sku=None,
                    current_specs=specs.get(pid, {}),
                    status=AuditStatus.PENDING,
                )

    async def _fetch_specs_for_ids(
        self, cur: aiomysql.Cursor, product_ids: list[int]
    ) -> dict[int, dict[str, str]]:
        """Batch fetch specifications for given product IDs."""
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
        rows = await cur.fetchall()

        result: dict[int, dict[str, str]] = defaultdict(dict)
        for r in rows:
            pid = r["product_id"]
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
        self, cur: aiomysql.Cursor, product_ids: list[int]
    ) -> dict[int, dict[str, str | None]]:
        """Batch fetch partner/vendor information for given product IDs."""
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
            ORDER BY pp.enabled DESC, pp.id DESC
        """
        await cur.execute(query, tuple(product_ids))
        rows = await cur.fetchall()

        result: dict[int, dict[str, str | None]] = {}
        for r in rows:
            pid = r["product_id"]
            if pid not in result:
                v_name = (r["vendor_name"] or "").strip() or None
                v_sku = (r["vendor_sku"] or "").strip() or None
                result[pid] = {
                    "vendor_name": v_name,
                    "vendor_sku": v_sku,
                }

        return result

    async def count_products(self, category_id: int | None = None) -> int:
        """Count total products available in the database."""
        pool = self._ensure_connected()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._init_session(cur)
                if category_id is not None:
                    await cur.execute("SELECT COUNT(*) as cnt FROM products WHERE category_id = %s;", (category_id,))
                else:
                    await cur.execute("SELECT COUNT(*) as cnt FROM products;")
                row = await cur.fetchone()
                return int(row["cnt"]) if row else 0

    async def stream_products(
        self,
        batch_size: int = 500,
        limit: int | None = None,
        category_id: int | None = None,
    ) -> AsyncIterator[list[Product]]:
        """
        Stream products in batches using limit/offset with batch attribute fetching.
        Yields list[Product] per batch.
        """
        pool = self._ensure_connected()
        offset = 0
        total_yielded = 0

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._init_session(cur)

                while True:
                    cur_batch_size = batch_size
                    if limit is not None:
                        remaining = limit - total_yielded
                        if remaining <= 0:
                            break
                        cur_batch_size = min(batch_size, remaining)

                    where_clause = ""
                    params: list[Any] = []
                    if category_id is not None:
                        where_clause = "WHERE p.category_id = %s"
                        params.append(category_id)

                    query = f"""
                        SELECT 
                            p.id as product_id,
                            p.base_id as shop_sku,
                            p.barcode,
                            p.name as title,
                            b.name as brand_name
                        FROM products p
                        LEFT JOIN brand b ON p.brand_id = b.id
                        {where_clause}
                        ORDER BY p.id ASC
                        LIMIT %s OFFSET %s;
                    """
                    params.extend([cur_batch_size, offset])
                    await cur.execute(query, tuple(params))
                    rows = await cur.fetchall()

                    if not rows:
                        break

                    pids = [r["product_id"] for r in rows]
                    specs = await self._fetch_specs_for_ids(cur, pids)
                    partners = await self._fetch_partners_for_ids(cur, pids)

                    batch_products: list[Product] = []
                    for r in rows:
                        pid = r["product_id"]
                        title = (r["title"] or "").strip() or f"Product #{pid}"
                        partner_info = partners.get(pid, {})
                        vendor_name = partner_info.get("vendor_name")
                        brand_name = (r.get("brand_name") or "").strip() or None
                        if (not vendor_name or "склад" in vendor_name.lower()) and brand_name:
                            vendor_name = brand_name

                        prod = Product(
                            product_id=pid,
                            shop_sku=str(r["shop_sku"]).strip() if r["shop_sku"] else str(pid),
                            title=title,
                            barcode=str(r["barcode"]).strip() if r["barcode"] else None,
                            vendor_name=vendor_name,
                            vendor_sku=partner_info.get("vendor_sku"),
                            manufacturer_sku=None,
                            current_specs=specs.get(pid, {}),
                            status=AuditStatus.PENDING,
                        )
                        batch_products.append(prod)

                    yield batch_products
                    total_yielded += len(batch_products)
                    offset += len(rows)

                    if len(rows) < cur_batch_size:
                        break
