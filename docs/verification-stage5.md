# Проверка этапа 5 — 20 октября 2026

Historical report: standard pytest now uses `test_pricewatch_pytest`, while
integration tests and demos use `test_pricewatch`. Commands below record the
former shared setup. Use [the current runbook](runbook.md) for new runs.

## Что реализовано

- Локальный интерактивный Chart.js 4.5.1 в Django Templates, без React и Node.
- Периоды 7/30/90 дней и всё время; уточнение дат включительно в зоне Django.
- Минимум, максимум, средняя, последняя цена, абсолютное и процентное изменение.
- Денежные расчёты PostgreSQL numeric / Python Decimal; строки цен в JSON.
- Наблюдения с фильтрами и отдельной пагинацией; технический журнал CheckRun
  за всё время отдельно от цен, без превращения ошибок в нулевые значения.
- Сравнение 2–5 принадлежащих пользователю товаров в одной выбранной валюте.
- Понятные пустое/одиночное/неизменное состояния и возврат к исходной цене.
- Ограничение графика 400 точками на товар, пакетные SQL-запросы без N+1.

Модели, схема PostgreSQL, парсер, расписание, фоновые задачи и авторизация
не менялись. Новых таблиц и Python-зависимостей нет.

## Автоматические проверки

В Docker, Python 3.13.16 / Django 5.2.18 / PostgreSQL 17:

```text
459 passed, 18 skipped in 34.79s
```

18 пропусков — явно opt-in интеграционные тесты. Они выполнены отдельно с
настоящими PostgreSQL, Redis, prefork Celery worker и Celery Beat; eager не включён:

```text
18 passed in 70.94s
```

Всего **477 успешных тестов**, в том числе **50 новых тестовых случаев аналитики**.
Проверены Decimal, рост/снижение, цена 0 и неопределённый процент, пустая
история, одна цена, постоянная цена, колебания с нулевым итоговым изменением,
RUB/USD/EUR/JPY, смена валюты одного товара, out-of-stock без цены, ошибки,
детерминированный порядок при совпадении времени, периоды и будущие даты,
местная полночь и DST, неверные фильтры, пагинация, XSS, чужие/отсутствующие
мониторинги, ограничения сравнения и объёма графика.

Покрытие **Python-строк**, без миграций:

| Область | Покрытие |
| --- | --- |
| Все `apps` | **94,32%**, 1726 из 1830 строк |
| `apps/monitoring/analytics.py` | **100%**, 72 из 72 строк |
| `apps/monitoring/templatetags/monitoring.py` | **100%** |

HTML: `artifacts/stage5-coverage/index.html`; JSON: `artifacts/stage5-coverage.json`.
Это не покрытие JavaScript или всех ветвей: взаимодействие Chart.js проверено
в браузере отдельно. Не заявляется 100% покрытие всей функциональности.

На 10 003 наблюдениях подтверждены полная статистика, крайние цены, порядок,
не более 400 точек, 20 строк таблицы. Пакетный расчёт для одного и пяти товаров
делает одинаковое число SQL-запросов: 5 для маленькой истории, 9 для большой.
Это число относится к модулю аналитики; HTTP-страница дополнительно читает
сессию, владельца, валюты, верхние карточки и пагинацию.

Команды (основные сервисы уже запущены; только тестовая БД пересоздаётся):

```powershell
docker compose -p pricewatch -f compose.yaml --profile integration stop integration-web integration-beat integration-worker fixture
docker compose -p pricewatch -f compose.yaml exec -T web python -m pytest --ds=config.settings.test --reuse-db --create-db --cov=apps --cov-report=term-missing --cov-report=html:artifacts/stage5-coverage --cov-report=json:artifacts/stage5-coverage.json --tb=short -q
docker compose -p pricewatch -f compose.yaml --profile integration up -d --wait fixture integration-worker integration-beat
docker compose -p pricewatch -f compose.yaml exec -T -e PRICEWATCH_INTEGRATION=1 -e PRICEWATCH_RUN_INTEGRATION=1 -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m pytest tests/integration --ds=config.settings.integration --reuse-db -q --tb=short
```

Все страницы и сетевые сценарии — локальные контролируемые данные/HTTP fixture.
Реальные магазины не открывались. Для статических ресурсов скачаны только
официальный npm-дистрибутив Chart.js и MIT-лицензии.

## Проверка браузером

Использован изолированный `integration-web` на 8011, отдельная БД
`test_pricewatch`. Автоматические расписания демонстрационных товаров выключены.

Подтверждено:

- Вход тестового пользователя и переход из списка в карточку.
- Два ряда сравнения в RUB; скрытие/возврат ряда нажатием на легенду.
- Подсказка с исходной точной денежной строкой и местным временем.
- Смена 30 → 7 дней: у каждого Demo стало 13 валидных цен.
- Диапазон 15–17 октября включительно: по 6 валидных цен; для Demo One
  показано, что итоговое изменение 0, но внутри периода были колебания.
