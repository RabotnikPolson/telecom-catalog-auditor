from typing import Optional
from urllib.parse import urlparse


DEFAULT_WHITELIST_DOMAINS: list[str] = [
    "kaspi.kz",
    "dns-shop.kz",
    "shop.kz",
    "mechta.kz",
    "technodom.kz",
    "sulpak.kz",
    "fora.kz",
    "e-katalog.kz",
    "al-style.kz",
    "marvel.kz",
    "treolan.kz",
    "asbis.kz",
    "rrc.kz",
    "apple.com",
    "samsung.com",
    "huawei.com",
    "mi.com",
    "xiaomi.kz",
    "tp-link.com",
    "tplink.com",
    "dlink.ru",
    "honor.com",
    "oppo.com",
    "vivo.com",
    "camelion.ru",
    "camelionbattery.com",
]


def extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if ":" in netloc:
            netloc = netloc.split(":")[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def is_whitelisted_domain(
    url: str,
    extra_domains: Optional[list[str]] = None,
) -> bool:
    target_domain = extract_domain(url)
    if not target_domain:
        return False

    whitelist = set(d.lower() for d in DEFAULT_WHITELIST_DOMAINS)
    if extra_domains:
        whitelist.update(d.lower().strip() for d in extra_domains if d.strip())

    for allowed in whitelist:
        if target_domain == allowed or target_domain.endswith(f".{allowed}"):
            return True

    return False
