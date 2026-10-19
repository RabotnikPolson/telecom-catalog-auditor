---
trigger: glob
globs: "main.py, src/entrypoints/**/*.py, tests/**/*.py"
description: "CLI architecture conventions, terminal output formatting, and pytest test suite rules"
---

# CLI & Testing Guidelines

## 1. CLI Conventions (`src/entrypoints/cli.py`, `main.py`)
- **UTF-8 Output**: Always call `setup_utf8_terminal()` at CLI entry before any terminal output.
- **Strict Output Boundary**: `print()` and Rich console calls are allowed **ONLY** inside `src/entrypoints/cli.py`. Never use `print()` inside use cases, adapters, or domain entities.
- **On-the-Fly Live Sync Priority**:
  When `--sku`, `--product-id`, or `--url` is passed, refresh products in this strict order:
  `MySQL (if configured) -> web crawler fetch -> local SQLite fallback`.
- **Error Handling**: Catch operational exceptions gracefully and display structured status without crashing the process or swallowing traceback logs in debug mode.

## 2. Testing Framework & Invariants (`tests/`)
- **Framework**: `pytest` with `pytest-asyncio` / `anyio`.
- **Test Stages**:
  - `test_mysql_reader.py` — Live MySQL streaming, dictionary cursor, and fetchmany batching.
  - `test_stage1.py` — Domain entities, `content_hash`, `barcode`, `ParentChildGrouper`, SQLite repository.
  - `test_stage1_db_integrity_and_cache.py` — SQLite status preservation, deduplication upsert, FIFO cache capacity, and TTL expiration.
  - `test_stage2_crawler.py` — Crawler parsing and state persistence.
  - `test_stage2_photos_and_desc.py` — Photos count evaluation and description factual audit.
  - `test_stage3_search.py` — Reference resolver (5 scenarios + cascade).
  - `test_stage4_crawler_and_judge.py` — Full audit pipeline with LLM judge.
- **Configuration Source of Truth**: All tool configurations (`mypy`, `ruff`, `pytest`) are declared centrally in `pyproject.toml`. Run checks directly (`python -m ruff check src/`, `python -m mypy`, `python -m pytest`) without ad-hoc CLI flags.
- **Combat Testing**: Unit tests must validate real behavior and edge cases. Never create fake mock-only scripts that bypass real validation logic.
- **Resource Cleanup in Tests**: Always close temporary SQLite databases, mock servers, and Playwright browsers.