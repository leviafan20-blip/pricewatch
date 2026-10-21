# Итоговая проверка этапа 6 — 20 октября 2026

Historical report: standard pytest now uses `test_pricewatch_pytest`, while
integration tests and demos use `test_pricewatch`. Commands below record the
former shared setup. Use [the current runbook](runbook.md) for new runs.

## Результат

Этап 6 завершён: правила уведомлений каждого мониторинга, личные настройки
email, внутренний inbox, прочитанные/непрочитанные события и счётчик в шапке.
Реализация предыдущего задания сохранена; при продолжении завершены браузерная
проверка, проверка последних изменений, перезапуск сервисов и этот отчёт.
Этап 7 не начат.

Действуют четыре режима: выключено, любое изменение, снижение, пересечение
порога `old >= threshold > new` в заданной валюте. Первое получение цены,
одинаковые цены, смена URL/валюты, отсутствие цены и ошибки проверки не создают
ложных уведомлений. Расчёты остаются Decimal; старые и новые цены берутся из
существующих Observation, а не копируются в новую историю.

Устройство компонента, файлы и диагностика подробно описаны в
[components/notifications.md](components/notifications.md).

## Автоматические проверки

Последний полный прогон актуального кода в Docker с PostgreSQL:

```text
503 passed, 21 skipped in 50.35s
```

Число выросло с прежних 502 до 503 из-за дополнительного теста срока жизни
почтового задания (55 секунд). Изменения также проверяют фиксацию отдельной
транзакции до обращения к Redis/почте. Пропуски — явно включаемые интеграционные
тесты; они выполняются отдельно с PostgreSQL, Redis, prefork worker и Beat,
без eager-режима:

```text
21 passed in 78.99s
```

Всего **524 успешных теста**: 503 основных и 21 интеграционный.
Этап добавил 44 основных тестовых случая и 3 интеграционных.

Проверены правила, точные суммы и проценты, нулевая исходная цена, одинаковые
значения, повторные пересечения порога, ошибки и отсутствие цены, разные
валюты, смена URL, идемпотентность, конкурентная отправка, восстановление
публикации после ошибки брокера, unknown после прерывания отправителя,
отключение email/правила и архив, права, CSRF, экранирование и пагинация без N+1.

В интеграции реально выполнены:

- HTTPX/JSON-LD проверка контролируемой страницы прежним worker.
- Beat → Redis → email-задача → стандартный файловый backend Django.
- Повтор двух email-задач и повтор CheckRun: одно письмо, одно событие.
- Ошибка адреса в отправителе: CheckRun остаётся succeeded, Observation сохранён.
- Два пересечения порога и отсутствие повторов на каждой цене ниже порога.
- Все прежние сценарии ручного и автоматического мониторинга.

Покрытие **Python-строк** (`apps`, без миграций):

| Область | Покрытие |
| --- | --- |
| Все приложения | **94,94%**, 1950 из 2054 строк (pytest округляет до 95%) |
| `apps/notifications/` | **100% строк** |
| Общий `monitoring/money.py` | **100% строк** |
| Аналитика `monitoring/analytics.py` | **100% строк** |

Это не покрытие всех ветвей и не инструментальное покрытие JavaScript.
HTML: `artifacts/stage6-coverage/index.html`;
JSON: `artifacts/stage6-coverage.json`;
JUnit: `artifacts/stage6-unit-tests.xml`, `artifacts/stage6-integration-tests.xml`.

Основная команда (перед ней остановлен только тестовый профиль):

```powershell
docker compose -p pricewatch -f compose.yaml --profile integration stop integration-web integration-beat integration-worker fixture
docker compose -p pricewatch -f compose.yaml exec -T web python -m pytest --ds=config.settings.test --reuse-db --create-db --cov=apps --cov-report=term-missing --cov-report=html:artifacts/stage6-coverage --cov-report=json:artifacts/stage6-coverage.json --junitxml=artifacts/stage6-unit-tests.xml -q --tb=short
docker compose -p pricewatch -f compose.yaml --profile integration up -d --wait fixture integration-worker integration-beat
docker compose -p pricewatch -f compose.yaml exec -T -e PRICEWATCH_INTEGRATION=1 -e PRICEWATCH_RUN_INTEGRATION=1 -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m pytest tests/integration --ds=config.settings.integration --reuse-db --junitxml=artifacts/stage6-integration-tests.xml -q --tb=short
```

`--create-db` относится только к `test_pricewatch`. Рабочая БД не очищается.
Настоящие магазины и внешние SMTP-серверы не использовались.

## Браузерная проверка

