import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Callable, Optional
from src.adapters.catalog.mysql_reader import MySQLCatalogReader
from src.adapters.catalog.telecom_crawler import LeafCategory, TelecomShopCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.domain.entities import AuditStatus, Product

logger = logging.getLogger(__name__)


@dataclass
class CrawlRunStats:
    categories_processed: int = 0
    pages_crawled: int = 0
    products_seen: int = 0
    products_added: int = 0
    products_updated: int = 0
    products_skipped: int = 0
    errors: list[str] = field(default_factory=list)


class CrawlShopCatalogUseCase:

    def __init__(
        self,
        crawler: Optional[TelecomShopCrawler] = None,
        repository: Optional[SQLiteProductRepository] = None,
        mysql_reader: Optional[MySQLCatalogReader] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.crawler = crawler
        self.repo = repository
        self.mysql_reader = mysql_reader
        self.progress_callback = progress_callback

    def _notify(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)
        logger.info(message)

    async def sync_from_mysql(
        self,
        limit: int | None = None,
        batch_size: int = 500,
        category_id: int | None = None,
    ) -> CrawlRunStats:
        """Fast catalog ingestion directly from shop MySQL database."""
        if not self.mysql_reader:
            raise ValueError("MySQLCatalogReader must be provided for MySQL sync")
        if not self.repo:
            raise ValueError("SQLiteProductRepository must be provided for MySQL sync")

        stats = CrawlRunStats()
        self._notify("Starting fast catalog sync directly from shop MySQL (READ-ONLY)...")

        async with self.mysql_reader:
            total_db = await self.mysql_reader.count_products(category_id=category_id)
            target_limit = min(limit, total_db) if limit is not None else total_db
            self._notify(f"Total available products in MySQL: {total_db} (Target limit: {target_limit})")

            async for batch in self.mysql_reader.stream_products(
                batch_size=batch_size, limit=limit, category_id=category_id
            ):
                stats.products_seen += len(batch)
                inserted = self.repo.batch_upsert(batch)
                stats.products_added += inserted
                self._notify(
                    f"Sync progress: {stats.products_seen}/{target_limit} products processed ({inserted} upserted into local DB)..."
                )

        self._notify(f"MySQL catalog sync finished: {stats.products_seen} products saved.")
        return stats

    async def execute(
        self,
        category_path: Optional[str] = None,
        limit: Optional[int] = None,
        force_rescan: bool = False,
        source: str = "mysql",
    ) -> CrawlRunStats:
        if source == "mysql" and self.mysql_reader:
            category_id = int(category_path) if category_path and category_path.isdigit() else None
            return await self.sync_from_mysql(limit=limit, category_id=category_id)

        if not self.crawler:
            raise ValueError("TelecomShopCrawler must be provided for web crawler mode")
        if not self.repo:
            raise ValueError("SQLiteProductRepository must be provided")

        stats = CrawlRunStats()

        target_categories: list[LeafCategory] = []
        if category_path:
            clean_cat = category_path.strip("/")
            target_categories.append(
                LeafCategory(
                    category_key=clean_cat,
                    name=clean_cat.split("/")[-1],
                    urlkey=clean_cat.split("/")[-1],
                    path=clean_cat,
                )
            )
        else:
            self._notify("Fetching categories tree from shop.telecom.kz...")
            target_categories = await self.crawler.get_leaf_categories()
            self._notify(f"Discovered {len(target_categories)} leaf categories.")

        for category in target_categories:
            if limit is not None and stats.products_seen >= limit:
                break

            await self._crawl_category(category, stats, limit=limit, force_rescan=force_rescan)
            stats.categories_processed += 1

        return stats

    async def _crawl_category(
        self,
        category: LeafCategory,
        stats: CrawlRunStats,
        limit: Optional[int] = None,
        force_rescan: bool = False,
    ) -> None:
        cat_key = category.category_key
        state = self.repo.get_crawler_state(cat_key)

        start_page = 1
        if state and not force_rescan:
            if state["is_completed"]:
                self._notify(f"Category '{cat_key}' already completed in previous run. Skipping.")
                return
            start_page = state["last_page"]
            self._notify(f"Resuming category '{cat_key}' from page {start_page}.")
        else:
            self._notify(f"Starting crawl for category '{cat_key}'.")

        current_page = start_page
        total_pages = 1

        while True:
            if limit is not None and stats.products_seen >= limit:
                break

            try:
                page_result = await self.crawler.crawl_category_page(cat_key, page=current_page)
            except Exception as exc:
                err_msg = f"Failed to crawl page {current_page} of '{cat_key}': {exc}"
                stats.errors.append(err_msg)
                logger.error(err_msg)
                break

            stats.pages_crawled += 1
            total_pages = page_result.total_pages

            if not page_result.items:
                self.repo.update_crawler_state(
                    cat_key,
                    last_page=current_page,
                    total_pages=total_pages,
                    is_completed=True,
                )
                break

            products_to_upsert: list[Product] = []

            for preview in page_result.items:
                if limit is not None and stats.products_seen >= limit:
                    break

                stats.products_seen += 1
                try:
                    product_obj, is_updated, is_skipped = await self._process_product(preview)
                    if is_skipped:
                        stats.products_skipped += 1
                    elif is_updated:
                        stats.products_updated += 1
                        products_to_upsert.append(product_obj)
                    else:
                        stats.products_added += 1
                        products_to_upsert.append(product_obj)
                except Exception as exc:
                    err_msg = f"Error processing product #{preview.product_id}: {exc}"
                    stats.errors.append(err_msg)
                    logger.error(err_msg)

            if products_to_upsert:
                self.repo.batch_upsert(products_to_upsert)

            is_completed = current_page >= total_pages or not page_result.has_next
            self.repo.update_crawler_state(
                cat_key,
                last_page=current_page,
                total_pages=total_pages,
                is_completed=is_completed,
            )

            self._notify(
                f"[{cat_key}] Page {current_page}/{total_pages} processed. "
                f"Batch: {len(products_to_upsert)} saved. Total seen: {stats.products_seen}"
            )

            if is_completed or not page_result.has_next:
                break

            current_page += 1

    async def _process_product(
        self, preview
    ) -> tuple[Optional[Product], bool, bool]:
        existing = self.repo.get_product_by_id(preview.product_id)

        try:
            details = await self.crawler.fetch_product_details(preview.detail_url)
        except Exception:
            details = None

        title = (details.title if details and details.title else preview.title).strip()
        if not title:
            title = f"Product #{preview.product_id}"

        specs = details.current_specs if details else {}
        shop_sku = details.shop_sku if details else None
        vendor_sku = details.vendor_sku if details and details.vendor_sku else preview.vendor_sku
        manufacturer_sku = details.manufacturer_sku if details else None
        vendor_name = details.vendor_name if details else None
        barcode = details.barcode if details else None

        calculated_hash = Product.compute_content_hash(title, specs)

        if existing is not None:
            if existing.status == AuditStatus.VERIFIED and existing.content_hash == calculated_hash:
                return existing, False, True

            if existing.content_hash == calculated_hash:
                return existing, False, True

            existing.title = title
            existing.current_specs = specs
            existing.shop_sku = shop_sku or existing.shop_sku
            existing.barcode = barcode or existing.barcode
            existing.vendor_name = vendor_name or existing.vendor_name
            existing.vendor_sku = vendor_sku or existing.vendor_sku
            existing.manufacturer_sku = manufacturer_sku or existing.manufacturer_sku
            existing.content_hash = calculated_hash
            existing.status = AuditStatus.PENDING
            existing.updated_at = datetime.now(timezone.utc)
            return existing, True, False

        new_product = Product(
            product_id=preview.product_id,
            shop_sku=shop_sku,
            title=title,
            barcode=barcode,
            vendor_name=vendor_name,
            vendor_sku=vendor_sku,
            manufacturer_sku=manufacturer_sku,
            current_specs=specs,
            content_hash=calculated_hash,
            status=AuditStatus.PENDING,
        )
        return new_product, False, False
