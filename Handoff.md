# Текущий контекст Codex — 24.10.2026

Сначала читать `AGENTS.md`, `AGREED_REQUIREMENTS.md` и согласованный `plans/active_plan.md`. Ниже полностью сохранён исторический снимок Antigravity от 23.10.2026. Его утверждения о готовности, тестах, окружении, правах и разрешениях не проверены заново этим документом; не использовать их как действующие указания.

Текущий согласованный объём — перенос конфигурации только `telecom_catalog_auditor`, точечная коррекция cached input GPT-6 Luna до $0.01/1M без смены модели, затем укрепление helper проверки. Stage 5, Stage 6, whitelist, изменение магазина и запись в production не разрешены. `core` — другой репозиторий; переход к нему только после завершения Python-проекта и отдельного обсуждения. `.env` и `Dockerfile-local` не менять. Ничего не отправлять в GitLab.

Навыки выбираются автоматически по задаче через `AGENTS.md`. По решению пользователя от 27.10.2026 основной агент активного чата сам планирует, реализует, проверяет полный diff и один раз запускает полный гейт на итоговом состоянии, включая live-тесты. `implementer` и `auditor` сохранены как необязательные роли и не запускаются автоматически; любой субагент требует отдельного прямого разрешения пользователя. Пропуски и недоступность проверок явно отражаются; это не полная успешная проверка. Автоисправления ограничены согласованным объёмом.

Для продуктовых решений действуют уточнения `AGREED_REQUIREMENTS.md`: полный технический трейс только Дамиру с серверной проверкой; менеджеры обрабатывают отдельные замечания и редактируют выбранные значения до применения; «Апрув» применяет только подтверждённые исправления. Частичная обработка отделена от вердикта аудита; общий `RESOLVED` из старого текста не принят как окончательная модель. Фото/описание в первой версии только показываются. Отсутствие значения в эталоне не является ошибкой магазина. Нужны защита от устаревших предложений, история решений/применения и повторная доставка результата без повторного исправления.

Точные права, тестовая среда, размещение и протокол интеграции остаются открытыми вопросами в требованиях. Старые ссылки на staging, Docker, скорость, успешные тесты и локальные Git-флаги являются историческими заявлениями, а не подтверждением текущего состояния или безопасности БД. Остальные правила сохранять как контекст; спорные положения уточнять, не отменять молча.

---

## Исторический снимок Antigravity (сохранён полностью)

# HANDOFF.md — Передача контекста по проекту «Аудит каталога shop.telecom.kz» (Stage 6)

    **Дата формирования:** 23.10.2026
    **Операционная система:** Windows (PowerShell)
    **Рабочие директории (Два отдельных репозитория):**
    1. **Python-микросервис аудитора (основной воркспейс):**
       `C:\Users\Дамир\.gemini\antigravity-ide\scratch\telecom_catalog_auditor` (ветка: `test`)
    2. **Монолит магазина Казахтелеком (`core` / Laravel 9):**
       `C:\Users\Дамир\.gemini\antigravity-ide\scratch\core` (ветка: `feature/catalog-auditor`)

    ---

    ## 1. Текущая цель и критерии готовности (Definition of Done)

    ### Текущая цель
    Реализовать **Stage 6**: веб-интерфейс «Аудит каталога» внутри административной панели магазина `shop.telecom.kz` (`Админка -> Контент (выпадающее меню) -> Аудит каталога`),