Продолжена в уже открытой вкладке и в изолированном `integration-web:8011`
с пользователем `stage6_demo`. Обновлена истёкшая браузерная сессия, затем:

1. Проверены сохранённые настройки товара: threshold, 1000 USD. Повторное
   сохранение формы завершилось сообщением успеха и не создало событие.
2. Через кнопку «Проверить сейчас» запрошена контролируемая страница с ценой
   1299,95 USD. При росте из области ниже порога уведомления нет, счётчик 0.
3. Только в Redis DB 15 изменена цена этой тестовой страницы на 999,95 USD.
   Следующая ручная проверка создала шестое событие: **−300 USD / −23,08%**,
   счётчик **0 → 1**. Письмо принято файловым backend.
4. Фильтр непрочитанных показал ровно одно событие. После прочтения счётчик
   **1 → 0**, показано «Непрочитанных уведомлений нет». История сохранена.
   Действие «Прочитать все» также проверено в предыдущем браузерном проходе.
5. Email выключен, сохранён, страница перезагружена: флаг остался выключенным.
   Затем адрес и включение сохранены и снова перезагружены. PostgreSQL
   подтвердил email_enabled=True и совпадение адреса с тестовым ожидаемым.
   На тот момент у пользователя было 6 событий, 0 непрочитанных, 6 sent.
6. Переход из уведомления открывает принадлежащий пользователю мониторинг.
   Старые и новые цены, знак и процент отображаются корректно.
7. Console error/warn отсутствуют; в свежих журналах web/worker/Beat не найдено
   Django/Celery ERROR, traceback или незарегистрированных задач.

Свежий снимок созданного уведомления:

![Новое пороговое уведомление и счётчик](../artifacts/stage6-notifications-final.png)

Дополнительные снимки:
`artifacts/stage6-email-settings-final.png`,
`artifacts/stage6-notifications.png`,
`artifacts/stage6-email-settings.png`,
`artifacts/stage6-monitor-rule.png`.

После браузера полный pytest пересоздал только тестовую БД, поэтому для нового
ручного просмотра демонстрацию нужно создать заново. Вкладки не закрывались
через `.close()`: проверенная вкладка оставлена на рабочем PriceWatch:8010 и
помечена для сохранения после завершения задания.

## Django, Docker, миграции и Git

- `docker compose config --quiet`, `manage.py check` — успешно.
- `makemigrations --check --dry-run`: No changes detected.
- `migrate --check` — успешно; применены:
  `accounts/0002_user_email_notifications_enabled`,
  `monitoring/0005_monitor_notification_currency_and_more`,
  `notifications/0001_initial`.
- `pip check` — No broken requirements found. Python-зависимости не добавлялись.
- Актуальный Docker-образ собран. Основные web/worker/Beat перезапущены,
  чтобы загрузить новые модели и Celery-задачи (healthcheck старого worker
  сам по себе не подтверждал наличие новой функциональности).
- Основные web/worker/Beat/PostgreSQL/Redis проходят healthcheck. Рабочий
  адрес — `http://127.0.0.1:8010/`; login и health endpoints доступны.
- Через Celery inspect подтверждена регистрация `notifications.send_email`
  и `notifications.dispatch_emails` в основном worker. Рабочая БД после
  проверок: User/Monitor/CheckRun/Observation/Notification = **0/0/0/0/0**,
  исходные записи рабочего окружения не изменялись.
- Тестовый профиль после итоговых проверок остановлен; рабочие сервисы оставлены.
- Записи рабочей БД не удалялись; демонстрационные записи находились только
  в `test_pricewatch`. Docker cleanup/prune, удаление посторонних контейнеров,
  образов и томов не выполнялись.
- Сохранилось прежнее предупреждение об одноразовом
  `pricewatch-stage3-migrations` (Exited 0): запуску не мешает, не удалялся.
- **Git не инициализирован**: в `D:\Django project` и родительском каталоге
  репозитория нет; `git status --short` сообщает `not a git repository`.
  Diff, staged/unstaged и коммиты проверить невозможно. Новый репозиторий,
  коммиты и push в рамках завершения этапа не создавались.

## Что было исправлено при реализации

- Сохранение email-настроек использует `get_user_model()`, а не тип ленивого
  `request.user`; адрес ограничен длиной поля User.email.
- Интеграционные письма читаются BytesParser, чтобы не повреждать UTF-8/8bit.
- В тесте независимости пользователей запросы к одному тестовому домену
  разведены во времени: прежняя одновременность могла правомерно дать
  rate_limited. Сам механизм URL/robots/ограничений частоты не ослаблялся.
- Последние ограничения времени жизни сообщений и самостоятельных транзакций
  проверены итоговым прогоном актуальных файлов.

