import asyncio
from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Optional
from config.vendor_profiles import find_vendor_profile
from config.whitelist_domains import extract_domain, get_domain_priority, is_whitelisted_domain
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.search.kaspi_client import KaspiSearchClient
from src.adapters.search.serper_client import SerperClient
from src.adapters.search.vendor_direct import VendorDirectResolver
from src.domain.entities import Product
from src.adapters.llm.openai_judge import SearchQueryBundle


@dataclass
class ResolvedReference:
    product_id: int
    reference_url: Optional[str]
    source_type: str
    query_used: Optional[str]
    scenario_applied: str
    status: str
    kaspi_code: Optional[str] = None
    candidate_urls: list[str] = field(default_factory=list)
    is_cached: bool = False
    search_trace: list[str] = field(default_factory=list)

def is_valid_model_code(code: str | None) -> bool:
    """
    Validates that a candidate string is an authentic factory model code / SKU
    (at least 3 characters, contains alphanumeric characters, and at least one digit
    or model separator like '-', '_', '/').
    Rejects harmless Russian or English words (e.g. 'черный', 'белый', 'gold').
    """
    if not code:
        return False
    clean = code.strip()
    if len(clean) < 3:
        return False
    has_alnum = any(c.isalnum() for c in clean)
    has_digit = any(c.isdigit() for c in clean)
    has_sep = any(c in "-_/" for c in clean)
    has_latin_or_digit = any((c.isascii() and c.isalnum()) for c in clean)
    return has_alnum and has_latin_or_digit and (has_digit or has_sep)


