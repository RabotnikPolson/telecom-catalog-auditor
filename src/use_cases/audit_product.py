import asyncio
from datetime import datetime, timezone
import json
import time
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
        t_audit_start = time.perf_counter()
        crawler_time_sec = 0.0
        urls_to_try: list[str] = []
        if reference_url:
            urls_to_try = [reference_url]
        else:
            resolver_fn = getattr(self.reference_resolver, "execute", None) or getattr(self.reference_resolver, "resolve", None)
            if resolver_fn:
                try:
                    call_res = resolver_fn(product, force_refresh=force_refresh)
                except TypeError:
                    call_res = resolver_fn(product)
                resolved = await call_res if asyncio.iscoroutine(call_res) else call_res
            else:
                resolved = None
            resolved_url = (getattr(resolved, "reference_url", None) or getattr(resolved, "url", None)) if resolved else None
            candidates = getattr(resolved, "candidate_urls", []) if resolved else []
            if resolved_url and resolved_url not in candidates:
                candidates.insert(0, resolved_url)

            if not candidates:
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
                    execution_time_sec=round(time.perf_counter() - t_audit_start, 2),
                )
                self.product_repo.save_audit_result(result)
                return result
            urls_to_try = candidates

        external_markdown: str | None = None
        target_url = urls_to_try[0]
        crawl_error = None

        for cand_url in urls_to_try:
            target_url = cand_url
            if not force_refresh:
                cached_raw = self.product_repo.get_specs_cache(cand_url)
                if cached_raw:
                    try:
                        parsed = json.loads(cached_raw)
                        external_markdown = parsed.get("markdown", cached_raw) if isinstance(parsed, dict) else cached_raw
                    except Exception:
                        external_markdown = cached_raw
                    if external_markdown and len(external_markdown.strip()) > 0:
                        break

            t_crawl_start = time.perf_counter()
            crawl_res = await self.crawler.crawl(cand_url)
            crawler_time_sec += (time.perf_counter() - t_crawl_start)
            if crawl_res.success and crawl_res.markdown and len(crawl_res.markdown.strip()) > 0:
                external_markdown = crawl_res.markdown
                cache_payload = json.dumps(
                    {
                        "title": crawl_res.title,
                        "markdown": crawl_res.markdown,
                    },
                    ensure_ascii=False,
                )
                self.product_repo.save_specs_cache(
                    resolved_url=cand_url,
                    specs_json=cache_payload,
                    status_code=crawl_res.status_code,
                )
                break
            else:
                crawl_error = crawl_res.error or f"Failed crawling {cand_url}"
                continue

        if not external_markdown:
            if target_url:
                self.product_repo.delete_url_cache_by_url(target_url)
                self.product_repo.delete_specs_cache(target_url)

            result = AuditResult(
                product_id=product.product_id,
                status=AuditStatus.ERROR,
                confidence_score=0.0,
                reference_url=target_url,
                discrepancies=[],
                matched_specs_count=0,
                total_specs_count=len(product.current_specs),
                audited_at=datetime.now(timezone.utc),
                details=f"Crawler error: {crawl_error}",
                crawler_time_sec=round(crawler_time_sec, 2),
                execution_time_sec=round(time.perf_counter() - t_audit_start, 2),
            )
            self.product_repo.save_audit_result(result)
            return result

        result = await self.judge.judge(
            product=product,
            reference_url=target_url,
            external_markdown=external_markdown,
        )
        if result.status in (AuditStatus.NOT_FOUND, AuditStatus.ERROR) and target_url:
            self.product_repo.delete_url_cache_by_url(target_url)
            self.product_repo.delete_specs_cache(target_url)

        result.crawler_time_sec = round(crawler_time_sec, 2)
        result.execution_time_sec = round(time.perf_counter() - t_audit_start, 2)
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
