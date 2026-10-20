# PriceWatch

Веб-приложение для мониторинга цен и изменений выбранных блоков страниц.
Разработка идёт последовательно; реализованы **этапы 1–6 — основа, безопасная
загрузка и извлечение данных, ручной и автоматический мониторинг, аналитика и уведомления**.

Готово: Django 5.2 LTS, PostgreSQL 17, закрытый вход, управление пользователями
через Django Admin, адаптивные шаблоны Bootstrap и автоматические тесты.
Есть отдельный Python-компонент HTTPX/BeautifulSoup: проверка URL и DNS/IP,
robots.txt, общий лимит домена, CSS/JSON-LD, точные денежные значения Decimal.
В интерфейсе доступны создание, редактирование, архив и история мониторингов.
Проверки выполняет Celery через Redis; результаты и ошибки хранятся в PostgreSQL.
Celery Beat запускает диспетчер расписаний; интервалы 1/3/6/12/24 часа,
пауза и следующая проверка хранятся в PostgreSQL. Есть локальный Chart.js,
периоды, Decimal-статистика, история с фильтрами и сравнение 2–5 товаров одной
валюты. Уведомления работают при изменении/снижении цены и пересечении порога:
в кабинете и асинхронно через Django email. Локально письма остаются в консоли;
реальный SMTP и учётные данные не нужны. Уведомления по умолчанию выключены.
Импорт пока не добавлен. Поток данных и команды проверки:
[`docs/components/analytics.md`](docs/components/analytics.md).
Устройство почтовой доставки и её ограничения:
[`docs/components/notifications.md`](docs/components/notifications.md).

## Быстрый запуск на Windows

Нужны Docker Desktop с Linux engine / WSL 2 и Docker Compose v2.
Python 3.13 находится внутри контейнера; устанавливать Python для этого запуска
не обязательно. Команды ниже выполняются в PowerShell из корня проекта.

1. Создайте локальный файл настроек, если его ещё нет:

   ```powershell
   if (-not (Test-Path .env)) { Copy-Item .env.example .env }
   ```

2. Если секреты в `.env` ещё не заполнены, сгенерируйте **два разных** случайных
   значения и вставьте как `DJANGO_SECRET_KEY` и `POSTGRES_PASSWORD`:

   ```powershell
   docker run --rm python:3.13-slim python -c "import secrets; print(secrets.token_urlsafe(50))"
   ```

   Если Python уже установлен, можно выполнить аналогичную команду через
   `python -c "import secrets; print(secrets.token_urlsafe(50))"`.

   При первоначальной подготовке этого рабочего каталога локальный `.env`
   уже создан со случайными значениями: повторно менять их не нужно.
   Его содержимое не входит в Git и образ.

3. Проверьте Docker и поднимите приложение:

   ```powershell
   docker info
   docker compose config --quiet
   docker compose up --build -d
   docker compose ps -a
   ```

   PostgreSQL сначала проходит healthcheck, затем одноразовый сервис `migrate`
   применяет миграции. После его успеха и готовности Redis запускаются `web`, `worker` и `beat`.
   Состояние `migrate: Exited (0)` означает успех.

4. Создайте администратора, самостоятельно задав пароль:

   ```powershell
   docker compose exec web python manage.py createsuperuser
   ```

5. Откройте http://127.0.0.1:8000/ и войдите. Администрирование доступно
   по http://127.0.0.1:8000/admin/. Эти адреса предполагают `WEB_PORT=8000`.
   В подготовленном локальном `.env` выбран свободный порт **8010**, поскольку
   8000 занят другим процессом: используйте http://127.0.0.1:8010/.
   Обычных пользователей создаёт администратор.
   Публичных страниц регистрации и восстановления пароля нет.

Изменения Python/HTML/CSS видны через bind mount и Django autoreload.
После изменения кода задач/расписания перезапустите их: `docker compose restart worker beat`.
Приложение опубликовано только на `127.0.0.1`; порты PostgreSQL и Redis наружу не открыты.
Это окружение **локальной разработки**, а не конфигурация публичного сервера.
При выборе другого `WEB_PORT` укажите этот порт и в `DJANGO_CSRF_TRUSTED_ORIGINS`.

## Проверки

Основной вариант использует настоящий PostgreSQL в Compose:

```powershell
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python -m pytest --ds=config.settings.test --reuse-db --cov=apps --cov-report=term-missing
docker compose exec web python manage.py collectstatic --noinput
```

Параметр `--ds=config.settings.test` явно выбирает тестовые настройки:
контейнер обычно передаёт `DJANGO_SETTINGS_MODULE=config.settings.local`,
который имеет приоритет над значением из `pyproject.toml`.
Pytest создаёт отдельную тестовую БД; `--reuse-db` сохраняет её между прогонами.
Схема рабочей БД и данные пользователей тестами не очищаются.
Для тестов нужна возможность создать тестовую БД; локальный Compose её даёт.

