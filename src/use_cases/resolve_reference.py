from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Optional
from config.vendor_profiles import find_vendor_profile
from config.whitelist_domains import get_domain_priority, is_whitelisted_domain
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
        if self.is_nameless_generic_product(product):
            return ResolvedReference(
                product_id=product.product_id,
                reference_url=None,
                source_type="NONE",
                query_used=None,
                scenario_applied="SCENARIO_5_INSUFFICIENT_DATA",
                status="INSUFFICIENT_DATA",
                is_cached=False,
            )

        has_trusted_vendor = bool(find_vendor_profile(product.vendor_name))
        has_vendor_sku = bool(product.vendor_sku and product.vendor_sku.strip())

        if has_trusted_vendor and has_vendor_sku:
            cached_query = f"vendor:{product.vendor_name}:{product.vendor_sku}"
            q_hash = self.compute_query_hash(product, cached_query)
            if self.use_url_cache and not force_refresh:
                cached_url = self.repo.get_url_cache(q_hash)
                if cached_url:
                    return ResolvedReference(
                        product_id=product.product_id,
                        reference_url=cached_url,
                        source_type="CACHE",
                        query_used=cached_query,
                        scenario_applied="SCENARIO_1_VENDOR_DIRECT",
                        status="FOUND",
                        is_cached=True,
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
                    )
                return await self._execute_search_cascade(
                    product, scenario_override="SCENARIO_2_VENDOR_FALLBACK", force_refresh=force_refresh
                )

            direct_url = self.vendor_resolver.build_direct_url(
                product.vendor_name, product.vendor_sku
            )
            if direct_url:
                self.repo.save_url_cache(q_hash, direct_url, source_type="VENDOR_DIRECT")
                return ResolvedReference(
                    product_id=product.product_id,
                    reference_url=direct_url,
                    source_type="VENDOR_DIRECT",
                    query_used=cached_query,
                    scenario_applied="SCENARIO_1_VENDOR_DIRECT",
                    status="FOUND",
                    candidate_urls=[direct_url],
                    is_cached=False,
                )

            return await self._execute_search_cascade(
                product, scenario_override="SCENARIO_2_VENDOR_FALLBACK", force_refresh=force_refresh
            )

        if has_trusted_vendor and not has_vendor_sku:
            return await self._execute_search_cascade(
                product, scenario_override="SCENARIO_3_VENDOR_NO_SKU", force_refresh=force_refresh
            )

        return await self._execute_search_cascade(
            product, scenario_override="SCENARIO_4_EXTERNAL_SEARCH", force_refresh=force_refresh
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

        parts.append("характеристики")
        query = " ".join(parts).strip()
        source_type = "PROVIDER_SKU_SEARCH" if (product.vendor_sku and not is_internal_sku) else "MODEL_SEARCH"
        return query, source_type

    def _build_search_query(self, product: Product) -> tuple[str, str]:
        if product.barcode and product.barcode.strip():
            clean_bc = product.barcode.strip()
            return f'"{clean_bc}" характеристики', "BARCODE_SEARCH"
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
        self, product: Product, scenario_override: str, force_refresh: bool = False
    ) -> ResolvedReference:
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
        else:
            if "характеристик" not in clean_model_query.lower():
                clean_model_query = f"{clean_model_query} характеристики"

        q_hash = self.compute_query_hash(product, clean_model_query)
        if self.use_url_cache and not force_refresh:
            cached_url = self.repo.get_url_cache(q_hash)
            if cached_url and is_whitelisted_domain(cached_url, extra_domains=self.whitelist_domains):
                return ResolvedReference(
                    product_id=product.product_id,
                    reference_url=cached_url,
                    source_type="CACHE",
                    query_used=clean_model_query,
                    scenario_applied=scenario_override,
                    status="FOUND",
                    candidate_urls=[cached_url],
                    is_cached=True,
                )

        results = await self.serper_client.search_and_filter(
            query=clean_model_query,
            whitelist_domains=self.whitelist_domains,
            num_results=10,
        )

        if results:
            sorted_res = self._sort_by_domain_priority(
                results, product, clean_query=clean_model_query, brand=extracted_brand
            )
            candidate_links = [
                str(item.get("link") or "").strip()
                for item in sorted_res
                if str(item.get("link") or "").strip()
                and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
            ]
            if candidate_links:
                best_link = candidate_links[0]
                self.repo.save_url_cache(q_hash, best_link, source_type=source_type)
                return ResolvedReference(
                    product_id=product.product_id,
                    reference_url=best_link,
                    source_type=source_type,
                    query_used=clean_model_query,
                    scenario_applied=scenario_override,
                    status="FOUND",
                    candidate_urls=candidate_links,
                    is_cached=False,
                )

        # STEP 2 (RESERVE / FALLBACK): Barcode Search if Model search returned no whitelisted pages
        if product.barcode and product.barcode.strip():
            bc_query = f'"{product.barcode.strip()}" характеристики'
            q_hash_bc = self.compute_query_hash(product, bc_query)
            if self.use_url_cache and not force_refresh:
                cached_bc = self.repo.get_url_cache(q_hash_bc)
                if cached_bc and is_whitelisted_domain(cached_bc, extra_domains=self.whitelist_domains):
                    return ResolvedReference(
                        product_id=product.product_id,
                        reference_url=cached_bc,
                        source_type="CACHE",
                        query_used=bc_query,
                        scenario_applied=scenario_override,
                        status="FOUND",
                        candidate_urls=[cached_bc],
                        is_cached=True,
                    )

            bc_results = await self.serper_client.search_and_filter(
                query=bc_query,
                whitelist_domains=self.whitelist_domains,
                num_results=10,
            )
            if bc_results:
                sorted_bc = self._sort_by_domain_priority(
                    bc_results, product, clean_query=clean_model_query, brand=extracted_brand
                )
                bc_candidate_links = [
                    str(item.get("link") or "").strip()
                    for item in sorted_bc
                    if str(item.get("link") or "").strip()
                    and is_whitelisted_domain(str(item.get("link") or "").strip(), extra_domains=self.whitelist_domains)
                ]
                if bc_candidate_links:
                    best_bc = bc_candidate_links[0]
                    self.repo.save_url_cache(q_hash_bc, best_bc, source_type="BARCODE_SEARCH")
                    return ResolvedReference(
                        product_id=product.product_id,
                        reference_url=best_bc,
                        source_type="BARCODE_SEARCH",
                        query_used=bc_query,
                        scenario_applied=scenario_override,
                        status="FOUND",
                        candidate_urls=bc_candidate_links,
                        is_cached=False,
                    )

        return ResolvedReference(
            product_id=product.product_id,
            reference_url=None,
            source_type=source_type,
            query_used=clean_model_query,
            scenario_applied=scenario_override,
            status="NOT_FOUND",
            candidate_urls=[],
            is_cached=False,
        )
