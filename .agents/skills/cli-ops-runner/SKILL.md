---
name: cli-ops-runner
description: Reference for running main.py CLI commands (categories, crawl, resolve, audit, live-test), single-product debugging with --limit 1 and --no-headless, and inspecting catalog_audit.db state. Use when executing CLI runs or debugging products.
---

# CLI Operations & Debugging Guide

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

# Fast MySQL sync: stream directly from shop database (recommended for bulk)
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

# Fast photos-only compliance audit (offline, zero LLM calls)
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
- Add `--no-headless` to visually observe Playwright navigation.
- Check SQLite database state:
```python
from src.adapters.db.sqlite_repo import SQLiteProductRepository
repo = SQLiteProductRepository("catalog_audit.db")
products = repo.get_auditable_products(limit=5)
for p in products:
    print(f"ID: {p.product_id} | Hash: {p.content_hash[:16]}... | Status: {p.status}")
```
