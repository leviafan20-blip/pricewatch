# PriceWatch

PriceWatch is a Django application for tracking product prices over time. It checks
product pages in the background, keeps a price history, and notifies you when a
price changes according to your rules.

The included Docker setup is for local development and evaluation. It is not
configured for a public production deployment.

## Features

- Create, edit, and archive monitors, with separate data for each user.
- Extract product names, prices, currencies, and availability from JSON-LD
  `Product` / `Offer` data, supported metadata, or custom CSS selectors.
- Run checks manually or every 1, 3, 6, 12, or 24 hours. Pause and resume automatic
  checks and see when the next check is due.
- Keep successful observations and check diagnostics separately. Failed checks
  preserve the previous price; duplicate task deliveries do not duplicate observations.
- View interactive price charts for 7, 30, or 90 days, or all time. Filter and
  paginate observations, see minimum, maximum, average, and latest prices, and
  compare 2-5 monitors in the same currency.
- Receive in-app notifications for any price change, a price decrease, or a drop
  below a threshold. Notifications include price differences, read status, and an
  unread count. The first price and unchanged prices do not trigger notifications;
  threshold alerts fire only when the price crosses below the threshold.
- Enable email notifications per user. Celery sends messages through Django's mail
  backend; local messages are written to the worker console rather than sent externally.
- Export filtered observations from one monitor or your entire history to CSV
  or XLSX, including archived monitors. Exports include all matching rows, not
  just the current page or chart sample, up to 50,000 observations per download.
- Manage users through Django Admin. There is no public registration.
- Switch between English and Russian without signing out. English is the default;
  your choice is saved in a browser cookie. Product names and other user data
  keep their original language.

## Tech Stack

| Area | Tools |
| --- | --- |
| Application | Python 3.13, Django 5.2 LTS |
| Database | PostgreSQL 17 |
| Background work | Celery, Celery Beat, Redis 7.4 |
| Page extraction | HTTPX, BeautifulSoup |
| Spreadsheet export | XlsxWriter |
| Interface | Django Templates, Bootstrap 5, Chart.js 4 |
| Development and tests | Docker Compose, pytest, pytest-django, pytest-cov, GitHub Actions |

Bootstrap and Chart.js are included locally. No frontend build step or Node.js
installation is required. Prices and statistics use `Decimal`; conversion to
JavaScript numbers happens only when drawing charts.

## Getting Started

Install Docker with Docker Compose v2. On Windows, use Docker Desktop with the
Linux engine / WSL 2. Python runs inside the container.

Run the following commands from the project root. The examples use PowerShell;
Docker commands also work in other shells.

### 1. Configure the environment

Copy the example without overwriting an existing configuration:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

On macOS or Linux, use `test -f .env || cp .env.example .env` instead.

For a new setup, run this command twice to generate two different random values:

```powershell
docker run --rm python:3.13-slim python -c "import secrets; print(secrets.token_urlsafe(50))"
```

Put one value in `DJANGO_SECRET_KEY` and the other in `POSTGRES_PASSWORD` in `.env`.
Set them before the first database start. Keep `.env` private; it is excluded from
Git and the Docker build context. Compose passes its values to Django; Django does
not load `.env` itself.

The default `WEB_PORT` is `8000`. If you change it, update the ports in
`DJANGO_CSRF_TRUSTED_ORIGINS` as well.

