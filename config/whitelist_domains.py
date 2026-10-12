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


DOMAIN_PRIORITY_MAP: dict[str, int] = {
    "kaspi.kz": 1,
    "dns-shop.kz": 2,
    "technodom.kz": 4,
    "shop.kz": 4,
    "mechta.kz": 4,
    "sulpak.kz": 4,
    "fora.kz": 4,
    "al-style.kz": 5,
    "e-katalog.kz": 5,
    "marvel.kz": 5,
    "treolan.kz": 5,
    "asbis.kz": 5,
    "rrc.kz": 5,
}


def get_domain_priority(url: str, brand: str | None = None) -> int:
    target_domain = extract_domain(url)
    if not target_domain:
        return 10

    # 1. PRIORITY 1: Kaspi marketplace (primary consumer catalog in Kazakhstan)
    if target_domain == "kaspi.kz" or target_domain.endswith(".kaspi.kz"):
        return 1

    # 2. PRIORITY 2: DNS shop (secondary detailed retail catalog in Kazakhstan)
    if target_domain == "dns-shop.kz" or target_domain.endswith(".dns-shop.kz"):
        return 2

    # 3. PRIORITY 3: Official manufacturer website
    if brand:
        b = brand.lower().strip()
        if len(b) >= 2:
            # Generate brand variants (e.g., 'tp-link', 'tplink', 'tp link')
            variants = {b, b.replace("-", ""), b.replace(" ", "")}
            for bv in variants:
                if len(bv) >= 2:
                    if target_domain.startswith(f"{bv}.") or f".{bv}." in target_domain:
                        return 3
            # Check normalized domain without hyphens (e.g. tp-link.com vs tplink, or vice versa)
            norm_domain = target_domain.replace("-", "")
            norm_b = b.replace("-", "").replace(" ", "")
            if len(norm_b) >= 2:
                if norm_domain.startswith(f"{norm_b}.") or f".{norm_b}." in norm_domain:
                    return 3

    # 4. Major retail & distributor stores
    for domain_name, priority in DOMAIN_PRIORITY_MAP.items():
        if target_domain == domain_name or target_domain.endswith(f".{domain_name}"):
            return priority

    return 10

