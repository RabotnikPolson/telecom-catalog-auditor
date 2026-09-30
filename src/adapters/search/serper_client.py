import asyncio
import logging
from typing import Any, Optional
import httpx
from config.settings import get_settings
from config.whitelist_domains import is_whitelisted_domain

logger = logging.getLogger(__name__)


class SerperClient:

    BASE_URL = "https://google.serper.dev/search"

    SPAM_DOMAINS = [
        "olx.kz",
        "market.kz",
        "aliexpress.com",
        "wildberries.ru",
        "ozon.ru",
        "avito.ru",
        "forum.",
        "pikabu.ru",
        "vk.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "youtube.com",
        "otzovik.com",
        "irecommend.ru",
    ]

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
        max_concurrency: int = 3,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        settings = get_settings()
        self.api_key = (api_key or settings.SERPER_API_KEY or "").strip()
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._external_client = client
        self._client: Optional[httpx.AsyncClient] = client

    async def __aenter__(self) -> "SerperClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None and self._external_client is None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))
        return self._client

    def is_spam_or_unwanted(self, url: str) -> bool:
        lower = url.lower()
        for marker in self.SPAM_DOMAINS:
            if marker in lower:
                return True
        return False

    async def search(self, query: str, num_results: int = 10) -> list[dict[str, Any]]:
        if not self.api_key:
            logger.warning("SerperClient: SERPER_API_KEY is not configured.")
            return []

        clean_query = query.strip()
        if not clean_query:
            return []

        payload = {
            "q": clean_query,
            "gl": "kz",
            "hl": "ru",
            "num": num_results,
        }
        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }

        client = self._get_client()
        async with self._semaphore:
            try:
                response = await client.post(self.BASE_URL, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                return data.get("organic", [])
            except httpx.HTTPStatusError as exc:
                logger.error(f"Serper API HTTP error: {exc.response.status_code}")
                return []
            except Exception as exc:
                logger.error(f"Serper API search error: {exc}")
                return []

    async def search_and_filter(
        self,
        query: str,
        whitelist_domains: Optional[list[str]] = None,
        num_results: int = 10,
    ) -> list[dict[str, Any]]:
        organic_results = await self.search(query, num_results=num_results)
        if not organic_results:
            return []

        whitelisted: list[dict[str, Any]] = []
        organic_clean: list[dict[str, Any]] = []

        for item in organic_results:
            link = str(item.get("link") or "").strip()
            if not link or not link.startswith("http"):
                continue

            if self.is_spam_or_unwanted(link):
                continue

            if is_whitelisted_domain(link, extra_domains=whitelist_domains):
                whitelisted.append(item)
            else:
                organic_clean.append(item)

        return whitelisted if whitelisted else organic_clean
