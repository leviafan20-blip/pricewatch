# Interface localization

PriceWatch supports English (`en`) and Russian (`ru`) with Django gettext catalogs
under `locale/`. English is used until the browser has an explicit language cookie,
including when its `Accept-Language` header prefers Russian.

## Request handling

`apps.core.middleware.LocaleMiddleware` extends Django's `LocaleMiddleware`.
Its only policy change is the English default before an explicit selection;
Django still activates translations and handles response headers. It follows
`SessionMiddleware` and precedes `CommonMiddleware`.

The RU/EN form in the application header posts to Django's `set_language` view
with CSRF protection. Django validates the `next` destination before redirecting.
The cookie lasts one year and does not change the authentication session. URLs
have no language prefixes, and filters and pagination stay in the return URL.
The selection belongs to a browser, not an account; another browser needs its own
selection. Cookie blocking or clearing removes the preference.
Cookies are scoped to a host, not a port: the main app on `8010` and demo on
`8011` share the language cookie when opened under the same host/profile. Use
separate browser profiles for independent preferences and authentication sessions.

Templates use `translate` and `blocktranslate`, with lazy translations for form
labels, choices and admin labels. There are no per-language template copies.
Known technical diagnostics are translated when displayed; historical rows and
scraper error codes are unchanged. Older arbitrary diagnostic text without a
catalog entry remains in its original language, as do product titles and names.

## Prices, charts and downloads

Money stays `Decimal`. The `amount` filter uses Django's locale separators while
preserving every stored decimal digit. Dates use Django's locale formats and the
existing application timezone; changing language does not change that timezone.
HTML date filter inputs keep ISO dates (`YYYY-MM-DD`).

Chart.js reads the document language. `Intl` formats dates, and exact tooltip
prices are grouped as strings without converting them to binary numbers. Only
plotted coordinates use JavaScript numbers. The chart failure message is
translated by the server in an escaped HTML data attribute. The chart JSON and
health JSON retain their existing data formats.

CSV/XLSX downloads deliberately retain English column headings and machine codes
in both languages, including `Price exact`. CSV prices use a decimal point and
XLSX keeps numeric cells and exact text. This stable interchange format avoids
breaking existing consumers. The export page, buttons and errors are translated.

Background emails use the worker's default language (English). The browser cookie
is not an account preference and is not available to Celery. The email template
is translatable, but per-account email language preferences are not implemented.

## Updating translations

The Docker image installs GNU gettext and compiles catalogs as `pricewatch`,
after copying sources with that user's ownership. Compiled `.mo` files are
generated assets, excluded from Git and the Docker build context. A normal image
build generates them; CI compiles them before checks and tests.
The Compose `migrate` service also compiles the bind-mounted catalogs before
the normal web and worker services start. This prevents the source bind mount
from hiding the image's translations on a fresh checkout.

For a bind-mounted checkout, compile the catalogs on the mounted filesystem too:

```powershell
docker compose build web
docker compose run --rm --no-deps web python manage.py makemessages -l en -l ru --no-obsolete --ignore=.venv --ignore=artifacts --ignore=tests --ignore=static/vendor
# Edit the msgstr entries in locale/*/LC_MESSAGES/django.po.
docker compose run --rm --no-deps web python manage.py compilemessages -l en -l ru --ignore=.venv --ignore=artifacts
docker compose restart web
```

Restart `integration-web` too if its demo is running. Django caches loaded
catalogs in the process. Restart workers after changing email translations.
Do not translate currency recognition dictionaries, URLs, codes or JSON keys.
Source messages for persisted scraper diagnostics remain in Russian for backward
compatibility; the English catalog translates them at display time. New UI
messages use English source strings.

The three `localize_labels` migrations update field/choice descriptions in Django's
migration state only. Their forward SQL is a no-op: no prices, accounts, rules or
historical diagnostics are rewritten. Apply them using the normal migration step.

## Verification

```powershell
docker compose exec web python -m pytest --ds=config.settings.test --reuse-db
```

This uses `test_pricewatch_pytest`. `tests/test_i18n.py` covers both languages,
cookie persistence, authentication continuity, CSRF, safe redirects, translated
forms/statuses/notifications, exact money, unchanged chart JSON and stable exports.
Chart code can also be tested with Node 22 or newer, with no npm dependencies:

```powershell
node --test tests/js/price-history.test.cjs
```

Do not run the full integration pytest suite while preserving the browser demo:
it clears `test_pricewatch`. Use read-only rendering checks for existing demo
pages and Celery health checks instead. Neither language switching nor GET page
rendering needs new observations or demo reseeding.

With an existing demo and `integration-web` running, this opt-in verifier checks
the existing pages and bounded chart payloads in both languages inside a
PostgreSQL READ ONLY transaction. It refuses other databases and never seeds data:

```powershell
docker compose exec -T integration-web python -m tests.integration.verify_i18n
```

It verifies rendering and data preservation without logging in or publishing
Celery tasks. Browser behavior and authentication are covered separately by the
HTTP smoke checks and the standard pytest suite.