подключённый по HTTP к внешнему Python-микросервису `telecom_catalog_auditor` (FastAPI + SQLite `catalog_audit.db`), с двухколоночным модальным окном сравнения характеристик и
кнопкой «Апрув» (автоматическое исправление ТХ товара в БД магазина с записью в лог).

    ### Критерии готовности (DoD)
    1. **Автономность Python-сервиса:**
       - Python-микросервис запускается вручную командой `python main.py serve --host 0.0.0.0 --port 8000` (на отдельном сервере или локально, без привязки к cron).
       - Если Python-сервис выключен или недоступен, страница в админке Laravel **не падает с 500**, а выводит информативный баннер о том, что сервис аудита сейчас не запущен.
    2. **Точка входа и права доступа в Laravel (`core`):**
       - В `resources/views/admin/master.blade.php` в выпадающем списке «Контент» (около строки 274, под «Контент изменения») добавлен пункт «Аудит каталога».
       - Доступ открыт для всех ролей, имеющих доступ к админке/контенту (`@role('content|admin|commerce')`).
    3. **Главная страница аудита (`/admin/catalog-audit`):**
       - Отображает таблицу проверенных товаров с серверной пагинацией (10/25/50/100) и фильтрацией (по статусу: `ALL`, `MISMATCH`, `VERIFIED`, `MISSING_SPECS`, `NOT_FOUND`, а
также поиск по названию, SKU, штрихкоду и ID).
- Выводит все содержательные поля из CLI-отчёта (`format_audit_card`), **строго исключая** метрики расхода токенов (`input_tokens`, `output_tokens`, `estimated_cost_usd`)
и времени выполнения (`execution_time_sec`, `crawler_time_sec`, `llm_time_sec`).
4. **Двухколоночное модальное окно (Side-by-Side Diff):**
- При клике на товар открывается просторное модальное окно **на этой же странице (без открытия новой вкладки)** в фирменной стилистике Казахтелекома (`#0066b3`).
- В модалке 2 разделённых столбца:
- **Столбец 1:** «Значение на сайте (Shop)» (красная подсветка расхождений).
- **Столбец 2:** «Эталон (Kaspi / Al-Style)» (зелёная подсветка + цитата-пруф `proof_quote` + ссылка на источник `reference_url`).
- Отображаются также: статус фото (`images_count` / `reference_images_count`), противоречия в описании (`description_discrepancies`), недостающие характеристики
(`missing_specs`), поисковый след (`search_trace`).
5. **Кнопка «Апрув» (Применить эталонные данные):**
- При нажатии менеджер подтверждает исправление; Laravel внутри `DB::transaction()` обновляет значения в `product_attributes`, записывает действие в штатную таблицу
`product_logs` (модель `App\Models\ProductLog` с `old_value`, `new_value`, `manager = Auth::id()`) и отправляет запрос в Python API (`POST /api/v1/audit/products/{id}/resolve`),
переводя статус товара в `RESOLVED`.
6. **Соблюдение инвариантов и чистоты Git:**
- В `telecom_catalog_auditor` пройдены проверки `ruff`, `mypy`, `pytest` (через скилл `verify-gate` или субагента `auditor`).
- В `core` файлы `.env` и `Dockerfile-local` остаются невидимыми для Git (`assume-unchanged`) и никогда не попадают в коммиты/пуш.

    ---

    ## 2. На каком этапе остановились и что делали последним

    - **Этап:** Завершено проектирование архитектуры Stage 6 (API-First контракт, дизайн двухколоночной модалки, механика кнопки «Апрув» через `ProductAttributes` и `ProductLog`).
    - **Последние выполненные действия:**
      1. Изучены модели и контроллеры атрибутов в репозитории `core`:
         - `app/Models/ProductAttributes.php`
         - `app/Models/Attributes.php`
         - `app/Http/Controllers/Content/ProductAttributesController.php`
      2. Подтверждён штатный механизм логирования изменений характеристик контент-менеджерами через модель `App\Models\ProductLog` (строки 87–95 в `ProductAttributesController.
php`).
      3. Изучен базовый шаблон админки `resources/views/admin/master.blade.php` (Bootstrap 3.3.5, jQuery 3.5.1, FontAwesome 5.15.4, DataTables, Select2) и найдено точное место
  вставки кнопки в меню «Контент» (строки 274–278).
      4. Сформирован и обновлён полный архитектурный план в артефакте `stage6_admin_content_audit_page_plan.md` и отправлен пользователю на финальное подтверждение перед началом
  написания кода.
      5. Никакие файлы кода для Stage 6 ещё не создавались и не модифицировались (ожидался ответ пользователя «Proceed» / «OK» согласно протоколу `AGENTS.md`).

    ---

    ## 3. Что уже реализовано и проверено, а что только обсуждалось

    ### Подтверждённые факты (Реализовано и проверено)
    1. **Ядро Python-аудитора (Stages 1–4 в `telecom_catalog_auditor`):**
       - Полностью реализованы и протестированы: чтение каталога из MySQL (`mysql_reader.py`), краулер витрины (`telecom_crawler.py`), 5 сценариев поиска эталона Kaspi/Al-Style
