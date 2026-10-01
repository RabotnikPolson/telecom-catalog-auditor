import asyncio
from datetime import datetime, timezone
import json
from typing import Sequence

from src.adapters.crawler.web_crawler import WebCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.llm.gemini_judge import GeminiJudge
from src.domain.entities import AuditResult, AuditStatus, Product
from src.use_cases.resolve_reference import ResolveReferenceUseCase


class AuditProductUseCase:

    def __init__(
        self,
        product_repo: SQLiteProductRepository,
        reference_resolver: ResolveReferenceUseCase,
        crawler: WebCrawler,
        judge: GeminiJudge,
    ) -> None:
        self.product_repo = product_repo
        self.reference_resolver = reference_resolver
        self.crawler = crawler
        self.judge = judge

    async def audit_product(
        self,
        product: Product,
        reference_url: str | None = None,
        force_refresh: bool = False,
    ) -> AuditResult:
        target_url = reference_url
        if not target_url:
            resolver_fn = getattr(self.reference_resolver, "execute", None) or getattr(self.reference_resolver, "resolve", None)
            if resolver_fn:
                call_res = resolver_fn(product)
                resolved = await call_res if asyncio.iscoroutine(call_res) else call_res
            else:
                resolved = None
            resolved_url = (getattr(resolved, "reference_url", None) or getattr(resolved, "url", None)) if resolved else None
            if not resolved or not resolved_url:
                result = AuditResult(
                    product_id=product.product_id,
                    status=AuditStatus.NOT_FOUND,
                    confidence_score=0.0,
                    reference_url=None,
                    discrepancies=[],
                    matched_specs_count=0,
                    total_specs_count=len(product.current_specs),
                    audited_at=datetime.now(timezone.utc),
                    details="Reference URL could not be resolved from suppliers or search",
                )
                self.product_repo.save_audit_result(result)
                return result
            target_url = resolved_url

        external_markdown: str | None = None
        if not force_refresh:
            cached_raw = self.product_repo.get_specs_cache(target_url)
            if cached_raw:
                try:
                    parsed = json.loads(cached_raw)
                    external_markdown = parsed.get("markdown", cached_raw) if isinstance(parsed, dict) else cached_raw
                except Exception:
                    external_markdown = cached_raw

        if not external_markdown:
            crawl_res = await self.crawler.crawl(target_url)
            if not crawl_res.success:
                result = AuditResult(
                    product_id=product.product_id,
                    status=AuditStatus.ERROR,
                    confidence_score=0.0,
                    reference_url=target_url,
                    discrepancies=[],
                    matched_specs_count=0,
                    total_specs_count=len(product.current_specs),
                    audited_at=datetime.now(timezone.utc),
                    details=f"Crawler error: {crawl_res.error}",
                )
                self.product_repo.save_audit_result(result)
                return result

            external_markdown = crawl_res.markdown
            cache_payload = json.dumps(
                {
                    "title": crawl_res.title,
                    "markdown": crawl_res.markdown,
                },
                ensure_ascii=False,
            )
            self.product_repo.save_specs_cache(
                resolved_url=target_url,
                specs_json=cache_payload,
                status_code=crawl_res.status_code,
            )

        result = await self.judge.judge(
            product=product,
            reference_url=target_url,
            external_markdown=external_markdown,
        )
        self.product_repo.save_audit_result(result)
        return result

    async def audit_by_product_id(
        self,
        product_id: int,
        reference_url: str | None = None,
        force_refresh: bool = False,
    ) -> AuditResult:
        product = self.product_repo.get_product_by_id(product_id)
        if not product:
            raise ValueError(f"Product with id={product_id} not found in database")
        return await self.audit_product(
            product=product,
            reference_url=reference_url,
            force_refresh=force_refresh,
        )

    async def audit_batch(
        self,
        products: Sequence[Product],
        force_refresh: bool = False,
        concurrency: int = 2,
    ) -> list[AuditResult]:
        semaphore = asyncio.Semaphore(concurrency)

        async def _worker(p: Product) -> AuditResult:
            async with semaphore:
                return await self.audit_product(
                    product=p,
                    force_refresh=force_refresh,
                )

        tasks = [_worker(p) for p in products]
        return await asyncio.gather(*tasks)
