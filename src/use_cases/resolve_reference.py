from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Optional
from config.vendor_profiles import find_vendor_profile
from config.whitelist_domains import extract_domain, get_domain_priority, is_whitelisted_domain
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.search.serper_client import SerperClient
from src.adapters.search.vendor_direct import VendorDirectResolver
from src.domain.entities import Product


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
    ) -> None:
        self.repo = repository
        self.vendor_resolver = vendor_resolver
        self.serper_client = serper_client
        self.whitelist_domains = whitelist_domains
        self.verify_direct_urls = verify_direct_urls
        self.judge = judge
        self.use_url_cache = use_url_cache

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
                res = await self.vendor_resolver.resolve_vendor_card(
                    product.vendor_name, product.vendor_sku
                )
                if res.is_available and res.reference_url:
                    final_url = res.reference_url
                    source_type = "VENDOR_DIRECT"
                    candidates = [res.reference_url]
                    if res.kaspi_code:
                        kaspi_direct = f"https://kaspi.kz/shop/p/-{res.kaspi_code}/"
                        final_url = kaspi_direct
                        source_type = "VENDOR_KASPI_DIRECT"
                        candidates = [kaspi_direct, res.reference_url]

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
                trace.append("[Vendor Direct] Supplier card unavailable or 404, falling back to search cascade")
                return await self._execute_search_cascade(
                    product, scenario_override="SCENARIO_2_VENDOR_FALLBACK", force_refresh=force_refresh, trace=trace
                )

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

    def _build_title_query(self, product: Product) -> tuple[str, str]:
        clean_title = product.title.strip()
        parts = [clean_title]

        if product.manufacturer_sku and product.manufacturer_sku.strip():
            m_sku = product.manufacturer_sku.strip()
            if m_sku.lower() not in clean_title.lower():
                parts.append(m_sku)

        is_internal_sku = False
        if product.vendor_name:
            v_lower = product.vendor_name.lower().strip()
            if any(w in v_lower for w in ["основной склад", "главный склад", "собственный склад", "склад"]):
                is_internal_sku = True
        if product.shop_sku and product.vendor_sku and str(product.shop_sku).strip() == str(product.vendor_sku).strip():
            is_internal_sku = True

        if not is_internal_sku and product.vendor_sku and product.vendor_sku.strip():
            v_sku = product.vendor_sku.strip()
            if v_sku.lower() not in clean_title.lower():
                parts.append(v_sku)

        query = " ".join(parts).strip()
        source_type = "PROVIDER_SKU_SEARCH" if (product.vendor_sku and not is_internal_sku) else "MODEL_SEARCH"
        return query, source_type

    def _build_search_query(self, product: Product) -> tuple[str, str]:
        if product.barcode and product.barcode.strip():
            clean_bc = product.barcode.strip()
            return f'"{clean_bc}"', "BARCODE_SEARCH"
        return self._build_title_query(product)

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

    async def _execute_search_cascade(
        self, product: Product, scenario_override: str, force_refresh: bool = False, trace: list[str] | None = None
    ) -> ResolvedReference:
        if trace is None:
            trace = []

        # STEP 1 (PRIMARY): Clean Brand + Model Search (via LLM query cleaner or heuristic)
        clean_model_query = None
        extracted_brand = None
        if self.judge and hasattr(self.judge, "clean_search_query"):
            try:
                res = await self.judge.clean_search_query(product.title)
                if isinstance(res, tuple):
                    clean_model_query, extracted_brand = res
                elif isinstance(res, str):
                    clean_model_query = res
            except Exception:
                clean_model_query = None
                extracted_brand = None

        source_type = "MODEL_SEARCH"
        if not clean_model_query:
            clean_model_query, source_type = self._build_title_query(product)

        trace.append(f"[Query Extraction] Clean model query: '{clean_model_query}', Brand: '{extracted_brand or 'N/A'}'")

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

        # 1. KASPI FIRST: Search on Kaspi.kz marketplace directly
        kaspi_query = f"site:kaspi.kz {clean_model_query}"
        trace.append(f"[Kaspi Search] Query: '{kaspi_query}'")
        kaspi_results = await self.serper_client.search_and_filter(
            query=kaspi_query,
            whitelist_domains=self.whitelist_domains,
            num_results=5,
        )
        if kaspi_results:
            kaspi_candidate_links = [
                str(item.get("link") or "").strip()
                for item in kaspi_results
                if str(item.get("link") or "").strip()
                and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
                and "/shop/p/" in str(item.get("link") or "").lower()
            ]
            trace.append(f"[Kaspi Search] Query returned {len(kaspi_results)} items, {len(kaspi_candidate_links)} valid Kaspi product cards")
            for link in kaspi_candidate_links:
                add_candidate(link)

        # 2. GENERAL RETAILERS & OFFICIAL SITES: Broad search across whitelisted domains (DNS, Sulpak, Mechta, Shop.kz, mi.com, etc.)
        general_query = clean_model_query
        trace.append(f"[General Search] Query: '{general_query}'")
        general_results = await self.serper_client.search_and_filter(
            query=general_query,
            whitelist_domains=self.whitelist_domains,
            num_results=10,
        )
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
            trace.append(f"[General Search] Query returned {len(general_results)} items, {len(general_links)} whitelisted")
            for link in general_links:
                add_candidate(link)

        # 3. BARCODE SEARCH: If barcode exists and pool is small, search by barcode
        if product.barcode and product.barcode.strip() and len(candidate_pool) < 4:
            bc_query = f'"{product.barcode.strip()}"'
            trace.append(f"[Barcode Search] Query: '{bc_query}'")
            bc_results = await self.serper_client.search_and_filter(
                query=bc_query,
                whitelist_domains=self.whitelist_domains,
                num_results=10,
            )
            if bc_results:
                sorted_bc = self._sort_by_domain_priority(
                    bc_results, product, clean_query=clean_model_query, brand=extracted_brand
                )
                bc_links = [
                    str(item.get("link") or "").strip()
                    for item in sorted_bc
                    if str(item.get("link") or "").strip()
                    and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
                ]
                trace.append(f"[Barcode Search] Query returned {len(bc_results)} items, {len(bc_links)} whitelisted")
                for link in bc_links:
                    add_candidate(link)

        def _pool_priority(u: str) -> int:
            d = extract_domain(u)
            if "kaspi.kz" in d:
                return 1
            if "dns-shop.kz" in d:
                return 2
            if any(m in d for m in ["mi.com", "xiaomi.kz", "apple.com", "samsung.com", "tp-link.com", "tplink.com"]):
                return 3
            if any(m in d for m in ["shop.kz", "mechta.kz", "technodom.kz", "sulpak.kz", "fora.kz"]):
                return 4
            return 5

        candidate_pool.sort(key=_pool_priority)

        if candidate_pool:
            best_link = candidate_pool[0]
            trace.append(f"[Resolved] Candidate pool created with {len(candidate_pool)} URLs. Primary: {best_link}")
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

