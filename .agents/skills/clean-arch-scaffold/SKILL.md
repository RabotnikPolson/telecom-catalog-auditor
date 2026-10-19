---
name: clean-arch-scaffold
description: Domain contracts for Reference Resolution (5 scenarios), OpenAI NLI Judge, Two-Level Caching, and scaffolding new UseCases, Adapters, or Vendor Profiles. Use when editing use_cases/, adapters/, or vendor_profiles.py.
---

# Domain Contracts & Scaffolding Guide

## 1. Reference Resolution Cascade (`resolve_reference.py`)
- **5 Scenarios**: `SCENARIO_1_VENDOR_DIRECT` through `SCENARIO_5_INSUFFICIENT_DATA`.
- **Cascade Order (Scenarios 2–4)**: 1) Clean model query → 2) Quoted `"<barcode>"` (NO `barcode:` operator) → 3) Scoped marketplace (`site:kaspi.kz OR site:dns-shop.kz ...`).
- **Concurrency & Timeouts**: Run vendor check (`timeout=2.0s`, cache in `_last_checked_response`) and Serper cascade in parallel via `asyncio.gather`. Return full sorted `candidate_urls` pool (`Kaspi -> DNS -> Official -> Whitelisted`).

## 2. Semantic NLI Arbitrage (`openai_judge.py`)
- **Model**: OpenAI `gpt-6-luna` (configured via `config/settings.py`) via `httpx`, `reasoning_effort="low"`, `response_format={"type": "json_object"}` (NO custom `temperature`). Rates: $0.10/1M in ($0.05 cached), $0.50/1M out.
- **No Regex Heuristics**: Never filter sub-modifications via regex; require exact `proof_quote` from LLM NLI prompt. Print CLI output sequentially.

## 3. Two-Level SQLite Caching
- `url_cache`: Key = `SHA256("ref:{product_id}:{normalized_query}")` → `resolved_url`.
- `specs_cache`: Key = `resolved_url` → extracted specs JSON.

## 4. Scaffolding New Components
- **New UseCase (`src/use_cases/<name>.py`)**: Inject repos/gateways via `__init__`, implement `async def`, return Domain Entities, add test in `tests/test_stage_N.py`.
- **New Adapter (`src/adapters/<name>/`)**: Use `asyncio.Semaphore` for rate limits, close clients in `try...finally`, raise `DomainException`.
- **New Vendor Profile**: Add entry with `search_url_template` to `config/vendor_profiles.py` and verify via `python main.py resolve --product-id <ID>`.

### Starter UseCase Template:
```python
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.domain.entities import AuditResult, AuditStatus


class SampleUseCase:
    def __init__(self, repository: SQLiteProductRepository) -> None:
        self.repository = repository

    async def execute(self, product_id: int) -> AuditResult:
        return AuditResult(
            product_id=product_id,
            status=AuditStatus.PENDING,
        )
```

### Starter Adapter Template:
```python
import asyncio
import httpx
from src.domain.exceptions import DomainException


class SampleApiAdapter:
    def __init__(self, max_concurrency: int = 5) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client: httpx.AsyncClient | None = None

    async def fetch_data(self, url: str) -> dict[str, str]:
        should_close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=5.0)
            should_close = True
        try:
            async with self._semaphore:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            raise DomainException(f"Adapter fetch failed: {e}") from e
        finally:
            if should_close:
                await client.aclose()
```
