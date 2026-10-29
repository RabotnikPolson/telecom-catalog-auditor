from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BrandSyncResult:
    audited_products: int
    mysql_products: int
    brands_found: int
    missing_products: int
    changes: int
    updated: int
    applied: bool


class SyncAuditedBrandsUseCase:
    def __init__(
        self,
        get_current: Callable[[], dict[int, str | None]],
        fetch_source: Callable[[Sequence[int]], Awaitable[dict[int, str | None]]],
        apply_updates: Callable[[dict[int, str | None]], int],
    ) -> None:
        self.get_current = get_current
        self.fetch_source = fetch_source
        self.apply_updates = apply_updates

    @staticmethod
    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    async def execute(self, *, apply: bool = False) -> BrandSyncResult:
        current = self.get_current()
        product_ids = tuple(current)
        source_brands = await self.fetch_source(product_ids)
        normalized = {
            product_id: self._clean(brand)
            for product_id, brand in source_brands.items()
            if product_id in current
        }
        changes = {
            product_id: brand
            for product_id, brand in normalized.items()
            if self._clean(current[product_id]) != brand
        }
        updated = self.apply_updates(changes) if apply else 0
        return BrandSyncResult(
            audited_products=len(current),
            mysql_products=len(normalized),
            brands_found=sum(brand is not None for brand in normalized.values()),
            missing_products=len(set(product_ids) - set(normalized)),
            changes=len(changes),
            updated=updated,
            applied=apply,
        )
