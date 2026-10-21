# Этап 6: уведомления об изменении цены

## Назначение и основные файлы

Уведомления используют уже сохранённые Observation и прежний жизненный цикл
CheckRun. Новая таблица Notification хранит событие и состояние почты; копий
истории цен, универсального транспорта, сигналов и стороннего scheduler нет.

| Файл | Ответственность |
| --- | --- |
| `apps/monitoring/models.py` | Режим и порог мониторинга |
| `apps/accounts/models.py` | Личное разрешение на email, по умолчанию False |
| `apps/monitoring/checks.py` | Проверка настроек и создание события при успешном завершении |
| `apps/monitoring/money.py` | Общий Decimal-расчёт изменения для аналитики и уведомлений |
| `apps/monitoring/analytics.py` | Использование общего расчёта вместо копирования формулы |
| `apps/notifications/models.py` | Notification, уникальность события, прочтение, индексы и email-состояния |
| `apps/notifications/services.py` | Правила, резервирование доставки и отправка через Django |
| `apps/notifications/tasks.py` | Две короткие Celery-обёртки: диспетчер и отправитель |
| `apps/notifications/forms.py`, `views.py`, `urls.py` | Личные настройки, inbox и POST прочтения |
| `apps/notifications/context_processors.py` | Один COUNT непрочитанных для шапки |
| `templates/notifications/` | Список, настройки email, текстовое письмо |
| `templates/monitoring/form.html` | Настройки уведомлений конкретного товара |
| `config/settings/base.py`, `integration.py`, `compose.yaml` | Beat, безопасные backend и доверенный URL приложения |
| `tests/notifications/` | PostgreSQL, правила, конкурентность, ошибки почты и доступ |
| `tests/integration/test_notifications.py` | Настоящие worker/Beat/Redis и файловая почта |
| `tests/integration/notifications_demo.py` | Изолированные данные для проверки браузером |

Схема меняется тремя добавочными миграциями:
`accounts/0002_user_email_notifications_enabled`,
`monitoring/0005_monitor_notification_currency_and_more`,
`notifications/0001_initial`. Существующие пользователи и мониторинги получают
выключенные настройки; наблюдения и расписания сохраняются.

## Правила и процесс обработки

1. Пользователь выбирает в форме товара off / any / decrease / threshold.
   Для threshold нужны неотрицательный Decimal и явная валюта. В остальных
   режимах порог и его валюта очищаются. Есть ограничения формы, сервиса и БД.
2. Ручная или плановая проверка выполняется прежним Celery worker через
   `inspect_url()`. Сеть по-прежнему находится вне транзакций, URL/robots/лимиты
   не обходятся. При ошибке проверки Notification не создаётся.
3. `_finish_run()` блокирует Monitor и CheckRun, сохраняет валидное Observation
   и явно вызывает `create_price_notification()`. Внутреннее событие и
   наблюдение фиксируются одной транзакцией. Сеть SMTP/Redis здесь не вызывается.
4. Берётся последняя предыдущая цена этого мониторинга (`price IS NOT NULL`),
   порядок `(observed_at, pk)`. Отсутствие товара без цены не равно нулю и
   пропускается. Первая цена — базовая точка. Если предыдущая валюта или URL
   отличаются, новая цена становится базовой точкой. Не выбирается более
   старая цена нужной валюты через промежуточную смену валюты.
   Устаревший результат для уже изменённого URL и архивный мониторинг не
   создают уведомление. Одинаковые цены не создают событие.
5. Режим any принимает рост и падение; decrease — только `new < old`;
   threshold — `old >= threshold > new`, только в валюте порога. Проверки,
   пока цена остаётся ниже порога, не повторяют событие. После возврата выше
   следующий переход вниз снова срабатывает.
6. Notification ссылается на старое и новое Observation через PROTECT; цены
   не копируются. OneToOne нового наблюдения исключает второе событие.
   Существующие блокировки исполнения CheckRun исключают повторное создание
   Observation при дубликате Celery-задачи. `rule` сохраняет режим события,
   `owner` — владельца для эффективного inbox. Через приложение история цен
   не редактируется; исправление прошлых цен потребовало бы отдельного решения.
