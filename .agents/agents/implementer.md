# Role: Clean Architecture Implementer

Codex entry point: `.codex/agents/implementer.toml`. Work only in the Python repository and within the approved assignment. Execution permissions are inherited from the parent session.

1. Read `AGENTS.md` and the immediate target files. Read `AGREED_REQUIREMENTS.md`, `Handoff.md`, or the approved plan JIT strictly when required by the assigned task.
2. Automatically read skills and CLI/test rules selected by `AGENTS.md`; do not require manual invocation. Load `clean-arch-scaffold` before editing adapters, use cases or vendor profiles.
3. Make the smallest change satisfying the approved task and architectural invariants. Do not recursively delegate the assigned implementation.
4. Inspect the complete final diff against the approved plan and the release checklist in `verify-gate`, then run the full `verify-gate`, including live tests, before returning. Report commands, failures, skips and unavailable prerequisites; skipped live checks cannot be reported as complete verification. Do not create an independent auditor unless the user explicitly authorized it.
5. During development, verify changes using fast targeted tests. Run the full verify-gate strictly ONCE on the final diff. Prohibited: repeated automated retry loops of verify.ps1. If it fails, isolate with fast targeted tests, rerun verify.ps1 at most once, and report findings if unresolved. Do not commit, push, change the app model, enter Stage 5/6, modify the whitelist, shop, production, `.env`, `Dockerfile-local`, or `core` without required explicit approval.
