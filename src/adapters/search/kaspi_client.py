import logging
import re
import urllib.parse
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _clean_token(text: str) -> str:
    """Normalize text by stripping non-alphanumeric characters and converting to lowercase."""
    return re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", "", text).lower()


def is_kaspi_card_relevant(card: dict[str, Any], query: str, brand: str | None = None) -> bool:
    """
    Lightweight zero-overengineering relevance check:
    1. Brand Guard: If brand is known, it must appear in title or shopLink.
    2. SKU / Model Match: The cleaned query (or its key digit tokens) must appear in title or shopLink.
    """
    title = str(card.get("title") or "")
    shop_link = str(card.get("shopLink") or "")
    combined_target = _clean_token(f"{title} {shop_link}")

    # 1. Simple Brand Guard
    if brand:
        clean_brand = _clean_token(brand)
        if len(clean_brand) >= 3 and clean_brand not in combined_target:
            return False

    # 2. Simple SKU / Model match
    clean_q = _clean_token(query)
    if not clean_q:
        return False

    # Direct substring match of cleaned query
    if len(clean_q) >= 4 and clean_q in combined_target:
        return True

    # Check key alphanumeric tokens containing digits (e.g., 'lr2226', '24et', '360')
    tokens = [
        _clean_token(t)
        for t in re.split(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", query)
        if any(c.isdigit() for c in t) and len(t) >= 3
    ]
    if tokens and all(t in combined_target for t in tokens):
        return True

    return False


class KaspiSearchClient:
    """
    Direct Kaspi Search Client utilizing Kaspi's public JSON endpoint.
    Used exclusively as a lazy fallback when external search engines (Serper/Google)
    miss unindexed Kaspi product pages.
    """

    def __init__(self, timeout: float = 2.0, city_code: str = "750000000") -> None:
        self.timeout = timeout
        self.city_code = city_code
        self.base_url = "https://kaspi.kz/yml/product-view/pl/results"

    async def search(
        self,
        query: str,
        brand: str | None = None,
        limit: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> list[str]:
        """
        Queries Kaspi internal search API and returns up to `limit` relevant product card URLs.
        Guaranteed to never raise exceptions, returning [] on timeout or network error.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        should_close = False
        if client is None:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru,en;q=0.9",
                "Referer": f"https://kaspi.kz/shop/search/?text={urllib.parse.quote(clean_query)}",
                "Origin": "https://kaspi.kz",
                "x-ks-city": self.city_code,
            }
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers=headers,
                follow_redirects=True,
            )
            should_close = True

        try:
            resp = await client.get(
                self.base_url,
                params={"page": 0, "text": clean_query},
            )
            if resp.status_code != 200:
                logger.warning(
                    f"Kaspi API returned HTTP {resp.status_code} for query '{clean_query}'"
                )
                return []

            data = resp.json().get("data", [])
            if not isinstance(data, list):
                return []

            candidates: list[str] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                shop_link = item.get("shopLink")
                if not shop_link or "/p/" not in str(shop_link):
                    continue

                if is_kaspi_card_relevant(item, clean_query, brand=brand):
                    full_url = f"https://kaspi.kz/shop{shop_link}"
                    if full_url not in candidates:
                        candidates.append(full_url)
                        if len(candidates) >= limit:
                            break

            return candidates

        except Exception as e:
            logger.warning(f"Kaspi search fallback encountered non-fatal error for '{clean_query}': {e}")
            return []
        finally:
            if should_close:
                await client.aclose()
