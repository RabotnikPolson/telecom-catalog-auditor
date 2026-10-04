from typing import Any, Optional


VENDOR_PROFILES: dict[str, dict[str, Any]] = {
    "Al-Style": {
        "name": "Al-Style",
        "domain": "al-style.kz",
        "aliases": ["al-style", "al style", "алстайл", "ал-стайл", "vender1"],
        "search_url_template": "https://www.al-style.kz/search/index.php?q={vendor_sku}&s=%D0%9F%D0%BE%D0%B8%D1%81%D0%BA",
    },
}


def find_vendor_profile(vendor_name: Optional[str]) -> Optional[dict[str, Any]]:
    if not vendor_name:
        return None

    clean = vendor_name.strip().lower()
    for canonical_name, profile in VENDOR_PROFILES.items():
        if clean == canonical_name.lower():
            return profile
        for alias in profile.get("aliases", []):
            if alias in clean:
                return profile

    return None
