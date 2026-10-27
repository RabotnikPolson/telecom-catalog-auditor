import argparse
import asyncio
import io
import logging
import os
import sqlite3
import sys
import time
import warnings
from collections.abc import AsyncGenerator
from pathlib import Path

warnings.filterwarnings("ignore", message=".*urllib3.*or chardet.*doesn't match a supported version.*")

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel

from config.settings import get_settings
from src.adapters.catalog.mysql_reader import MySQLCatalogReader, MySQLReaderError
from src.adapters.catalog.telecom_crawler import TelecomShopCrawler
from src.adapters.crawler.web_crawler import WebCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.llm.openai_judge import OpenAIJudge
from src.adapters.search.serper_client import SerperClient
from src.adapters.search.vendor_direct import VendorDirectResolver
from src.domain.entities import AuditResult, AuditStatus, Product
from src.domain.exceptions import GroupingError
from src.domain.services import ParentChildGrouper
from src.use_cases.audit_product import AuditProductUseCase
from src.use_cases.crawl_shop_catalog import CrawlShopCatalogUseCase
from src.use_cases.resolve_reference import ResolveReferenceUseCase


def setup_utf8_terminal() -> None:
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer") and hasattr(sys.stderr, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


async def run_crawl_command(args: argparse.Namespace) -> int:
    setup_utf8_terminal()

    db_path = Path(args.db)
    repo = SQLiteProductRepository(db_path)
    settings = get_settings()

    def on_progress(msg: str) -> None:
        print(f"  [+] {msg}")

    source = getattr(args, "source", "mysql")
    if source == "mysql" and settings.MYSQL_HOST:
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
                            p.images_count_known = True
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
                            description=details.description,
                            images_count=details.photos_count or 0,
                            images_count_known=details.photos_count is not None,
                        )
                        print(f"  [+] Web crawl OK: SKU/ID '{key}' -> ID #{p.product_id} | Shop SKU: {p.shop_sku or 'N/A'} | {p.title[:50]}")
                        print(f"      Current specs on store: {len(p.current_specs)} items.")
                except Exception as exc:
                    p = None
                    _ = exc

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
                if not details:
                    continue
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
    def __init__(self, console: Console, total_products: int, concurrency: int, photos_only: bool = False) -> None:
        self.console = console
        self.total = total_products
        self.concurrency = concurrency
        self.photos_only = photos_only
        self.completed = 0
        self.verified_count = 0
        self.mismatch_count = 0
        self.missing_specs_count = 0
        self.not_found_count = 0
        self.error_count = 0
        self.photo_checked_count = 0
        self.photo_issue_count = 0
        self.worker_status: dict[int, str] = {w: "Waiting..." for w in range(1, concurrency + 1)}

    def set_worker_status(self, worker_id: int, status_text: str) -> None:
        self.worker_status[worker_id] = status_text

    def record_completed(self, status: AuditStatus, photos_only: bool = False, photos_error: bool = False, images_status: str = "OK") -> None:
        self.completed += 1
        if photos_only:
            if photos_error:
                self.error_count += 1
            else:
                self.photo_checked_count += 1
                if images_status != "OK":
                    self.photo_issue_count += 1
            return
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

        progress = f"\\[{self.completed:02d}/{self.total:02d}] \\[{bar}] {pct}%" if self.total else f"Обработано: {self.completed}"
        lines = [
            f"[bold cyan]Progress:[/bold cyan] {progress} | "
            f"[bold green]Verified: {self.verified_count}[/bold green] | "
            f"[bold red]Mismatch: {self.mismatch_count}[/bold red] | "
            f"[bold yellow]Missing: {self.missing_specs_count}[/bold yellow] | "
            f"[bold magenta]Not Found: {self.not_found_count}[/bold magenta] | "
            f"[bold red]Errors: {self.error_count}[/bold red]"
        ]
        if self.photos_only:
            lines = [
                f"[bold cyan]Progress:[/bold cyan] {progress} | "
                f"[bold green]Photos checked: {self.photo_checked_count}[/bold green] | "
                f"[bold yellow]Photo issues: {self.photo_issue_count}[/bold yellow] | "
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


def format_audit_card(idx: int, total_count: int | None, prod: Product, result: AuditResult) -> str:
    lines = []
    position = f"{idx:02d}/{total_count}" if total_count is not None else f"{idx:04d}"
    lines.append(f"\\[{position}] Product ID #{prod.product_id} | {escape(prod.title[:150])}")
    lines.append(f"      Shop SKU: {escape(prod.shop_sku or 'N/A')} | Vendor SKU: {escape(prod.vendor_sku or 'N/A')} | Barcode: {escape(prod.barcode or 'N/A')}")
    lines.append(f"      Shop URL: https://shop.telecom.kz/product/{prod.product_id}")
    if result.photos_only_run:
        lines.append("      Проверка только фото")
        if result.photos_error:
            lines.append(f"      Фото: сравнить не удалось — {escape(result.photos_error)}")
        else:
            assessment = "без замечаний" if result.images_status == "OK" else "есть замечание"
            lines.append(f"      Фото: магазин {result.images_count}, эталон {result.reference_images_count} — {assessment}")
            photo_url = result.audit_parts.get("photos_url")
            if photo_url:
                lines.append(f"      Ref URL: {escape(photo_url)}")
        lines.append("      Характеристики и описание в этом запуске не проверялись.")
        lines.append("")
        return "\n".join(lines)
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


def _audit_is_current(product: Product, result: AuditResult | None, photos_only: bool, skip_desc: bool) -> bool:
    if result is None or not result.audit_parts:
        return False
    required = ["photos"] if photos_only else ["specs", "photos"]
    if not photos_only and not skip_desc and product.description is not None:
        required.append("description")
    for part in required:
        if part == "photos" and not product.images_count_known:
            return False
        photo_url = result.audit_parts.get("photos_url") or result.reference_url
        expected = product.audit_input_hash(part, photo_url if part == "photos" else None)
        if result.audit_parts.get(part) != expected:
            return False
    return True


async def _audit_catalog_batches(
    args: argparse.Namespace,
    repo: SQLiteProductRepository,
    group_id: int | None,
    main_cat_id: int | None,
) -> AsyncGenerator[list[Product]]:
    if args.source == "mysql":
        async with MySQLCatalogReader() as reader:
            stream = reader.stream_products(
                batch_size=100,
                offset=args.offset,
                category_id=group_id,
                main_category_id=main_cat_id,
                only_active=True,
            )
            try:
                async for batch in stream:
                    for product in batch:
                        product.images_count_known = True
                    yield batch
            finally:
                await stream.aclose()
    else:
        offset = args.offset
        while True:
            batch = repo.get_auditable_products(limit=100, offset=offset)
            if not batch:
                break
            yield batch
            offset += len(batch)
            if len(batch) < 100:
                break


async def run_audit_command(args: argparse.Namespace) -> int:
    setup_utf8_terminal()
    processed = skipped = unsuccessful = findings = 0
    total_time = 0.0
    total_tokens = 0
    total_cost = 0.0
    started = time.perf_counter()
    aborted = False
    reason = "выборка закончилась"
    exit_code = 0
    crawler: WebCrawler | None = None
    judge: OpenAIJudge | None = None
    settings = get_settings()
    console = Console()
    workers: list[asyncio.Task[None]] = []
    live: Live | None = None
    tui: AuditTUI | None = None

    try:
        keys = _extract_target_keys(args)
        group_id = getattr(args, "group", None)
        main_cat_id = getattr(args, "main_cat", None)
        category = getattr(args, "category", None)
        if category:
            if not str(category).isdigit():
                raise ValueError("--category должен содержать числовой ID группы.")
            if group_id is not None and group_id != int(category):
                raise ValueError("--category и --group указывают разные группы.")
            group_id = int(category)
        filtered = group_id is not None or main_cat_id is not None
        if keys and filtered:
            raise ValueError("Укажите товары по ID/SKU/URL или фильтр группы/категории, без их сочетания.")
        if args.limit < 0 or args.offset < 0:
            raise ValueError("--limit и --offset не могут быть отрицательными.")
        if filtered and args.source != "mysql":
            raise ValueError("Для группы или категории необходим источник MySQL: SQLite не хранит связи категорий.")

        db_path = Path(args.db)
        repo = SQLiteProductRepository(db_path)
        photos_only = getattr(args, "photos_only", False)
        skip_desc = getattr(args, "skip_desc", False)
        skip_audited = getattr(args, "skip_audited", True) and not args.force_refresh
        limit = 0 if keys else args.limit
        concurrency = max(1, getattr(args, "concurrency", 1))
        grouper = ParentChildGrouper()
        seen_master_keys: set[str] = set()
        explicit_products: list[Product] = []
        if keys:
            explicit_products = await _live_sync_products(keys, repo, source=args.source)
            # Each missing requested key is an unsuccessful lookup, not a skip.
            missing = max(0, len(keys) - len(explicit_products))
            processed += missing
            unsuccessful += missing
        else:
            label = f"группа #{group_id}" if group_id is not None else "общий каталог"
            if main_cat_id is not None:
                label += f", категория #{main_cat_id}"
            print(f"[*] Выборка: {label}. Лимит попыток: {limit or 'до конца выборки'}.")

        async def batches() -> AsyncGenerator[list[Product]]:
            if keys:
                yield explicit_products
            else:
                stream = _audit_catalog_batches(args, repo, group_id, main_cat_id)
                try:
                    async for batch in stream:
                        yield batch
                finally:
                    await stream.aclose()

        stream = batches()
        try:
            async for batch in stream:
                if not keys:
                    grouper.group_products(batch)
                else:
                    for product in batch:
                        if not product.master_key:
                            try:
                                product.master_key = grouper.extract_master_key(product.title, product.current_specs)
                            except GroupingError:
                                product.master_key = f"pid_{product.product_id}"
                previous = repo.get_latest_audit_results_for_ids([p.product_id for p in batch])
                selected: list[Product] = []
                for product in batch:
                    if limit and processed + len(selected) >= limit:
                        break
                    if not keys and (not product.is_master or product.master_key in seen_master_keys):
                        skipped += 1
                        continue
                    if not keys and skip_audited and _audit_is_current(product, previous.get(product.product_id), photos_only, skip_desc):
                        print(f"[*] Пропущен товар #{product.product_id}: нужные проверки уже выполнены, данные магазина не изменились.")
                        skipped += 1
                        continue
                    if not keys and product.master_key:
                        seen_master_keys.add(product.master_key)
                    selected.append(product)
                if selected:
                    if judge is None:
                        key = getattr(args, "openai_key", None) or getattr(args, "gemini_key", None) or settings.OPENAI_API_KEY or os.environ.get("OPENAI_API_KEY")
                        if not key:
                            raise ValueError("Не задан OPENAI_API_KEY: проверку моделью невозможно начать.")
                        judge = OpenAIJudge(api_key=key)
                        resolver = ResolveReferenceUseCase(
                            repository=repo, vendor_resolver=VendorDirectResolver(),
                            serper_client=SerperClient(api_key=settings.SERPER_API_KEY),
                            verify_direct_urls=True, judge=judge,
                        )
                        crawler = WebCrawler(max_concurrent=concurrency, headless=not args.no_headless, timeout_ms=30000)
                        audit = AuditProductUseCase(product_repo=repo, reference_resolver=resolver, crawler=crawler, judge=judge)
                        print("=" * 80)
                        print("TELECOM CATALOG AUDITOR :: STAGE 4 FACTUAL ARBITER")
                        print(f"Target DB     : {db_path.resolve()}")
                        print(f"LLM Engine    : OpenAI (model: {settings.OPENAI_MODEL})")
                        print(f"Workers       : {concurrency}")
                        print("=" * 80)
                        await crawler.start()
                        tui = AuditTUI(console, 0, concurrency, photos_only)
                        if console.is_terminal:
                            live = Live(tui.render_live_panel(), console=console, refresh_per_second=4, transient=True)
                            live.start()

                    queue: asyncio.Queue[Product] = asyncio.Queue()
                    for product in selected:
                        queue.put_nowait(product)
                    completed_cards: dict[int, str] = {}
                    next_to_print = processed + 1
                    fatal: Exception | None = None

                    async def worker(worker_id: int) -> None:
                        nonlocal processed, unsuccessful, findings, fatal, next_to_print
                        nonlocal total_time, total_tokens, total_cost
                        while not queue.empty() and fatal is None:
                            product = queue.get_nowait()
                            processed += 1
                            index = processed
                            if tui is not None:
                                tui.set_worker_status(worker_id, f"Auditing #{product.product_id} | {product.title[:150]}")
                                if live is not None:
                                    live.update(tui.render_live_panel())
                            try:
                                result = await audit.audit_product(
                                    product=product,
                                    reference_url=args.reference_url if keys and len(explicit_products) == 1 else None,
                                    force_refresh=args.force_refresh,
                                    skip_desc=skip_desc,
                                    skip_tech=getattr(args, "skip_tech", False),
                                    photos_only=photos_only,
                                    prior_result=previous.get(product.product_id),
                                    prior_loaded=True,
                                )
                            except asyncio.CancelledError:
                                unsuccessful += 1
                                raise
                            except Exception as exc:
                                unsuccessful += 1
                                fatal = exc
                                if settings.DEBUG:
                                    logging.getLogger(__name__).exception("Audit worker stopped")
                                completed_cards[index] = f"[!] Товар #{product.product_id}: обработка прервана общей ошибкой."
                            else:
                                if tui is not None:
                                    tui.record_completed(result.status, photos_only, bool(result.photos_error), result.images_status)
                                failed = bool(result.photos_error) if photos_only else result.status in (AuditStatus.ERROR, AuditStatus.NOT_FOUND)
                                unsuccessful += int(failed)
                                has_findings = (result.images_status not in ("OK", "UNKNOWN")) if photos_only else bool(
                                    result.status in (AuditStatus.MISMATCH, AuditStatus.MISSING_SPECS)
                                    or result.description_discrepancies
                                    or result.images_status not in ("OK", "UNKNOWN")
                                )
                                findings += int(not failed and has_findings)
                                total_time += result.execution_time_sec or 0.0
                                total_tokens += result.input_tokens + result.output_tokens
                                total_cost += result.estimated_cost_usd or 0.0
                                completed_cards[index] = format_audit_card(index, None, product, result)
                                if failed:
                                    completed_cards[index] += "\n[!] Товар не удалось проверить. Продолжаю проверку остальных товаров."
                            while next_to_print in completed_cards:
                                console.print(completed_cards.pop(next_to_print))
                                next_to_print += 1
                            queue.task_done()
                            if tui is not None:
                                tui.set_worker_status(worker_id, "Idle")
                                if live is not None:
                                    live.update(tui.render_live_panel())

                    workers = [asyncio.create_task(worker(worker_id)) for worker_id in range(1, min(concurrency, len(selected)) + 1)]
                    try:
                        await asyncio.gather(*workers)
                    finally:
                        for task in workers:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*workers, return_exceptions=True)
                    if fatal is not None:
                        raise fatal
                if limit and processed >= limit:
                    reason = f"достигнут лимит {limit} товаров; остаток выборки не проверялся"
                    break
        finally:
            await stream.aclose()
        if processed == 0:
            print("[*] Выборка закончилась. Новых товаров для проверки нет.")
        exit_code = 1 if unsuccessful else 0
    except asyncio.CancelledError:
        aborted = True
        reason = "остановлен пользователем; выборка не закончена"
        exit_code = 130
    except (MySQLReaderError, sqlite3.Error, OSError, ValueError) as exc:
        aborted = True
        reason = str(exc)
        exit_code = 2
        print(f"[!] Запуск прерван: {reason}")
        if settings.DEBUG:
            logging.getLogger(__name__).exception("Audit run stopped")
    except Exception as exc:
        aborted = True
        reason = f"невозможно продолжить обработку: {exc}"
        exit_code = 2
        print(f"[!] Запуск прерван: {reason}")
        if settings.DEBUG:
            logging.getLogger(__name__).exception("Audit run stopped unexpectedly")
    finally:
        if live is not None:
            live.stop()
        try:
            if crawler is not None:
                await crawler.close()
        except Exception as exc:
            aborted = True
            exit_code = 2
            reason = f"ошибка закрытия браузера: {exc}"
        finally:
            if judge is not None:
                try:
                    await judge.aclose()
                except Exception as exc:
                    aborted = True
                    exit_code = 2
                    reason = f"ошибка закрытия клиента модели: {exc}"
        state = "прерван" if aborted else "завершён"
        print(f"Итог: запуск {state} — {reason}.")
        print(f"Обработано: {processed} | Пропущено: {skipped} | Не удалось проверить: {unsuccessful} | С замечаниями: {findings}.")
        if aborted:
            print("Уже сохранённые отчёты сохранены. Выборка не закончена.")
        if processed > 1 and total_tokens > 0:
            wall = time.perf_counter() - started
            print(f"BATCH METRICS: Wall Time: {wall:.2f}s | Total Time: {total_time:.2f}s | Total Tokens: {total_tokens:,} | Total Est. Cost: ~${total_cost:.5f}")
    return exit_code


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
        help="Max audit attempts (default: 5; 0: all selected products)",
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
        help="Check shop photo count against a confirmed matching reference; uses search, crawl and model verification",
    )
    audit_parser.add_argument(
        "--source",
        choices=["mysql", "crawler"],
        default="mysql",
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
        try:
            exit_code = asyncio.run(run_audit_command(args))
        except KeyboardInterrupt:
            exit_code = 130
        sys.exit(exit_code)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
