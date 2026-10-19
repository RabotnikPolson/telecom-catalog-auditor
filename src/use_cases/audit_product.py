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


def evaluate_photos_status(shop_count: int, reference_count: int) -> str:
    """
    Evaluates photos compliance according to catalog standards:
    - shop_count == 0 -> NO_IMAGES (CRITICAL)
    - reference_count > 0 and shop_count < reference_count -> LESS_THAN_REFERENCE
    - Target threshold: min(3, reference_count) when reference_count > 0, else 2:
      * shop_count < target -> LOW_COUNT
      * shop_count >= target -> OK
    """
    if shop_count == 0:
        return "NO_IMAGES"
    if reference_count > 0:
        if shop_count < reference_count:
            return "LESS_THAN_REFERENCE"
        target_min = min(3, reference_count)
        if shop_count < target_min:
            return "LOW_COUNT"
        return "OK"
    else:
        if shop_count < 2:
            return "LOW_COUNT"
        return "OK"


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
        skip_desc: bool = False,
        skip_tech: bool = False,
        photos_only: bool = False,
    ) -> AuditResult:
        t_audit_start = time.perf_counter()
        crawler_time_sec = 0.0

        # Just-in-Time: ensure audited product exists in local repo for audit_results foreign key
        if hasattr(self.product_repo, "save_product"):
            try:
                self.product_repo.save_product(product)
            except Exception:
                pass

        # Fast path: photos-only audit (zero network or LLM calls)
        if photos_only:
            img_status = evaluate_photos_status(product.images_count, 0)
            status = AuditStatus.VERIFIED if product.images_count >= 1 else AuditStatus.MISMATCH
            res = AuditResult(
                product_id=product.product_id,
                status=status,
                confidence_score=1.0,
                reference_url=None,
                discrepancies=[],
                missing_specs=[],
                matched_specs_count=len(product.current_specs),
                total_specs_count=len(product.current_specs),
                audited_at=datetime.now(timezone.utc),
                details=f"Photos-only audit: {product.images_count} photos on store ({img_status})",
                images_count=product.images_count,
                reference_images_count=0,
                images_status=img_status,
                description_quality="GOOD",
            )
            self.product_repo.save_audit_result(res)
            return res

        urls_to_try: list[str] = []
        if reference_url:
            urls_to_try = [reference_url]
            search_trace = [f"[Manual Reference] Using direct reference URL override: {reference_url}"]
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

            search_trace = list(getattr(resolved, "search_trace", [])) if resolved else []

            if not candidates:
                search_trace.append("[Audit] No reference candidate URLs available to evaluate -> Status: NOT_FOUND")
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
                    search_trace=search_trace,
                    execution_time_sec=round(time.perf_counter() - t_audit_start, 2),
                    images_count=product.images_count,
                    images_status=evaluate_photos_status(product.images_count, 0),
                )
                self.product_repo.save_audit_result(result)
                return result
            urls_to_try = candidates

        last_result: AuditResult | None = None
        crawl_error: str | None = None

        async def _evaluate_candidates(
            candidates_list: list[str],
            label_prefix: str,
            max_to_test: int | None = None,
        ) -> AuditResult | None:
            nonlocal crawler_time_sec, last_result, crawl_error
            to_test = candidates_list[:max_to_test] if max_to_test else candidates_list
            for cand_idx, cand_url in enumerate(to_test, 1):
                target_url = cand_url
                external_markdown: str | None = None
                ref_images_cnt = 0
                search_trace.append(f"[{label_prefix} {cand_idx}/{len(to_test)}] Fetching specs: {cand_url}")

                if not force_refresh:
                    cached_raw = self.product_repo.get_specs_cache(cand_url)
                    if cached_raw:
                        try:
                            parsed = json.loads(cached_raw)
                            if isinstance(parsed, dict):
                                external_markdown = parsed.get("markdown", cached_raw)
                                ref_images_cnt = int(parsed.get("images_count", 0))
                            else:
                                external_markdown = cached_raw
                            search_trace.append(f"[{label_prefix} {cand_idx}] Loaded specs from local specs_cache")
                        except Exception:
                            external_markdown = cached_raw

                if not external_markdown or len(external_markdown.strip()) == 0:
                    t_crawl_start = time.perf_counter()
                    crawl_res = await self.crawler.crawl(cand_url)
                    crawler_time_sec += (time.perf_counter() - t_crawl_start)
                    if crawl_res.success and crawl_res.markdown and len(crawl_res.markdown.strip()) > 0:
                        external_markdown = crawl_res.markdown
                        ref_images_cnt = getattr(crawl_res, "images_count", 0)
                        cache_payload = json.dumps(
                            {
                                "title": crawl_res.title,
                                "markdown": crawl_res.markdown,
                                "images_count": ref_images_cnt,
                            },
                            ensure_ascii=False,
                        )
                        self.product_repo.save_specs_cache(
                            resolved_url=cand_url,
                            specs_json=cache_payload,
                            status_code=crawl_res.status_code,
                        )
                        search_trace.append(f"[{label_prefix} {cand_idx}] Page crawled successfully (HTTP {crawl_res.status_code or 200})")
                    else:
                        crawl_error = crawl_res.error or f"Failed crawling {cand_url}"
                        search_trace.append(f"[{label_prefix} {cand_idx}] Crawl failed: {crawl_error}")
                        continue

                try:
                    result = await self.judge.judge(
                        product=product,
                        reference_url=target_url,
                        external_markdown=external_markdown,
                        skip_desc=skip_desc,
                    )
                except TypeError:
                    result = await self.judge.judge(
                        product=product,
                        reference_url=target_url,
                        external_markdown=external_markdown,
                    )
                result.crawler_time_sec = round(crawler_time_sec, 2)
                result.execution_time_sec = round(time.perf_counter() - t_audit_start, 2)
                result.images_count = product.images_count
                result.reference_images_count = ref_images_cnt
                result.images_status = evaluate_photos_status(product.images_count, ref_images_cnt)

                search_trace.append(f"[{label_prefix} {cand_idx}] LLM Judge verdict: {result.status.value} (Confidence: {result.confidence_score:.2f})")
                last_result = result

                if result.status in (AuditStatus.NOT_FOUND, AuditStatus.ERROR):
                    if target_url:
                        self.product_repo.delete_url_cache_by_url(target_url)
                        self.product_repo.delete_specs_cache(target_url)
                    search_trace.append(f"[{label_prefix} {cand_idx}] Rejected as non-matching ({result.details or result.status.value}), cascading to next candidate...")
                    continue

                search_trace.append(f"[{label_prefix} {cand_idx}] Accepted as authoritative reference ({result.status.value})")
                return result

            return None

        winning_result: AuditResult | None = None
        is_step1_code = (resolved.source_type == "CODE_PINPOINT")
        step1_max = 2 if is_step1_code else None

        if urls_to_try:
            winning_result = await _evaluate_candidates(
                urls_to_try,
                label_prefix="Step 1 Candidate" if is_step1_code else "Candidate",
                max_to_test=step1_max,
            )

        # Escalate to Step 2 (Marketing Model Search) if Step 1 pinpoint candidates were rejected or exhausted
        if winning_result is None and is_step1_code:
            search_trace.append(
                f"[Escalate] Step 1 pinpoint candidates rejected by AI Judge (tested {min(len(urls_to_try), step1_max or 2)}). "
                "Escalating to Step 2 (Marketing Model Search)..."
            )
            if hasattr(self.reference_resolver, "resolve_marketing_fallback"):
                resolved_step2 = await self.reference_resolver.resolve_marketing_fallback(
                    product=product, force_refresh=force_refresh, trace=search_trace
                )
                if resolved_step2.candidate_urls:
                    winning_result = await _evaluate_candidates(
                        resolved_step2.candidate_urls,
                        label_prefix="Step 2 Candidate",
                        max_to_test=None,
                    )

        if winning_result:
            winning_result.product_id = product.product_id
            winning_result.search_trace = search_trace
            self.product_repo.save_audit_result(winning_result)
            return winning_result

        if last_result:
            search_trace.append("[Evaluation] All candidate URLs exhausted without a verified match.")
            last_result.product_id = product.product_id
            last_result.search_trace = search_trace
            self.product_repo.save_audit_result(last_result)
            return last_result

        search_trace.append(f"[Evaluation] All candidates failed. Crawler error: {crawl_error or 'No candidates accessible'}")
        result = AuditResult(
            product_id=product.product_id,
            status=AuditStatus.ERROR,
            confidence_score=0.0,
            reference_url=urls_to_try[0] if urls_to_try else None,
            discrepancies=[],
            matched_specs_count=0,
            total_specs_count=len(product.current_specs),
            audited_at=datetime.now(timezone.utc),
            details=f"Crawler error: {crawl_error or 'No candidates accessible'}",
            search_trace=search_trace,
            crawler_time_sec=round(crawler_time_sec, 2),
            execution_time_sec=round(time.perf_counter() - t_audit_start, 2),
            images_count=product.images_count,
            images_status=evaluate_photos_status(product.images_count, 0),
        )
        self.product_repo.save_audit_result(result)
        return result

    async def audit_by_product_id(
        self,
        product_id: int,
        reference_url: str | None = None,
        force_refresh: bool = False,
        skip_desc: bool = False,
        skip_tech: bool = False,
        photos_only: bool = False,
    ) -> AuditResult:
        product = self.product_repo.get_product_by_id(product_id)
        if not product:
            raise ValueError(f"Product with id={product_id} not found in database")
        return await self.audit_product(
            product=product,
            reference_url=reference_url,
            force_refresh=force_refresh,
            skip_desc=skip_desc,
            skip_tech=skip_tech,
            photos_only=photos_only,
        )

    async def audit_batch(
        self,
        products: Sequence[Product],
        force_refresh: bool = False,
        concurrency: int = 2,
        skip_desc: bool = False,
        skip_tech: bool = False,
        photos_only: bool = False,
    ) -> list[AuditResult]:
        semaphore = asyncio.Semaphore(concurrency)

        async def _worker(p: Product) -> AuditResult:
            async with semaphore:
                return await self.audit_product(
                    product=p,
                    force_refresh=force_refresh,
                    skip_desc=skip_desc,
                    skip_tech=skip_tech,
                    photos_only=photos_only,
                )

        tasks = [_worker(p) for p in products]
        return await asyncio.gather(*tasks)
