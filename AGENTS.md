# AGENTS.md — Telecom Catalog Auditor Constitution

Autonomous Python 3.13+ microservice for factual auditing of `shop.telecom.kz` (~20,000 items) using Clean Architecture.

## 0. ORCHESTRATION & STOP-POINT PROTOCOL
1. **Plan First (`>1 file` or architecture change)**: **STOP**. Create an Implementation Plan Artifact (or write to `plans/active_plan.md`) with affected files, data flow, risks, and **WAIT** for explicit user confirmation ("Proceed", "OK").
2. **Hard Locks**: **NEVER** proceed to Stage 5 (Excel) or Stage 6 (FastAPI) and **NEVER** modify `config/whitelist_domains.py` without explicit user approval.
3. **Subagent Delegation**:
  - For multi-file coding: delegate implementation to the `implementer` subagent via `invoke_subagent`.
  - Before reporting ANY code change complete: invoke the `auditor` subagent (or run the `verify-gate` skill) to validate compliance.
4. **Zero Overengineering**: Smallest possible diff. Modify ONLY target files. **PROHIBITED**: `# TODO`, `pass`, `...`, `NotImplementedError`, or root debug scripts (`test_foo.py`). Debug via CLI with `--limit 1`.

## 1. CORE ARCHITECTURAL INVARIANTS (NON-NEGOTIABLE)
- **Stack & Types**: Python 3.13+, Pydantic v2 (`ConfigDict(frozen=True, extra="ignore")`), explicit `str | None` (no `Optional`/`Any`). Pass all dependencies via `__init__` (zero global state).
- **SQLite JIT Persistence ONLY**: Remote MySQL (`READ ONLY`, `READ UNCOMMITTED`) is the source of truth. Bulk-inserting catalog items (`repo.batch_upsert` during streaming) is **STRICTLY FORBIDDEN**. Save to SQLite `products` ONLY inside `AuditProductUseCase.audit_product(prod)` right before writing `audit_results`.
- **Schema Safety**: Migrations must work on existing `catalog_audit.db`. Never drop unique indexes backing `ON CONFLICT`. Bump `PRAGMA user_version` on schema change.
- **Category Fallback Ban**: Local SQLite has no category data — with `--group`/`--main-cat` never fall back to it; if MySQL finds nothing, exit 0.
- **In-Memory & O(1) Hygiene**: Master/child grouping (`ParentChildGrouper`) and `--skip-audited` SHA-256 hash checks run 100% in RAM. **Zero N+1 queries** inside loops.
- **Resource Lifecycle**:
  - `httpx.AsyncClient`: Always close in `try...finally: if should_close: await client.aclose()`. No early `return` before `try...finally`.
  - `Playwright`: Close page/context in `try...finally`. Block images/fonts/analytics. **NEVER** use `networkidle` (use `wait_for_selector` timeout=2000ms + 300ms debounce). Truncate DOM to 50k chars.
  - `MySQL`: Stream via `fetchmany(batch_size)`, NEVER `fetchall()`.
- **Security & Output**: Parameterized SQL only (`?` / `%s`). Load secrets via `config/settings.py`. Run `setup_utf8_terminal` at startup; `print()` allowed ONLY in `cli.py`.

## 2. PROJECT MAP
- `src/domain/`: `entities.py` (`Product`, `AuditResult`, `AuditStatus`), `services.py` (`ParentChildGrouper`), `exceptions.py`.
- `src/use_cases/`: `crawl_shop_catalog.py`, `resolve_reference.py` (5 scenarios), `audit_product.py` (JIT write + metrics).
- `src/adapters/`: `db/sqlite_repo.py`, `catalog/mysql_reader.py`, `catalog/telecom_crawler.py`, `crawler/web_crawler.py`, `search/vendor_direct.py`, `search/serper_client.py`, `llm/openai_judge.py` (`gpt-6-luna`, `reasoning_effort="low"`, JSON mode).
- `src/entrypoints/cli.py`: CLI entry (`crawl`, `resolve`, `live-test`, `audit`).
- `config/`: `settings.py`, `vendor_profiles.py`, `whitelist_domains.py`.
