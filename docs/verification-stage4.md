# Проверка этапа 4 — расписание PriceWatch

Historical report: the commands below used the former shared test database.
Standard pytest now uses `test_pricewatch_pytest`; integration and demo setup
uses `test_pricewatch`. Follow [the current runbook](runbook.md) for new runs.

Проверено 20 октября 2026 года в Docker с настоящими PostgreSQL, Redis,
Celery prefork worker и Celery Beat. Обращений к реальным магазинам не было.

## Результаты автоматических проверок

- Полный стандартный набор: **409 passed, 18 skipped**, 24,59 с.
- Покрытие Python-кода приложений: **94%**.
- 18 пропущенных сценариев требуют живого тестового worker/Beat и запускаются отдельно.
- Настоящая интеграция: **18 passed**, 71,99 с; eager отключён.
- Проверены интервалы 1/3/6/12/24, пауза/возобновление, архив, отсутствие дублей,
  конкурентные диспетчеры, блокировки PostgreSQL, ограничение партии, восстановление
  публикации, ограничение количества доставок, несколько пользователей, UTC/DST.
- Повторены прежние тесты авторизации, ручных проверок и безопасности загрузки.
- Финальная сборка Docker успешна; основной PostgreSQL, Redis, Django,
  Celery worker и Beat имеют статус `healthy`.
- `manage.py check`, `migrate --check`, `makemigrations --check --dry-run`
  и `pip check` завершились без ошибок; применены миграции monitoring 0001–0004.
- После итогового пересоздания `/health/live/`, `/health/ready/` и
  `/accounts/login/` на `127.0.0.1:8010` вернули HTTP 200.

Команды выполненного прогона:

```powershell
docker compose exec web python -m pytest --ds=config.settings.test --reuse-db --cov=apps --cov-report=term-missing
docker compose --profile integration up -d --wait fixture integration-worker integration-beat
docker compose exec -e PRICEWATCH_INTEGRATION=1 -e PRICEWATCH_RUN_INTEGRATION=1 -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m pytest tests/integration --ds=config.settings.integration --reuse-db -q
```

Тестовые сервисы были остановлены перед стандартным прогоном. После браузерного
наполнения тестовой БД подготовка нового стенда использует `--create-db`;
последовательность приведена в `components/scheduling.md`.

## Реальный перезапуск

Через `tests.integration.restart_probe` создан отдельный тестовый Monitor с часовым
интервалом. Beat автоматически выполнил первую проверку. Затем остановлены тестовые
Django и worker; Beat некоторое время продолжал отправлять короткие пробуждения.
Следующая дата в тестовой БД переведена на семь дней в прошлое. После остановки
и запуска Beat, worker и Django произошла одна актуальная проверка.

| Показатель | Результат |
| --- | --- |
| CheckRun до перезапуска | 1 |
| CheckRun после восстановления | 2 |
| Observation после восстановления | 2 |
| HTTP-загрузок товара за весь сценарий | 2 |
| Перекрывающихся HTTP-запросов | 0 |
| Предыдущее наблюдение и цена | Сохранены |
| Последняя постановка UTC | 20.10.2026 14:51:40.313050 |
| Следующая проверка UTC | 20.10.2026 15:51:40.313050 |
| Разница | 3600 секунд |
| Часовой пояс соединения PostgreSQL | UTC |

В интерфейсе Asia/Yekaterinburg этим моментам соответствуют **19:51:40**
и **20:51:40**. После снятия показателей пробный Monitor поставлен на паузу.
Машинный результат сохранён в `artifacts/stage4-restart.json`.

## Браузер

В изолированном интерфейсе проверены:

1. Создание товара с выключенным расписанием по умолчанию.
2. Включение каждые 3 часа; Beat сам создал успешную проверку.
3. Отображение последней постановки и следующего времени через 3 часа.
4. Пауза: следующая дата исчезла, цена и история остались.
5. Возобновление с интервалом 24 часа: новая успешная проверка и дата следующего дня.
6. Понятное различение ручных и плановых проверок в истории и подпись часового пояса.

Снимок `artifacts/stage4-schedule.png` показывает две успешные автоматические
проверки, цену 1299,95 USD, включённый суточный интервал и следующую дату.

## Созданные файлы

```text
apps/monitoring/scheduling.py
apps/monitoring/migrations/0004_scheduled_monitoring.py
tests/monitoring/test_scheduling.py
tests/monitoring/test_schedule_views.py
tests/integration/test_schedule.py
tests/integration/restart_probe.py
docs/components/scheduling.md
docs/verification-stage4.md
```

## Изменённые файлы

```text
apps/monitoring/models.py
apps/monitoring/checks.py
apps/monitoring/tasks.py
apps/monitoring/forms.py
apps/monitoring/views.py
apps/monitoring/urls.py
config/settings/base.py
config/settings/integration.py
compose.yaml
Dockerfile
templates/monitoring/detail.html
templates/monitoring/list.html
templates/core/dashboard.html
static/css/app.css
tests/monitoring/test_tasks.py
README.md
docs/architecture.md
docs/runbook.md
docs/components/monitoring.md
```

Зависимости Python, авторизация и парсер не менялись. Миграция добавляет поля,
индекс и ограничения; существующие расписания по умолчанию выключены. Удаление
записей рабочей БД и очистка Docker не выполнялись.
Тестовые сервисы после проверки остановлены. HTTP fixture завершён штатным
SIGTERM (код 143); миграционный контейнер — с кодом 0. Compose предупреждает
о прежнем одноразовом `pricewatch-stage3-migrations` (Exited 0): он не используется
и не мешает запуску, не удалялся в рамках запрета на очистку Docker.
Посторонний `open-webui` остался работающим и healthy.

## Границы и ограничения

- Beat пробуждает диспетчер раз в минуту; дата означает доступность, а не точную
  гарантию начала сети. В тестовом профиле тик 2 секунды, интервалы товара те же.
- Не более 20 мониторингов за вызов; после простоя одна актуальная попытка на товар.
- Доставка сообщений допускает повторы при сбоях, но слот, активная попытка и
  наблюдение защищены ограничениями БД и advisory lock.
- Для незавершённой доставки предусмотрены 5 минут ожидания и максимум три
  автоматические отправки. Ошибка сайта ждёт следующего обычного интервала.
- Расчёт интервалов — UTC; личный выбор часового пояса не добавлен, интерфейс
  использует `TIME_ZONE=Asia/Yekaterinburg`.
- Прямые сессии PostgreSQL обязательны для advisory locks; transaction pooling не подходит.
- Список реальных разрешённых доменов по-прежнему настраивает администратор;
  защита SSRF, robots и ограничения частоты сохранены.
- Графики, уведомления, Excel и другие следующие этапы не реализованы.

Подробное объяснение потока данных, файлов, ошибок и команд отладки:
`docs/components/scheduling.md`.