(`resolve_reference.py`), Playwright-парсер (`web_crawler.py`), судья на базе OpenAI (`openai_judge.py`), JIT-сохранение в SQLite (`audit_product.py`, `sqlite_repo.py`) и CLI
(`cli.py`).
2. **Развёртывание репозитория магазина (`core`) в локальном Docker:**
- Репозиторий `core` склонирован в `C:\Users\Дамир\.gemini\antigravity-ide\scratch\core`.
- Обойдена проблема невалидных для Windows NTFS имён файлов в `storage/framework/laravel-excel/` с помощью `git sparse-checkout`.
- Создана изолированная ветка `feature/catalog-auditor`.
- В `Dockerfile-local` локально исправлен базовый образ с архивного Debian 11 (`php:8.2.3-fpm`, выдававшего 404 на `apt-get update`) на актуальный Debian 12 Bookworm
(`php:8.2-fpm`), добавлено создание папок `storage/framework/{sessions,views,cache/data}`, заменён `composer update` на `composer install`.
- Собраны и запущены контейнеры `app` (`composer-php`, порт 80) и `redis` (`redis:alpine`, порт 6379).
- Установлены все 192 Composer-пакета в `vendor/`.
- Проверена работоспособность локального сайта: `http://localhost/` возвращает `HTTP 200 OK` (1.19 МБ HTML).
3. **Защита локальных инфраструктурных файлов (`Stealth Mode`):**
- Файлы `.env` и `Dockerfile-local` в репозитории `core` помечены флагом `git update-index --assume-unchanged`. Команда `git status` в `core` полностью чистая.

    ### Только спроектировано и обсуждалось (Ещё НЕ написано в коде)
    1. **Фаза 1 (Python FastAPI):**
       - Файл `src/entrypoints/api.py` (эндпоинты `/api/v1/audit/products`, `/api/v1/audit/products/{id}`, `/api/v1/audit/products/{id}/resolve`, `/api/v1/audit/summary`,
`/api/v1/audit/health`).
- Методы пагинации и пометки `is_resolved` в `src/adapters/db/sqlite_repo.py` (включая миграцию схемы SQLite с инкрементом `PRAGMA user_version`).
- Команда `serve` в `main.py` / `src/entrypoints/cli.py`.
2. **Фаза 2 и 3 (Laravel `core` — Админка и Апрув):**
- Контроллер `app/Http/Controllers/Admin/CatalogAuditController.php` (`index`, `data`, `apply`).
- Шаблоны `resources/views/admin/content/catalog_audit.blade.php` и модальное окно `resources/views/admin/content/catalog_audit_modal.blade.php`.
- Маршруты в `routes/web.php` и ссылка в `resources/views/admin/master.blade.php`.

    ---

    ## 4. Принятые решения и причины (включая отклонённые подходы)

    ### Принятые решения
    1. **Подход API-First (сначала контракт JSON и бэкенд, затем верстка):**
       - *Причина:* Чтобы не переписывать Blade/JS-разметку модалки при несовпадении ключей ответа.
    2. **Скрытие локальных правок `.env` и `Dockerfile-local` через `git update-index --assume-unchanged` (Вариант 2 — Stealth Mode):**
       - *Причина:* В репозитории `core` файлы `.env` и `Dockerfile-local` исторически отслеживаются в Git. Любой коммит этих файлов сломает окружение другим разработчикам или
уронит продакшен при мердже.
3. **Использование `git sparse-checkout` для исключения `/storage/framework/`:**
- *Причина:* В истории Git репозитория `core` закоммичен мусорный файл кэша с символами `\r\n` и `:` в имени (`storage/framework/laravel-excel/laravel-excel-
  hj9FNK9ezdBRzaFZIqkaJAWk7eTJ44Mp.36\r\nCookie:...`). На Windows NTFS обычный `git checkout` падает с фатальной ошибкой создания файла.
4. **Апрув расхождений только через транзакцию в Laravel + `ProductLog`:**
- *Причина:* Python-сервис имеет строгое правило `READ ONLY` к MySQL базе магазина (`AGENTS.md`). Любое изменение характеристик на витрине должно проходить штатным путём
через Eloquent (`ProductAttributes`) с обязательным логированием ID менеджера в `ProductLog`.
5. **Чтение результатов аудита на странице админки напрямую из Python FastAPI (SQLite), а не через тяжёлые запросы к MySQL:**
- *Причина:* База данных `DB_HOST=10.200.10.100` (staging MySQL) доступна через OpenVPN с сетевой задержкой ~50–90 мс на каждый SQL-запрос. Главная страница магазина
делает ~150 запросов и грузится ~40 секунд. Чтение готового JSON из локального FastAPI (SQLite) занимает `< 5–10 мс`, поэтому страница аудита будет открываться мгновенно.

    ### Отклонённые подходы (НЕ ПОВТОРЯТЬ!)
    1. **ОТКЛОНЕНО: Коммит и пуш «исправленного» `Dockerfile-local` в общую ветку `core`.**
       - *Почему опасно:* Пользователь вынес строгий выговор за предложение запушить изменённый `Dockerfile-local`. Команда разработки магазина работает на macOS со своими