При завершении новых дефектов приложения в браузере не обнаружено.

## Ручная проверка без магазинов и внешней почты

После тестов, при существующей `test_pricewatch`:

```powershell
docker compose -p pricewatch -f compose.yaml --profile integration up -d --wait fixture integration-worker integration-beat
docker compose -p pricewatch -f compose.yaml exec -T -e PRICEWATCH_INTEGRATION=1 -e POSTGRES_DB=test_pricewatch -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m tests.integration.notifications_demo
docker compose -p pricewatch -f compose.yaml --profile integration up -d --wait integration-web
```

Откройте `http://127.0.0.1:8011/accounts/login/`, войдите как `stage6_demo`
с паролем `PriceWatch-demo-6!`. Генератор откажется перезаписывать существующего
пользователя; при повторном просмотре используйте уже созданные записи.

Проверьте пять начальных событий, прочтение и фильтр. В настройках email
переключите флаг, сохраните и перезагрузите страницу. В форме кофемашины
проверьте порог 1000 USD. Для нового события сначала запустите ручную проверку
с обычной ценой fixture 1299,95, затем скопируйте 32-символьный token из URL
тестового товара `/products/TOKEN/success` и выполните:

```powershell
$priceWatchDemoToken = 'TOKEN_ИЗ_URL_ТЕСТОВОГО_ТОВАРА'
docker compose -p pricewatch -f compose.yaml exec -T redis redis-cli -n 15 SET "pricewatch:integration:${priceWatchDemoToken}:price" 999.95 EX 600
```

Снова нажмите «Проверить сейчас» и обновите страницу: новое событие, +1 к
непрочитанным, затем −1 после прочтения. Письма тестового профиля находятся
в `artifacts/stage6-mail/`; console backend обычного локального запуска —
в логах worker. Это тестовые письма, внешняя доставка не выполняется.

Остановка только тестовых сервисов:

```powershell
docker compose -p pricewatch -f compose.yaml --profile integration stop integration-web integration-beat integration-worker fixture
```

Не закрывайте вкладки инструментом агента: временное ограничение Codex Desktop
сохранено, код приложения для его обхода не менялся.

## Ограничения

- После начала SMTP-отправки failed/unknown не повторяются автоматически:
  иначе возможны дубликаты. При аварии письмо может не уйти, но внутреннее
  уведомление и история остаются в PostgreSQL.
- Локально внешняя почта не настроена. Для настоящей доставки нужен обычный
  SMTP в настройках развёртывания с секретами из окружения.
- Обычно email ожидает следующего тика Beat; до 20 сообщений за минутный
  цикл, общие worker с проверками. При росте нагрузки нужна оценка задержек.
- Счётчик обновляется при переходе/перезагрузке, без WebSocket и постоянного polling.
- Валюты не конвертируются, первый результат другой валюты/URL — новая база.
- Старые события автоматически не удаляются; глубокая OFFSET-пагинация
  потребует измерения при большой истории.

## Созданные файлы этапа

```text
apps/accounts/migrations/0002_user_email_notifications_enabled.py
apps/monitoring/migrations/0005_monitor_notification_currency_and_more.py
apps/monitoring/money.py
apps/notifications/__init__.py
apps/notifications/apps.py
apps/notifications/context_processors.py
apps/notifications/forms.py
apps/notifications/models.py
apps/notifications/services.py
apps/notifications/tasks.py
apps/notifications/urls.py
apps/notifications/views.py
apps/notifications/migrations/__init__.py
apps/notifications/migrations/0001_initial.py
templates/notifications/email.txt
templates/notifications/list.html
templates/notifications/settings.html
tests/notifications/__init__.py
tests/notifications/conftest.py
tests/notifications/test_notifications.py
tests/notifications/test_views.py
tests/integration/test_notifications.py
tests/integration/notifications_demo.py
docs/components/notifications.md
docs/verification-stage6.md
```

## Изменённые файлы этапа

```text
apps/accounts/models.py
apps/monitoring/analytics.py
apps/monitoring/checks.py
apps/monitoring/forms.py
apps/monitoring/models.py
apps/monitoring/views.py
config/settings/base.py
config/settings/integration.py
config/urls.py
compose.yaml
templates/base.html
templates/monitoring/form.html
static/css/app.css
tests/integration/http_server.py
tests/integration/test_schedule.py
README.md
docs/architecture.md
docs/runbook.md
```

Это перечень по выполненным изменениям; Git diff отсутствует из-за отсутствия
репозитория. Скриншоты, файлы писем и отчёты тестов/покрытия находятся в
игнорируемом `artifacts/` и не включаются в Docker-образ.
