---
name: verify-gate
description: Runs the mandatory Windows PowerShell verification gate (ruff, pytest, mypy, and a CLI smoke test on a copy of catalog_audit.db). Use this before finishing any code modification or when the user asks to test/validate the project.
---

# Mandatory Verification Gate

## Instructions
1. Execute the verification suite directly via the canonical helper script:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .agents/skills/verify-gate/scripts/verify.ps1
   ```
2. Helper script: [scripts/verify.ps1](./scripts/verify.ps1)
3. Select this skill automatically before completing changes; manual invocation is unnecessary. The acting agent runs the full gate once on the final diff. Do not spawn an implementer or independent auditor automatically and do not duplicate an unchanged successful gate unless the user explicitly requests an independent review.
4. **No-Loop Policy on Full Gate**: Do NOT re-run `verify.ps1` in an automated multi-attempt loop. If any check in `verify.ps1` fails: isolate the failing component, reproduce and fix using a fast targeted command (`python -m pytest tests/test_xxx.py -k <test> -q --tb=short` or `python -m ruff check <file>`), and re-run `verify.ps1` at most ONCE. If it still does not pass, halt immediately and present findings to the user without burning additional context.
5. Include configured live tests. Record skipped tests, unavailable network, credentials, source DB and unexecuted stages. A partial run or skipped live tests is incomplete verification even with exit code 0. Preserve ruff, pytest, mypy and CLI smoke checks. `mypy` already uses strict settings in `pyproject.toml`; `-Strict` does not add a stricter mode.

6. The helper attempts all four stages and aggregates failures. Exit 0 means all required checks, including all required live MySQL tests, passed without skips; exit 1 means failure; exit 2 means incomplete verification (for example skipped/missing live tests or absent source SQLite). Report each outcome rather than just the exit code.
7. Each run owns a UUID directory under repository `scratch/`, its JUnit report and smoke SQLite copy/sidecars. The source DB is opened read-only for SQLite backup. Missing `catalog_audit.db` explicitly skips smoke instead of creating an empty source. Cleanup validates containment and removes only exact run-owned files, then the directory only if empty; unrelated files are retained. The old fixed `scratch_smoke.db` is never touched.
8. `-Strict` is accepted only for compatibility with a deprecation warning; ordinary mypy already uses the same strict configuration. Full pytest collection is preserved; the unique JUnit report supplies explicit live pass/skip/failure/missing counts and skipped-test reasons.

## Final-Diff Self-Review

Before reporting completion, the acting agent inspects the complete final diff and records any applicable violation of this checklist:

- Explicit Python 3.13 type hints (`str | None`) and domain entities instead of raw dictionaries at domain boundaries.
- Constructor dependency injection and no new global mutable state.
- No `repo.batch_upsert` during catalog streaming; SQLite product persistence remains JIT inside the approved audit flow.
- Schema migrations preserve unique indexes backing `ON CONFLICT`; category filters never fall back to local SQLite.
- `httpx.AsyncClient`, Playwright pages/contexts and MySQL resources close reliably in `try...finally` paths.
- Vendor HTTP checks, Playwright blocking/wait rules and resolver candidate handling continue to satisfy `AGENTS.md` where the changed code touches them.
- `config/whitelist_domains.py` is unchanged unless the user explicitly approved it.
- Tests cover real behavior and configured live checks; mocked focused tests never replace the full gate.

The final report states `PASS`, `FAIL` or `INCOMPLETE` for the approved stage, cites exact files for violations, and separates source problems from unavailable or skipped checks. Independent review is reported only when the user explicitly requested it and it actually occurred.