- Страница 2 из 3 наблюдений; график остаётся доступен.
- Пустая история, одна цена и неизменный товар имеют отдельные сообщения.
- RUB и USD не сравниваются; график отсутствует с объяснением причины.
- 10 003 наблюдения: **387 отобранных точек**, линия отключена, статистика
  использует все 10 003 значения. Временная ось ограничена данными;
  для единственного наблюдения показывается окно ±1 час.
- Canvas имеет `data-rendered=true`; console error/warn при проверках отсутствуют.
- Сохранённая цена Demo One 10 990 RUB остаётся после контролируемого timeout.

Снимки актуальной реализации:

![История цены Demo One](../artifacts/stage5-price-history.png)

![Сравнение двух мониторингов](../artifacts/stage5-comparison.png)

Дополнительный снимок большого графика: `artifacts/stage5-large-history.png`.
Генератор и воспроизводимые команды: [components/analytics.md](components/analytics.md).
После браузерной проверки итоговый pytest пересоздал только тестовую БД;
для повторного просмотра демонстрацию нужно создать заново.

В ходе разработки исправлены: отображение отсутствующей цены и отрицательных
сумм; обработка явно пустого выбора валюты; включение наблюдения ровно на
верхней временной границе; чрезмерно широкая временная ось одного наблюдения.
Генератор демонстрации теперь явно выбирает интеграционные настройки,
чтобы Compose `web` не подменял тестовый password hasher локальным.
Настройки авторизации самого приложения не менялись.

## Docker и сохранность данных

- `docker compose config --quiet`, Django system check — успешно.
- `makemigrations --check --dry-run`: No changes detected.
- `migrate --check`, `pip check`, `collectstatic` — успешно.
- Образ `pricewatch-django:local` собран; основные web/worker/beat/postgres/redis
  запущены с обновлённым образом и проходят healthcheck.
- Рабочий URL: `http://127.0.0.1:8010/`. Login, live/ready и оба локальных
  скрипта графика отвечают HTTP 200.
- Рабочая БД `pricewatch`: User/Monitor/CheckRun/Observation **0/0/0/0**,
  как перед этапом. Демонстрационные записи создавались только в тестовой БД.
- Тестовые сервисы после проверок остановлены; рабочие остаются включёнными.
- Очистка Docker, удаление посторонних контейнеров, образов, томов или данных
  рабочей БД не выполнялись. `open-webui` не затрагивался.
- Осталось прежнее предупреждение Compose об одноразовом
  `pricewatch-stage3-migrations` (Exited 0): работе не мешает, не удалялся.

Отдельно учтено временное ограничение пользователя из-за сбоев Codex Desktop:
в дальнейших браузерных проверках не вызывать `.close()` автоматически.
Код PriceWatch ради обхода этой проблемы не менялся.

## Созданные файлы

```text
apps/monitoring/analytics.py
templates/monitoring/_analytics.html
templates/monitoring/_chart_scripts.html
templates/monitoring/_history_pagination.html
templates/monitoring/compare.html
static/js/price-history.js
static/vendor/chartjs/chart.umd.min.js
static/vendor/chartjs/LICENSE.md
static/vendor/chartjs/LICENSE-color.md
static/vendor/chartjs/VERSION
static/vendor/chartjs/README.md
tests/monitoring/test_analytics.py
tests/integration/analytics_demo.py
docs/components/analytics.md
docs/verification-stage5.md
```

## Изменённые файлы

```text
apps/monitoring/forms.py
apps/monitoring/views.py
apps/monitoring/urls.py
apps/monitoring/templatetags/monitoring.py
templates/base.html
templates/monitoring/detail.html
templates/monitoring/list.html
static/css/app.css
README.md
docs/architecture.md
docs/runbook.md
```

Снимки и отчёты покрытия в `artifacts/` намеренно исключены из Git/образа.

## Ограничения

График большой истории — выборка, может пропускать локальные колебания;
глобальные крайние цены сохранены, статистика полная. Сравнение ограничено
пятью товарами текущей страницы. Персональные временные зоны и конвертация
не добавлены. Средняя не взвешена по времени.

Объём приложения ограничен, но SQL-агрегаты и оконные функции обрабатывают
все подходящие строки. На миллионах наблюдений «Всё время», COUNT и глубокий
OFFSET потребуют измерения EXPLAIN ANALYZE и возможных индексов/агрегатов.
Кеш и дополнительные таблицы заранее не введены. Координаты Chart.js используют
точность Number; очень близкие большие цены могут визуально совпасть, хотя
таблица, tooltip и серверные расчёты сохраняют исходные значения.

Уведомления, импорт/экспорт Excel и другие следующие этапы не реализованы.
