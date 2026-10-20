# Локальное сопровождение

Все команды выполняются из корня проекта. `.env` должен быть заполнен.

## Запуск и состояние

```powershell
docker info
docker compose config --quiet
docker compose up --build -d
docker compose ps -a
docker compose logs --tail=100 web worker beat migrate postgres redis
```

Проверки: http://127.0.0.1:8000/health/live/ и
http://127.0.0.1:8000/health/ready/. Первая проверяет процесс, вторая — доступ к БД.
При `WEB_PORT=8010` в локальном `.env` используйте 8010 вместо 8000.
Readiness не заменяет проверку наличия миграций: её обеспечивает сервис `migrate`.

## Аналитика цен

Карточка товара содержит график, фильтры 7/30/90/all, даты и валюту. Статистика
считается по выбранному периоду, верхняя карточка — последняя известная цена
за всё время. При смене валюты выберите одну явно. В списке можно отметить
2–5 товаров текущей страницы для сравнения одной валюты.

Наблюдения и технический журнал имеют отдельную пагинацию. Ошибка проверки
не создаёт нулевую цену; средняя учитывает успешные наблюдения с ценой.
Тесты, изолированная демонстрационная БД, файлы и отладка графика описаны
в `components/analytics.md`; результаты проверки — в `verification-stage5.md`.

## Уведомления

Правило задаётся в настройках товара; по умолчанию выключено. Inbox доступен
по `/notifications/`, личный email-флаг — `/notifications/settings/`.
Первое получение цены, одинаковые цены, смена валюты/URL и ошибки не уведомляют.
Порог требует пересечения `old >= threshold > new`, без конвертации валют.

В локальном запуске письма находятся в `docker compose logs worker`, реального
SMTP нет. Beat ставит до 20 писем в очередь раз в минуту. Сбой почты не меняет
CheckRun/Observation. Failed/unknown нельзя вслепую повторять из-за возможной
передачи письма SMTP. Подробности и тестовый браузерный сценарий:
`components/notifications.md`; результаты — `verification-stage6.md`.

## Пользователи

```powershell
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py changepassword USERNAME
```

Обычные пользователи создаются через `/admin/`. Права `staff` выдаются только тем,
кому нужен доступ к административной панели. Восстановление пароля через email
на этом этапе отсутствует.

## Миграции и тесты

```powershell
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python -m pytest --ds=config.settings.test --reuse-db --cov=apps --cov-report=term-missing
docker compose exec web python manage.py collectstatic --noinput
```

В контейнере явно указывайте `--ds=config.settings.test`, чтобы локальные
настройки из окружения не переопределили тестовый режим.

После будущего изменения моделей сначала создайте и проверьте миграцию,
затем примените её `docker compose run --rm migrate`. Не создавайте миграции
автоматически при запуске контейнера.

## Локальная проверка извлечения

```powershell
docker compose exec web python manage.py parse_product tests/fixtures/scraping/json_ld_product.html
docker compose exec web python manage.py parse_product tests/fixtures/scraping/manual_product.html --title-selector h1 --price-selector .price --currency RUB --availability-selector .availability
```

Это локальные файлы: запросы к магазинам не выполняются. Для нового HTML нужны
UTF-8 и размер до 2 МиБ. Подробности: `docs/components/scraping.md`.
Сетевой компонент требует миграций `monitoring` и списка точных
доменов `SCRAPING_ALLOWED_HOSTS`. Пустой список запрещает удалённую загрузку.
Ограничения сети нельзя отключать ради проверки локального файла.

## Очередь ручных проверок

```powershell
docker compose exec worker celery -A config inspect ping
docker compose logs --tail=100 worker redis
docker compose restart worker beat
```

Worker и Beat не перезапускаются Django autoreload. При изменении кода задач
перезапускайте их явно. Redis не публикует порт, хранит очередь в `redis_data`
с AOF; результаты проверок хранятся в PostgreSQL.

Застрявшее задание можно повторно отправить кнопкой на странице товара.
Используется тот же CheckRun, история не дублируется. Не меняйте статус через SQL.
При `queue_unavailable` проверьте Redis/worker; сетевые и parser-коды относятся
к безопасной загрузке этапа 2. Полное объяснение: `components/monitoring.md`.

Настоящая интеграция (последовательно, с сохраняемой тестовой БД):

```powershell
docker compose --profile integration stop integration-beat integration-web integration-worker fixture
docker compose exec web python -m pytest --ds=config.settings.test --reuse-db --create-db
docker compose --profile integration up -d --wait fixture integration-worker integration-beat
docker compose exec -e PRICEWATCH_INTEGRATION=1 -e PRICEWATCH_RUN_INTEGRATION=1 -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m pytest tests/integration --ds=config.settings.integration --reuse-db
```

Профиль использует PostgreSQL `test_pricewatch`, Redis DB 15 и отдельную
очередь. Контролируемый HTTP-сервер не имеет опубликованного порта. Его
тестовый TCP-адаптер загружается только явной командой integration-worker;
обычный worker сохраняет все ограничения URL/DNS/IP.

## Автоматическое расписание

В карточке товара выберите интервал, включите переключатель и сохраните.
Первая проверка становится доступной сразу, диспетчер просыпается раз в минуту.
Следующий срок, пауза и исходный слот проверки хранятся в PostgreSQL.

```powershell
docker compose logs --tail=100 beat worker
docker compose exec web python manage.py migrate --check
docker compose exec web python -m pytest tests/monitoring/test_scheduling.py tests/monitoring/test_schedule_views.py --ds=config.settings.test --reuse-db
```

Просрочка после простоя не создаёт историческую очередь: выполняется одна
актуальная проверка. Активная проверка блокирует создание параллельной.
При `queue_delivery_timeout` проверьте Redis/worker: исчерпаны три отправки
одного UUID. Сеть магазина всё равно ограничена robots и доменным лимитом.
UTC, правила смены интервала и команды реального перезапуска описаны в
`components/scheduling.md`.

Перед стандартными тестами останавливайте тестовые сервисы; тестовая БД
не должна одновременно использоваться браузерными сценариями:

```powershell
docker compose --profile integration stop integration-beat integration-web integration-worker fixture
```

## Доступ к PostgreSQL

```powershell
docker compose exec postgres psql -U pricewatch -d pricewatch
```

При изменённых `POSTGRES_USER`/`POSTGRES_DB` используйте свои имена.
База не публикует порт в Windows; подключение приложения идёт по сети Compose.
Данные сохраняются в `postgres_data` между перезапусками контейнеров.

## Остановка

```powershell
docker compose down
```

Обычная остановка сохраняет volume. Не добавляйте `--volumes`, если нужно
сохранить учётные записи и будущую историю наблюдений.

## Если Docker Desktop не стартует

Сначала проверьте работоспособность самого Docker (`docker info`), затем
Linux engine и WSL в Docker Desktop. Ошибка daemon возникает до выполнения
Compose. Не используйте factory reset и не удаляйте Docker volumes для диагностики.
Пока Docker недоступен, возможен запуск тестов на явно выбранном SQLite fallback
из README или на отдельно запущенном PostgreSQL.