Без Docker можно проверить независимую Python-логику на явном SQLite fallback:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements/dev.txt
.\.venv\Scripts\python.exe -m pytest tests/scraping/test_extraction.py tests/scraping/test_safety.py --ds=config.settings.test_sqlite
```

Python 3.12 также поддерживается для локальных проверок; контейнер и CI используют
3.13. Ручной мониторинг требует PostgreSQL advisory locks; полный набор на SQLite
не поддерживается. SQLite fallback не используется приложением.
MD5-хеширование включено только в тестовых настройках для ускорения; приложение
использует штатное безопасное хеширование Django.

GitHub Actions запускает проверки, миграции, тесты и сборку статики с PostgreSQL 17.
Для локальной проверки на отдельно запущенном PostgreSQL задайте `POSTGRES_DB`,
`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` в окружении
и запустите `.\.venv\Scripts\python.exe -m pytest`.

## Основные файлы

- `config/settings/base.py` — общая конфигурация и PostgreSQL.
- `config/settings/local.py` — локальный режим с явным секретом.
- `config/settings/test.py` — тесты на PostgreSQL.
- `apps/accounts/` — собственная модель пользователя, формы и admin.
- `apps/core/` — защищённый кабинет и проверки состояния.
- `templates/`, `static/` — интерфейс; Bootstrap хранится локально.
- `compose.yaml`, `Dockerfile` — окружение разработки.
- `tests/` — проверки доступа, сессий, CSRF, admin и состояния БД.
- `docs/architecture.md` — принятые архитектурные решения.
- `docs/components/foundation.md` — объяснение реализации первого этапа.
- `apps/monitoring/scraping/` — безопасная загрузка и извлечение данных.
- `docs/components/scraping.md` — устройство и диагностика второго этапа.
- `apps/monitoring/checks.py`, `models.py`, `tasks.py` — ручные проверки и история.
- `docs/components/monitoring.md` — устройство, восстановление и интеграционные тесты третьего этапа.
- `apps/monitoring/scheduling.py` — конкурентный диспетчер и восстановление плановых заданий.
- `docs/components/scheduling.md` — правила времени, пауза, простой и проверка перезапуска.
- `docs/runbook.md` — команды сопровождения и диагностики.

`.env` читает Docker Compose. Django не загружает его самостоятельно: при запуске
Python вне Docker переменные нужно передать через окружение процесса.
Секреты и пароли не передаются в репозиторий. Прямые и транзитивные версии Python-
зависимостей зафиксированы в `requirements/`; обновляйте их вместе и проверяйте тесты.

## Проверка парсера без внешних сайтов

```powershell
docker compose exec web python manage.py parse_product tests/fixtures/scraping/json_ld_product.html
docker compose exec web python manage.py parse_product tests/fixtures/scraping/manual_product.html --title-selector h1 --price-selector .price --currency RUB --availability-selector .availability
```

Команда читает только локальный UTF-8 HTML. Она не делает HTTP-запросов и не
требует подключения к БД. Результат — JSON, цена представлена точной строкой.
Примеры настройки селекторов, ошибки и ограничения описаны в
`docs/components/scraping.md`.

## Ручной и автоматический мониторинг

После входа откройте `/monitors/`, добавьте URL разрешённого сайта и нажмите
«Проверить сейчас» либо включите расписание в карточке. Ошибка сохраняется в CheckRun, успешные данные — в Observation.
Предыдущая цена не удаляется. Повторная отправка активного задания использует
тот же UUID. Архив сохраняет историю и отключает новые проверки.

Интервал выбирается из 1, 3, 6, 12 и 24 часов. При включении первая проверка
доступна в ближайший цикл диспетчера (обычно в течение минуты). Пауза отменяет
ожидающие автоматические проверки. После простоя пропущенные интервалы
сворачиваются в одну попытку. Даты хранятся в UTC и отображаются в часовом
поясе приложения; фоновая загрузка сохраняет ограничения robots и частоты.

Список `SCRAPING_ALLOWED_HOSTS` по умолчанию пуст. Реальные домены добавляются
администратором после проверки правил доступа; wildcard и внутренние адреса
не разрешаются. Для локальной проверки используйте изолированный профиль:

```powershell
docker compose --profile integration stop integration-beat integration-web integration-worker fixture
docker compose exec web python -m pytest --ds=config.settings.test --reuse-db --create-db
docker compose --profile integration up -d --wait fixture integration-worker integration-beat
docker compose exec -e PRICEWATCH_INTEGRATION=1 -e PRICEWATCH_RUN_INTEGRATION=1 -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m pytest tests/integration --ds=config.settings.integration --reuse-db
```

Это реальные PostgreSQL, Redis, Celery prefork worker, Beat и HTTP fixture в Docker,
с отдельной тестовой БД/очередью. Eager-режим не используется; магазин не нужен.
Стандартный прогон пропускает эти интеграционные тесты с явной причиной.
Не запускайте одновременно несколько прогонов с одной тестовой БД.
Интерфейс тестового профиля, контролируемые URL, устройство моделей и отладка
описаны в `docs/components/monitoring.md` и `docs/components/scheduling.md`.
Тестовая БД предназначена только для проверок и очищается pytest; рабочие
пользователи и история находятся в другой БД. Перед стандартным прогоном
остановите тестовый профиль, если он уже запущен.

Сетевая точка входа для прикладного кода: `apps.monitoring.services.inspect_url`.
Она требует PostgreSQL для общих лимитов и явного списка `SCRAPING_ALLOWED_HOSTS`
в `.env` (точные домены через запятую). Пока домены не выбраны, список пуст:
удалённая загрузка безопасно запрещена. После изменения переменной повторите
`docker compose up -d`. Тесты используют фикстуры и подменённые DNS/HTTP-ответы;
случайный внешний сетевой доступ в тестах scraping блокируется.

## Границы текущей версии

Redis, Celery, модели товаров и API мониторинга добавятся по следующим
заданиям. Пока кабинет показывает честное пустое состояние. Пользовательский
интерфейс не использует JavaScript; Django Admin использует штатные скрипты Django.
Настройка HTTPS, сервера приложений и защиты входа от перебора войдёт в подготовку
публичного развёртывания; этот Compose должен оставаться доступным только локально.
