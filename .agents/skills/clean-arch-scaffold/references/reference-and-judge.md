# Reference resolution and judge

- Preserve the five scenarios in `resolve_reference.py` and the current source order.
- Search cascade: clean model query, quoted barcode without a `barcode:` operator, then scoped marketplace search.
- Vendor check uses a timeout no greater than 2 seconds, reuses `_last_checked_response`, and may run beside search with `asyncio.gather`.
- Return the full sorted candidate pool; do not exit after the first unverified candidate.
- The application judge remains `gpt-6-luna`, `reasoning_effort="low"`, JSON response mode and no custom temperature unless the user separately approves a model change.
- Do not replace semantic comparison with rules or regular expressions tied to a product, SKU or characteristic name. Require factual evidence and exact proof quotes under the approved plan.
