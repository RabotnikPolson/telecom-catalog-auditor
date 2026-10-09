from dataclasses import dataclass
import re
from typing import Optional
import httpx
from config.vendor_profiles import find_vendor_profile


@dataclass
class VendorResolutionResult:
    is_available: bool
    reference_url: Optional[str]
    kaspi_code: Optional[str] = None


class VendorDirectResolver:

    def __init__(self, timeout: float = 2.0) -> None:
        self.timeout = timeout
        self._last_checked_response: tuple[str, httpx.Response] | None = None

    def build_direct_url(
        self, vendor_name: Optional[str], vendor_sku: Optional[str]
    ) -> Optional[str]:
        if not vendor_name or not vendor_sku:
            return None

        clean_sku = str(vendor_sku).strip()
        if not clean_sku:
            return None

        profile = find_vendor_profile(vendor_name)
        if not profile:
            return None

        template = profile.get("search_url_template")
        if not template:
            return None

        return template.format(vendor_sku=clean_sku)

    async def resolve_vendor_card(
        self,
        vendor_name: Optional[str],
        vendor_sku: Optional[str],
        client: Optional[httpx.AsyncClient] = None,
    ) -> VendorResolutionResult:
        search_url = self.build_direct_url(vendor_name, vendor_sku)
        if not search_url:
            return VendorResolutionResult(is_available=False, reference_url=None)

        should_close = False
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            should_close = True

        try:
            is_valid = await self.verify_url_availability(search_url, client=client)
            if not is_valid:
                return VendorResolutionResult(is_available=False, reference_url=None)

            if self._last_checked_response and self._last_checked_response[0] == search_url:
                response = self._last_checked_response[1]
            else:
                response = await client.get(search_url) if client else None

            if not response or response.status_code >= 400:
                return VendorResolutionResult(is_available=False, reference_url=None)

            text_lower = response.text.lower()
            not_found_markers = [
                "товар не найден",
                "ничего не найдено",
                "страница не найдена",
                "запрашиваемая страница не существует",
                "no products found",
                "к сожалению, на ваш поисковый запрос ничего не найдено",
            ]
            for marker in not_found_markers:
                if marker in text_lower and "результаты поиска" not in text_lower:
                    return VendorResolutionResult(is_available=False, reference_url=None)

            if "al-style.kz" in search_url:
                if "ничего не найдено" in text_lower:
                    return VendorResolutionResult(is_available=False, reference_url=None)

                card_match = re.search(
                    r'href=["\'](/catalog/[a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-]+/)["\']',
                    response.text,
                )
                if card_match:
                    card_url = f"https://www.al-style.kz{card_match.group(1)}"
                    kaspi_code = None
                    try:
                        card_resp = await client.get(card_url)
                        if card_resp.status_code == 200:
                            kaspi_match = re.search(
                                r'Kaspi.*?<span>\s*(\d{7,12})\s*</span>',
                                card_resp.text,
                                re.DOTALL | re.I,
                            )
                            if kaspi_match:
                                kaspi_code = kaspi_match.group(1).strip()
                    except Exception:
                        pass

                    return VendorResolutionResult(
                        is_available=True,
                        reference_url=card_url,
                        kaspi_code=kaspi_code,
                    )

            return VendorResolutionResult(
                is_available=True,
                reference_url=str(response.url),
            )
        except Exception:
            return VendorResolutionResult(is_available=False, reference_url=None)
        finally:
            if should_close:
                await client.aclose()

    async def verify_url_availability(
        self, url: str, client: Optional[httpx.AsyncClient] = None
    ) -> bool:
        should_close = False
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            should_close = True

        try:
            response = await client.get(url)
            self._last_checked_response = (url, response)
            if response.status_code >= 400:
                return False

            text_lower = response.text.lower()
            not_found_markers = [
                "товар не найден",
                "ничего не найдено",
                "страница не найдена",
                "запрашиваемая страница не существует",
                "no products found",
                "к сожалению, на ваш поисковый запрос ничего не найдено",
            ]
            for marker in not_found_markers:
                if marker in text_lower and "результаты поиска" not in text_lower:
                    return False

            if "al-style.kz" in url and "ничего не найдено" in text_lower:
                return False

            return True
        except Exception:
            return False
        finally:
            if should_close:
                await client.aclose()
