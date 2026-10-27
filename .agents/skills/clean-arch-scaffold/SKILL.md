---
name: clean-arch-scaffold
description: Project contracts for changes to use cases, adapters, database persistence, reference resolution, the OpenAI judge, or vendor profiles. Use only when editing those areas.
---

# Clean Architecture Contracts

Preserve constructor dependency injection, domain entities at boundaries, Python 3.13 typing and the invariants in `AGENTS.md`. Make the smallest cohesive change in the approved files.

Read only the reference needed for the current edit:

- Reference resolution, search cascade, judge or vendor profile: [references/reference-and-judge.md](references/reference-and-judge.md).
- SQLite, cache, migrations or MySQL streaming: [references/persistence.md](references/persistence.md).
- Creating a new use case or adapter: [references/scaffolding.md](references/scaffolding.md).

Do not load unrelated references. This skill does not authorize whitelist, model, dependency, schema, shop or production changes.
