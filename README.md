# Telecom Catalog Auditor («Shop Telecom KZ»)

Автономный корпоративный микросервис для регулярного фактологического аудита характеристик каталога `shop.telecom.kz`.

## Архитектура (Clean Architecture)
```text
telecom_catalog_auditor/
├── config/
│   ├── __init__.py
│   └── settings.py          # Pydantic v2 BaseSettings (.env)
├── src/
│   ├── __init__.py
│   ├── domain/              # СЛОЙ 1: DOMAIN (Чистая бизнес-логика, Pydantic v2)
│   │   ├── __init__.py
│   │   ├── entities.py      # Product (с детерминированным SHA-256 hash), SpecItem, AuditResult
│   │   ├── exceptions.py    # Доменные исключения
│   │   └── services.py      # ParentChildGrouper (нормализация, защита памяти, дедупликация)
│   └── adapters/            # СЛОЙ 2: ADAPTERS (Внешние интеграции и БД)
│       ├── __init__.py
│       └── db/
│           ├── __init__.py
│           ├── schema.py    # SQLite DDL, WAL-режим, dual-cache таблицы
│           └── sqlite_repo.py# Репозиторий товаров и двухуровневого кэша
├── tests/
│   ├── __init__.py
│   └── test_stage1.py       # 11 юнит-тестов (content_hash, grouper, sqlite WAL)
├── demo_stage1.py           # Демо-запуск Этапа 1
├── requirements.txt
└── .env.example
```

## Установка зависимостей
```bash
pip install -r requirements.txt
```

## Запуск тестов
```bash
pytest tests/test_stage1.py -v
```

## Запуск демонстрационного скрипта (Этап 1)
```bash
python demo_stage1.py
```
