import argparse
import asyncio
import io
import os
from pathlib import Path
import sys
import warnings

warnings.filterwarnings("ignore", message=".*urllib3.*or chardet.*doesn't match a supported version.*")
try:
    from requests.exceptions import RequestsDependencyWarning
    warnings.filterwarnings("ignore", category=RequestsDependencyWarning)
except ImportError:
    pass

from config.settings import get_settings
from src.adapters.catalog.telecom_crawler import TelecomShopCrawler
from src.adapters.crawler.web_crawler import WebCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.llm.gemini_judge import GeminiJudge
from src.adapters.search.serper_client import SerperClient
from src.adapters.search.vendor_direct import VendorDirectResolver
from src.domain.entities import AuditStatus, Product
from src.use_cases.audit_product import AuditProductUseCase
from src.use_cases.crawl_shop_catalog import CrawlShopCatalogUseCase
from src.use_cases.resolve_reference import ResolveReferenceUseCase


def setup_utf8_terminal() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass


async def run_crawl_command(args: argparse.Namespace) -> int:
    setup_utf8_terminal()

    db_path = Path(args.db)
    repo = SQLiteProductRepository(db_path)

    if getattr(args, "reset_state", False):
        repo.reset_crawler_state(args.category)
        print(f"[*] Crawler state reset for: {args.category or 'ALL'}")

    crawler = TelecomShopCrawler(
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        max_concurrency=args.concurrency,
    )

    def on_progress(msg: str) -> None:
        print(f"  [+] {msg}")

    print("=" * 70)
    print("TELECOM CATALOG AUDITOR :: STAGE 2 NIGHT CRAWLER")
    print("=" * 70)
    print(f"Target DB     : {db_path.resolve()}")
    print(f"WAL Mode      : {'ACTIVE' if repo.check_wal_mode() else 'INACTIVE'}")
    print(f"Category      : {args.category or 'ALL LEAF CATEGORIES'}")
    print(f"Product Limit : {args.limit or 'NO LIMIT'}")
    print(f"Rate Limiter  : {args.delay_min}s - {args.delay_max}s jitter (Concurrency: {args.concurrency})")
    print(f"Force Rescan  : {args.force}")
    print("-" * 70)

    use_case = CrawlShopCatalogUseCase(
        crawler=crawler,
        repository=repo,
        progress_callback=on_progress,
    )

    async with crawler:
        stats = await use_case.execute(
            category_path=args.category,
            limit=args.limit,
            force_rescan=args.force,
        )

    print("-" * 70)
    print("CRAWL SESSION COMPLETED")
    print(f"Categories Processed : {stats.categories_processed}")
    print(f"Pages Crawled        : {stats.pages_crawled}")
    print(f"Total Products Seen  : {stats.products_seen}")
    print(f"Products Added (NEW) : {stats.products_added}")
    print(f"Products Updated     : {stats.products_updated}")
    print(f"Products Skipped     : {stats.products_skipped}")
    print(f"Errors Encountered   : {len(stats.errors)}")
    print(f"Total Products in DB : {repo.count_products()}")
    print("=" * 70)

    if stats.errors:
        for err in stats.errors[:5]:
            print(f"  [ERROR] {err}")
        if len(stats.errors) > 5:
            print(f"  ... and {len(stats.errors) - 5} more errors")

    return 0


def _extract_target_keys(args: argparse.Namespace) -> list[str]:
    raw_keys: list[str] = []

    sku_val = getattr(args, "sku", None)
    if sku_val:
        if isinstance(sku_val, (list, tuple)):
            for item in sku_val:
                raw_keys.extend([s.strip() for s in str(item).split(",") if s.strip()])
        else:
            raw_keys.extend([s.strip() for s in str(sku_val).split(",") if s.strip()])

    pid_val = getattr(args, "product_id", None)
    if pid_val:
        if isinstance(pid_val, (list, tuple)):
            for item in pid_val:
                raw_keys.extend([s.strip() for s in str(item).split(",") if s.strip()])
        else:
            raw_keys.extend([s.strip() for s in str(pid_val).split(",") if s.strip()])

    url_val = getattr(args, "url", None)
    if url_val:
        if isinstance(url_val, (list, tuple)):
            for item in url_val:
                raw_keys.extend([s.strip() for s in str(item).split(",") if s.strip()])
        else:
            raw_keys.extend([s.strip() for s in str(url_val).split(",") if s.strip()])

    sku_file = getattr(args, "sku_file", None)
    if sku_file:
        file_path = Path(sku_file)
        if file_path.exists():
            for line in file_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    raw_keys.extend([s.strip() for s in line.split(",") if s.strip()])
        else:
            print(f"[!] Warning: SKU file '{sku_file}' not found.")

    seen = set()
    unique_keys = []
    for k in raw_keys:
        if k not in seen:
            seen.add(k)
            unique_keys.append(k)
    return unique_keys


