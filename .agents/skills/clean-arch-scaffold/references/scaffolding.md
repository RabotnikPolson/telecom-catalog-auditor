# New component scaffolding

Read this file only when creating a new use case or adapter.

## Use case

- Put it under `src/use_cases/`.
- Inject repositories and gateways through `__init__`.
- Use explicit async return types and return domain entities.
- Add behavior tests in the applicable existing stage file unless a separate test module is justified in the approved plan.

## Adapter

- Put it under the appropriate `src/adapters/` package.
- Reuse clients where supported and close owned resources in `try...finally`.
- Use the existing `DomainError` hierarchy.
- Add rate limiting only when the external service requires it; do not introduce global mutable state.

## Vendor profile

- Add only the approved profile fields in `config/vendor_profiles.py`.
- Validate through the existing CLI on an explicit verification SQLite copy after reading `cli-ops-runner`.
- A vendor profile does not authorize a whitelist change.