7. Абсолютное изменение — `new - old`, процент — `(new - old) / old * 100`.
   Оба вычисляются общим `money.price_change()` с Decimal и точностью 60.
   При старой цене 0 процент не определён. Суммы выводятся точно; процент
   округляется до двух знаков только при показе. Валюты не конвертируются.

## Как отправляется email

Личный переключатель действует для всех своих товаров; правило товара
определяет значимость события. Старые события при включении email не рассылаются.

При создании события email получает `pending` только если пользователь включил
почту и указал адрес; иначе `off`. Сама Notification служит небольшой outbox.
Один дополнительный тик Beat раз в 60 секунд ставит до 20 писем в очередь.
В интеграционном профиле тик — 2 секунды, бизнес-правила те же.

Диспетчер резервирует строки через `FOR UPDATE SKIP LOCKED` и отодвигает
`next_email_at` на 60 секунд. Резервирование фиксируется до публикации в Redis.
При недоступном брокере запись остаётся pending: следующий цикл повторяет
публикацию того же ID. Очередное сообщение живёт 55 секунд; просроченные
доставки worker отбрасывает. Нельзя считать Celery task_id механизмом
дедупликации: защиту даёт состояние в PostgreSQL.

Отправитель в короткой самостоятельной транзакции блокирует Notification,
проверяет текущие настройки/адрес/активность пользователя и состояние товара.
Пауза email, архив или выключение правила отменяют ожидающее письмо.
Затем фиксирует `sending`, закрывает транзакцию и вызывает стандартный
`EmailMessage.send()`. Дубликат задачи видит состояние, отличное от pending,
и ничего не отправляет. Тайм-аут Django — 10 секунд, Celery — 25/30 секунд.

После успеха сохраняются `sent` и время: это принятие backend, а не гарантия
доставки во входящие. После исключения или результата 0 — `failed` с безопасным
кодом `email_send_failed`. Содержимое исключения, пароль SMTP и адреса не пишутся
в диагностические логи. Наблюдение и внутреннее уведомление остаются сохранёнными.

Если worker погиб после начала отправки, Beat через две минуты переводит
зависшее sending в `unknown`. Автоматического повтора после начала отправки
нет: SMTP может принять письмо и потерять подтверждение. Выбран режим не более
одной попытки отправки на событие, с видимым результатом ошибки/неопределённости.
Это предотвращает дубликаты при повторе задания, но при аварии между фиксацией
sending и реальной отправкой письмо может не уйти. Внутренний канал сохранён.
Для SMTP нельзя одновременно гарантировать доставку без потерь и отсутствие
дубликатов одной локальной транзакцией. Ручной повтор без проверки не добавлен.

Детерминированный Message-ID включает ID события и UUID CheckRun; это полезно
для диагностики, но не заменяет защиту БД. Уже начатую отправку нельзя отозвать
после изменения пользовательских настроек.

