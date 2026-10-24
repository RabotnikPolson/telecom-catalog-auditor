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
3. Select this skill automatically before completing changes; manual invocation is unnecessary. Implementer and independent Codex code auditor each run the full gate. Auditor may create check outputs but never edits source files.
4. **Self-Correction Loop (implementer only)**: Correct failures only within the approved task and target files, then re-run (up to 3 attempts). Report unrelated failures without expanding scope. Auditor returns findings for the implementer.
5. Include configured live tests. Record skipped tests, unavailable network, credentials, source DB and unexecuted stages. A partial run or skipped live tests is incomplete verification even with exit code 0. Preserve ruff, pytest, mypy and CLI smoke checks. `mypy` already uses strict settings in `pyproject.toml`; `-Strict` does not add a stricter mode.

6. The helper attempts all four stages and aggregates failures. Exit 0 means all required checks, including all required live MySQL tests, passed without skips; exit 1 means failure; exit 2 means incomplete verification (for example skipped/missing live tests or absent source SQLite). Report each outcome rather than just the exit code.
7. Each run owns a UUID directory under repository `scratch/`, its JUnit report and smoke SQLite copy/sidecars. The source DB is opened read-only for SQLite backup. Missing `catalog_audit.db` explicitly skips smoke instead of creating an empty source. Cleanup validates containment and removes only exact run-owned files, then the directory only if empty; unrelated files are retained. The old fixed `scratch_smoke.db` is never touched.
8. `-Strict` is accepted only for compatibility with a deprecation warning; ordinary mypy already uses the same strict configuration. Full pytest collection is preserved; the unique JUnit report supplies explicit live pass/skip/failure/missing counts and skipped-test reasons.
