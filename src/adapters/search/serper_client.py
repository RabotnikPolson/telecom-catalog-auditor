import asyncio
import logging
from typing import Any

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
        api_key: str | None = None,
        timeout: float = 15.0,
        max_concurrency: int = 15,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if api_key is not None:
            self.api_key = api_key.strip()
        else:
            settings = get_settings()
            self.api_key = (settings.SERPER_API_KEY or "").strip()
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._external_client = client
        self._client: httpx.AsyncClient | None = client

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=5.0),
            http2=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40, keepalive_expiry=30.0),
        )

    async def __aenter__(self) -> "SerperClient":
        if self._client is None:
            self._client = self._create_client()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None and self._external_client is None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._create_client()
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
            for attempt in range(3):
                try:
                    response = await client.post(self.BASE_URL, json=payload, headers=headers)
                    if response.status_code == 200:
                        data = response.json()
                        if isinstance(data, dict):
                            organic = data.get("organic")
                            if isinstance(organic, list):
                                return [item for item in organic if isinstance(item, dict)]
                        return []
                    elif response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    else:
                        response.raise_for_status()
                except (httpx.RequestError, httpx.HTTPStatusError, OSError) as exc:
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    logger.error(f"Serper API search error for '{clean_query}': {exc}")
                    return []
                except Exception as exc:
                    logger.error(f"Serper API unexpected error for '{clean_query}': {exc}")
                    return []
            return []

    async def search_and_filter(
        self,
        query: str,
        whitelist_domains: list[str] | None = None,
        num_results: int = 10,
    ) -> list[dict[str, Any]]:
        organic_results = await self.search(query, num_results=num_results)
        if not organic_results:
            return []

        whitelisted: list[dict[str, Any]] = []

        for item in organic_results:
            link = str(item.get("link") or "").strip()
            if not link or not link.startswith("http"):
                continue

            if self.is_spam_or_unwanted(link):
                continue

            if is_whitelisted_domain(link, extra_domains=whitelist_domains):
                whitelisted.append(item)

        return whitelisted