образами и настройками; пуш наших правок `Dockerfile-local` или `.env` категорически запрещён.
2. **ОТКЛОНЕНО: Добавление `.env` и `Dockerfile-local` в `.gitignore`.**
- *Почему не работает:* В Git правила `.gitignore` игнорируются для файлов, которые **уже находятся в индексе** (tracked). Если сделать `git rm --cached .env`, при слиянии
ветки в прод файл `.env` удалится на сервере! Поэтому используется только `git update-index --assume-unchanged`.
3. **ОТКЛОНЕНО: Открытие карточки каждого товара в новой вкладке браузера.**
- *Почему отклонено:* Пользователь явно указал, что контент-менеджеру неудобно плодить вкладки — нужна просторная двухколоночная модалка на этой же странице.
4. **ОТКЛОНЕНО: Отображение времени выполнения и стоимости токенов LLM в веб-интерфейсе.**
- *Почему отклонено:* Прямое требование пользователя — скрыть расход токенов и тайминги из UI админки (они нужны только в CLI).

    ---

    ## 5. Изменённые файлы и незавершённые изменения

    ### Репозиторий 1: `C:\Users\Дамир\.gemini\antigravity-ide\scratch\telecom_catalog_auditor`
    - **Ветка:** `test` (отслеживает `origin/test`).
    - **Статус (`git status`):** Чисто (`working tree clean`). Незавершённых изменений в файлах нет.

    ### Репозиторий 2: `C:\Users\Дамир\.gemini\antigravity-ide\scratch\core`
    - **Ветка:** `feature/catalog-auditor`.
    - **Статус (`git status`):** Чисто (`working tree clean`).
    - **Локально модифицированные файлы со статусом `assume-unchanged` (`git ls-files -v | Select-String "^[a-z]"`):**
      - `h .env` — локально настроено подключение к Docker Redis (`REDIS_HOST=redis`, без пароля) при сохранении VPN-доступа к staging MySQL (`DB_HOST=10.200.10.100`). **НЕ
ТРОГАТЬ И НЕ КОММИТИТЬ.**
- `h Dockerfile-local` — локально переведён с `php:8.2.3-fpm` на `php:8.2-fpm` для работы контейнера на Windows. **НЕ ТРОГАТЬ И НЕ КОММИТИТЬ.**
- **Файлы со статусом `skip-worktree` (`S`):**
- Директория `storage/framework/*` скрыта через `sparse-checkout`, чтобы NTFS не падал на битых именах файлов кэша Laravel Excel.

    ---

    ## 6. Известные ошибки, блокеры и точные сообщения об ошибках

    1. **Медленная загрузка главной страницы магазина (`http://localhost/` грузится ~40 секунд):**
       - *Причина (Подтверждённый факт):* Приложение в Docker на Windows подключается к удалённой БД `10.200.10.100` через OpenVPN (`10.8.0.11`). Сетевой пинг составляет ~55 мс.
