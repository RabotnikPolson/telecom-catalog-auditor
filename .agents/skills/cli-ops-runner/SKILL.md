---
name: cli-ops-runner
description: Reference for running main.py CLI commands (categories, crawl, resolve, audit, live-test), single-product debugging with --limit 1 and --no-headless, and inspecting catalog_audit.db state. Use when executing CLI runs or debugging products.
---

# CLI Operations & Debugging Guide

Select automatically for CLI operations and product debugging. Commands below are reference examples, not authorization for bulk runs. For debugging use `--limit 1` and an explicit verification SQLite copy via `--db`; never mutate the original DB merely to inspect it. MySQL remains READ ONLY. Category filters never fall back to local SQLite. Do not run commands in `core` or touch shop/production data.

## 1. Catalog & Categories Navigation
```powershell
# Show active main categories
python main.py categories

# Show product groups inside category 1
python main.py categories --main-cat 1

# Search groups by keyword
python main.py categories --search "роутер"
```

## 2. Crawler & Catalog Sync
```powershell
# Night crawler: fetch from web display
python main.py crawl --source crawler --category smartfony-i-aksessuary/telefony --limit 50

# Stream from MySQL (no bulk SQLite product upsert; JIT persistence rule still applies)
python main.py crawl --source mysql --limit 5000
```

## 3. Reference Resolution (Stage 3)
```powershell
# Resolve reference candidate URLs for specific product IDs
python main.py resolve --product-id 74615 76830 --verify

# Resolve with explicit live-sync from MySQL
python main.py resolve --sku 417095 450322 --source mysql --verify
```

## 4. Factual Audit (Stage 4)
```powershell
# Single product factual audit
python main.py audit --sku 417095

# Batch audit by group ID with incremental skip
python main.py audit --group 78 --limit 10 --skip-audited

# Photos-only compliance audit (zero LLM calls; catalog lookup may use MySQL)
python main.py audit --group 78 --limit 50 --photos-only

# Audit specs & photos only (skip description evaluation)
python main.py audit --sku 417095 --skip-desc

# Audit photos & description only (skip specs arbitration)
python main.py audit --sku 417095 --skip-tech

# Force re-fetching without cache
python main.py audit --product-id 40124 --force-refresh
```

## 5. Live Pipeline Testing (Stages 1–3)
```powershell
# Run live crawl & reference resolution test for a batch of product IDs
python main.py live-test --limit 5 --start-id 74615

# Run live test against custom database
python main.py live-test --db catalog_audit.db --limit 10
```

## 6. Single Product Debugging
- Add `--limit 1` to process only one item.
- `audit --no-headless` can show Playwright navigation; this flag is not exposed by every subcommand. Check `<subcommand> --help` first.
- Inspect SQLite through read-only queries (`sqlite3.connect('file:catalog_audit.db?mode=ro', uri=True)`) with explicit connection cleanup. Avoid repository initialization for read-only inspection because it may migrate/write the database. Do not add root debug scripts or non-CLI `print()` calls.