Используются [стандартные почтовые backend Django 5.2](https://docs.djangoproject.com/en/5.2/topics/email/)
и [идемпотентные Celery-задачи с поздним подтверждением](https://docs.celeryq.dev/en/stable/userguide/tasks.html).

## Интерфейс и права

- GET `/notifications/` — все события, `?state=unread` — непрочитанные,
  пагинация по 20. JOIN старого/нового наблюдения и товара избегает N+1.
- POST `/notifications/<id>/read/` — одно событие; чужое возвращает 404.
- POST `/notifications/read-all/` — только свои непрочитанные.
- GET/POST `/notifications/settings/` — собственный адрес и email-флаг.
- Header COUNT ограничен владельцем и использует частичный индекс unread.
- GET ничего не отмечает прочитанным. Все изменения требуют входа и CSRF.
- Отключение email отменяет свои pending-письма. Повторное включение не
  возобновляет старые off/cancelled события. Имена и диагностика экранируются.

## Самостоятельная проверка и отладка

Локальный backend уже console: письма выводятся в лог worker и не уходят наружу,
SMTP-пароль не нужен. Чтобы увидеть письмо:

```powershell
docker compose -p pricewatch -f compose.yaml logs --tail=100 worker
```

Настройте правило товара, затем email аккаунта. Первое успешное получение цены
не отправляет письмо; нужна следующая отличающаяся цена в той же валюте и URL.
При корректном событии письмо попадает в очередь на следующем тике Beat.

Автоматическая проверка без реальных сайтов:

```powershell
docker compose -p pricewatch -f compose.yaml exec -T web python -m pytest tests/notifications --ds=config.settings.test --reuse-db -q
```

For the browser demo, first prepare `test_pricewatch` with the integration
readiness command in [the runbook](../runbook.md), with integration services
stopped. Standard pytest uses `test_pricewatch_pytest` and does not prepare the
demo database. Do not run integration tests during this demonstration:

```powershell
docker compose -p pricewatch -f compose.yaml --profile integration up -d --wait fixture integration-worker integration-beat
docker compose -p pricewatch -f compose.yaml exec -T -e PRICEWATCH_INTEGRATION=1 -e POSTGRES_DB=test_pricewatch -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m tests.integration.notifications_demo
docker compose -p pricewatch -f compose.yaml --profile integration up -d --wait integration-web
```

На `http://127.0.0.1:8011/accounts/login/`: `stage6_demo` / `PriceWatch-demo-6!`.
Генератор создаёт только тестовые записи: два товара, 10 наблюдений и 5 событий,
внешних запросов нет. Пять писем сохраняются стандартным filebased backend в
`artifacts/stage6-mail/`. Повторный запуск генератора откажется менять
существующего пользователя: для повторного просмотра используйте его записи.
Рабочая БД и реальные учётные записи не затрагиваются.

После проверки остановите только тестовый профиль:

```powershell
docker compose -p pricewatch -f compose.yaml --profile integration stop integration-web integration-beat integration-worker fixture
```

Standard pytest leaves the demo database unchanged. Integration tests use the
same `test_pricewatch` database as this demo and clear its records; stop the
integration profile and finish the demo before running them. Application
migrations still use `python manage.py migrate --noinput`.

Для диагностики смотрите `email_status`, `next_email_at`, `email_started_at`,
`email_sent_at`, `email_error_code` в Django shell и логи worker/beat.
Pending с наступившим next_email_at обычно означает неработающий Beat/Redis/worker;
sent в локальном окружении ищите в выводе console, а не в реальном ящике.
Failed и unknown не сбрасывайте в pending вручную без проверки: письмо могло уйти.
Поле статуса события и UUID в Message-ID позволяют сопоставить запись и файл.

Для внешней доставки в отдельной конфигурации развёртывания подключается
обычный `django.core.mail.backends.smtp.EmailBackend` с EMAIL_HOST/PORT/TLS,
учётными данными из переменных окружения и корректным PRICEWATCH_BASE_URL.
В текущем локальном запуске SMTP намеренно не включён. Пользовательские адреса
нельзя проверять отправкой реальных писем в стандартных тестах.

## Ограничения

- Основной канал надёжного хранения — PostgreSQL inbox. Email имеет описанный
  компромисс между потерей при аварии и риском повторной доставки SMTP.
- Внешний SMTP и подтверждение почтового адреса не настроены; публичной
  регистрации нет. Backend должен подключать администратор развёртывания.
- Доставка не мгновенная: тик раз в минуту, до 20 сообщений за цикл, общие
  worker для проверок и почты. При росте нагрузки сначала измерьте задержки;
  отдельная очередь/worker возможны позже, сейчас не нужны.
- Старые события не удаляются автоматически. Очень длинный inbox и глубокий
  OFFSET потребуют измерения, но каждая HTML-страница загружает только 20 строк.
- Время хранится в UTC, отображается в зоне Django Asia/Yekaterinburg.
- Ошибки парсинга/доменных лимитов не являются изменением цены и не уведомляют.
  Одновременные запросы к одному домену могут правомерно получить rate_limited;
  механизм загрузки и его ограничения не изменялись.
