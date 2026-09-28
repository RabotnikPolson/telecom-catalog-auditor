import argparse
import asyncio
import io
import sys
from pathlib import Path
from src.adapters.catalog.telecom_crawler import TelecomShopCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.use_cases.crawl_shop_catalog import CrawlShopCatalogUseCase


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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.subcommand == "crawl":
        exit_code = asyncio.run(run_crawl_command(args))
        sys.exit(exit_code)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
