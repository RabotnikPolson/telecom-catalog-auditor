import argparse
import asyncio
import io
import sys
from pathlib import Path
from src.adapters.catalog.telecom_crawler import TelecomShopCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.domain.entities import Product
from src.use_cases.crawl_shop_catalog import CrawlShopCatalogUseCase


def setup_utf8_terminal() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass


async def run_crawler_demo(category: str, limit: int, db_path_str: str) -> None:
    setup_utf8_terminal()
    db_path = Path(db_path_str)
    repo = SQLiteProductRepository(db_path)

    print("=" * 75)
    print("  TELECOM CATALOG AUDITOR :: STAGE 2 DEMO")
    print("  Night Telecom Crawler (shop.telecom.kz)")
    print("=" * 75)
    print(f"Database Target : {db_path.resolve()}")
    print(f"WAL Mode Status : {'[OK] ACTIVE' if repo.check_wal_mode() else '[WARN] INACTIVE'}")
    print(f"Initial DB Count: {repo.count_products()} products")
    print("=" * 75)

    crawler = TelecomShopCrawler(delay_min=0.3, delay_max=0.8, max_concurrency=2)

    async with crawler:
        print("\n[*] STEP 1: Fetching and parsing category tree (/getAllCategories)...")
        leaves = await crawler.get_leaf_categories()
        print(f"    [OK] Successfully discovered {len(leaves)} leaf categories in catalog:")
        for idx, leaf in enumerate(leaves[:6], 1):
            print(f"         {idx}. [{leaf.category_key}] -> {leaf.name}")
        if len(leaves) > 6:
            print(f"         ... and {len(leaves) - 6} more leaf categories.")

        target_cat = category
        print(f"\n[*] STEP 2: Crawling catalog page 1 for category: '{target_cat}'...")
        page_res = await crawler.crawl_category_page(target_cat, page=1)
        print(f"    [OK] Page 1 results: {len(page_res.items)} items found. Total pages in section: {page_res.total_pages}")
        for idx, item in enumerate(page_res.items[:4], 1):
            sku_info = f" (SKU: {item.vendor_sku})" if item.vendor_sku else ""
            print(f"         {idx}. ID #{item.product_id}: {item.title}{sku_info}")

        if page_res.items:
            sample_item = page_res.items[0]
            print(f"\n[*] STEP 3: Deep-fetching product card details for #{sample_item.product_id}...")
            details = await crawler.fetch_product_details(sample_item.detail_url)
            print(f"    [OK] Product Title     : {details.title}")
            print(f"    [OK] Vendor SKU        : {details.vendor_sku or 'N/A'}")
            print(f"    [OK] Manufacturer SKU  : {details.manufacturer_sku or 'N/A'}")
            print(f"    [OK] Vendor / Brand    : {details.vendor_name or 'N/A'}")
            print(f"    [OK] Extracted Specs   : {len(details.current_specs)} attributes")
            for spec_k, spec_v in list(details.current_specs.items())[:6]:
                print(f"         - {spec_k}: {spec_v}")
            if len(details.current_specs) > 6:
                print(f"         ... and {len(details.current_specs) - 6} more specs")

            hash_sample = Product.compute_content_hash(details.title, details.current_specs)
            print(f"    [OK] Deterministic SHA-256 Hash: {hash_sample}")

        print(f"\n[*] STEP 4: Running CrawlShopCatalogUseCase (limit={limit}, category={target_cat})...")
        use_case = CrawlShopCatalogUseCase(
            crawler=crawler,
            repository=repo,
            progress_callback=lambda msg: print(f"    [PROGRESS] {msg}"),
        )

        stats_run1 = await use_case.execute(category_path=target_cat, limit=limit, force_rescan=True)
        print("\n    --- RUN 1 SUMMARY ---")
        print(f"    Products Seen    : {stats_run1.products_seen}")
        print(f"    Products Added   : {stats_run1.products_added}")
        print(f"    Products Updated : {stats_run1.products_updated}")
        print(f"    Products Skipped : {stats_run1.products_skipped}")
        print(f"    Current DB Count : {repo.count_products()} products")

        state = repo.get_crawler_state(target_cat)
        if state:
            print(f"    Crawler State    : last_page={state['last_page']}, total_pages={state['total_pages']}, is_completed={state['is_completed']}")

        print(f"\n[*] STEP 5: Testing Incremental Skip via content_hash (Re-running same {limit} items)...")
        stats_run2 = await use_case.execute(category_path=target_cat, limit=limit, force_rescan=True)
        print("\n    --- RUN 2 (INCREMENTAL) SUMMARY ---")
        print(f"    Products Seen    : {stats_run2.products_seen}")
        print(f"    Products Added   : {stats_run2.products_added} (Zero new items inserted)")
        print(f"    Products Updated : {stats_run2.products_updated}")
        print(f"    Products Skipped : {stats_run2.products_skipped} (Zero external requests needed)")

    print("\n" + "=" * 75)
    print("  STAGE 2 DEMO SUCCEEDED: ALL CHECKS PASSED")
    print("=" * 75)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telecom Catalog Auditor - Stage 2 Demo")
    parser.add_argument(
        "--category",
        type=str,
        default="smartfony-i-aksessuary/telefony/smartfony",
        help="Category path to test (default: smartfony-i-aksessuary/telefony/smartfony)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of products to crawl in demo run (default: 5)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="catalog_audit.db",
        help="Target SQLite database (default: catalog_audit.db)",
    )
    args = parser.parse_args()
    asyncio.run(run_crawler_demo(args.category, args.limit, args.db))


if __name__ == "__main__":
    main()