class ResolveReferenceUseCase:

    NAMELESS_PATTERNS: list[str] = [
        r"^чехол\b",
        r"^защитное стекло\b",
        r"^защитная пленка\b",
        r"^кабель\b",
        r"^адаптер\b",
        r"^переходник\b",
        r"^подставка\b",
        r"^держатель\b",
        r"^ремешок\b",
    ]

    def __init__(
        self,
        repository: SQLiteProductRepository,
        vendor_resolver: VendorDirectResolver,
        serper_client: SerperClient,
        whitelist_domains: Optional[list[str]] = None,
        verify_direct_urls: bool = False,
        judge: Optional[Any] = None,
        use_url_cache: bool = True,
        kaspi_client: Optional[KaspiSearchClient] = None,
    ) -> None:
        self.repo = repository
        self.vendor_resolver = vendor_resolver
        self.serper_client = serper_client
        self.whitelist_domains = whitelist_domains
        self.verify_direct_urls = verify_direct_urls
        self.judge = judge
        self.use_url_cache = use_url_cache
        self.kaspi_client = kaspi_client or KaspiSearchClient()

    def is_nameless_generic_product(self, product: Product) -> bool:
        if product.barcode and product.barcode.strip():
            return False
        if product.vendor_sku and product.vendor_sku.strip():
            return False
        if product.manufacturer_sku and product.manufacturer_sku.strip():
            return False

        title_lower = product.title.strip().lower()
        has_digit = bool(re.search(r"\d", title_lower))
        if has_digit:
            return False

        for pattern in self.NAMELESS_PATTERNS:
            if re.search(pattern, title_lower):
                return True

        if len(title_lower.split()) <= 2 and not product.current_specs:
            return True

        return False

    def compute_query_hash(self, product: Product, query_str: str) -> str:
        key = f"ref:{product.product_id}:{query_str.strip().lower()}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    async def execute(self, product: Product, force_refresh: bool = False) -> ResolvedReference:
        trace: list[str] = []

        if self.is_nameless_generic_product(product):
            trace.append(f"[Inspection] Generic/nameless item '{product.title[:40]}' without identifiers -> Status: INSUFFICIENT_DATA")
            return ResolvedReference(
                product_id=product.product_id,
                reference_url=None,
                source_type="NONE",
                query_used=None,
                scenario_applied="SCENARIO_5_INSUFFICIENT_DATA",
                status="INSUFFICIENT_DATA",
                is_cached=False,
                search_trace=trace,
            )

        has_trusted_vendor = bool(find_vendor_profile(product.vendor_name))
        has_vendor_sku = bool(product.vendor_sku and product.vendor_sku.strip())

        if has_trusted_vendor and has_vendor_sku:
            cached_query = f"vendor:{product.vendor_name}:{product.vendor_sku}"
            q_hash = self.compute_query_hash(product, cached_query)
            trace.append(f"[Vendor Direct] Checking supplier '{product.vendor_name}' for SKU '{product.vendor_sku}'")

            if self.use_url_cache and not force_refresh:
                cached_url = self.repo.get_url_cache(q_hash)
                if cached_url:
                    trace.append(f"[Cache Hit] Vendor direct URL resolved from cache: {cached_url}")
                    return ResolvedReference(
                        product_id=product.product_id,
                        reference_url=cached_url,
                        source_type="CACHE",
                        query_used=cached_query,
                        scenario_applied="SCENARIO_1_VENDOR_DIRECT",
                        status="FOUND",
                        candidate_urls=[cached_url],
                        is_cached=True,
                        search_trace=trace,
                    )

            if self.verify_direct_urls:
                # Concurrent lookup: probe supplier card while simultaneously running search cascade
                cascade_trace: list[str] = list(trace)
                vendor_task = self.vendor_resolver.resolve_vendor_card(
                    product.vendor_name, product.vendor_sku
                )
                cascade_task = self._execute_search_cascade(
                    product,
                    scenario_override="SCENARIO_2_VENDOR_FALLBACK",
                    force_refresh=force_refresh,
                    trace=cascade_trace,
                )
                res, cascade_ref = await asyncio.gather(vendor_task, cascade_task)

                if res.is_available and res.reference_url:
                    final_url = res.reference_url
                    source_type = "VENDOR_DIRECT"
                    candidates = [res.reference_url]
                    if res.kaspi_code:
                        kaspi_direct = f"https://kaspi.kz/shop/p/-{res.kaspi_code}/"
                        final_url = kaspi_direct
                        source_type = "VENDOR_KASPI_DIRECT"
                        candidates = [kaspi_direct, res.reference_url]

                    for cand in cascade_ref.candidate_urls:
                        if cand not in candidates:
                            candidates.append(cand)

                    self.repo.save_url_cache(q_hash, final_url, source_type=source_type)
                    trace.append(f"[Vendor Direct] Active product card found: {final_url} (Kaspi code: {res.kaspi_code or 'N/A'})")
                    return ResolvedReference(
                        product_id=product.product_id,
                        reference_url=final_url,
                        source_type=source_type,
                        query_used=cached_query,
                        scenario_applied="SCENARIO_1_VENDOR_DIRECT",
                        status="FOUND",
                        kaspi_code=res.kaspi_code,
                        candidate_urls=candidates,
                        is_cached=False,
                        search_trace=trace,
                    )

                trace.append("[Vendor Direct] Supplier card unavailable or 404, using concurrent search cascade")
                for step in cascade_trace:
                    if step not in trace:
                        trace.append(step)
                cascade_ref.search_trace = trace
                return cascade_ref

            direct_url = self.vendor_resolver.build_direct_url(
                product.vendor_name, product.vendor_sku
            )
            if direct_url:
                self.repo.save_url_cache(q_hash, direct_url, source_type="VENDOR_DIRECT")
                trace.append(f"[Vendor Direct] Generated direct URL: {direct_url}")
                return ResolvedReference(
                    product_id=product.product_id,
                    reference_url=direct_url,
                    source_type="VENDOR_DIRECT",
                    query_used=cached_query,
                    scenario_applied="SCENARIO_1_VENDOR_DIRECT",
                    status="FOUND",
                    candidate_urls=[direct_url],
                    is_cached=False,
                    search_trace=trace,
                )

            trace.append("[Vendor Direct] Could not build direct vendor URL, falling back to search cascade")
            return await self._execute_search_cascade(
                product, scenario_override="SCENARIO_2_VENDOR_FALLBACK", force_refresh=force_refresh, trace=trace
            )

        if has_trusted_vendor and not has_vendor_sku:
            trace.append(f"[Vendor Check] Vendor '{product.vendor_name}' known but vendor_sku is missing -> External search")
            return await self._execute_search_cascade(
                product, scenario_override="SCENARIO_3_VENDOR_NO_SKU", force_refresh=force_refresh, trace=trace
            )

        trace.append("[Vendor Check] No trusted vendor profile -> Running external search cascade")
        return await self._execute_search_cascade(
            product, scenario_override="SCENARIO_4_EXTERNAL_SEARCH", force_refresh=force_refresh, trace=trace
        )

    def _sort_by_domain_priority(
        self,
        results: list[dict[str, Any]],
        product: Product,
        clean_query: str | None = None,
        brand: str | None = None,
    ) -> list[dict[str, Any]]:
        target_brand = brand
        if not target_brand and clean_query:
            parts = clean_query.split()
            if parts:
                target_brand = parts[0].strip()

        return sorted(
            results,
            key=lambda item: get_domain_priority(str(item.get("link") or ""), brand=target_brand),
        )

    async def _get_search_bundle(self, product: Product) -> SearchQueryBundle:
        bundle: SearchQueryBundle | None = None
        if self.judge and hasattr(self.judge, "decompose_search_query"):
            try:
                bundle = await self.judge.decompose_search_query(product.title, vendor_name=product.vendor_name)
            except Exception:
                bundle = None

        if not bundle:
            from src.adapters.llm.openai_judge import OpenAIJudge
            bundle = OpenAIJudge._heuristic_decompose(product.title, vendor_name=product.vendor_name)
        return bundle

    async def resolve_marketing_fallback(
        self,
        product: Product,
        force_refresh: bool = False,
        trace: list[str] | None = None,
    ) -> ResolvedReference:
        if trace is None:
            trace = []
        bundle = await self._get_search_bundle(product)
        return await self._execute_marketing_search_cascade(
            product=product,
            bundle=bundle,
            scenario_override="SCENARIO_4_EXTERNAL_SEARCH",
            force_refresh=force_refresh,
            trace=trace,
        )

    async def _execute_search_cascade(
        self, product: Product, scenario_override: str, force_refresh: bool = False, trace: list[str] | None = None
    ) -> ResolvedReference:
        if trace is None:
            trace = []

        bundle = await self._get_search_bundle(product)

        brand_prefix = (
            f"{bundle.brand} "
            if (bundle.brand and bundle.brand.lower() not in bundle.marketing_model.lower())
            else ""
        )
        clean_model_query = f"{brand_prefix}{bundle.marketing_model}".strip()
        extracted_brand = bundle.brand

        trace.append(
            f"[Query Extraction] Marketing: '{bundle.marketing_model}', Clean Query: '{clean_model_query}', "
            f"Code: '{bundle.model_code or 'N/A'}', Brand: '{bundle.brand or 'N/A'}', Aliases: {bundle.brand_aliases}"
        )

        q_hash = self.compute_query_hash(product, clean_model_query)
        if self.use_url_cache and not force_refresh:
            cached_url = self.repo.get_url_cache(q_hash)
            if cached_url and is_whitelisted_domain(cached_url, extra_domains=self.whitelist_domains):
                trace.append(f"[Cache Hit] Model query matched in url_cache: {cached_url}")
                return ResolvedReference(
                    product_id=product.product_id,
                    reference_url=cached_url,
                    source_type="CACHE",
                    query_used=clean_model_query,
                    scenario_applied=scenario_override,
                    status="FOUND",
                    candidate_urls=[cached_url],
                    is_cached=True,
                    search_trace=trace,
                )

        candidate_pool: list[str] = []
        seen_urls: set[str] = set()

        def add_candidate(url: str) -> None:
            url_clean = url.strip()
            if url_clean and url_clean not in seen_urls:
                seen_urls.add(url_clean)
                candidate_pool.append(url_clean)

        # STEP 1 (PRIMARY): Factory Model Code / Part Number (PINPOINT SEARCH)
        has_valid_code = is_valid_model_code(bundle.model_code)
        if has_valid_code and bundle.model_code:
            code_clean = bundle.model_code.strip()
            code_kaspi_query = f"site:kaspi.kz {code_clean}"
            code_gen_query = f"{bundle.brand or ''} {code_clean}".strip()
            trace.append(f"[Vector 1: Code Search] Kaspi: '{code_kaspi_query}' | General: '{code_gen_query}'")

            code_kaspi_task = self.serper_client.search_and_filter(
                query=code_kaspi_query,
                whitelist_domains=self.whitelist_domains,
                num_results=5,
            )
            code_gen_task = self.serper_client.search_and_filter(
                query=code_gen_query,
                whitelist_domains=self.whitelist_domains,
                num_results=5,
            )
            code_kaspi_results, code_gen_results = await asyncio.gather(code_kaspi_task, code_gen_task)

            if code_kaspi_results:
                kaspi_code_links = [
                    str(item.get("link") or "").strip()
                    for item in code_kaspi_results
                    if str(item.get("link") or "").strip()
                    and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
                    and "/shop/p/" in str(item.get("link") or "").lower()
                ]
                trace.append(f"[Vector 1: Code Search] Kaspi query returned {len(code_kaspi_results)} items, {len(kaspi_code_links)} valid Kaspi cards")
                for link in kaspi_code_links:
                    add_candidate(link)

            if code_gen_results:
                sorted_code_gen = self._sort_by_domain_priority(
                    code_gen_results, product, clean_query=code_gen_query, brand=extracted_brand
                )
                gen_code_links = [
                    str(item.get("link") or "").strip()
                    for item in sorted_code_gen
                    if str(item.get("link") or "").strip()
                    and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
                ]
                trace.append(f"[Vector 1: Code Search] General query returned {len(code_gen_results)} items, {len(gen_code_links)} whitelisted")
                for link in gen_code_links:
                    add_candidate(link)

            # LAZY FALLBACK: If external search returned 0 candidates for this model code, query Kaspi directly
            if not candidate_pool:
                direct_kaspi_links = await self.kaspi_client.search(
                    query=code_clean,
                    brand=extracted_brand or product.vendor_name,
                    limit=2,
                )
                if direct_kaspi_links:
                    trace.append(
                        f"[Vector 1: Kaspi Direct Fallback] Direct API found {len(direct_kaspi_links)} valid card(s) for code '{code_clean}'"
                    )
                    for link in direct_kaspi_links:
                        add_candidate(link)

            if candidate_pool:
                target_brand = extracted_brand or (clean_model_query.split()[0].strip() if clean_model_query else None)
                candidate_pool.sort(key=lambda u: get_domain_priority(u, brand=target_brand))
                best_link = candidate_pool[0]
                trace.append(f"[Resolved: Step 1 Code] Candidate pool created with {len(candidate_pool)} URLs. Primary: {best_link}")
                return ResolvedReference(
                    product_id=product.product_id,
                    reference_url=best_link,
                    source_type="CODE_PINPOINT",
                    query_used=code_clean,
                    scenario_applied=scenario_override,
                    status="FOUND",
                    candidate_urls=candidate_pool,
                    is_cached=False,
                    search_trace=trace,
                )

            trace.append("[Vector 1: Code Search] Found 0 candidates, immediately falling back to Step 2 Marketing Search")

        # STEP 2 (or direct when no model_code): Marketing Model Search Cascade
        return await self._execute_marketing_search_cascade(
            product=product,
            bundle=bundle,
            scenario_override=scenario_override,
            force_refresh=force_refresh,
            trace=trace,
        )

    async def _execute_marketing_search_cascade(
        self,
        product: Product,
        bundle: SearchQueryBundle,
        scenario_override: str,
        force_refresh: bool = False,
        trace: list[str] | None = None,
    ) -> ResolvedReference:
        if trace is None:
            trace = []

        brand_prefix = (
            f"{bundle.brand} "
            if (bundle.brand and bundle.brand.lower() not in bundle.marketing_model.lower())
            else ""
        )
        clean_model_query = f"{brand_prefix}{bundle.marketing_model}".strip()
        extracted_brand = bundle.brand
        source_type = "MODEL_SEARCH"

        candidate_pool: list[str] = []
        seen_urls: set[str] = set()

        def add_candidate(url: str) -> None:
            url_clean = url.strip()
            if url_clean and url_clean not in seen_urls:
                seen_urls.add(url_clean)
                candidate_pool.append(url_clean)

        kaspi_query = f"site:kaspi.kz {clean_model_query}"
        general_query = clean_model_query
        trace.append(f"[Vector 2: Marketing Search] Kaspi: '{kaspi_query}' | General: '{general_query}'")

        kaspi_task = self.serper_client.search_and_filter(
            query=kaspi_query,
            whitelist_domains=self.whitelist_domains,
            num_results=5,
        )
        general_task = self.serper_client.search_and_filter(
            query=general_query,
            whitelist_domains=self.whitelist_domains,
            num_results=10,
        )
        kaspi_results, general_results = await asyncio.gather(kaspi_task, general_task)

        if kaspi_results:
            kaspi_candidate_links = [
                str(item.get("link") or "").strip()
                for item in kaspi_results
                if str(item.get("link") or "").strip()
                and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
                and "/shop/p/" in str(item.get("link") or "").lower()
            ]
            trace.append(f"[Vector 2: Marketing Search] Kaspi returned {len(kaspi_results)} items, {len(kaspi_candidate_links)} valid Kaspi cards")
            for link in kaspi_candidate_links:
                add_candidate(link)

        if general_results:
            sorted_res = self._sort_by_domain_priority(
                general_results, product, clean_query=clean_model_query, brand=extracted_brand
            )
            general_links = [
                str(item.get("link") or "").strip()
                for item in sorted_res
                if str(item.get("link") or "").strip()
                and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
            ]
            trace.append(f"[Vector 2: Marketing Search] General returned {len(general_results)} items, {len(general_links)} whitelisted")
            for link in general_links:
                add_candidate(link)

        # VECTOR 3: Ecosystem Brand Aliases (STRICT QUOTA GUARD: len(candidate_pool) < 2)
        if len(candidate_pool) < 2 and bundle.brand_aliases:
            alias_targets = bundle.brand_aliases[:2]
            alias_queries = [f"site:kaspi.kz {alias} {bundle.marketing_model}" for alias in alias_targets]
            for q in alias_queries:
                trace.append(f"[Vector 3: Alias Search] (Quota Guard: pool size {len(candidate_pool)} < 2) Query: '{q}'")
            alias_tasks = [
                self.serper_client.search_and_filter(
                    query=q,
                    whitelist_domains=self.whitelist_domains,
                    num_results=5,
                )
                for q in alias_queries
            ]
            alias_results_list = await asyncio.gather(*alias_tasks)
            for alias_results in alias_results_list:
                if alias_results:
                    alias_links = [
                        str(item.get("link") or "").strip()
                        for item in alias_results
                        if str(item.get("link") or "").strip()
                        and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
                        and "/shop/p/" in str(item.get("link") or "").lower()
                    ]
                    trace.append(f"[Vector 3: Alias Search] Query returned {len(alias_results)} items, {len(alias_links)} valid Kaspi cards")
                    for link in alias_links:
                        add_candidate(link)

        # VECTOR 4: Barcode Search (STRICT QUOTA GUARD: len(candidate_pool) < 2)
        if len(candidate_pool) < 2 and product.barcode and product.barcode.strip():
            bc_query = f'"{product.barcode.strip()}"'
            trace.append(f"[Vector 4: Barcode Search] (Quota Guard: pool size {len(candidate_pool)} < 2) Query: '{bc_query}'")
            bc_results = await self.serper_client.search_and_filter(
                query=bc_query,
                whitelist_domains=self.whitelist_domains,
                num_results=10,
            )
            if bc_results:
                sorted_bc = self._sort_by_domain_priority(
                    bc_results, product, clean_query=bundle.marketing_model, brand=extracted_brand
                )
                bc_links = [
                    str(item.get("link") or "").strip()
                    for item in sorted_bc
                    if str(item.get("link") or "").strip()
                    and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
                ]
                trace.append(f"[Vector 4: Barcode Search] Query returned {len(bc_results)} items, {len(bc_links)} whitelisted")
                for link in bc_links:
                    add_candidate(link)

        # LAZY FALLBACK: If candidate pool is still empty after all Serper vectors, query Kaspi directly with marketing model
        if not candidate_pool:
            direct_kaspi_links = await self.kaspi_client.search(
                query=bundle.marketing_model or clean_model_query,
                brand=extracted_brand or product.vendor_name,
                limit=2,
            )
            if direct_kaspi_links:
                trace.append(
                    f"[Vector 5: Kaspi Direct Fallback] Direct API found {len(direct_kaspi_links)} valid card(s) for '{bundle.marketing_model}'"
                )
                for link in direct_kaspi_links:
                    add_candidate(link)

        target_brand = extracted_brand or (clean_model_query.split()[0].strip() if clean_model_query else None)
        candidate_pool.sort(key=lambda u: get_domain_priority(u, brand=target_brand))

        if candidate_pool:
            best_link = candidate_pool[0]
            trace.append(f"[Resolved: Step 2 Marketing] Candidate pool created with {len(candidate_pool)} URLs. Primary: {best_link}")
            return ResolvedReference(
                product_id=product.product_id,
                reference_url=best_link,
                source_type=source_type,
                query_used=clean_model_query,
                scenario_applied=scenario_override,
                status="FOUND",
                candidate_urls=candidate_pool,
                is_cached=False,
                search_trace=trace,
            )

        trace.append(f"[Resolved] All search attempts exhausted for model '{clean_model_query}'. No whitelisted candidate pages found -> Status: NOT_FOUND")
        return ResolvedReference(
            product_id=product.product_id,
            reference_url=None,
            source_type=source_type,
            query_used=clean_model_query,
            scenario_applied=scenario_override,
            status="NOT_FOUND",
            candidate_urls=[],
            is_cached=False,
            search_trace=trace,
        )

