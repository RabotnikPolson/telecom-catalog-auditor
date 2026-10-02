from dataclasses import dataclass
import hashlib
import re
from typing import Any, Optional
from config.vendor_profiles import find_vendor_profile
from config.whitelist_domains import is_whitelisted_domain
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
    ) -> None:
        self.repo = repository
        self.vendor_resolver = vendor_resolver
        self.serper_client = serper_client
        self.whitelist_domains = whitelist_domains
        self.verify_direct_urls = verify_direct_urls

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

    async def execute(self, product: Product) -> ResolvedReference:
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
                    self.repo.save_url_cache(q_hash, res.reference_url, source_type="VENDOR_DIRECT")
                    return ResolvedReference(
                        product_id=product.product_id,
                        reference_url=res.reference_url,
                        source_type="VENDOR_DIRECT",
                        query_used=cached_query,
                        scenario_applied="SCENARIO_1_VENDOR_DIRECT",
                        status="FOUND",
                        kaspi_code=res.kaspi_code,
                        is_cached=False,
                    )
                return await self._execute_search_cascade(
                    product, scenario_override="SCENARIO_2_VENDOR_FALLBACK"
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
                    is_cached=False,
                )

            return await self._execute_search_cascade(
                product, scenario_override="SCENARIO_2_VENDOR_FALLBACK"
            )

        if has_trusted_vendor and not has_vendor_sku:
            return await self._execute_search_cascade(
                product, scenario_override="SCENARIO_3_VENDOR_NO_SKU"
            )

        return await self._execute_search_cascade(
            product, scenario_override="SCENARIO_4_EXTERNAL_SEARCH"
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
        self, results: list[dict[str, Any]], product: Product
    ) -> list[dict[str, Any]]:
        brand_candidates: list[str] = []
        if product.vendor_name and "склад" not in product.vendor_name.lower():
            brand_candidates.append(product.vendor_name.lower().strip())
        for token in product.title.split():
            clean_tok = re.sub(r"[^a-zA-Z0-9]", "", token).lower()
            if len(clean_tok) >= 3 and clean_tok not in ("ноутбук", "смартфон", "телефон", "кабель", "планшет", "noutbuk", "smartfon"):
                brand_candidates.append(clean_tok)
                break

        def priority_score(item: dict[str, Any]) -> int:
            link = str(item.get("link") or "").lower()
            for b in brand_candidates:
                if f"{b}." in link or f"/{b}" in link or f".{b}" in link:
                    return 1
            if "kaspi.kz" in link:
                return 2
            if "dns-shop.kz" in link:
                return 3
            if any(d in link for d in ["technodom.kz", "shop.kz", "mechta.kz", "sulpak.kz", "fora.kz"]):
                return 4
            if any(d in link for d in ["al-style.kz", "e-katalog.kz", "marvel.kz", "treolan.kz"]):
                return 5
            return 10

        return sorted(results, key=priority_score)

    async def _execute_search_cascade(
        self, product: Product, scenario_override: str
    ) -> ResolvedReference:
        # Step A: If product has barcode, attempt barcode search first
        if product.barcode and product.barcode.strip():
            bc_query = f'"{product.barcode.strip()}" характеристики'
            q_hash_bc = self.compute_query_hash(product, bc_query)
            cached_url = self.repo.get_url_cache(q_hash_bc)

            if cached_url and is_whitelisted_domain(cached_url, extra_domains=self.whitelist_domains):
                return ResolvedReference(
                    product_id=product.product_id,
                    reference_url=cached_url,
                    source_type="CACHE",
                    query_used=bc_query,
                    scenario_applied=scenario_override,
                    status="FOUND",
                    is_cached=True,
                )

            bc_results = await self.serper_client.search_and_filter(
                query=bc_query,
                whitelist_domains=self.whitelist_domains,
                num_results=10,
            )
            if bc_results:
                sorted_bc = self._sort_by_domain_priority(bc_results, product)
                best_link = str(sorted_bc[0].get("link") or "").strip()
                if best_link and is_whitelisted_domain(best_link, extra_domains=self.whitelist_domains):
                    self.repo.save_url_cache(q_hash_bc, best_link, source_type="BARCODE_SEARCH")
                    return ResolvedReference(
                        product_id=product.product_id,
                        reference_url=best_link,
                        source_type="BARCODE_SEARCH",
                        query_used=bc_query,
                        scenario_applied=scenario_override,
                        status="FOUND",
                        is_cached=False,
                    )

        # Step B: Fallback to Title / Model / Manufacturer SKU search
        query, source_type = self._build_title_query(product)
        q_hash = self.compute_query_hash(product, query)

        cached_url = self.repo.get_url_cache(q_hash)
        if cached_url and is_whitelisted_domain(cached_url, extra_domains=self.whitelist_domains):
            return ResolvedReference(
                product_id=product.product_id,
                reference_url=cached_url,
                source_type="CACHE",
                query_used=query,
                scenario_applied=scenario_override,
                status="FOUND",
                is_cached=True,
            )

        results = await self.serper_client.search_and_filter(
            query=query,
            whitelist_domains=self.whitelist_domains,
            num_results=10,
        )

        if results:
            sorted_res = self._sort_by_domain_priority(results, product)
            best_link = str(sorted_res[0].get("link") or "").strip()
            if best_link and is_whitelisted_domain(best_link, extra_domains=self.whitelist_domains):
                self.repo.save_url_cache(q_hash, best_link, source_type=source_type)
                return ResolvedReference(
                    product_id=product.product_id,
                    reference_url=best_link,
                    source_type=source_type,
                    query_used=query,
                    scenario_applied=scenario_override,
                    status="FOUND",
                    is_cached=False,
                )

        return ResolvedReference(
            product_id=product.product_id,
            reference_url=None,
            source_type=source_type,
            query_used=query,
            scenario_applied=scenario_override,
            status="NOT_FOUND",
            is_cached=False,
        )