Главная страница `shop.telecom.kz` выполняет ~150 последовательных SQL-запросов без прогретого кэша (`150 * 60 мс + 900 мс TTFB`).
- *Влияние на нашу задачу:* На страницу `/admin/catalog-audit` это не влияет, так как данные аудита берутся из локального SQLite через FastAPI за `< 10 мс`.
2. **Ошибка NTFS при отключении `sparse-checkout` в `core`:**
- *Точное сообщение об ошибке:*
`error: invalid path 'storage/framework/laravel-excel/laravel-excel-hj9FNK9ezdBRzaFZIqkaJAWk7eTJ44Mp.36\r\nCookie: ...'`
- *Решение (уже применено):* Всегда оставлять включённым `git sparse-checkout` с исключением `!/storage/framework/` и `git config core.protectNTFS false`.
3. **Запрет на параллельное изменение существующих файлов ядра `core`:**
- В репозитории `core` разрешено создавать только новые изолированные файлы для нашего модуля и внести ровно 2 точечные вставки: маршрут в `routes/web.php` и пункт меню в
`resources/views/admin/master.blade.php`.

    ---

    ## 7. Последние запущенные проверки: команды и результаты

    1. **Проверка чистоты Git в обоих репозиториях (23.10.2026):**
       - Команда:
         ```powershell
         git -C "C:\Users\Дамир\.gemini\antigravity-ide\scratch\telecom_catalog_auditor" status --short -b
         git -C "C:\Users\Дамир\.gemini\antigravity-ide\scratch\core" status --short -b
         ```
       - Результат:
         - `telecom_catalog_auditor`: `## test...origin/test` (0 изменённых файлов).
         - `core`: `## feature/catalog-auditor` (0 видимых для Git изменений).
    2. **Проверка флагов `assume-unchanged` в `core` (23.10.2026):**
       - Команда:
         ```powershell
         git -C "C:\Users\Дамир\.gemini\antigravity-ide\scratch\core" ls-files -v | Select-String "^[a-z]"
         ```
       - Результат:
         ```text
         h .env
         h Dockerfile-local
         ```
    3. **Проверка локального веб-сервера магазина в Docker (из предыдущей сессии):**
       - Команда: `curl.exe -I http://localhost/`
       - Результат: `HTTP/1.1 200 OK` (контейнеры `app` и `redis` работают).
       - *Примечание (Предположение):* Так как между сессиями происходил рестарт сервера/системы (`23.10.2026`), перед проверкой UI в браузере нужно убедиться, что Docker Desktop
запущен и контейнеры `core` в статусе `Up` (`docker compose -f docker-compose-local.yml ps`).

    ---

    ## 8. Следующий конкретный шаг

    1. **Дождаться явного подтверждения плана от пользователя** («Proceed» / «OK» / «Погнали») по артефакту `stage6_admin_content_audit_page_plan.md` (согласно правилу `0.1 Plan
First` и `0.2 Hard Locks` в `AGENTS.md`).
    2. **Сразу после подтверждения — выполнить Фазу 1 (Бэкенд Python в `telecom_catalog_auditor`):**
       - В `src/adapters/db/sqlite_repo.py`:
         - Добавить поддержку поля `is_resolved INTEGER NOT NULL DEFAULT 0` в таблицу `audit_results` (через безопасную миграцию с инкрементом `PRAGMA user_version`, не удаляя
  существующие `UNIQUE` индексы).
         - Реализовать методы `get_paginated_audit_results(page, per_page, status, search)`, `get_audit_detail(product_id)` и `mark_audit_resolved(product_id)`.
       - Создать `src/entrypoints/api.py` (FastAPI приложение с CORS и эндпоинтами `/api/v1/audit/products`, `/api/v1/audit/products/{product_id}`,
  `/api/v1/audit/products/{product_id}/resolve`, `/api/v1/audit/summary`, `/api/v1/audit/health`). Убедиться, что поля токенов и времени выполнения исключены из ответа API.
       - В `src/entrypoints/cli.py` (и `main.py`): добавить подкоманду `serve` (`--host`, `--port`).
       - Написать unit-тесты в `tests/unit/test_api.py` и прогнать гейт верификации (`ruff check .`, `mypy src`, `pytest`).
    3. **Затем выполнить Фазу 2 и 3 (Интеграция в `core` / Laravel):**
       - Создать `app/Http/Controllers/Admin/CatalogAuditController.php`.
       - Создать Blade-шаблоны `resources/views/admin/content/catalog_audit.blade.php` и модальное окно.
       - Добавить маршруты в `routes/web.php` и пункт меню в `resources/views/admin/master.blade.php`.

    ---

    ## 9. Какие действия пользователь уже одобрил, а какие ещё требуют согласования

    ### Уже одобрено пользователем (Подтверждённые факты)
    - Размещение Python-сервиса отдельно от сервера магазина, с ручным запуском (без cron).
    - Расположение кнопки в админке: `Админка -> Контент -> Аудит каталога`.
    - Доступ к странице для всех ролей, имеющих доступ к админке.
    - Состав полей: все поля из терминального вывода CLI, **кроме** расхода токенов, стоимости и времени выполнения.
    - Наличие фильтрации и серверной пагинации на главной странице аудита.
    - Открытие карточки товара по клику в **просторном двухколоночном модальном окне** (1-й столбец — наши поля на сайте, 2-й столбец — как указано в эталоне) без открытия новой
