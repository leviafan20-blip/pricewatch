# Проверка второго этапа PriceWatch

Проверено 20 октября 2026 года в Docker: Linux, Python 3.13.16,
Django 5.2.18, PostgreSQL 17.11, HTTPX 0.28.1, HTTPcore 1.0.9,
BeautifulSoup 4.15.0.

## Результат

Реализованы безопасная загрузка HTML и отдельный парсер товара. Авторизация,
маршруты и шаблоны первого этапа не менялись. Таблиц товаров, истории,
расписания, очередей и новых страниц нет.

- Полный набор: **276 passed**, из них 25 прежних тестов первого этапа.
- Покрытие собственного Python-кода `apps`: **93%**.
- `manage.py check`: без ошибок.
- `manage.py migrate --check`: все миграции применены.
- `makemigrations --check --dry-run`: изменений нет.
- `pip check`: конфликтов зависимостей нет.
- Создана и применена `monitoring.0001_initial`: только служебная таблица
  `SiteAccessState` для общих лимитов и временного права на сетевой запрос.
- Обе команды парсера из README выполнены в контейнере на локальных файлах.
- Финальная сборка Docker успешна; PostgreSQL и Django имеют статус `healthy`,
  одноразовый контейнер миграций завершился с кодом `0`.
- После пересоздания контейнеров `/health/live/`, `/health/ready/` и
  `/accounts/login/` на `http://127.0.0.1:8010` вернули HTTP `200`.

Команда полного тестирования:

```powershell
docker compose exec web python -m pytest --ds=config.settings.test --cov=apps --cov-report=term-missing
```

Тесты scraping блокируют случайное DNS-разрешение и открытие TCP-соединений.
Исключение DNS — только настроенный PostgreSQL, необходимый тестам конкурентных
блокировок; HTTP-ответы и DNS сайтов полностью подменены. Обращений к реальным
магазинам не выполнялось.

## Что проверено

- Точные разрешённые домены, IDNA, стандартные порты, запрет credentials,
  управляющих символов, внутренних IPv4/IPv6 и смешанных DNS-ответов.
- Фактический TCP target IP, исходный Host, TLS SNI и обязательная проверка
  сертификата, игнорирование proxy/CA-переменных окружения.
- Повторная проверка DNS на перенаправлениях, циклы, неверный Location,
  переходы на запрещённые сайты и HTTPS → HTTP.
- Размер исходного и распакованного ответа; gzip/deflate, повреждённое сжатие,
  кодировки, типы документов, сетевые ошибки и медленная передача заголовков.
- robots Allow/Disallow, наиболее длинное правило, маски, User-Agent,
  404/410, 401/403, временная недоступность, HTML вместо правил и Cache-Control.
- Crawl-delay уже перед первой HTML-загрузкой, Retry-After и общий лимит
  независимых экземпляров; конкурентное создание, завершение и истечение lease.
- CSS, JSON-LD Product/Offer/@graph/@id, метаданные, Decimal, разные валюты,
  отсутствие цены, ноль, неизвестное наличие, неправильные селекторы,
  неоднозначные предложения, старая и текущая цены внутри общего контейнера.
- Безопасные коды ошибок и отсутствие чувствительных query-параметров в логах.
- Локальная management command, включая размер/кодировку файлов и отказ от URL.

## Самостоятельная проверка

```powershell
docker compose exec web python manage.py parse_product tests/fixtures/scraping/json_ld_product.html
```

Ожидаются: `PriceWatch Desk Lamp`, цена `1299.95`, `USD`, `in_stock`, источник `json_ld`.

```powershell
docker compose exec web python manage.py parse_product tests/fixtures/scraping/manual_product.html --title-selector h1 --price-selector .price --currency RUB --availability-selector .availability
```

Ожидаются: `Кофемолка PriceWatch`, цена `12499.90`, `RUB`, `in_stock`, источник `manual`.
Старая цена `14999.00` не выбирается.

Полное объяснение процесса, диагностики и ограничений: `docs/components/scraping.md`.

## Созданные файлы

Все пути ниже относятся к корню проекта.

```text
apps/monitoring/__init__.py
apps/monitoring/apps.py
apps/monitoring/models.py
apps/monitoring/services.py
apps/monitoring/migrations/__init__.py
apps/monitoring/migrations/0001_initial.py
apps/monitoring/management/__init__.py
apps/monitoring/management/commands/__init__.py
apps/monitoring/management/commands/parse_product.py
apps/monitoring/scraping/__init__.py
apps/monitoring/scraping/errors.py
apps/monitoring/scraping/types.py
apps/monitoring/scraping/safety.py
apps/monitoring/scraping/transport.py
apps/monitoring/scraping/fetcher.py
apps/monitoring/scraping/robots.py
apps/monitoring/scraping/rates.py
apps/monitoring/scraping/normalization.py
apps/monitoring/scraping/extractors.py
tests/scraping/conftest.py
tests/scraping/test_command.py
tests/scraping/test_extraction.py
tests/scraping/test_fetcher.py
tests/scraping/test_robots.py
tests/scraping/test_safety.py
tests/scraping/test_rates.py
tests/scraping/test_service.py
tests/fixtures/scraping/ambiguous_offers.html
tests/fixtures/scraping/installment_price.html
tests/fixtures/scraping/json_ld_graph.html
tests/fixtures/scraping/json_ld_product.html
tests/fixtures/scraping/manual_product.html
tests/fixtures/scraping/metadata_product.html
tests/fixtures/scraping/missing_price.html
tests/fixtures/scraping/out_of_stock.html
docs/components/scraping.md
docs/verification-stage2.md
```

## Изменённые файлы

```text
config/settings/base.py
compose.yaml
.env.example
requirements/base.txt
requirements/constraints.txt
README.md
docs/architecture.md
docs/runbook.md
```

## Ограничения и состояние

- Разрешённые реальные сайты ещё не заданы. `SCRAPING_ALLOWED_HOSTS` по умолчанию
  пуст; удалённая загрузка запрещена. Подключение конкретных сайтов потребует
  явного добавления точных доменов и проверки их правил доступа.
- Нет JavaScript-рендеринга, входа в магазины, обхода CAPTCHA, персональных цен
  и универсального определения вариантов товара.
- Поддерживаются обычные однозначные цены и явный набор валют. Диапазоны,
  рассрочка и составное ценообразование не интерпретируются автоматически.
- Разбитые на несколько числовых DOM-узлов цены требуют точного селектора
  или атрибута `data-price`; ручной режим не подмешивает JSON-LD.
- Кеш robots локален процессу, ограничен 256 записями и максимум 24 часами;
  общий лимит домена сохраняется в PostgreSQL.
- DNS ожидание ограничено, но системный DNS-вызов принудительно не прерывается;
  число незавершённых операций ограничено. Время ожидания БД и CPU-разбора HTML
  не входит в сетевой срок 45 секунд.
- Проверены подменённые сетевые ответы и публичный API HTTPcore, а не живые
  сайты. Этот этап не подтверждает поддержку конкретного реального магазина.
- Удалённый GitHub CI не запускался. Контейнеры PriceWatch оставлены работающими;
  ресурсы других проектов не изменялись и очистка Docker не выполнялась.
