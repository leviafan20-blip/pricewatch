# Этап 4: автоматические проверки по расписанию

## За что отвечает компонент

Компонент хранит интервал в PostgreSQL и создаёт актуальные проверки без
участия браузера. Celery Beat содержит **одну** периодическую задачу: раз в
60 секунд разбудить диспетчер. Отдельных записей Beat для каждого товара,
django-celery-beat и новых зависимостей не требуется.

Настройки товаров и время следующего запуска — источник истины в БД.
Файл Beat хранит только состояние общего таймера; его потеря не удаляет
пользовательские расписания. В Compose запускается один Beat. Даже случайные
повторные вызовы диспетчера защищены PostgreSQL, а не локальной памятью процесса.
Свойства Beat описаны в [официальной документации Celery](https://docs.celeryq.dev/en/main/userguide/periodic-tasks.html).

## Основные файлы

| Файл | Ответственность |
| --- | --- |
| `apps/monitoring/models.py` | Поля расписания Monitor, доставка CheckRun, ограничения БД |
| `apps/monitoring/migrations/0004_scheduled_monitoring.py` | Добавочная миграция без удаления истории |
| `apps/monitoring/scheduling.py` | Включение/пауза, выбор просроченных мониторингов, восстановление публикации |
| `apps/monitoring/checks.py` | Общий жизненный цикл ручных/плановых проверок и публикация |
| `apps/monitoring/tasks.py` | Celery-задача диспетчера, прежняя задача выполнения |
| `config/settings/base.py` | Таймер Beat, UTC и срок годности пробуждений |
| `apps/monitoring/forms.py`, `views.py`, `urls.py` | Форма расписания и POST с проверкой владельца |
| `templates/monitoring/detail.html`, `list.html` | Интервал, состояние, последние/следующие даты, источник проверки |
| `compose.yaml`, `Dockerfile` | Отдельные Beat-процессы, тома состояния и непривилегированный запуск |
| `tests/monitoring/test_scheduling.py`, `test_schedule_views.py` | Блокировки, состояния, права, UTC и форма |
| `tests/integration/test_schedule.py` | Настоящие Beat, Redis, prefork worker, PostgreSQL и HTTP fixture |
| `tests/integration/restart_probe.py` | Воспроизводимая проверка перезапуска компонентов |

## Что сохраняется в PostgreSQL

У Monitor добавлены:

- `auto_enabled` — разрешены ли автоматические проверки; по умолчанию `False`;
- `check_interval_hours` — только 1, 3, 6, 12 или 24; по умолчанию 6;
- `next_check_at` — ближайшее время доступности плановой проверки;
- `last_scheduled_at` — время последнего создания планового CheckRun.

Для выключенного или архивного Monitor `next_check_at` равен NULL.
Ограничения БД не допускают включённое расписание без даты либо у архивного
товара. Миграция оставляет существующие мониторинги выключенными.

CheckRun получает `scheduled_for`, `next_delivery_at`, `delivery_attempts`.
`scheduled_for` фиксирует исходный слот расписания, а NULL означает ручную
проверку. Он неизменяем через обычный `save()`, как копии URL/селекторов.
Уникальность `(monitor, scheduled_for)` защищает один слот от повторного
создания даже после завершения попытки. Существующее ограничение одного
`queued/running` на Monitor продолжает действовать.

`next_delivery_at` and `delivery_attempts` apply to both manual and scheduled
attempts. `execution_attempts` counts committed worker claims separately.
Terminal attempts clear their delivery date. Observation has no new fields
and the dispatcher does not alter observation history.

## Как взаимодействуют процессы

1. **Beat** отправляет короткое пробуждение `monitoring.dispatch_due_checks`
   в **Redis**. Его срок годности — 55 секунд при интервале 60 секунд.
2. **Worker** runs the dispatcher. It selects enabled schedules that are due,
   plus expired delivery reservations, including manual checks with scheduling
   disabled. Archived monitors, inactive owners, and paused scheduled attempts
   cannot restart; orphaned work is cancelled once its execution lock is free.
3. В короткой транзакции диспетчер берёт до 20 строк Monitor через
   `SELECT ... FOR UPDATE OF monitor SKIP LOCKED`. Уже занятые строки
   пропускаются; параллельный диспетчер не создаёт второй CheckRun.
4. Диспетчер использует общий `_get_or_create_run_locked()`, сохраняет копии
   URL/селекторов и резервирует доставку. Транзакция заканчивается **до Redis**.
5. Через прежний `publish_check()` UUID CheckRun отправляется в очередь.
6. Worker выполняет прежний `execute_check()`. Advisory lock Monitor
   исключает параллельную загрузку, затем вызывается `inspect_url()`.
7. PostgreSQL получает Observation и успешный CheckRun либо только ошибку
   CheckRun. Парсер, DNS/IP/TLS-защита, robots и доменные лимиты не дублируются.

Пропуск заблокированных строк выбран именно для очереди работы, а не для
пользовательских отчётов: см. [PostgreSQL SELECT](https://www.postgresql.org/docs/current/sql-select.html).
Сеть магазина не выполняется внутри транзакции. Advisory locks worker
по-прежнему требуют прямого соединения PostgreSQL или session pooling,
а не PgBouncer transaction pooling.

## Правила времени, паузы и простоя

При включении или возобновлении `next_check_at` становится текущим временем.
Первое задание создаст ближайший доступный цикл диспетчера. Повторное сохранение
тех же настроек не сдвигает срок. Изменение интервала включённого Monitor
переносит следующую проверку на `сейчас + новый интервал`.

После постановки следующая дата рассчитывается как `сейчас + интервал`,
независимо от успеха магазина. Расписание не привязано к времени последней
успешной цены. Сеть/парсинг с ошибкой не вызывают немедленных HTTP-повторов:
следующая попытка будет в очередном интервале.

После простоя пропущенные интервалы **сворачиваются в одну актуальную попытку**.
Например, после недели простоя при часовом интервале создаётся один CheckRun,
а не 168. В `scheduled_for` остаётся исходный просроченный слот; время
постановки и `last_scheduled_at` отражают фактическое восстановление.
Если ручная или плановая проверка уже активна, новый CheckRun не создаётся,
а наступивший цикл объединяется с активной работой.

Пауза очищает следующую дату и отменяет ожидающие **автоматические** задания.
Ручные проверки остаются доступны. Архив отключает расписание и отменяет
все ожидающие проверки, как на этапе 3. Уже начавшаяся проверка может
завершиться. Worker повторно проверяет выключение/архив до начала сети,
поэтому поздно пришедшее сообщение не обходит паузу.

`USE_TZ=True`, aware datetime и PostgreSQL `timestamptz` обеспечивают хранение
моментов в UTC. Celery также явно настроен на UTC. Интервалы означают прошедшие
часы, а не местное календарное время, поэтому переходы DST не меняют длительность.
Шаблоны показывают время Django `TIME_ZONE` (`Asia/Yekaterinburg`) и подписывают
пояс. На этом этапе нет персональной настройки часового пояса аккаунта.

## Восстановление публикации и границы гарантий

Общей атомарной транзакции PostgreSQL + Redis нет. Поэтому обещать физическую
доставку сообщения ровно один раз было бы неверно. Гарантируется одно логическое
задание на слот и одно Observation на CheckRun; повторная доставка безопасна.

Перед публикацией CheckRun получает резерв `next_delivery_at = сейчас + 5 минут`.
Другой диспетчер или ручной POST не публикует его повторно в течение резерва.
Если процесс завершился между фиксацией БД и Redis, будущий цикл повторит
публикацию **того же UUID**. Это работает и после перезапуска Django/worker/Beat.

При наличии живой advisory lock восстановление не вмешивается в работу.
Для потерянных публикаций предусмотрено максимум три автоматические отправки
одной попытки. После исчерпания получается `failed / queue_delivery_timeout`,
а не бесконечное накопление копий. Явная ошибка брокера сохраняется как
`queue_unavailable`, не переписывая уже начавшуюся/успешную проверку.
Прежние Observation при этом не затрагиваются.

This publication limit includes the initial reservation, for both manual and
scheduled runs; it is not three additional retries. Broker redeliveries do not
increase `delivery_attempts`. Instead, each committed worker claim increments
`execution_attempts` before network I/O, with a separate maximum of three.
After three interrupted claims, the next delivery or dispatcher recovery marks
`execution_attempts_exhausted` without another fetch. The third execution can
still finish successfully. Both limits clear delivery reservations on failure
and preserve existing observations and notifications.

Просроченные пробуждения Beat отбрасываются Celery по `expires`. Redis может
временно хранить эти короткие сообщения до чтения worker; это не серия
пропущенных товарных проверок. За один вызов диспетчер обрабатывает не более
20 мониторингов. Точное время старта зависит от нагрузки, robots и лимитов сайта.

## Проверка и отладка

```powershell
docker compose up --build -d --wait
docker compose ps -a
docker compose logs --tail=100 beat worker redis
docker compose exec web python manage.py migrate --check
docker compose exec web python -m pytest --ds=config.settings.test --reuse-db --cov=apps --cov-report=term-missing
```

После изменения Python-кода задач или настроек таймера перезапустите worker
и Beat; autoreload Django не перезапускает их:

```powershell
docker compose restart worker beat
```

При отсутствии запусков проверьте последовательно: `auto_enabled`, архив,
активность владельца, `next_check_at`, журнал Beat, доступность Redis, журнал
диспетчера и состояние активного CheckRun. Логи `Schedule dispatch` показывают
создание, публикацию, восстановление, объединение цикла и занятость. Полные
URL и HTML туда не записываются. Не исправляйте даты/статусы через SQL в рабочей БД.

`queue_delivery_timeout` требует проверки очереди и worker; старую попытку
не нужно превращать в успешную вручную. Обычные ошибки `timeout`,
`robots_denied`, `missing_price` и т. п. диагностируются как на этапе 2.

Настоящий интеграционный прогон, **последовательно**, до браузерной проверки:

```powershell
docker compose --profile integration stop integration-beat integration-web integration-worker fixture
docker compose exec -e PRICEWATCH_INTEGRATION=1 -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m pytest tests/test_health.py::test_readiness_reports_database_available --ds=config.settings.integration --reuse-db
docker compose --profile integration up -d --wait fixture integration-worker integration-beat
docker compose exec -e PRICEWATCH_INTEGRATION=1 -e PRICEWATCH_RUN_INTEGRATION=1 -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m pytest tests/integration --ds=config.settings.integration --reuse-db
```

Standard pytest uses `test_pricewatch_pytest`. Integration tests and browser
demos share `test_pricewatch`, never the application database. Stop integration
services before the readiness preparation above; it creates or migrates that
database without recreating it or clearing demo rows. The full integration suite
does clear its records, so do not run it while preserving a browser demo. Always
use `--reuse-db` with the integration worker and do not run two suites against
the same database at once.

Профиль использует Redis DB 15, отдельную очередь и контролируемый HTTP-сервер.
Тестовый Beat тикает раз в 2 секунды, но интервалы Monitor остаются часовыми.
Транспорт магазина подменяется только явным тестовым worker-адаптером;
обычные web/worker не получают разрешения внутренних адресов.

Реальный перезапуск можно повторить **после** тестов:

```powershell
docker compose --profile integration up -d --wait integration-web
docker compose exec integration-web python -m tests.integration.restart_probe prepare
docker compose exec integration-web python -m tests.integration.restart_probe wait-first
docker compose --profile integration stop integration-web integration-worker
docker compose exec -e DJANGO_SETTINGS_MODULE=config.settings.integration -e PRICEWATCH_INTEGRATION=1 -e POSTGRES_DB=test_pricewatch -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m tests.integration.restart_probe overdue
Start-Sleep -Seconds 6
docker compose --profile integration stop integration-beat
docker compose --profile integration up -d --wait integration-worker integration-beat integration-web
docker compose exec integration-web python -m tests.integration.restart_probe verify
```

Результат сохраняется в `artifacts/stage4-restart.json`. Скрипт проверяет имя
тестовой БД/настройки и отказывается работать с рабочей БД. Он сохраняет прошлое
наблюдение, проверяет ровно одну новую попытку и ставит свой тестовый Monitor
на паузу. Контрольный HTTP-сервер остаётся единственным источником товара.

После проверки остановите только тестовые сервисы:

```powershell
docker compose --profile integration stop integration-beat integration-web integration-worker fixture
```

Для публичного развёртывания остаются отдельные эксплуатационные задачи:
резервные копии, мониторинг процессов и подбор количества worker. Уведомления,
графики и экспорт не добавлялись.
