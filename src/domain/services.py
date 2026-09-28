from dataclasses import dataclass, field
import re
from typing import Sequence
from .entities import Product
from .exceptions import GroupingError


@dataclass
class GroupingSummary:
    total_products: int
    master_count: int
    child_count: int
    groups: dict[str, list[Product]] = field(default_factory=dict)
    masters: list[Product] = field(default_factory=list)
    children: list[Product] = field(default_factory=list)


class ParentChildGrouper:
    CATEGORY_PREFIXES: list[str] = [
        r"\bсмартфон\b",
        r"\bсотовый телефон\b",
        r"\bмобильный телефон\b",
        r"\bтелефон\b",
        r"\bпланшет\b",
        r"\bумные часы\b",
        r"\bsmart\s*phone\b",
        r"\bsmartphone\b",
    ]

    PACKAGING_NOISE: list[str] = [
        r"\(slim\s*box\)",
        r"\bslim\s*box\b",
        r"\bslimbox\b",
        r"\bбез з/у\b",
        r"\bбез зарядного устройства\b",
        r"\bростест\b",
        r"\bросэп\b",
        r"\bglobal\b",
        r"\beu\b",
        r"\bkz\b",
        r"\bkaz\b",
        r"\bказ\b",
        r"\be-?sim\b",
        r"\bdual\s*sim\b",
        r"\b2sim\b",
        r"\b1sim\b",
        r"\bnew\b",
        r"\bновинка\b",
    ]

    MULTI_WORD_COLORS: list[str] = [
        r"т[её]мная\s+ночь",
        r"сияющая\s+звезда",
        r"альпийск(?:ий|ая)\s+зелен[ья]",
        r"небесно[- ]голубой",
        r"глубок(?:ий|ая)\s+фиолетов(?:ый|ая)",
        r"тих(?:ий|ая)\s+океан",
        r"космическ(?:ий|ая)\s+сер(?:ый|ая)",
        r"космическ(?:ий|ая)\s+черн(?:ый|ая)",
        r"зв[её]здн(?:ый|ая)\s+свет",
        r"розов(?:ое|ая)\s+золот(?:о|ая)",
        r"серебрист(?:ый|ая)\s+иней",
        r"ослепительно\s+бел(?:ый|ая)",
        r"матов(?:ый|ая)\s+ч[её]рн(?:ый|ая)",
        r"space\s+gr[ae]y",
        r"space\s+black",
        r"midnight\s+green",
        r"pacific\s+blue",
        r"sierra\s+blue",
        r"deep\s+purple",
        r"alpine\s+green",
        r"rose\s+gold",
        r"phantom\s+black",
        r"phantom\s+silver",
        r"phantom\s+white",
        r"mystic\s+bronze",
        r"natural\s+titanium",
        r"black\s+titanium",
        r"white\s+titanium",
        r"blue\s+titanium",
        r"cosmic\s+black",
        r"prism\s+white",
    ]

    SINGLE_WORD_COLORS: list[str] = [
        r"ч[её]рн(?:ый|ая|ое|ом|ые)?",
        r"бел(?:ый|ая|ое|ом|ые)?",
        r"син(?:ий|яя|ее|ем|ие)?",
        r"голуб(?:ой|ая|ое|ом|ые)?",
        r"красн(?:ый|ая|ое|ом|ые)?",
        r"зел[её]н(?:ый|ая|ое|ом|ые)?",
        r"сер(?:ый|ая|ое|ом|ые)?",
        r"золот(?:ой|ая|ое|ом|ые)?",
        r"серебрист(?:ый|ая|ое|ом|ые)?",
        r"серебр(?:о)?",
        r"розов(?:ый|ая|ое|ом|ые)?",
        r"фиолетов(?:ый|ая|ое|ом|ые)?",
        r"ж[её]лт(?:ый|ая|ое|ом|ые)?",
        r"оранжев(?:ый|ая|ое|ом|ые)?",
        r"графитов(?:ый|ая|ое|ом|ые)?",
        r"графит",
        r"титанов(?:ый|ая|ое|ом|ые)?",
        r"титан",
        r"пурпурн(?:ый|ая|ое|ом|ые)?",
        r"лавандов(?:ый|ая|ое|ом|ые)?",
        r"мятн(?:ый|ая|ое|ом|ые)?",
        r"бирюзов(?:ый|ая|ое|ом|ые)?",
        r"бежев(?:ый|ая|ое|ом|ые)?",
        r"midnight",
        r"starlight",
        r"graphite",
        r"silver",
        r"gold",
        r"black",
        r"white",
        r"blue",
        r"green",
        r"red",
        r"purple",
        r"yellow",
        r"pink",
        r"orange",
        r"gr[ae]y",
        r"titanium",
        r"lavender",
        r"mint",
        r"coral",
        r"cream",
        r"violet",
    ]

    def __init__(self) -> None:
        all_color_patterns = self.MULTI_WORD_COLORS + [
            rf"\b{c}\b" for c in self.SINGLE_WORD_COLORS
        ]
        self._color_regex = re.compile(
            rf"(?:{'|'.join(all_color_patterns)})",
            re.IGNORECASE | re.UNICODE
        )

        all_noise = self.CATEGORY_PREFIXES + self.PACKAGING_NOISE
        self._noise_regex = re.compile(
            rf"(?:{'|'.join(all_noise)})",
            re.IGNORECASE | re.UNICODE
        )

        self._storage_regex = re.compile(
            r"(\b\d+)\s*(gb|гб|tb|тб|mb|мб)\b",
            re.IGNORECASE | re.UNICODE
        )

    def extract_master_key(self, title: str, specs: dict[str, str] | None = None) -> str:
        text = title.strip().lower()

        def _normalize_storage(match: re.Match[str]) -> str:
            val = match.group(1)
            unit = match.group(2).lower()
            if unit in ("гб", "gb"):
                return f"{val}gb"
            if unit in ("тб", "tb"):
                return f"{val}tb"
            if unit in ("мб", "mb"):
                return f"{val}mb"
            return f"{val}{unit}"

        text = self._storage_regex.sub(_normalize_storage, text)
        text = self._noise_regex.sub(" ", text)
        text = self._color_regex.sub(" ", text)

        has_memory = bool(re.search(r"\b\d+(?:gb|tb|mb)\b", text))
        if not has_memory and specs:
            for spec_key, spec_val in specs.items():
                k_low = spec_key.lower()
                if any(m in k_low for m in ("встроенная память", "память", "объем памяти", "storage", "rom")):
                    mem_match = self._storage_regex.search(spec_val.lower())
                    if mem_match:
                        mem_str = _normalize_storage(mem_match)
                        text = f"{text} {mem_str}"
                        break

        text = re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE)
        tokens = [t.strip() for t in text.split() if t.strip()]

        if not tokens:
            raise GroupingError(f"Failed to generate master key for product title: '{title}'")

        master_key = "_".join(tokens)
        return master_key

    def group_products(self, products: Sequence[Product]) -> GroupingSummary:
        if not products:
            return GroupingSummary(total_products=0, master_count=0, child_count=0)

        groups: dict[str, list[Product]] = {}

        for prod in products:
            key = self.extract_master_key(prod.title, prod.current_specs)
            prod.master_key = key
            if key not in groups:
                groups[key] = []
            groups[key].append(prod)

        masters: list[Product] = []
        children: list[Product] = []

        for key, bucket in groups.items():
            if not bucket:
                continue

            sorted_bucket = sorted(
                bucket,
                key=lambda p: (
                    p.sku_completeness_score,
                    len(p.current_specs),
                    -p.product_id
                ),
                reverse=True
            )

            master_item = sorted_bucket[0]
            master_item.is_master = True
            master_item.parent_sku = None

            master_reference_sku = (
                master_item.manufacturer_sku
                or master_item.vendor_sku
                or f"PID-{master_item.product_id}"
            )

            masters.append(master_item)

            for child in sorted_bucket[1:]:
                child.is_master = False
                child.parent_sku = master_reference_sku
                children.append(child)

        return GroupingSummary(
            total_products=len(products),
            master_count=len(masters),
            child_count=len(children),
            groups=groups,
            masters=masters,
            children=children
        )
