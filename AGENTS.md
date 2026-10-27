# AGENTS.md — Telecom Catalog Auditor Constitution

Autonomous Python 3.13+ microservice for factual auditing of `shop.telecom.kz` (~20,000 items) using Clean Architecture.

## CODEX CONTEXT & AUTOMATIC ROUTING
- These instructions apply only to `telecom_catalog_auditor`. `core` is a separate PHP/Laravel repository with different rules and checks; do not read, edit or run it during this migration. Moving to it requires a separate discussion.
- At task start read this file, `AGREED_REQUIREMENTS.md`, and `plans/current.md`. Read only the current plan linked there. Use `Handoff.md` only when resuming interrupted work or when the current pointer says it is needed. Files under `plans/archive/` are history: do not read them unless a concrete conflict requires it. Historical claims require verification and do not grant authorization.
- Select and read skills automatically by task; users need not invoke them manually:
  - `.agents/skills/clean-arch-scaffold/SKILL.md` before editing `src/use_cases/`, `src/adapters/`, or `config/vendor_profiles.py`.
  - `.agents/skills/cli-ops-runner/SKILL.md` before CLI operations or product debugging.
  - `.agents/skills/verify-gate/SKILL.md` before completing code changes or when asked to validate the project. The acting agent runs the full gate once on the final diff, including live tests; report skipped or unavailable checks and never call incomplete live verification a full success.
- For `main.py`, `src/entrypoints/**/*.py`, or `tests/**/*.py`, read `.agents/rules/cli-and-tests.md`. Its applicability comes from this instruction, not Antigravity glob metadata or Codex command-permission `.rules` files.
- Optional role entry points are `.codex/agents/implementer.toml` and `.codex/agents/auditor.toml`; detailed instructions live in `.agents/agents/`. The main agent does not spawn them automatically. Use a subagent only after the user explicitly authorizes that specific delegation; do not invent `invoke_subagent`.
- Human involvement is for approving plans and material decisions; automatic routing does not bypass those boundaries. Do not change the application's judge model to match the Codex agent model.
- Do not change the shop or write to production without separate explicit approval. Do not change or stage secrets, databases, local `.env`, or `Dockerfile-local`; no GitLab push is authorized by this migration.
- Keep context lean: use `rg`, targeted line ranges, `git diff --stat` and diffs of approved files. Do not dump complete large source files, archived plans, full databases, search traces or an unbounded repository diff when a focused read answers the question.

## 0. ORCHESTRATION & STOP-POINT PROTOCOL
1. **Plan First (`>1 file` or architecture change)**: **STOP**. Create a focused plan under `plans/`, update `plans/current.md` to point to it, include affected files, data flow, risks and expected user-visible output, then **WAIT** for explicit user confirmation ("Proceed", "OK"). Never overwrite a completed or archived plan.
2. **Hard Locks**: **NEVER** proceed to Stage 5 (Excel) or Stage 6 (FastAPI) and **NEVER** modify `config/whitelist_domains.py` without explicit user approval.
3. **Single-Agent Delivery by Default**:
  - The main agent in the active chat owns the approved plan, implementation, full-diff self-review, testing, final report and manual acceptance instructions. Do not spawn an `implementer`, `auditor` or other subagent automatically.
  - Before reporting a code change complete, inspect the complete `git diff` against the approved plan and the release checklist in `.agents/skills/verify-gate/SKILL.md`, then run the canonical full gate once on the final state. Focused tests during development do not replace that gate. Correct in-scope failures and rerun the affected checks and final gate; report unrelated failures without expanding scope.
  - Subagents are optional and require the user's explicit approval for a named, bounded task. Consider requesting an independent `auditor` only for high-risk work such as schema migrations, security or whitelist changes, FastAPI/core integration, permissions, MySQL/production writes, recovery protocols or major architecture changes. Risk alone is not authorization to spawn one.
  - If the user explicitly requests an `auditor`, it is the Codex code reviewer, not the product-auditing service. It may run checks and create their temporary outputs but must never edit source files. Roles inherit execution permissions; these instructions do not create an isolated sandbox.
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