async def run_resolve_command(args: argparse.Namespace) -> int:
    setup_utf8_terminal()
    db_path = Path(args.db)
    repo = SQLiteProductRepository(db_path)
    settings = get_settings()

    vendor_resolver = VendorDirectResolver()
    serper_client = SerperClient(api_key=settings.SERPER_API_KEY)
    judge = GeminiJudge()
    use_case = ResolveReferenceUseCase(
        repository=repo,
        vendor_resolver=vendor_resolver,
        serper_client=serper_client,
        verify_direct_urls=args.verify,
        judge=judge,
    )

    print("=" * 75)
    print("TELECOM CATALOG AUDITOR :: STAGE 3 CASCADE REFERENCE RESOLVER")
    print("=" * 75)
    print(f"Target DB     : {db_path.resolve()}")
    print(f"Serper API Key: {'[OK] CONFIGURED' if settings.SERPER_API_KEY else '[WARN] MOCK / OFFLINE MODE'}")
    print(f"Verify URLs   : {args.verify}")
    print("-" * 75)

    target_keys = _extract_target_keys(args)
    products: list[Product] = []

    if target_keys:
        print(f"[*] Live syncing {len(target_keys)} product(s) from shop.telecom.kz...")
        async with TelecomShopCrawler(delay_min=0.1, delay_max=0.3) as shop_crawler:
            for key in target_keys:
                p = None
                try:
                    details = await shop_crawler.fetch_product_on_the_fly(str(key))
                    if details:
                        p = Product(
                            product_id=details.product_id,
                            shop_sku=details.shop_sku,
                            title=details.title,
                            url=details.detail_url,
                            vendor_name=details.vendor_name,
                            vendor_sku=details.vendor_sku,
                            manufacturer_sku=details.manufacturer_sku,
                            barcode=details.barcode,
                            current_specs=details.current_specs,
                        )
                        repo.save_product(p)
                        print(f"  [+] Live sync OK: SKU/ID '{key}' -> ID #{p.product_id} | Shop SKU: {p.shop_sku or 'N/A'} | {p.title[:50]}")
                except Exception:
                    pass

                if not p:
                    p = repo.find_product(key)
                    if p:
                        print(f"  [*] Using cached record from local DB: ID #{p.product_id} ({p.title[:50]})")

                if p:
                    products.append(p)
                else:
                    print(f"  [!] Warning: Product '{key}' not found on shop.telecom.kz or in DB (skipping).")

        if not products:
            print(f"[ERROR] None of the specified products {target_keys} were found.")
            return 1
    else:
        products = repo.get_auditable_products(limit=args.limit or 10)

    if not products:
        print("[*] No products found in database to resolve.")
        return 0

    found_count = 0
    cached_count = 0
    skipped_count = 0

    for idx, prod in enumerate(products, 1):
        ref = await use_case.execute(prod)
        if ref.status == "FOUND":
            found_count += 1
            if ref.is_cached:
                cached_count += 1
        elif ref.status == "INSUFFICIENT_DATA":
            skipped_count += 1

        print(f"[{idx}/{len(products)}] ID #{prod.product_id} | Shop SKU: {prod.shop_sku or 'N/A'} | {prod.title[:50]}")
        print(f"      Vendor: {prod.vendor_name or 'N/A'} | Vendor SKU: {prod.vendor_sku or 'N/A'} | Barcode: {prod.barcode or 'N/A'}")
        print(f"      Status: {ref.status} | Scenario: {ref.scenario_applied} | Source: {ref.source_type}")
        print(f"      URL   : {ref.reference_url or 'NONE'}")
        if ref.kaspi_code:
            print(f"      Kaspi : {ref.kaspi_code}")
        print(f"      Cached: {'HIT' if ref.is_cached else 'MISS'}")
        print()

    print("-" * 75)
    print(f"Total Processed: {len(products)} | Found: {found_count} (Cached: {cached_count}) | Skipped: {skipped_count}")
    print("=" * 75)
    return 0


