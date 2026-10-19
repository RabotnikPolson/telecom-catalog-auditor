import argparse
import asyncio
import io
import os
from pathlib import Path
import sys
import time
import warnings

warnings.filterwarnings("ignore", message=".*urllib3.*or chardet.*doesn't match a supported version.*")
try:
    from requests.exceptions import RequestsDependencyWarning
    warnings.filterwarnings("ignore", category=RequestsDependencyWarning)
except ImportError:
    pass

from config.settings import get_settings
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel

from src.adapters.catalog.mysql_reader import MySQLCatalogReader
from src.adapters.catalog.telecom_crawler import TelecomShopCrawler
from src.adapters.crawler.web_crawler import WebCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.llm.openai_judge import OpenAIJudge
from src.adapters.search.serper_client import SerperClient
from src.adapters.search.vendor_direct import VendorDirectResolver
from src.domain.entities import AuditResult, AuditStatus, Product
from src.domain.services import ParentChildGrouper
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
    settings = get_settings()

    source = getattr(args, "source", "mysql")
    if source == "mysql" and settings.MYSQL_HOST:
        def on_progress(msg: str) -> None:
            print(f"  [+] {msg}")

        print("=" * 70)
        print("TELECOM CATALOG AUDITOR :: HIGH-SPEED MYSQL INGESTION")
        print("=" * 70)
        print(f"Target DB     : {db_path.resolve()}")
        print(f"WAL Mode      : {'ACTIVE' if repo.check_wal_mode() else 'INACTIVE'}")
        print(f"MySQL Source  : {settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.MYSQL_DATABASE}")
        print(f"Product Limit : {args.limit or 'NO LIMIT'}")
        print("-" * 70)

        mysql_reader = MySQLCatalogReader()
        use_case = CrawlShopCatalogUseCase(
            mysql_reader=mysql_reader,
            repository=repo,
            progress_callback=on_progress,
        )
        stats = await use_case.execute(
            category_path=args.category,
            limit=args.limit,
            source="mysql",
        )
        print("-" * 70)
        print("MYSQL INGESTION COMPLETED")
        print(f"Total Products Seen  : {stats.products_seen}")
        print(f"Products Upserted    : {stats.products_added}")
        print(f"Total Products in DB : {repo.count_products()}")
        print("=" * 70)
        return 0

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


async def _live_sync_products(
    target_keys: list[str],
    repo: SQLiteProductRepository,
    source: str = "mysql",
) -> list[Product]:
    """
    Live-sync product details before resolution or audit.
    Prioritizes fast direct MySQL read, falls back to web crawler or local DB cache.
    """
    settings = get_settings()
    products: list[Product] = []
    keys_to_fetch = list(target_keys)

    # 1. Try MySQL first if available and selected
    if source == "mysql" and settings.MYSQL_HOST:
        print(f"[*] Live syncing {len(keys_to_fetch)} product(s) directly from shop MySQL (READ-ONLY)...")
        try:
            async with MySQLCatalogReader() as mysql_reader:
                remaining_keys = []
                for key in keys_to_fetch:
                    p = None
                    try:
                        p = await mysql_reader.fetch_product(key)
                        if p:
                            repo.save_product(p)
                            print(f"  [+] MySQL sync OK: Key '{key}' -> ID #{p.product_id} | Shop SKU: {p.shop_sku or 'N/A'} | {p.title[:150]}")
                            print(f"      Current specs: {len(p.current_specs)} items | Barcode: {p.barcode or 'N/A'}")
                            products.append(p)
                    except Exception as e:
                        print(f"  [!] MySQL error for key '{key}': {e}")

                    if not p:
                        remaining_keys.append(key)
                keys_to_fetch = remaining_keys
        except Exception as e:
            print(f"  [!] MySQL connection error: {e}. Falling back to web crawler / local DB.")

    # 2. For any remaining keys, fallback to TelecomShopCrawler
    if keys_to_fetch:
        print(f"[*] Fallback: web-fetching {len(keys_to_fetch)} product(s) from shop.telecom.kz...")
        async with TelecomShopCrawler(delay_min=0.1, delay_max=0.3) as shop_crawler:
            for key in keys_to_fetch:
                p = None
                try:
                    details = await shop_crawler.fetch_product_on_the_fly(str(key))
                    if details and details.title and details.current_specs:
                        p = Product(
                            product_id=details.product_id,
                            shop_sku=details.shop_sku,
                            title=details.title,
                            vendor_name=details.vendor_name,
                            vendor_sku=details.vendor_sku,
                            manufacturer_sku=details.manufacturer_sku,
                            barcode=details.barcode,
                            current_specs=details.current_specs,
                        )
                        repo.save_product(p)
                        print(f"  [+] Web crawl OK: SKU/ID '{key}' -> ID #{p.product_id} | Shop SKU: {p.shop_sku or 'N/A'} | {p.title[:50]}")
                        print(f"      Current specs on store: {len(p.current_specs)} items.")
                except Exception:
                    pass

                if not p:
                    p = repo.find_product(key)
                    if p:
                        print(f"  [*] Using cached record from local DB: ID #{p.product_id} | Shop SKU: {p.shop_sku or 'N/A'} ({p.title[:50]})")

                if p:
                    products.append(p)
                else:
                    print(f"  [!] Warning: Product '{key}' not found in MySQL, on shop.telecom.kz, or in DB (skipping).")

    return products


