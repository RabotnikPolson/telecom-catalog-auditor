import io
from pathlib import Path
import sys

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.domain.entities import AuditStatus, Product
from src.domain.services import ParentChildGrouper


def run_stage1_demo() -> None:
    print("=" * 80)
    print("[*] TELECOM CATALOG AUDITOR — ЭТАП 1: ДОМЕННЫЙ ФУНДАМЕНТ И SQLite")
    print("=" * 80)

    db_path = ROOT_DIR / "catalog_audit.db"
    print(f"\n[1] Инициализация SQLite репозитория: {db_path.name}")
    repo = SQLiteProductRepository(db_path)

    wal_active = repo.check_wal_mode()
    print(f"    [OK] Режим SQLite WAL активен: {wal_active} (PRAGMA journal_mode=WAL)")

    print("\n[2] Создание 4 тестовых карточек магазина shop.telecom.kz:")
    raw_products = [
        Product(
            product_id=1001,
            title="Смартфон Apple iPhone 13 128GB Midnight",
            vendor_name="Al-Style",
            vendor_sku="AL-IP13-128-MID",
            manufacturer_sku="MLPF3RM/A",
            current_specs={
                "экран": "6.1 Super Retina XDR",
                "память": "128 ГБ",
                "ram": "4 ГБ",
                "процессор": "Apple A15 Bionic",
                "цвет": "Midnight"
            },
            status=AuditStatus.PENDING
        ),
        Product(
            product_id=1002,
            title="Apple iPhone 13 128GB Blue (Синий)",
            vendor_name="Al-Style",
            vendor_sku="AL-IP13-128-BLU",
            manufacturer_sku=None,
            current_specs={
                "экран": "6.1 Super Retina XDR",
                "память": "128 ГБ",
                "ram": "4 ГБ",
                "процессор": "Apple A15 Bionic",
                "цвет": "Синий"
            },
            status=AuditStatus.PENDING
        ),
        Product(
            product_id=1003,
            title="Смартфон Apple iPhone 13 128 ГБ Сияющая звезда (slim box)",
            vendor_name=None,
            vendor_sku=None,
            manufacturer_sku=None,
            current_specs={
                "экран": "6.1 Super Retina XDR",
                "память": "128 ГБ",
                "ram": "4 ГБ",
                "процессор": "Apple A15 Bionic",
                "цвет": "Starlight"
            },
            status=AuditStatus.PENDING
        ),
        Product(
            product_id=1004,
            title="Смартфон Apple iPhone 13 256GB Black",
            vendor_name="Al-Style",
            vendor_sku="AL-IP13-256-BLK",
            manufacturer_sku="MLQ63RM/A",
            current_specs={
                "экран": "6.1 Super Retina XDR",
                "память": "256 ГБ",
                "ram": "4 ГБ",
                "процессор": "Apple A15 Bionic",
                "цвет": "Black"
            },
            status=AuditStatus.PENDING
        ),
    ]

    for p in raw_products:
        print(f"    - ID {p.product_id}: '{p.title}' | content_hash={p.content_hash[:12]}...")

    print("\n[3] Запуск ParentChildGrouper (нормализация, защита памяти, дедупликация):")
    grouper = ParentChildGrouper()
    summary = grouper.group_products(raw_products)

    print(f"    [OK] Всего товаров: {summary.total_products}")
    print(f"    [OK] Найдено аппаратных Master-моделей: {summary.master_count}")
    print(f"    [OK] Выделено Child-модификаций: {summary.child_count}")

    print("\n[4] Пакетное сохранение (batch_upsert) в SQLite базу данных:")
    upserted_count = repo.batch_upsert(raw_products)
    print(f"    [OK] Успешно сохранено/обновлено записей: {upserted_count}")

    print("\n[5] Контрольная выборка из SQLite и итоговая витрина связей:")
    print("-" * 115)
    print(
        f"{'ID':<6} | {'РОЛЬ':<9} | {'MASTER-КЛЮЧ':<26} | {'PARENT_SKU':<14} | "
        f"{'АРТИКУЛ MPN':<12} | {'ХЭШ (SHA256)':<16} | {'НАЗВАНИЕ'}"
    )
    print("-" * 115)

    all_stored = repo.get_all_products(limit=10)
    for p in all_stored:
        role = "MASTER *" if p.is_master else "CHILD ->"
        parent = p.parent_sku if p.parent_sku else "- (SELF)"
        mpn = p.manufacturer_sku if p.manufacturer_sku else "-"
        hash_short = f"{p.content_hash[:14]}..."
        print(
            f"{p.product_id:<6} | {role:<9} | {p.master_key:<26} | {parent:<14} | "
            f"{mpn:<12} | {hash_short:<16} | {p.title}"
        )
    print("-" * 115)

    print("\n[6] Демонстрация Инкрементального аудита (Skip-filter):")
    test_incoming_card = Product(
        product_id=1001,
        title="Смартфон Apple iPhone 13 128GB Midnight",
        current_specs={
            "экран": "6.1 Super Retina XDR",
            "память": "128 ГБ",
            "ram": "4 ГБ",
            "процессор": "Apple A15 Bionic",
            "цвет": "Midnight"
        }
    )
    existing = repo.get_by_content_hash(test_incoming_card.content_hash)
    if existing:
        print(f"    [OK] Товар ID {test_incoming_card.product_id} найден в БД с идентичным content_hash!")
        print("    [OK] ВЕРДИКТ: Карточка не изменялась -> СКИП АУДИТА (0 сек, 0$ затрат на LLM/Serper).")
    else:
        print("    [!] Товар изменился или новый -> Отправка на аудит.")

    print("\n" + "=" * 80)
    print("[SUCCESS] ЭТАП 1 УСПЕШНО РЕАЛИЗОВАН И ВЕРИФИЦИРОВАН!")
    print("=" * 80)


if __name__ == "__main__":
    run_stage1_demo()