async def run_live_test_command(args: argparse.Namespace) -> int:
    setup_utf8_terminal()
    db_path = Path(args.db)
    repo = SQLiteProductRepository(db_path)
    settings = get_settings()

    start_id = args.start_id or 74615
    limit = args.limit or 10
    product_ids = [start_id + i for i in range(limit)]

    print("=" * 80)
    print("TELECOM CATALOG AUDITOR :: LIVE PIPELINE TEST (STAGES 1, 2, 3)")
    print("=" * 80)
    print(f"Target DB     : {db_path.resolve()}")
    print(f"WAL Mode      : {'[OK] ACTIVE' if repo.check_wal_mode() else '[WARN] INACTIVE'}")
    print(f"Serper API Key: {'[OK] CONFIGURED' if settings.SERPER_API_KEY else '[WARN] MOCK / OFFLINE'}")
    print(f"Products Count: {len(product_ids)} (IDs: #{product_ids[0]} - #{product_ids[-1]})")
    print("-" * 80)

    crawler = TelecomShopCrawler(delay_min=0.2, delay_max=0.5, max_concurrency=2)
    vendor_resolver = VendorDirectResolver()
    serper_client = SerperClient(api_key=settings.SERPER_API_KEY)
    use_case = ResolveReferenceUseCase(
        repository=repo,
        vendor_resolver=vendor_resolver,
        serper_client=serper_client,
        verify_direct_urls=True,
    )

    saved_products: list[Product] = []

    print("[STAGE 2: LIVE CRAWL] Fetching real product cards from shop.telecom.kz...")
    print("-" * 80)

    async with crawler:
        for idx, pid in enumerate(product_ids, 1):
            url = f"https://shop.telecom.kz/product/{pid}"
            try:
                details = await crawler.fetch_product_details(url)
                product = Product(
                    product_id=details.product_id,
                    shop_sku=details.shop_sku,
                    title=details.title,
                    url=details.detail_url,
                    vendor_name=details.vendor_name,
                    vendor_sku=details.vendor_sku,
                    manufacturer_sku=details.manufacturer_sku,
                    barcode=details.barcode,
                    current_specs=details.current_specs,
                )
                repo.save_product(product)
                saved_products.append(product)

                print(f"[{idx:02d}/{len(product_ids)}] ID #{pid} | Shop SKU: {product.shop_sku or 'N/A'} | {product.title[:45]}")
                print(f"      Vendor: {product.vendor_name or 'N/A'} | Vendor SKU: {product.vendor_sku or 'N/A'} | Barcode: {product.barcode or 'N/A'}")
                print(f"      Specs: {len(product.current_specs)} items | Hash: {product.content_hash[:16]}...")
                print()
            except Exception as e:
                print(f"[{idx:02d}/{len(product_ids)}] [ERROR] ID #{pid}: {e}\n")

    print("=" * 80)
    print("[STAGE 3: REFERENCE RESOLUTION] Resolving reference URLs with Al-Style deep card...")
    print("=" * 80)

    direct_found = 0
    search_found = 0
    cached_hits = 0
    skipped_count = 0

    for idx, prod in enumerate(saved_products, 1):
        ref = await use_case.execute(prod)
        if ref.source_type == "VENDOR_DIRECT":
            direct_found += 1
        elif ref.source_type in ("BARCODE_SEARCH", "PROVIDER_SKU_SEARCH", "MODEL_SEARCH"):
            search_found += 1
        elif ref.source_type == "CACHE":
            cached_hits += 1
        elif ref.status == "INSUFFICIENT_DATA":
            skipped_count += 1

        print(f"[{idx:02d}/{len(saved_products)}] ID #{prod.product_id} | Shop SKU: {prod.shop_sku or 'N/A'} | {prod.title[:45]}")
        print(f"      Vendor: {prod.vendor_name or 'N/A'} | Vendor SKU: {prod.vendor_sku or 'N/A'} | Barcode: {prod.barcode or 'N/A'}")
        print(f"      Scenario: {ref.scenario_applied} | Source: {ref.source_type} | Status: {ref.status}")
        print(f"      Ref URL : {ref.reference_url or 'NONE'}")
        if ref.kaspi_code:
            print(f"      Kaspi   : {ref.kaspi_code} (Found on Al-Style!)")
        print(f"      Cache   : {'HIT' if ref.is_cached else 'MISS'}")
        print()

    print("-" * 80)
    print(f"SUMMARY: Processed: {len(saved_products)} | Vendor Direct: {direct_found} | Search/Cache: {search_found + cached_hits} | Skipped: {skipped_count}")
    print("=" * 80)
    return 0