async def run_categories_command(args: argparse.Namespace) -> int:
    setup_utf8_terminal()
    settings = get_settings()
    if not settings.MYSQL_HOST:
        print("[!] Error: MySQL host is not configured in settings/env.")
        return 1

    async with MySQLCatalogReader() as reader:
        main_cat_id = getattr(args, "main_cat", None)
        search = getattr(args, "search", None)

        if main_cat_id is not None or search:
            groups = await reader.get_groups(main_category_id=main_cat_id, search=search, only_active=True)
            # print("=" * 85)
            print(f"CATALOG PRODUCT GROUPS (Active on Display) | Found: {len(groups)}")
            if main_cat_id:
                print(f"Filtered by Main Category ID: {main_cat_id}")
            if search:
                print(f"Search query: '{search}'")
            print("=" * 85)
            print(f"{'GROUP ID':<10} | {'GROUP NAME':<35} | {'CATEGORY':<25} | {'ACTIVE':<8}")
            print("-" * 85)
            for g in groups:
                print(f"{g['group_id']:<10} | {g['group_name'][:35]:<35} | {g['cat_name'][:25]:<25} | {g['active_count']:<8}")
            print("=" * 85)
            print("Tip: Run audit on a group with: python main.py audit --group <GROUP_ID> --limit 10")
            return 0
        else:
            categories = await reader.get_categories(only_active=True)
            print("=" * 85)
            print(f"CATALOG MAIN CATEGORIES (Active on Display) | Total: {len(categories)}")
            print("=" * 85)
            print(f"{'CAT ID':<8} | {'CATEGORY NAME':<35} | {'URL KEY':<25} | {'ACTIVE':<8}")
            print("-" * 85)
            for c in categories:
                print(f"{c['id']:<8} | {c['name'][:35]:<35} | {c['urlkey'][:25]:<25} | {c['active_count']:<8}")
            print("=" * 85)
            print("Tip: To view product groups inside a category: python main.py categories --main-cat <ID>")
            print("Tip: To search groups by name: python main.py categories --search 'роутер'")
            return 0


async def run_resolve_command(args: argparse.Namespace) -> int:
    setup_utf8_terminal()
    db_path = Path(args.db)
    repo = SQLiteProductRepository(db_path)
    settings = get_settings()

    vendor_resolver = VendorDirectResolver()
    serper_client = SerperClient(api_key=settings.SERPER_API_KEY)
    judge = OpenAIJudge()
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
        source_mode = getattr(args, "source", "mysql")
        products = await _live_sync_products(target_keys, repo, source=source_mode)
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
        elif ref.source_type in ("BARCODE_SEARCH", "PROVIDER_SKU_SEARCH", "MODEL_SEARCH", "CODE_PINPOINT"):
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


