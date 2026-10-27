import asyncio
import json
import sqlite3
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from src.adapters.crawler.web_crawler import WebCrawler
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.llm.gemini_judge import GeminiJudge
from src.domain.entities import AuditResult, AuditStatus, Product
from src.domain.exceptions import DomainError
from src.use_cases.resolve_reference import ResolvedReference, ResolveReferenceUseCase


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

    def _persist_audit(self, product: Product, result: AuditResult) -> None:
        if hasattr(self.product_repo, "save_product_and_audit_result"):
            self.product_repo.save_product_and_audit_result(product, result)
        else:
            if hasattr(self.product_repo, "save_product"):
                self.product_repo.save_product(product)
            self.product_repo.save_audit_result(result)

    async def audit_product(
        self,
        product: Product,
        reference_url: str | None = None,
        force_refresh: bool = False,
        skip_desc: bool = False,
        skip_tech: bool = False,
        photos_only: bool = False,
        prior_result: AuditResult | None = None,
        prior_loaded: bool = False,
    ) -> AuditResult:
        previous = prior_result if prior_loaded else self.product_repo.get_latest_audit_result(product.product_id)
        photos_handled = False
        try:
            result = await self._audit_product(
                product, reference_url, force_refresh, skip_desc, skip_tech, photos_only,
                previous, True,
            )
            photos_handled = photos_only
        except (DomainError, httpx.HTTPError, TimeoutError, ValueError) as exc:
            result = AuditResult(
                product_id=product.product_id,
                status=AuditStatus.ERROR,
                details=f"Не удалось проверить товар: {exc}",
                total_specs_count=len(product.current_specs),
                photos_only_run=photos_only,
                photos_error=str(exc) if photos_only else None,
            )
        if photos_handled:
            return result
        if result.status in (AuditStatus.ERROR, AuditStatus.NOT_FOUND):
            retained = previous.model_copy(deep=True) if previous else result
            attempted_parts = ["photos"]
            if not photos_only and not skip_tech:
                attempted_parts.append("specs")
            if not photos_only and not skip_desc and product.description is not None:
                attempted_parts.append("description")
            for part in attempted_parts:
                retained.audit_parts.pop(part, None)
            self._persist_audit(product, retained)
        return result

    async def _audit_product(
        self,
        product: Product,
        reference_url: str | None = None,
        force_refresh: bool = False,
        skip_desc: bool = False,
        skip_tech: bool = False,
        photos_only: bool = False,
        prior_result: AuditResult | None = None,
        prior_loaded: bool = False,
    ) -> AuditResult:
        t_audit_start = time.perf_counter()
        crawler_time_sec = 0.0
        previous = prior_result if prior_loaded else self.product_repo.get_latest_audit_result(product.product_id)

        if photos_only:
            return await self._audit_photos(product, reference_url, force_refresh, previous)

        resolved: ResolvedReference | None = None
        urls_to_try: list[str] = []
        if reference_url:
            urls_to_try = [reference_url]
            search_trace = [f"[Manual Reference] Using direct reference URL override: {reference_url}"]
            resolved = ResolvedReference(
                product_id=product.product_id,
                reference_url=reference_url,
                source_type="MANUAL_OVERRIDE",
                query_used=None,
                scenario_applied="MANUAL",
                status="FOUND",
                candidate_urls=[reference_url],
                is_cached=False,
                search_trace=search_trace,
            )
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
                search_err = getattr(resolved, "search_error", None)
                if search_err:
                    search_trace.append(f"[Audit] Search cascade failed due to technical error: {search_err} -> Status: ERROR")
                    result = AuditResult(
                        product_id=product.product_id,
                        status=AuditStatus.ERROR,
                        confidence_score=0.0,
                        reference_url=None,
                        discrepancies=[],
                        matched_specs_count=0,
                        total_specs_count=len(product.current_specs),
                        audited_at=datetime.now(UTC),
                        details=f"Search failed due to external API or network error: {search_err}",
                        search_trace=search_trace,
                        execution_time_sec=round(time.perf_counter() - t_audit_start, 2),
                        images_count=product.images_count,
                        images_status=evaluate_photos_status(product.images_count, 0),
                    )
                else:
                    search_trace.append("[Audit] No reference candidate URLs available to evaluate -> Status: NOT_FOUND")
                    result = AuditResult(
                        product_id=product.product_id,
                        status=AuditStatus.NOT_FOUND,
                        confidence_score=0.0,
                        reference_url=None,
                        discrepancies=[],
                        matched_specs_count=0,
                        total_specs_count=len(product.current_specs),
                        audited_at=datetime.now(UTC),
                        details="Reference URL could not be resolved from suppliers or search",
                        search_trace=search_trace,
                        execution_time_sec=round(time.perf_counter() - t_audit_start, 2),
                        images_count=product.images_count,
                        images_status=evaluate_photos_status(product.images_count, 0),
                    )
                self._persist_audit(product, result)
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
                # A crawler's zero can mean no images or no extracted count; it is not
                # evidence that the reference really has zero photos.
                result.images_status = (
                    evaluate_photos_status(product.images_count, ref_images_cnt)
                    if product.images_count_known and ref_images_cnt > 0 else "UNKNOWN"
                )

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
        is_step1_code = bool(not reference_url and resolved and resolved.source_type == "CODE_PINPOINT")
        is_from_cache = bool(not reference_url and resolved and resolved.source_type == "CACHE")
        step1_max = 2 if is_step1_code else None

        if urls_to_try:
            winning_result = await _evaluate_candidates(
                urls_to_try,
                label_prefix="Step 1 Candidate" if is_step1_code else ("Cached Candidate" if is_from_cache else "Candidate"),
                max_to_test=step1_max,
            )

        # Escalate to fresh live search if cached candidate was rejected
        if winning_result is None and is_from_cache:
            search_trace.append(
                "[Cache Rejected] Cached reference candidate rejected by judge. "
                "Escalating to live external search cascade..."
            )
            resolver_fn = getattr(self.reference_resolver, "execute", None) or getattr(self.reference_resolver, "resolve", None)
            if resolver_fn:
                try:
                    resolved_live = await resolver_fn(product, force_refresh=True)
                except Exception as exc:
                    resolved_live = None
                    search_trace.append(f"[Search Error] Live search after cache rejection failed: {exc}")

                live_candidates = getattr(resolved_live, "candidate_urls", []) if resolved_live else []
                first_live = getattr(resolved_live, "reference_url", None)
                if first_live and first_live not in live_candidates:
                    live_candidates.insert(0, first_live)

                if resolved_live:
                    for t in getattr(resolved_live, "search_trace", []):
                        if t not in search_trace:
                            search_trace.append(t)

                if live_candidates:
                    winning_result = await _evaluate_candidates(
                        live_candidates,
                        label_prefix="Live Candidate",
                        max_to_test=None,
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
            parts = dict(previous.audit_parts) if previous else {}
            if not skip_tech:
                parts["specs"] = product.audit_input_hash("specs")
            if not skip_desc and product.description is not None:
                parts["description"] = product.audit_input_hash("description")
            if product.images_count_known and winning_result.reference_images_count > 0:
                parts["photos"] = product.audit_input_hash("photos", winning_result.reference_url)
                if winning_result.reference_url:
                    parts["photos_url"] = winning_result.reference_url
            elif previous and "photos" in previous.audit_parts:
                winning_result.images_count = previous.images_count
                winning_result.reference_images_count = previous.reference_images_count
                winning_result.images_status = previous.images_status
            winning_result.audit_parts = parts
            if previous and skip_desc:
                winning_result.description_discrepancies = previous.description_discrepancies
                winning_result.description_quality = previous.description_quality
            self._persist_audit(product, winning_result)
            return winning_result

        if last_result:
            search_trace.append("[Evaluation] All candidate URLs exhausted without a verified match.")
            last_result.product_id = product.product_id
            last_result.search_trace = search_trace
            self._persist_audit(product, last_result)
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
            audited_at=datetime.now(UTC),
            details=f"Crawler error: {crawl_error or 'No candidates accessible'}",
            search_trace=search_trace,
            crawler_time_sec=round(crawler_time_sec, 2),
            execution_time_sec=round(time.perf_counter() - t_audit_start, 2),
            images_count=product.images_count,
            images_status=evaluate_photos_status(product.images_count, 0),
        )
        self._persist_audit(product, result)
        return result

    async def _audit_photos(
        self,
        product: Product,
        reference_url: str | None,
        force_refresh: bool,
        previous: AuditResult | None,
    ) -> AuditResult:
        error = "Нет достоверного количества фото магазина."
        accepted_url: str | None = None
        reference_count: int | None = None
        if product.images_count_known:
            error = "Подходящий эталон не найден."
            resolved: ResolvedReference | None = None
            if reference_url:
                candidates = [reference_url]
            else:
                resolver_fn = getattr(self.reference_resolver, "execute", None) or getattr(self.reference_resolver, "resolve", None)
                try:
                    resolved = await resolver_fn(product, force_refresh=force_refresh) if resolver_fn else None
                except sqlite3.Error:
                    raise
                except Exception as exc:
                    resolved = None
                    error = f"Поиск эталона завершился ошибкой: {exc}"
                candidates = list(getattr(resolved, "candidate_urls", []) or [])
                first = getattr(resolved, "reference_url", None)
                if first and first not in candidates:
                    candidates.insert(0, first)
            if candidates:
                error = "Подходящий эталон не найден."
            for url in candidates:
                raw = None if force_refresh else self.product_repo.get_specs_cache(url)
                try:
                    cached = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    cached = None
                if isinstance(cached, dict) and cached.get("markdown") and isinstance(cached.get("images_count"), int) and cached["images_count"] > 0:
                    markdown = str(cached["markdown"])
                    count = cached.get("images_count")
                else:
                    try:
                        crawled = await self.crawler.crawl(url)
                    except sqlite3.Error:
                        raise
                    except Exception as exc:
                        error = f"Эталон не удалось открыть: {exc}"
                        continue
                    if not crawled.success or not crawled.markdown:
                        continue
                    markdown = crawled.markdown
                    count = getattr(crawled, "images_count", None)
                    self.product_repo.save_specs_cache(
                        resolved_url=url,
                        specs_json=json.dumps({"title": crawled.title, "markdown": markdown, "images_count": count}, ensure_ascii=False),
                        status_code=crawled.status_code,
                    )
                try:
                    matches = await self.judge.matches_model(product, url, markdown)
                except sqlite3.Error:
                    raise
                except Exception as exc:
                    error = f"Проверка модели эталона завершилась ошибкой: {exc}"
                    continue
                if not matches:
                    continue
                if not isinstance(count, int) or count < 1:
                    error = "У подходящего эталона нет количества фото."
                    continue
                accepted_url = url
                reference_count = count
                break

        if accepted_url is None or reference_count is None:
            result = previous.model_copy(deep=True) if previous else AuditResult(
                product_id=product.product_id,
                status=AuditStatus.ERROR,
                details=error,
            )
            result.photos_only_run = True
            result.photos_error = error
            result.audit_parts.pop("photos", None)
            self._persist_audit(product, result)
            return result

        result = previous.model_copy(deep=True) if previous else AuditResult(
            product_id=product.product_id,
            status=AuditStatus.PENDING,
        )
        result.images_count = product.images_count
        result.reference_images_count = reference_count
        result.images_status = evaluate_photos_status(product.images_count, reference_count)
        if not previous or not previous.reference_url or previous.status not in (
            AuditStatus.VERIFIED, AuditStatus.MISMATCH, AuditStatus.MISSING_SPECS,
        ):
            result.reference_url = accepted_url
        result.audit_parts["photos"] = product.audit_input_hash("photos", accepted_url)
        result.audit_parts["photos_url"] = accepted_url
        result.photos_only_run = True
        result.audited_at = datetime.now(UTC)
        if not previous:
            result.details = "Проверены только фото."
        self._persist_audit(product, result)
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