async def run_audit_command(args: argparse.Namespace) -> int:
    setup_utf8_terminal()

    db_path = Path(args.db)
    repo = SQLiteProductRepository(db_path)

    target_keys = _extract_target_keys(args)

    if target_keys:
        products = []
        print(f"[*] Live syncing {len(target_keys)} product(s) from shop.telecom.kz...")
        async with TelecomShopCrawler(delay_min=0.1, delay_max=0.3) as shop_crawler:
            for key in target_keys:
                target = None
                try:
                    details = await shop_crawler.fetch_product_on_the_fly(str(key))
                    if details:
                        target = Product(
                            product_id=details.product_id,
                            shop_sku=details.shop_sku,
                            title=details.title,
                            url=details.detail_url,
                            vendor_name=details.vendor_name,
                            vendor_sku=details.vendor_sku,
                            manufacturer_sku=details.manufacturer_sku,
                            barcode=details.barcode,
                            current_specs=details.current_specs,
                        )
                        repo.save_product(target)
                        print(f"  [+] Live sync OK: SKU/ID '{key}' -> ID #{target.product_id} | Shop SKU: {target.shop_sku or 'N/A'} | {target.title[:50]}")
                        print(f"      Current specs on store: {len(target.current_specs)} items.")
                except Exception:
                    pass

                if not target:
                    target = repo.find_product(key)
                    if target:
                        print(f"  [*] Using cached record from local DB: ID #{target.product_id} ({target.title[:50]})")

                if target:
                    products.append(target)
                else:
                    print(f"  [!] Warning: Product '{key}' not found on shop.telecom.kz or in DB (skipping).")

        if not products:
            print(f"[!] Error: None of the specified products {target_keys} were found.")
            return 1
    else:
        products = repo.get_auditable_products(limit=args.limit)
        if not products:
            print(f"[!] No products found in database {db_path}")
            return 1

    settings = get_settings()
    gemini_key = args.gemini_key or settings.GEMINI_API_KEY
    has_llm = bool(gemini_key or settings.OPENROUTER_API_KEY or os.environ.get("OPENROUTER_API_KEY"))
    if not has_llm:
        print("[!] WARNING: Neither OPENROUTER_API_KEY nor GEMINI_API_KEY is set. Arbitration calls will return ERROR status.")
        print("    Pass --gemini-key <KEY> or set OPENROUTER_API_KEY in .env.")

    judge = GeminiJudge(api_key=gemini_key)
    vendor_resolver = VendorDirectResolver(timeout=10.0)
    serper_client = SerperClient(api_key=settings.SERPER_API_KEY)
    ref_resolver = ResolveReferenceUseCase(
        repository=repo,
        vendor_resolver=vendor_resolver,
        serper_client=serper_client,
        verify_direct_urls=True,
        judge=judge,
    )

    crawler = WebCrawler(
        max_concurrent=2,
        headless=not args.no_headless,
        timeout_ms=30000,
    )
    audit_use_case = AuditProductUseCase(
        product_repo=repo,
        reference_resolver=ref_resolver,
        crawler=crawler,
        judge=judge,
    )

    print("=" * 80)
    print("TELECOM CATALOG AUDITOR :: STAGE 4 FACTUAL ARBITER")
    print("=" * 80)
    print(f"Target DB     : {db_path.resolve()}")
    print(f"Products Count: {len(products)}")
    print(f"Headless      : {not args.no_headless}")
    print(f"Force Refresh : {args.force_refresh}")
    print("-" * 80)

    try:
        await crawler.start()
        verified_count = 0
        mismatch_count = 0
        missing_specs_count = 0
        not_found_count = 0
        error_count = 0

        total_time_all = 0.0
        total_tokens_all = 0
        total_cost_all = 0.0

        for idx, prod in enumerate(products, 1):
            print(f"[{idx:02d}/{len(products)}] Auditing ID #{prod.product_id} | {prod.title[:150]}")
            print(f"      Shop SKU: {prod.shop_sku or 'N/A'} | Vendor SKU: {prod.vendor_sku or 'N/A'} | Barcode: {prod.barcode or 'N/A'}")
            print(f"      Current Specs Count: {len(prod.current_specs)}")

            result = await audit_use_case.audit_product(
                product=prod,
                reference_url=args.reference_url if len(products) == 1 else None,
                force_refresh=args.force_refresh,
            )

            if result.status == AuditStatus.VERIFIED:
                verified_count += 1
                print(f"      STATUS: [VERIFIED] (Confidence: {result.confidence_score:.2f} | Matched: {result.matched_specs_count}/{result.total_specs_count})")
                print(f"      Ref URL: {result.reference_url}")
                if result.details:
                    print(f"      Details: {result.details}")
            elif result.status == AuditStatus.MISMATCH:
                mismatch_count += 1
                print(f"      STATUS: [MISMATCH] (Confidence: {result.confidence_score:.2f} | Discrepancies: {len(result.discrepancies)})")
                print(f"      Ref URL: {result.reference_url}")
                for d in result.discrepancies:
                    print(f"        * Spec: '{d.spec_name}' [{d.severity.upper()}]")
                    print(f"          Shop Value     : {d.shop_value}")
                    print(f"          Reference Value: {d.reference_value}")
                if result.details:
                    print(f"      Details: {result.details}")
            elif result.status == AuditStatus.MISSING_SPECS:
                missing_specs_count += 1
                print(f"      STATUS: [MISSING_SPECS] (Confidence: {result.confidence_score:.2f})")
                print(f"      Ref URL: {result.reference_url}")
                if result.details:
                    print(f"      Details: {result.details}")
            elif result.status == AuditStatus.NOT_FOUND:
                not_found_count += 1
                print(f"      STATUS: [NOT_FOUND] - {result.details}")
            else:
                error_count += 1
                print(f"      STATUS: [ERROR] - {result.details}")

            if result.missing_specs:
                print(f"      * MISSING SPECS (В эталоне найдены важные ТХ, отсутствующие на витрине):")
                for m in result.missing_specs:
                    print(f"        + {m.spec_name}: {m.reference_value}")

            if result.execution_time_sec or result.input_tokens:
                total_tok = result.input_tokens + result.output_tokens
                print(f"      Metrics: Time: {result.execution_time_sec:.2f}s (Crawler: {result.crawler_time_sec:.2f}s | LLM: {result.llm_time_sec:.2f}s) | Tokens: {total_tok:,} (in: {result.input_tokens:,} | out: {result.output_tokens:,}) | Cost: ~${result.estimated_cost_usd:.5f}")

            if result.execution_time_sec:
                total_time_all += result.execution_time_sec
            if result.input_tokens:
                total_tokens_all += (result.input_tokens + result.output_tokens)
            if result.estimated_cost_usd:
                total_cost_all += result.estimated_cost_usd
            print()

        print("-" * 80)
        print(f"AUDIT SUMMARY: Total: {len(products)} | Verified: {verified_count} | Mismatches: {mismatch_count} | Missing Specs: {missing_specs_count} | Not Found: {not_found_count} | Errors: {error_count}")
        if len(products) > 1 and total_tokens_all > 0:
            print(f"BATCH METRICS: Total Time: {total_time_all:.2f}s | Total Tokens: {total_tokens_all:,} | Total Est. Cost: ~${total_cost_all:.5f}")
        print("=" * 80)
        return 0
    finally:
        await crawler.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telecom-auditor",
        description="Telecom Catalog Auditor CLI",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    crawl_parser = subparsers.add_parser("crawl", help="Run catalog crawler")
    crawl_parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Category path or urlkey (e.g. smartfony-i-aksessuary/telefony/smartfony)",
    )
    crawl_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of products to process",
    )
    crawl_parser.add_argument(
        "--delay-min",
        type=float,
        default=0.6,
        help="Minimum polite delay in seconds (default 0.6)",
    )
    crawl_parser.add_argument(
        "--delay-max",
        type=float,
        default=1.5,
        help="Maximum polite delay in seconds (default 1.5)",
    )
    crawl_parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Max concurrent HTTP requests (default 2)",
    )
    crawl_parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore crawler_state and force rescan",
    )
    crawl_parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Reset crawler_state for target category before running",
    )
    crawl_parser.add_argument(
        "--db",
        type=str,
        default="catalog_audit.db",
        help="Path to SQLite database (default: catalog_audit.db)",
    )

    resolve_parser = subparsers.add_parser("resolve", help="Resolve reference URLs for products")
    resolve_parser.add_argument(
        "--product-id",
        nargs="*",
        type=str,
        default=None,
        help="Target product ID(s) (e.g. 74615 76830 or comma-separated 74615,76830)",
    )
    resolve_parser.add_argument(
        "--sku",
        "--shop-sku",
        nargs="*",
        type=str,
        default=None,
        help="Target Store SKU(s) (e.g. 417095 450322 or comma-separated 417095,450322)",
    )
    resolve_parser.add_argument(
        "--url",
        nargs="*",
        type=str,
        default=None,
        help="Direct product URL(s) on shop.telecom.kz",
    )
    resolve_parser.add_argument(
        "--sku-file",
        type=str,
        default=None,
        help="Path to text file with SKUs (one per line or comma-separated)",
    )
    resolve_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max products to resolve (default: 10)",
    )
    resolve_parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify direct URLs with HTTP HEAD",
    )
    resolve_parser.add_argument(
        "--db",
        type=str,
        default="catalog_audit.db",
        help="Path to SQLite database (default: catalog_audit.db)",
    )

    live_parser = subparsers.add_parser("live-test", help="Run live pipeline test on shop.telecom.kz")
    live_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of products to test (default: 10)",
    )
    live_parser.add_argument(
        "--start-id",
        type=int,
        default=74615,
        help="Starting product ID (default: 74615)",
    )
    live_parser.add_argument(
        "--db",
        type=str,
        default="catalog_audit.db",
        help="Path to SQLite database (default: catalog_audit.db)",
    )

    audit_parser = subparsers.add_parser("audit", help="Run 1-shot factual audit against external reference")
    audit_parser.add_argument(
        "--product-id",
        nargs="*",
        type=str,
        default=None,
        help="Target product ID(s) (e.g. 74615 76830 or comma-separated 74615,76830)",
    )
    audit_parser.add_argument(
        "--sku",
        "--shop-sku",
        nargs="*",
        type=str,
        default=None,
        help="Target Store SKU(s) (e.g. 417095 450322 or comma-separated 417095,450322)",
    )
    audit_parser.add_argument(
        "--url",
        nargs="*",
        type=str,
        default=None,
        help="Direct product URL(s) on shop.telecom.kz",
    )
    audit_parser.add_argument(
        "--sku-file",
        type=str,
        default=None,
        help="Path to text file with SKUs (one per line or comma-separated)",
    )
    audit_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Max products to audit (default: 5)",
    )
    audit_parser.add_argument(
        "--reference-url",
        type=str,
        default=None,
        help="Manual reference URL override (only with --product-id)",
    )
    audit_parser.add_argument(
        "--gemini-key",
        type=str,
        default=None,
        help="Gemini API Key override",
    )
    audit_parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in graphical mode",
    )
    audit_parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Bypass specs_cache and refetch external page",
    )
    audit_parser.add_argument(
        "--db",
        type=str,
        default="catalog_audit.db",
        help="Path to SQLite database (default: catalog_audit.db)",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.subcommand == "crawl":
        exit_code = asyncio.run(run_crawl_command(args))
        sys.exit(exit_code)
    elif args.subcommand == "resolve":
        exit_code = asyncio.run(run_resolve_command(args))
        sys.exit(exit_code)
    elif args.subcommand == "live-test":
        exit_code = asyncio.run(run_live_test_command(args))
        sys.exit(exit_code)
    elif args.subcommand == "audit":
        exit_code = asyncio.run(run_audit_command(args))
        sys.exit(exit_code)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
