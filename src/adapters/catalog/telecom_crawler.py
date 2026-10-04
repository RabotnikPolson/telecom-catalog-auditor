import asyncio
from dataclasses import dataclass, field
import html
import json
import random
import re
from typing import Any, Optional, Union
import httpx


@dataclass
class LeafCategory:
    category_key: str
    name: str
    urlkey: str
    path: str


@dataclass
class CatalogItemPreview:
    product_id: int
    slug: str
    title: str
    detail_url: str
    vendor_sku: Optional[str] = None


@dataclass
class CategoryPageResult:
    category_path: str
    page: int
    total_pages: int
    items: list[CatalogItemPreview] = field(default_factory=list)
    has_next: bool = False


@dataclass
class ProductDetailResult:
    product_id: int
    title: str
    vendor_name: Optional[str]
    vendor_sku: Optional[str]
    manufacturer_sku: Optional[str]
    current_specs: dict[str, str]
    detail_url: str
    shop_sku: Optional[str] = None
    barcode: Optional[str] = None



class TelecomShopCrawler:

    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    }

    def __init__(
        self,
        base_url: str = "https://shop.telecom.kz",
        delay_min: float = 0.6,
        delay_max: float = 1.5,
        max_concurrency: int = 2,
        timeout: float = 20.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._external_client = client
        self._client: Optional[httpx.AsyncClient] = client

    async def __aenter__(self) -> "TelecomShopCrawler":
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self.DEFAULT_HEADERS,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None and self._external_client is None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self.DEFAULT_HEADERS,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client

    async def _polite_delay(self) -> None:
        delay = random.uniform(self.delay_min, self.delay_max)
        await asyncio.sleep(delay)

    async def get_categories_tree(self) -> list[dict[str, Any]]:
        client = self._get_client()
        async with self._semaphore:
            url = f"{self.base_url}/getAllCategories"
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return data
            return []

    async def get_leaf_categories(self) -> list[LeafCategory]:
        tree = await self.get_categories_tree()
        leaves: list[LeafCategory] = []

        def recurse(node: dict[str, Any], current_path_parts: list[str]) -> None:
            urlkey = str(node.get("urlkey") or "").strip()
            name = str(node.get("name") or "").strip()
            if not urlkey:
                return

            new_path_parts = current_path_parts + [urlkey]
            children = node.get("children") or node.get("get_groups") or []
            valid_children = [c for c in children if isinstance(c, dict) and c.get("enabled", 1) == 1]

            if not valid_children:
                full_path = "/".join(new_path_parts)
                leaves.append(
                    LeafCategory(
                        category_key=full_path,
                        name=name,
                        urlkey=urlkey,
                        path=full_path,
                    )
                )
            else:
                for child in valid_children:
                    recurse(child, new_path_parts)

        for top_cat in tree:
            if top_cat.get("enabled", 1) == 1:
                recurse(top_cat, [])

        return leaves

    async def crawl_category_page(self, category_path: str, page: int = 1) -> CategoryPageResult:
        clean_path = category_path.strip("/")
        url = f"{self.base_url}/catalogue/{clean_path}?page={page}"
        client = self._get_client()

        async with self._semaphore:
            await self._polite_delay()
            response = await client.get(url)
            response.raise_for_status()
            text = response.text

        items = self._parse_catalog_items(text)
        total_pages = self._extract_total_pages(text, page)
        has_next = page < total_pages

        return CategoryPageResult(
            category_path=clean_path,
            page=page,
            total_pages=total_pages,
            items=items,
            has_next=has_next,
        )

    def _clean_html_text(self, raw_html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", raw_html)
        text = html.unescape(text)
        return " ".join(text.split())

    def _parse_catalog_items(self, page_html: str) -> list[CatalogItemPreview]:
        results: list[CatalogItemPreview] = []
        seen_ids: set[int] = set()

        product_links = re.findall(
            r'href=["\'](?:https://shop\.telecom\.kz)?/product/(\d+)\?product_unit=([^"\']+)["\']',
            page_html,
        )

        basket_sku_map: dict[int, str] = {}
        button_tags = re.findall(r"<button[^>]*>", page_html, re.DOTALL)
        for btn in button_tags:
            pid_m = re.search(r'data-id=["\'](\d+)["\']', btn)
            sku_m = re.search(r"addToBasket\(this,\s*['\"][^'\"]*['\"],\s*(\d+)", btn)
            if pid_m and sku_m:
                basket_sku_map[int(pid_m.group(1))] = sku_m.group(1).strip()

        title_blocks = re.findall(
            r'<h3[^>]*class=["\'][^"\']*card-order__title[^"\']*["\'][^>]*>\s*<a[^>]*href=["\'][^"\']*/product/(\d+)\?[^"\']*["\'][^>]*>(.*?)</a>',
            page_html,
            re.DOTALL,
        )
        title_map: dict[int, str] = {}
        for pid_str, raw_title in title_blocks:
            try:
                pid = int(pid_str)
                title_map[pid] = self._clean_html_text(raw_title)
            except ValueError:
                pass

        for pid_str, slug in product_links:
            try:
                pid = int(pid_str)
            except ValueError:
                continue

            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            title = title_map.get(pid) or slug.replace("-", " ").title()
            vendor_sku = basket_sku_map.get(pid)

            detail_url = f"{self.base_url}/product/{pid}?product_unit={slug}"
            results.append(
                CatalogItemPreview(
                    product_id=pid,
                    slug=slug,
                    title=title,
                    detail_url=detail_url,
                    vendor_sku=vendor_sku,
                )
            )

        return results

    def _extract_total_pages(self, page_html: str, current_page: int) -> int:
        page_numbers = set()
        matches = re.findall(r'[?&;]page=(\d+)', page_html)
        for m in matches:
            try:
                page_numbers.add(int(m))
            except ValueError:
                pass

        text_page_links = re.findall(
            r'<li[^>]*class=["\'][^"\']*page-item[^"\']*["\'][^>]*>\s*<a[^>]*class=["\']page-link["\'][^>]*>(\d+)</a>',
            page_html,
        )
        for m in text_page_links:
            try:
                page_numbers.add(int(m))
            except ValueError:
                pass

        if not page_numbers:
            return current_page

        return max(max(page_numbers), current_page)

    async def fetch_product_details(
        self, product_url_or_id: Union[str, int]
    ) -> ProductDetailResult:
        if isinstance(product_url_or_id, int) or (
            isinstance(product_url_or_id, str) and product_url_or_id.isdigit()
        ):
            url = f"{self.base_url}/product/{product_url_or_id}"
            pid_hint = int(product_url_or_id)
        elif str(product_url_or_id).startswith("http"):
            url = str(product_url_or_id)
            pid_match = re.search(r"/product/(\d+)", url)
            pid_hint = int(pid_match.group(1)) if pid_match else 0
        else:
            url = f"{self.base_url}/{str(product_url_or_id).lstrip('/')}"
            pid_match = re.search(r"/product/(\d+)", url)
            pid_hint = int(pid_match.group(1)) if pid_match else 0

        client = self._get_client()
        async with self._semaphore:
            await self._polite_delay()
            response = await client.get(url)
            response.raise_for_status()
            page_html = response.text

        return self._parse_product_details_html(page_html, url, pid_hint)

    def _parse_product_details_html(
        self, page_html: str, url: str, pid_hint: int = 0
    ) -> ProductDetailResult:
        pid = pid_hint
        if pid <= 0:
            m = re.search(r"/product/(\d+)", url)
            if m:
                pid = int(m.group(1))

        if pid <= 0:
            m_btn = re.search(r'data-id=["\'](\d+)["\']', page_html)
            if m_btn:
                pid = int(m_btn.group(1))

        shop_sku = None
        barcode = None
        vendor_sku = None
        vendor_name = None
        title = ""

        m_data = re.search(r'<input[^>]*id=["\']productData["\'][^>]*value=["\'](.*?)["\']', page_html, re.DOTALL)
        if m_data:
            try:
                raw_val = html.unescape(m_data.group(1))
                data = json.loads(raw_val)
                if isinstance(data, dict):
                    raw_base_id = data.get("base_id")
                    if raw_base_id and str(raw_base_id).strip():
                        shop_sku = str(raw_base_id).strip()

                    raw_barcode = data.get("barcode")
                    if raw_barcode and str(raw_barcode).strip():
                        barcode = str(raw_barcode).strip()

                    provider_sku = (data.get("partners") or {}).get("article_provider")
                    if provider_sku and str(provider_sku).strip():
                        vendor_sku = str(provider_sku).strip()

                    name_from_json = data.get("name")
                    if name_from_json and str(name_from_json).strip():
                        title = self._clean_html_text(str(name_from_json))

                    all_p = data.get("all_partners") or []
                    if all_p and isinstance(all_p, list) and isinstance(all_p[0], dict):
                        partner_dict = all_p[0].get("partner") or {}
                        p_email = str(partner_dict.get("email") or "").lower()
                        p_addr = str(partner_dict.get("address") or "").lower()
                        if "al-style.kz" in p_email or "al-style.kz" in p_addr:
                            vendor_name = "Al-Style"
                        else:
                            p_name = partner_dict.get("name")
                            if p_name and str(p_name).strip():
                                vendor_name = str(p_name).strip()

                        if not vendor_sku:
                            p_sku = all_p[0].get("article_provider")
                            if p_sku and str(p_sku).strip():
                                vendor_sku = str(p_sku).strip()
            except (json.JSONDecodeError, ValueError, Exception):
                pass

        if not title:
            h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.DOTALL | re.I)
            if h1_match:
                title = self._clean_html_text(h1_match.group(1))

        if not title:
            title_tag = re.search(r"<title>(.*?)</title>", page_html, re.DOTALL | re.I)
            if title_tag:
                title = self._clean_html_text(title_tag.group(1)).split("|")[0].strip()

        if not shop_sku:
            sku_m = re.search(
                r'class=["\'][^"\']*vendor-code[^"\']*["\'][^>]*>\s*Артикул:\s*([^<]+)',
                page_html,
                re.I,
            )
            if sku_m:
                shop_sku = sku_m.group(1).strip()
            else:
                sku_m2 = re.search(r"Артикул:\s*([a-zA-Z0-9_\-\/]+)", page_html, re.I)
                if sku_m2:
                    shop_sku = sku_m2.group(1).strip()


        specs: dict[str, str] = {}
        spec_items = re.findall(
            r'<span[^>]*class=["\']specifications-block__spec-term-text["\'][^>]*>(.*?)</span>\s*</dt>\s*<dd[^>]*class=["\']specifications-block__spec-definition["\'][^>]*>(.*?)</dd>',
            page_html,
            re.DOTALL,
        )
        for raw_k, raw_v in spec_items:
            k = self._clean_html_text(raw_k)
            v = self._clean_html_text(raw_v)
            if k and v:
                specs[k] = v

        if not specs:
            table_rows = re.findall(
                r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>",
                page_html,
                re.DOTALL,
            )
            for raw_k, raw_v in table_rows:
                k = self._clean_html_text(raw_k)
                v = self._clean_html_text(raw_v)
                if k and v:
                    specs[k] = v

        manufacturer_sku = None
        excluded_sku_keys = ["процессор", "видеокарт", "чипсет", "матриц", "экран"]
        for key in ["код модели", "артикул производителя", "партномер", "partnumber", "модель"]:
            for spec_key, spec_val in specs.items():
                s_lower = spec_key.lower()
                if any(ex in s_lower for ex in excluded_sku_keys):
                    continue
                if key in s_lower:
                    manufacturer_sku = spec_val
                    break
            if manufacturer_sku:
                break

        if not vendor_name:
            for key in ["производитель", "бренд", "марка", "вендор"]:
                for spec_key, spec_val in specs.items():
                    if key in spec_key.lower():
                        vendor_name = spec_val
                        break
                if vendor_name:
                    break

        return ProductDetailResult(
            product_id=pid,
            shop_sku=shop_sku,
            title=title,
            vendor_name=vendor_name,
            vendor_sku=vendor_sku,
            manufacturer_sku=manufacturer_sku,
            current_specs=specs,
            detail_url=url,
            barcode=barcode,
        )

    async def search_product_ids(self, query: str) -> list[int]:
        """Search shop.telecom.kz for query and return discovered product IDs in order."""
        client = self._get_client()
        async with self._semaphore:
            await self._polite_delay()
            url = f"{self.base_url}/search"
            try:
                response = await client.get(url, params={"q": query})
                if response.status_code != 200:
                    return []
                seen: list[int] = []
                for pid_str in re.findall(r"/product/(\d+)", response.text):
                    try:
                        pid = int(pid_str)
                        if pid not in seen:
                            seen.append(pid)
                    except ValueError:
                        continue
                return seen
            except Exception:
                return []

    async def fetch_product_on_the_fly(self, target: Union[str, int]) -> Optional[ProductDetailResult]:
        """
        Find and fetch product details directly from shop.telecom.kz.
        target can be a full product URL, internal product_id, or store SKU / query.
        """
        target_str = str(target).strip()
        if not target_str:
            return None

        # 1. Direct URL provided
        if target_str.startswith("http") or "/product/" in target_str:
            try:
                return await self.fetch_product_details(target_str)
            except Exception:
                return None

        # 2. Digits provided (could be shop_sku, vendor_sku, or product_id)
        if target_str.isdigit():
            # First try site search by SKU
            pids = await self.search_product_ids(target_str)
            if pids:
                try:
                    return await self.fetch_product_details(pids[0])
                except Exception:
                    pass

            # If search produced nothing, try direct URL /product/{id}
            try:
                return await self.fetch_product_details(int(target_str))
            except Exception:
                return None

        # 3. Text query
        pids = await self.search_product_ids(target_str)
        if pids:
            try:
                return await self.fetch_product_details(pids[0])
            except Exception:
                return None

        return None