### 2. Build and start the services

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps -a
```

Compose starts PostgreSQL and Redis, compiles the English/Russian catalogs and applies Django migrations through the
one-off `migrate` service, then starts the web application, Celery worker, and
Celery Beat. `migrate` exiting with code `0` is expected. Wait for the long-running
services to report healthy before opening the application.

The RU/EN switch is available on the login page and in the application header.
English is used until you explicitly choose a language; the selection is saved
for one year in that browser. See [localization](docs/components/localization.md)
for translation maintenance and the stable export/email language conventions.

PostgreSQL, Redis, and Beat state use named Docker volumes. Database and Redis
ports are not exposed to the host; the web service binds to `127.0.0.1` only.

### 3. Check the setup and create an administrator

```powershell
docker compose exec web python manage.py check
docker compose exec web python manage.py migrate --check
docker compose exec web python manage.py createsuperuser
```

The migration check verifies that startup applied all migrations. The last command
prompts for administrator credentials. Use Django Admin to create other users.

Open [PriceWatch](http://127.0.0.1:8000/) and
[Django Admin](http://127.0.0.1:8000/admin/), using your `WEB_PORT` if it differs.

### 4. Configure a monitor

Remote fetching is disabled by default. Before adding a real product URL, review
that site's access rules and add its exact hostname to `SCRAPING_ALLOWED_HOSTS`
in `.env`. Use a comma-separated list without schemes, paths, or wildcards;
subdomains must be listed separately. Run `docker compose up -d` after changing it.

Sign in, create a monitor, and run a manual check. If automatic extraction cannot
identify one product and price, set CSS selectors in the monitor form. You can
then enable a schedule and choose a notification rule. Email preferences are
separate user settings; notification rules are disabled by default.

To inspect the parser without contacting any website:

```powershell
docker compose exec web python manage.py parse_product tests/fixtures/scraping/json_ld_product.html
```

To inspect locally generated email messages:

```powershell
docker compose logs --tail=100 worker
```

Operational commands and troubleshooting are in the [runbook](docs/runbook.md).

## Running Tests

With the Docker services running:

```powershell
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python -m pytest --ds=config.settings.test --reuse-db --cov=apps --cov-report=term-missing
```

Select the test settings explicitly: the web container normally uses local
settings. Standard pytest uses `test_pricewatch_pytest`; `--reuse-db` keeps it
between runs. It does not clear demo data in `test_pricewatch`. The PostgreSQL
role needs permission to create the test database on its first run; the local
Compose role already has it. The suite covers authentication, user isolation,
extraction, URL safety, task handling, scheduling, analytics, and notifications.
Scraping tests use local fixtures and mocked network responses rather than real stores.

Tests requiring live Celery services are skipped in the standard run. The
[runbook](docs/runbook.md) describes the opt-in integration profile,
which uses PostgreSQL, Redis, a real worker and Beat, and a controlled HTTP server.
That profile and the browser demos use `test_pricewatch`, separate from both
standard pytest and the application database (`pricewatch` by default). Prepare
it separately as described in the runbook. Integration tests and browser demos
share data storage: run them sequentially, and stop the integration services
before preparing or testing their database. Integration tests clear its data.

GitHub Actions runs Django checks, migration checks, tests with PostgreSQL, and
static file collection. Integration tests require the separate Docker profile.

## Project Structure

```text
apps/
  accounts/           User model, login, and admin configuration
  core/               Dashboard and health checks
  monitoring/         Monitors, observations, tasks, scheduling, and analytics
    scraping/         URL validation, HTTP loading, and product extraction
  notifications/      Price-change rules, inbox, and email delivery
config/               Django settings, URLs, and Celery configuration
templates/            Django Templates
static/               Application assets and bundled Bootstrap / Chart.js
tests/                Unit, application, and opt-in integration tests
docs/                 Architecture, component notes, and operational guides
requirements/         Pinned Python dependencies
compose.yaml          Local application services and integration profile
Dockerfile            Python development image
.env.example          Environment template without secrets
```

Implementation details are in the [architecture notes](docs/architecture.md) and
component guides for [scraping](docs/components/scraping.md),
[scheduling](docs/components/scheduling.md), [analytics](docs/components/analytics.md),
[notifications](docs/components/notifications.md), and
[exports](docs/components/exports.md). Earlier component guides are in Russian;
the export guide is in English.

## Limitations

- This is a local development setup using Django's development server and debug
  mode. HTTPS, production serving, and login rate limiting are not configured.
- The interface is currently in Russian. Timestamps are stored in UTC and shown
  in the configured Django timezone, currently `Asia/Yekaterinburg`.
- Extraction depends on each site's HTML. The loader accepts only allowlisted
  HTTP(S) hosts on standard ports, validates DNS and IP addresses, checks redirects,
  and blocks private and internal addresses to reduce SSRF risk. Timeouts and
  response-size limits also apply. Adding a hostname does not bypass these checks.
- Requests respect `robots.txt` and a shared per-host rate limit. A robots denial
  or temporary failure to retrieve its rules can prevent a check. There is no
  CAPTCHA, authentication, or access-control bypass.
- JavaScript is not executed and Playwright is not integrated. Pages whose prices
  require browser rendering may not work. Ambiguous prices or multiple offers
  require explicit selectors; the parser does not guess a price.
- Currencies are not converted or combined. Supported currency formats are limited.
  Large chart series are sampled to at most 400 points per monitor; statistics
  still use all valid prices in the selected period.
- Email is console-only locally. External SMTP delivery is not configured. Once
  sending starts, failed or interrupted sends are not automatically retried, to
  avoid duplicate delivery; an email can therefore be lost. In-app notifications
  and saved observations remain available.
- Export generation runs in the web process, with bounded database batches and
  temporary files. Requests above 50,000 rows are rejected rather than truncated.
  XLSX prices that cannot be serialized exactly within Excel's 15 significant
  digits are stored as text; `Price exact`
  always preserves the decimal string. CSV uses UTF-8 with a BOM and semicolons;
  import price columns as text when exact precision is needed. See the
  [export guide](docs/components/exports.md) for details.
- Arbitrary page-content change tracking, CSV/Excel import, and a public API are
  not implemented.