class AuditTUI:
    def __init__(self, console: Console, total_products: int, concurrency: int) -> None:
        self.console = console
        self.total = total_products
        self.concurrency = concurrency
        self.completed = 0
        self.verified_count = 0
        self.mismatch_count = 0
        self.missing_specs_count = 0
        self.not_found_count = 0
        self.error_count = 0
        self.worker_status: dict[int, str] = {w: "Waiting..." for w in range(1, concurrency + 1)}

    def set_worker_status(self, worker_id: int, status_text: str) -> None:
        self.worker_status[worker_id] = status_text

    def record_completed(self, status: AuditStatus) -> None:
        self.completed += 1
        if status == AuditStatus.VERIFIED:
            self.verified_count += 1
        elif status == AuditStatus.MISMATCH:
            self.mismatch_count += 1
        elif status == AuditStatus.MISSING_SPECS:
            self.missing_specs_count += 1
        elif status == AuditStatus.NOT_FOUND:
            self.not_found_count += 1
        else:
            self.error_count += 1

    def render_live_panel(self) -> Panel:
        pct = int((self.completed / self.total) * 100) if self.total else 0
        bar_len = 24
        filled = int((pct / 100) * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        lines = [
            f"[bold cyan]Progress:[/bold cyan] \\[{self.completed:02d}/{self.total:02d}] \\[{bar}] {pct}% | "
            f"[bold green]Verified: {self.verified_count}[/bold green] | "
            f"[bold red]Mismatch: {self.mismatch_count}[/bold red] | "
            f"[bold yellow]Missing: {self.missing_specs_count}[/bold yellow] | "
            f"[bold magenta]Not Found: {self.not_found_count}[/bold magenta] | "
            f"[bold red]Errors: {self.error_count}[/bold red]"
        ]
        if self.concurrency > 1:
            lines.append("")
            for w in range(1, self.concurrency + 1):
                w_status = escape(self.worker_status.get(w, "Idle"))
                lines.append(f"[bold cyan]Worker #{w}:[/bold cyan] {w_status}")
        else:
            w_status = escape(self.worker_status.get(1, "Idle"))
            lines.append(f"[bold cyan]Status:[/bold cyan] {w_status}")

        return Panel(
            "\n".join(lines),
            title=f"[bold white]TELECOM AUDITOR :: LIVE AUDIT MONITOR[/bold white] [dim](Concurrency: {self.concurrency})[/dim]",
            border_style="cyan",
        )


def format_audit_card(idx: int, total_count: int, prod: Product, result: AuditResult) -> str:
    lines = []
    lines.append(f"\\[{idx:02d}/{total_count}] Product ID #{prod.product_id} | {escape(prod.title[:150])}")
    lines.append(f"      Shop SKU: {escape(prod.shop_sku or 'N/A')} | Vendor SKU: {escape(prod.vendor_sku or 'N/A')} | Barcode: {escape(prod.barcode or 'N/A')}")
    lines.append(f"      Shop URL: https://shop.telecom.kz/product/{prod.product_id}")
    photo_badge = f"[bold green]\\[{result.images_status}][/bold green]" if result.images_status == "OK" else (
        f"[bold red]\\[{result.images_status}][/bold red]" if result.images_status == "NO_IMAGES" else f"[bold yellow]\\[{result.images_status}][/bold yellow]"
    )
    ref_photo_str = f"/{result.reference_images_count}" if result.reference_images_count else ""
    lines.append(f"      Photos: {result.images_count}{ref_photo_str} {photo_badge} | Description: {result.description_quality}")

    if result.status == AuditStatus.VERIFIED:
        lines.append(f"      STATUS: [bold green]\\[VERIFIED][/bold green] (Confidence: {result.confidence_score:.2f} | Matched: {result.matched_specs_count}/{result.total_specs_count})")
        lines.append(f"      Ref URL: {escape(result.reference_url or '')}")
        if result.details:
            lines.append(f"      Details: {escape(result.details)}")
    elif result.status == AuditStatus.MISMATCH:
        lines.append(f"      STATUS: [bold red]\\[MISMATCH][/bold red] (Confidence: {result.confidence_score:.2f} | Discrepancies: {len(result.discrepancies)})")
        lines.append(f"      Ref URL: {escape(result.reference_url or '')}")
        for d in result.discrepancies:
            lines.append(f"        * Spec: '{escape(d.spec_name)}' \\[{escape(d.severity.upper())}]")
            lines.append(f"          Shop Value     : {escape(d.shop_value)}")
            lines.append(f"          Reference Value: {escape(d.reference_value)}")
        if result.details:
            lines.append(f"      Details: {escape(result.details)}")
    elif result.status == AuditStatus.MISSING_SPECS:
        lines.append(f"      STATUS: [bold yellow]\\[MISSING_SPECS][/bold yellow] (Confidence: {result.confidence_score:.2f})")
        lines.append(f"      Ref URL: {escape(result.reference_url or '')}")
        if result.details:
            lines.append(f"      Details: {escape(result.details)}")
    elif result.status == AuditStatus.NOT_FOUND:
        lines.append(f"      STATUS: [bold magenta]\\[NOT_FOUND][/bold magenta] - {escape(result.details or '')}")
        if result.search_trace:
            lines.append("      SEARCH TRACE:")
            for trace_step in result.search_trace:
                lines.append(f"        > {escape(trace_step)}")
    else:
        lines.append(f"      STATUS: [bold red]\\[ERROR][/bold red] - {escape(result.details or '')}")
        if result.search_trace:
            lines.append("      SEARCH TRACE:")
            for trace_step in result.search_trace:
                lines.append(f"        > {escape(trace_step)}")

    if result.missing_specs:
        lines.append("      * MISSING SPECS (В эталоне найдены важные ТХ, отсутствующие на витрине):")
        for m in result.missing_specs:
            lines.append(f"        + {escape(m.spec_name)}: {escape(m.reference_value)}")

    if result.description_discrepancies:
        lines.append("      * DESCRIPTION DISCREPANCIES (Ошибки в описании товара):")
        for dd in result.description_discrepancies:
            lines.append(f"        - Описание: \"{escape(dd.claim_in_description)}\"")
            lines.append(f"          Фактически: \"{escape(dd.factual_truth)}\" \\[{escape(dd.contradiction_type)}]")

    if result.execution_time_sec or result.input_tokens:
        total_tok = result.input_tokens + result.output_tokens
        lines.append(f"      Metrics: Time: {result.execution_time_sec:.2f}s (Crawler: {result.crawler_time_sec:.2f}s | LLM: {result.llm_time_sec:.2f}s) | Tokens: {total_tok:,} (in: {result.input_tokens:,} | out: {result.output_tokens:,}) | Cost: ~${result.estimated_cost_usd:.5f}")

    lines.append("")
    return "\n".join(lines)


async def run_audit_command(args: argparse.Namespace) -> int:
    setup_utf8_terminal()

    db_path = Path(args.db)
    repo = SQLiteProductRepository(db_path)
    settings = get_settings()

    target_keys = _extract_target_keys(args)
    audited_master_keys: set[str] = set()

    if target_keys:
        source_mode = getattr(args, "source", "mysql")
        products = await _live_sync_products(target_keys, repo, source=source_mode)
        if not products:
            print(f"[!] Error: None of the specified products {target_keys} were found.")
            return 1
        grouper = ParentChildGrouper()
        for p in products:
            if not p.master_key:
                try:
                    p.master_key = grouper.extract_master_key(p.title, p.current_specs)
                except Exception:
                    pass
    else:
        source_mode = getattr(args, "source", "mysql")
        products = []
        skip_audited = getattr(args, "skip_audited", True) and not getattr(args, "force_refresh", False)
        audited_ids: set[int] = set()
        audited_master_keys: set[str] = set()
        if skip_audited:
            audited_ids = repo.get_audited_product_ids()
            audited_master_keys = repo.get_audited_master_keys()
            if audited_ids or audited_master_keys:
                print(f"[*] Incremental Mode: Loaded {len(audited_ids)} audited products ({len(audited_master_keys)} master models) from DB (skipping).")

        main_cat_id = getattr(args, "main_cat", None)
        group_id = getattr(args, "group", None)
        cat_arg = getattr(args, "category", None)
        if cat_arg and str(cat_arg).isdigit():
            group_id = int(cat_arg)

        target_limit = args.limit or 5
        grouper = ParentChildGrouper()
        seen_batch_master_keys: set[str] = set()
        skipped_count = 0

        if source_mode == "mysql" and settings.MYSQL_HOST:
            filter_desc = []
            if main_cat_id:
                filter_desc.append(f"Main Cat #{main_cat_id}")
            if group_id:
                filter_desc.append(f"Group #{group_id}")
            desc_str = f" ({', '.join(filter_desc)})" if filter_desc else ""
            print(f"[*] Streaming active products directly from shop MySQL (READ-ONLY, limit={target_limit}{desc_str})...")
            try:
                async with MySQLCatalogReader() as mysql_reader:
                    async for batch in mysql_reader.stream_products(
                        batch_size=min(500, max(50, target_limit * 4)),
                        offset=getattr(args, "offset", 0) or 0,
                        category_id=group_id,
                        main_category_id=main_cat_id,
                        only_active=True,
                    ):
                        grouper.group_products(batch)
                        for prod in batch:
                            if not prod.is_master:
                                skipped_count += 1
                                continue
                            if skip_audited:
                                if prod.product_id in audited_ids:
                                    skipped_count += 1
                                    continue
                                if prod.master_key and prod.master_key in audited_master_keys:
                                    skipped_count += 1
                                    continue
                            if prod.master_key:
                                if prod.master_key in seen_batch_master_keys:
                                    skipped_count += 1
                                    continue
                                seen_batch_master_keys.add(prod.master_key)

                            products.append(prod)
                            if len(products) >= target_limit:
                                break
                        if len(products) >= target_limit:
                            break
            except Exception as e:
                print(f"[!] MySQL streaming error: {e}. Falling back to local DB.")

        if not products:
            db_candidates = repo.get_auditable_products(limit=target_limit * 4, offset=getattr(args, "offset", 0) or 0)
            if db_candidates:
                grouper.group_products(db_candidates)
                for prod in db_candidates:
                    if not prod.is_master:
                        continue
                    if skip_audited:
                        if prod.product_id in audited_ids:
                            continue
                        if prod.master_key and prod.master_key in audited_master_keys:
                            continue
                    if prod.master_key:
                        if prod.master_key in seen_batch_master_keys:
                            continue
                        seen_batch_master_keys.add(prod.master_key)
                    products.append(prod)
                    if len(products) >= target_limit:
                        break

        if not products:
            print("[*] No un-audited master products found (all audited or none match filter).")
            return 0

    settings = get_settings()
    openai_key = (
        getattr(args, "openai_key", None)
        or getattr(args, "gemini_key", None)
        or settings.OPENAI_API_KEY
        or os.environ.get("OPENAI_API_KEY")
    )
    if not openai_key:
        print("[!] WARNING: OPENAI_API_KEY is not set. Arbitration calls will return ERROR status.")
        print("    Pass --openai-key <KEY> or set OPENAI_API_KEY in .env.")

    judge = OpenAIJudge(api_key=openai_key)
    vendor_resolver = VendorDirectResolver()
    serper_client = SerperClient(api_key=settings.SERPER_API_KEY)
    ref_resolver = ResolveReferenceUseCase(
        repository=repo,
        vendor_resolver=vendor_resolver,
        serper_client=serper_client,
        verify_direct_urls=True,
        judge=judge,
    )

    raw_concurrency = getattr(args, "concurrency", 1) or 1
    effective_concurrency = max(1, min(raw_concurrency, len(products)))

    crawler = WebCrawler(
        max_concurrent=effective_concurrency,
        headless=not args.no_headless,
        timeout_ms=30000,
    )
    audit_use_case = AuditProductUseCase(
        product_repo=repo,
        reference_resolver=ref_resolver,
        crawler=crawler,
        judge=judge,
    )

    console = Console()
    tui = AuditTUI(console=console, total_products=len(products), concurrency=effective_concurrency)

    print("=" * 80)
    print("TELECOM CATALOG AUDITOR :: STAGE 4 FACTUAL ARBITER")
    print("=" * 80)
    print(f"Target DB     : {db_path.resolve()}")
    print(f"Products Count: {len(products)}")
    print(f"LLM Engine    : OpenAI (model: {settings.OPENAI_MODEL})")
    print(f"Workers       : {effective_concurrency}")
    print(f"Headless      : {not args.no_headless}")
    print(f"Force Refresh : {args.force_refresh}")
    print("=" * 80)

    try:
        await crawler.start()

        queue: asyncio.Queue[tuple[int, Product]] = asyncio.Queue()
        for idx, prod in enumerate(products, 1):
            await queue.put((idx, prod))

        print_lock = asyncio.Lock()
        completed_cards: dict[int, str] = {}
        next_to_print = 1
        total_time_all = 0.0
        total_tokens_all = 0
        total_cost_all = 0.0
        start_time_all = time.perf_counter()
        total_products_count = len(products)

        async def worker_loop(worker_id: int, live_ctx: Live | None) -> None:
            nonlocal total_time_all, total_tokens_all, total_cost_all, next_to_print
            while not queue.empty():
                try:
                    idx, prod = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                tui.set_worker_status(worker_id, f"Auditing #{prod.product_id} | {prod.title[:150]}...")
                if live_ctx:
                    live_ctx.update(tui.render_live_panel())

                try:
                    result = await audit_use_case.audit_product(
                        product=prod,
                        reference_url=args.reference_url if len(products) == 1 else None,
                        force_refresh=args.force_refresh,
                        skip_desc=getattr(args, "skip_desc", False),
                        skip_tech=getattr(args, "skip_tech", False),
                        photos_only=getattr(args, "photos_only", False),
                    )
                except Exception as exc:
                    result = AuditResult(
                        product_id=prod.product_id,
                        status=AuditStatus.ERROR,
                        confidence_score=0.0,
                        reference_url=None,
                        discrepancies=[],
                        matched_specs_count=0,
                        total_specs_count=len(prod.current_specs),
                        details=f"Unexpected error: {exc}",
                        execution_time_sec=0.0,
                    )

                if prod.master_key:
                    audited_master_keys.add(prod.master_key)

                tui.record_completed(result.status)
                if result.execution_time_sec:
                    total_time_all += result.execution_time_sec
                if result.input_tokens:
                    total_tokens_all += (result.input_tokens + result.output_tokens)
                if result.estimated_cost_usd:
                    total_cost_all += result.estimated_cost_usd

                card_str = format_audit_card(idx, total_products_count, prod, result)
                async with print_lock:
                    completed_cards[idx] = card_str
                    while next_to_print in completed_cards:
                        card_to_emit = completed_cards.pop(next_to_print)
                        if live_ctx:
                            live_ctx.console.print(card_to_emit)
                        else:
                            console.print(card_to_emit)
                        next_to_print += 1

                queue.task_done()
                tui.set_worker_status(worker_id, "Idle")
                if live_ctx:
                    live_ctx.update(tui.render_live_panel())

        if console.is_terminal:
            with Live(tui.render_live_panel(), console=console, refresh_per_second=4, transient=True) as live:
                tasks = [
                    asyncio.create_task(worker_loop(w_id, live))
                    for w_id in range(1, effective_concurrency + 1)
                ]
                await asyncio.gather(*tasks)
        else:
            tasks = [
                asyncio.create_task(worker_loop(w_id, None))
                for w_id in range(1, effective_concurrency + 1)
            ]
            await asyncio.gather(*tasks)

        # Flush any remaining cards if sequence had any gaps
        async with print_lock:
            for rem_idx in sorted(completed_cards.keys()):
                card_to_emit = completed_cards.pop(rem_idx)
                console.print(card_to_emit)

        wall_time = time.perf_counter() - start_time_all
        speedup = (total_time_all / wall_time) if wall_time > 0 else 1.0
        print("-" * 80)
        print(f"AUDIT SUMMARY: Total: {len(products)} | Verified: {tui.verified_count} | Mismatches: {tui.mismatch_count} | Missing Specs: {tui.missing_specs_count} | Not Found: {tui.not_found_count} | Errors: {tui.error_count}")
        if len(products) > 1 and total_tokens_all > 0:
            print(f"BATCH METRICS: Wall Time: {wall_time:.2f}s | Total Time: {total_time_all:.2f}s | Speedup: {speedup:.1f}x | Total Tokens: {total_tokens_all:,} | Total Est. Cost: ~${total_cost_all:.5f}")
        print("=" * 80)
        return 0
    finally:
        await crawler.close()
        await judge.aclose()


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
        "--source",
        choices=["mysql", "crawler"],
        default="mysql" if get_settings().MYSQL_HOST else "crawler",
        help="Catalog source: mysql (fast direct DB read) or crawler (web scraper)",
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
        "--source",
        choices=["mysql", "crawler"],
        default="mysql" if get_settings().MYSQL_HOST else "crawler",
        help="Live sync source: mysql or crawler",
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

    cat_parser = subparsers.add_parser("categories", help="List shop catalog categories and product groups")
    cat_parser.add_argument(
        "--main-cat",
        type=int,
        default=None,
        help="Filter groups within main category ID (e.g. 11 for 'Все для интернета')",
    )
    cat_parser.add_argument(
        "--search",
        type=str,
        default=None,
        help="Search groups by keyword (e.g. 'роутер' or 'wi-fi')",
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
        "--category",
        type=str,
        default=None,
        help="Category ID to filter active products from MySQL (e.g. 416)",
    )
    audit_parser.add_argument(
        "--main-cat",
        type=int,
        default=None,
        help="Filter active products by main category ID (e.g. 11 for 'Все для интернета', 1 for 'Смартфоны')",
    )
    audit_parser.add_argument(
        "--group",
        type=int,
        default=None,
        help="Filter active products by product group ID (e.g. 78 for 'Модемы и сетевое оборудование WI-FI')",
    )
    audit_parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Starting product offset (default: 0)",
    )
    audit_parser.add_argument(
        "--skip-audited",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip products already audited in database (default: True, use --no-skip-audited to disable)",
    )
    audit_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Max products to audit (default: 5)",
    )
    audit_parser.add_argument(
        "--concurrency",
        type=int,
        choices=list(range(1, 16)),
        default=1,
        help="Number of concurrent audit workers (1-10, default: 1)",
    )
    audit_parser.add_argument(
        "--reference-url",
        type=str,
        default=None,
        help="Manual reference URL override (only with --product-id)",
    )
    audit_parser.add_argument(
        "--openai-key",
        type=str,
        default=None,
        help="OpenAI API Key override",
    )
    audit_parser.add_argument(
        "--gemini-key",
        type=str,
        default=None,
        help="Backwards-compatible alias for --openai-key",
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
        "--skip-desc",
        action="store_true",
        help="Skip description audit (audit specs & photos only)",
    )
    audit_parser.add_argument(
        "--skip-tech",
        action="store_true",
        help="Skip specs arbitration (audit photos & description only)",
    )
    audit_parser.add_argument(
        "--photos-only",
        action="store_true",
        help="Fast offline check of product photos without web search or LLM",
    )
    audit_parser.add_argument(
        "--source",
        choices=["mysql", "crawler"],
        default="mysql" if get_settings().MYSQL_HOST else "crawler",
        help="Live sync source: mysql or crawler",
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

    if args.subcommand == "categories":
        exit_code = asyncio.run(run_categories_command(args))
        sys.exit(exit_code)
    elif args.subcommand == "crawl":
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
