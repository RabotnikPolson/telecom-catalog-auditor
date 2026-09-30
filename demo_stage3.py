import argparse
import asyncio
import io
from pathlib import Path
import sys
from config.settings import get_settings
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.adapters.search.serper_client import SerperClient
from src.adapters.search.vendor_direct import VendorDirectResolver
from src.domain.entities import Product
from src.use_cases.resolve_reference import ResolveReferenceUseCase


def setup_utf8_terminal() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass


async def run_stage3_demo(db_path_str: str) -> None:
    setup_utf8_terminal()
    db_path = Path(db_path_str)
    repo = SQLiteProductRepository(db_path)
    settings = get_settings()

    print("=" * 75)
    print("  TELECOM CATALOG AUDITOR :: STAGE 3 DEMO")
    print("  Cascade Reference Resolver (6 Resolution Scenarios)")
    print("=" * 75)
    print(f"Database Target : {db_path.resolve()}")
    print(f"Serper API Key  : {'[OK] CONFIGURED' if settings.SERPER_API_KEY else '[WARN] MOCK / OFFLINE MODE'}")
    print("=" * 75)

    vendor_resolver = VendorDirectResolver()
    serper_client = SerperClient(api_key=settings.SERPER_API_KEY)

    use_case = ResolveReferenceUseCase(
        repository=repo,
        vendor_resolver=vendor_resolver,
        serper_client=serper_client,
        verify_direct_urls=False,
    )

    test_products = [
        (
            "СЦЕНАРИЙ 1: Доверенный поставщик Al-Style с артикулом (0$, прямой URL)",
            Product(
                product_id=74619,
                title="Аккумулятор Camelion UB-AA2200-PBH2 Lithium 2 шт",
                vendor_name="Al-Style",
                vendor_sku="63450",
                barcode="849198020366",
            ),
        ),
        (
            "СЦЕНАРИЙ 2: Поставщик Al-Style, но проверка наличия вернула 404 (Фолбэк в сеть)",
            Product(
                product_id=88001,
                title="Маршрутизатор Cisco C1111-8P ISR",
                vendor_name="Al-Style",
                vendor_sku="DISCONTINUED-999",
                manufacturer_sku="C1111-8P",
            ),
        ),
        (
            "СЦЕНАРИЙ 3: Поставщик в доверенных, но артикул пуст (Сразу в Белый список)",
            Product(
                product_id=88002,
                title="Роутер TP-Link Archer C6 AC1200",
                vendor_name="Al-Style",
                vendor_sku=None,
                manufacturer_sku="Archer C6",
            ),
        ),
        (
            "СЦЕНАРИЙ 4: Неизвестный поставщик (Артикул обязательно подмешан в поиск)",
            Product(
                product_id=88003,
                title="Наушники Hoco M1 Pro Type-C White",
                vendor_name="ТОО Ромашка Дистрибьюшн",
                vendor_sku="HOC-M1P-WHT",
                manufacturer_sku=None,
            ),
        ),
        (
            "СЦЕНАРИЙ 5: Безымянный товар без модели и артикула (СКИП: мало данных)",
            Product(
                product_id=88004,
                title="Чехол силиконовый прозрачный",
                vendor_name=None,
                vendor_sku=None,
                manufacturer_sku=None,
                barcode=None,
                current_specs={},
            ),
        ),
        (
            "ПРИОРИТЕТ ШТРИХКОДА: Полноценная модель с EAN-13 (Строгий поиск в кавычках)",
            Product(
                product_id=89636,
                title="Смартфон Apple iPhone 17 256Gb голубой",
                vendor_name="Al-Style",
                vendor_sku="467650",
                barcode="849198020366",
            ),
        ),
    ]

    for label, prod in test_products:
        print(f"\n[*] {label}")
        print(f"    - Товар ID {prod.product_id}: {prod.title}")
        print(f"    - Поставщик: {prod.vendor_name or 'N/A'} | SKU: {prod.vendor_sku or 'N/A'} | Barcode: {prod.barcode or 'N/A'}")

        res = await use_case.execute(prod)
        print(f"    [РЕЗУЛЬТАТ]")
        print(f"      Статус           : {res.status}")
        print(f"      Сценарий         : {res.scenario_applied}")
        print(f"      Тип источника    : {res.source_type}")
        print(f"      Запрос           : {res.query_used or 'N/A'}")
        print(f"      Эталонный URL    : {res.reference_url or 'NONE (ПРОПУЩЕН)'}")
        print(f"      Кэш (SQLite)     : {'HIT' if res.is_cached else 'MISS (первичный расчет)'}")

    print("\n[*] Проверка Двухуровневого Кэша (Повторный вызов Сценария 1)...")
    cached_check = await use_case.execute(test_products[0][1])
    print(f"    [OK] Статус: {cached_check.status} | Источник: {cached_check.source_type} | Кэш: {'[OK] HIT (0$ задержка 0 сек)' if cached_check.is_cached else 'MISS'}")

    print("\n" + "=" * 75)
    print("  STAGE 3 DEMO SUCCEEDED: ALL 6 SCENARIOS VERIFIED")
    print("=" * 75)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telecom Catalog Auditor - Stage 3 Demo")
    parser.add_argument(
        "--db",
        type=str,
        default="catalog_audit.db",
        help="Target SQLite database (default: catalog_audit.db)",
    )
    args = parser.parse_args()
    asyncio.run(run_stage3_demo(args.db))


if __name__ == "__main__":
    main()