вкладки.
- Наличие кнопки «Апрув» в модальном окне, которая автоматически обновляет характеристики товара на эталонные значения.
- Использование «Варианта 2» (Stealth Mode через `assume-unchanged`) для `.env` и `Dockerfile-local` в репозитории `core`.

    ### Требует согласования (Hard Locks по `AGENTS.md`)
    - **Финальная отмашка («Proceed» / «OK») на старт кодирования Stage 6** по обновлённому плану `stage6_admin_content_audit_page_plan.md` (в `AGENTS.md` п. 0.2 стоит жёсткий
замок: *«NEVER proceed to Stage 5 (Excel) or Stage 6 (FastAPI) and NEVER modify config/whitelist_domains.py without explicit user approval»*).
- Любые изменения в `config/whitelist_domains.py` или переход к Stage 5 (Excel-экспорт) — **строго запрещены** без отдельного запроса пользователя.

    ---

    ## 10. Какие планы, документы и артефакты нужно прочитать

    Перед началом работы новому агенту необходимо прочитать следующие файлы в указанном порядке:

    ### В репозитории `telecom_catalog_auditor`
    1. `C:\Users\Дамир\.gemini\antigravity-ide\scratch\telecom_catalog_auditor\AGENTS.md` — **Конституция проекта** (архитектурные инварианты, запрет глобального состояния,
правила миграций SQLite, протокол делегирования и верификации).
2. `C:\Users\Дамир\.gemini\antigravity-ide\scratch\telecom_catalog_auditor\.agents\skills\verify-gate\SKILL.md` — обязательный проверочный гейт перед завершением любых
изменений в Python-коде.
3. `C:\Users\Дамир\.gemini\antigravity-ide\scratch\telecom_catalog_auditor\src\domain\entities.py` — структуры `Product`, `AuditResult`, `DiscrepancyItem`, `MissingSpecItem`,
`DescriptionDiscrepancyItem`.
4. `C:\Users\Дамир\.gemini\antigravity-ide\scratch\telecom_catalog_auditor\src\entrypoints\cli.py` (особенно функция `format_audit_card` на строках 519–579) — эталон того,
какие именно поля сейчас выводятся в терминале.
5. `C:\Users\Дамир\.gemini\antigravity-ide\scratch\telecom_catalog_auditor\src\adapters\db\sqlite_repo.py` — текущая схема таблиц `products` и `audit_results` и методы
репозитория.

    ### В репозитории `core` (Laravel)
    6. `C:\Users\Дамир\.gemini\antigravity-ide\scratch\core\app\Http\Controllers\Content\ProductAttributesController.php` (строки 40–100) — как устроено обновление
`ProductAttributes` и запись истории правок в `ProductLog`.
7. `C:\Users\Дамир\.gemini\antigravity-ide\scratch\core\app\Models\Attributes.php` и `app\Models\ProductAttributes.php` — модели характеристик товара.
8. `C:\Users\Дамир\.gemini\antigravity-ide\scratch\core\resources\views\admin\master.blade.php` (строки 1–60 для подключённых CSS/JS библиотек и строки 197–279 для меню
«Контент»).

    ### Ключевые артефакты планирования (в директории артефактов)
    9. `C:\Users\Дамир\.gemini\antigravity-cli\brain\cb8cdfbf-647c-4d0c-8e72-4c44a7069eae\stage6_admin_content_audit_page_plan.md` — **главный актуальный план реализации Stage
6** (схема последовательности, JSON-контракты API, эскиз двухколоночной модалки, код транзакции апрува).
10. `C:\Users\Дамир\.gemini\antigravity-cli\brain\cb8cdfbf-647c-4d0c-8e72-4c44a7069eae\laravel_core_rules_and_safety_constitution.md` — правила безопасности при работе с
репозиторием `core` и базой данных магазина.
11. `C:\Users\Дамир\.gemini\antigravity-cli\brain\cb8cdfbf-647c-4d0c-8e72-4c44a7069eae\git_workflow_and_boundaries_guide.md` — памятка по обновлению ветки из `origin` без
потери локальных настроек и без риска затереть прод.
