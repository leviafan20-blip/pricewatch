# RU/EN localization verification — 21 October 2026

The interface defaults to English until an explicit RU/EN choice. Translation
uses Django gettext, shared templates, the standard `set_language` endpoint and
a small `LocaleMiddleware` subclass for the English-before-selection policy.
Implementation and maintenance instructions are in
[components/localization.md](components/localization.md).

## Results

| Check | Result |
| --- | --- |
| Complete standard pytest suite on PostgreSQL | 625 passed, 23 opt-in integration tests skipped |
| Application coverage | 95% |
| Node built-in chart tests | 5 passed |
| Django system check | No issues |
| Migration autodetector | No changes detected |
| Migration application | Three label-only migrations applied in the main and demo databases |
| GNU gettext catalogs | No untranslated or fuzzy entries; compilation successful |
| Docker image build | Successful; compilemessages runs as `pricewatch` |
| collectstatic without a source bind mount | Successful: 138 files |
| Clean image runtime user and app directory | `pricewatch`, `/app` mode `0755` |
| Main Compose startup | Successful, including startup catalog compilation and migrations |
| Main and integration services | All long-running services healthy |
| Anonymous real HTTP checks in both web services | English default despite Russian Accept-Language; CSRF-protected RU/EN switching; persistent cookie; return URL preserved |
| Read-only template checks on the main database | 12 successful page checks |
| Read-only template checks on existing demo data | 20 successful page checks, both languages |
| git diff --check | Successful |

The 23 integration tests were deliberately not run: their transactional teardown
clears the existing `test_pricewatch` demo. Ordinary pytest uses
`test_pricewatch_pytest`. The alternative page checks ran inside PostgreSQL
transactions explicitly marked READ ONLY; they did not seed data or publish tasks.
The demo still contains two monitors, eleven check runs, eleven observations and
five notifications. Hashes of all user, monitor, run, observation and notification
rows matched before and after the checks and after the service updates.

No real shop was contacted. Browser interaction is covered by real HTTP requests,
Django client tests and Node chart tests; a visual browser layout review was not
performed in this step. No browser tab/context close calls were used.

## Migrations and data

- `accounts/0003_localize_labels`: email preference label.
- `monitoring/0007_localize_labels`: check statuses, intervals, notification rules
  and availability labels.
- `notifications/0002_localize_labels`: email delivery status labels.

These are state-only label changes; no application rows or database field types
are changed. User-entered names, prices, currency codes, URLs and identifiers are
preserved. Existing scraping messages remain canonical and are translated only
when rendered.

## Deliberate limits

- Language preference is a browser cookie, not an account field. Background
  emails use English by default.
- CSV/XLSX keep their existing English headings, exact decimal strings and
  machine codes in both locales. Their pages and errors are translated.
- Arbitrary historical diagnostic text without a catalog entry keeps its
  original language. User content is never automatically translated.
- Changing language does not change the existing application timezone.
- CI configuration was updated to compile catalogs and run the chart tests;
  no remote CI run was triggered because no commit or push was made.
- Compose reports an existing orphan container, `pricewatch-stage3-migrations`.
  It was left untouched; no cleanup or volume/database removal was performed.

## Manual check

1. Open `/accounts/login/` with no language cookie: the interface is English.
2. Select RU and sign in. Switch back to EN from a monitor detail page; the
   account session and current filters should remain intact.
3. Check history, comparison, schedule controls, notifications and email settings
   in both languages. Product names should stay unchanged.
4. Reopen the browser using the same normal profile: the selected language remains.
5. Export the same filtered history in both locales: columns, exact prices,
   currencies and rows remain identical.

Use the existing demo at `http://127.0.0.1:8011/` for populated pages, with a
separate browser profile from the main app. Do not rerun integration tests or
demo seed scripts while preserving that dataset.

## Visual review follow-up

The dashboard was a static welcome page. It now checks for the signed-in user's
own monitors with one bounded `exists()` query. Existing monitors, including
archived ones, show "Your monitors and price history" with the monitor list as
the primary action. Accounts without their own monitors retain onboarding;
other users' monitors do not affect the page. Both variants support RU/EN.

The notification inbox now displays "Passed to the mail system (delivery not
confirmed)" for `sent`, with a Russian translation. This is a presentation-only
change: status codes, backend behavior and delivery timestamps are unchanged.
No additional migration is needed, and the text does not claim recipient delivery.

Follow-up verification: 64 targeted tests passed; the full standard suite passed
with **637 passed, 23 skipped, 95% coverage**. The read-only demo verifier also
checked both new texts in RU/EN across its 20 page checks. All application data
hashes remained unchanged. Django check, the migration autodetector, Docker image
build/catalog compilation and git diff --check succeeded; services are healthy.

## Changed files

The list below includes the complete source/documentation changes for this stage.
Generated `.mo` files and temporary verification artifacts are ignored by Git.

```text
.dockerignore
.github/workflows/ci.yml
.gitignore
Dockerfile
README.md
apps/accounts/apps.py
apps/accounts/migrations/0003_localize_labels.py
apps/accounts/models.py
apps/core/apps.py
apps/core/middleware.py
apps/core/views.py
apps/monitoring/apps.py
apps/monitoring/checks.py
apps/monitoring/forms.py
apps/monitoring/history.py
apps/monitoring/management/commands/parse_product.py
apps/monitoring/migrations/0007_localize_labels.py
apps/monitoring/models.py
apps/monitoring/scheduling.py
apps/monitoring/scraping/extractors.py
apps/monitoring/scraping/fetcher.py
apps/monitoring/scraping/normalization.py
apps/monitoring/scraping/rates.py
apps/monitoring/scraping/robots.py
apps/monitoring/scraping/safety.py
apps/monitoring/scraping/transport.py
apps/monitoring/templatetags/monitoring.py
apps/monitoring/views.py
apps/notifications/forms.py
apps/notifications/migrations/0002_localize_labels.py
apps/notifications/models.py
apps/notifications/services.py
apps/notifications/views.py
compose.yaml
config/settings/base.py
config/urls.py
docs/components/localization.md
docs/runbook.md
docs/verification-stage9.md
locale/en/LC_MESSAGES/django.po
locale/ru/LC_MESSAGES/django.po
static/css/app.css
static/js/price-history.js
templates/403.html
templates/404.html
templates/500.html
templates/base.html
templates/core/dashboard.html
templates/monitoring/_analytics.html
templates/monitoring/_history_pagination.html
templates/monitoring/_pagination.html
templates/monitoring/_status.html
templates/monitoring/archive.html
templates/monitoring/compare.html
templates/monitoring/detail.html
templates/monitoring/export.html
templates/monitoring/form.html
templates/monitoring/list.html
templates/notifications/email.txt
templates/notifications/list.html
templates/notifications/settings.html
templates/registration/login.html
tests/integration/verify_i18n.py
tests/js/price-history.test.cjs
tests/monitoring/test_analytics.py
tests/monitoring/test_exports.py
tests/monitoring/test_schedule_views.py
tests/monitoring/test_views.py
tests/test_dashboard_feedback.py
tests/test_i18n.py
```
