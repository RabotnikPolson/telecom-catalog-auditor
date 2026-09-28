# Telecom Catalog Auditor («Shop Telecom KZ»)

Автономный корпоративный микросервис для регулярного ночного аудита характеристик каталога `shop.telecom.kz`.

## Архитектура (Clean Architecture)
```text
telecom_catalog_auditor/
├── config/
│   ├── __init__.py
│   ├── settings.py          # Pydantic v2 BaseSettings
│   ├── vendor_profiles.py   # Шаблоны URL поставщиков
│   └── whitelist_domains.py # Whitelist доменов для Serper
├── src/
│   ├── __init__.py
│   ├── domain/              # СЛОЙ 1: DOMAIN (Сущности, Pydantic v2, хэширование)
│   │   ├── __init__.py
│   │   ├── entities.py      # Product, SpecItem, AuditResult, AuditStatus
│   │   ├── exceptions.py    # Доменные ошибки
│   │   └── services.py      # ParentChildGrouper (защита памяти, дедупликация)
│   ├── use_cases/           # СЛОЙ 2: APPLICATION
│   │   ├── __init__.py
│   │   ├── crawl_shop_catalog.py
│   │   ├── resolve_reference.py
│   │   └── audit_product.py
│   └── adapters/            # СЛОЙ 3: ADAPTERS
│       ├── __init__.py
│       ├── db/
│       │   ├── __init__.py
│       │   ├── schema.py    # SQLite DDL, WAL-режим, таблицы кэша и состояния
│       │   └── sqlite_repo.py
│       ├── catalog/         # Сбор каталога магазина
│       │   ├── __init__.py
│       │   ├── telecom_crawler.py # Ночной сборщик shop.telecom.kz
│       │   └── file_importer.py
│       ├── search/
│       ├── crawler/
│       ├── llm/
│       └── reporter/
├── tests/
│   ├── __init__.py
│   ├── test_stage1.py
│   └── test_stage2_crawler.py
├── demo_stage1.py
├── demo_crawler.py
├── TECHNICAL_SPECIFICATION.md
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Установка зависимостей
```bash
pip install -r requirements.txt
```

## Запуск тестов
```bash
python -m pytest tests/test_stage1.py -v
```

## Запуск демонстрационного скрипта (Этап 1)
```bash
python demo_stage1.py
```
