# CSV and XLSX exports

Exports download existing observations. They do not fetch pages, create checks,
or change observations, schedules, or notifications. No new tables or migrations
are required.

## Files and responsibilities

| File | Responsibility |
| --- | --- |
| `apps/monitoring/history.py` | Shared filter validation and history selection |
| `apps/monitoring/forms.py` | Existing period, date, and currency fields |
| `apps/monitoring/exports.py` | Safe text, exact prices, bounded CSV/XLSX generation |
| `apps/monitoring/views.py`, `urls.py` | Authentication, ownership, download responses |
| `templates/monitoring/_analytics.html` | Monitor export buttons in the history filter form |
| `templates/monitoring/list.html`, `export.html` | User-wide export settings and downloads |
| `tests/monitoring/test_exports.py` | PostgreSQL selection and independent CSV/ZIP/XML checks |
| `requirements/base.txt` | Pinned XlsxWriter dependency; no pandas or frontend tooling |

## User workflow

On a monitor's detail page, choose the history period, dates, and currency, then
select **Download CSV** or **Download XLSX** (the interface labels are Russian).
The buttons submit all filter fields directly, including edits not yet applied
to the chart. Pagination fields have no effect on exports. Archived monitors
remain exportable.

The monitor list also links to **Export history**. This page exports all monitors
owned by the signed-in user, including archived ones. It supports the same period
and date fields, with an additional **All currencies** option. Each row retains
its own currency; no currency conversion or cross-currency totals are performed.
The active/archive tab on the monitor list does not restrict this user-wide export.

An empty result produces a valid file containing headers only. Successful
out-of-stock observations without a price have blank price and currency cells.
Failed checks are not observations and are excluded.

## Request and data flow

1. `GET /monitors/<id>/export/` resolves the monitor with `owner=request.user`.
   A foreign or missing ID returns 404, including for staff users. The user-wide
   route is `GET /monitors/history/export/`; its monitor query is owner-scoped.
2. `history_filters()` uses the existing `HistoryFilterForm` and
   `analytics.history_window()`. Valid periods are `7`, `30` (default), `90`, and
   `all`. Dates use `YYYY-MM-DD`, are inclusive in the active Django timezone,
   and intersect the selected rolling period. Use `all` for arbitrary older dates.
   A single monitor with multiple currencies still requires an explicit currency,
   matching its history table. Invalid filters return HTTP 400 with form errors,
   never an unfiltered download.
3. `history_rows()` applies the same selection as the detail table, including
   price-less observations alongside the selected currency. Pagination and the
   400-point chart sample are not involved. Rows are ordered by observation time,
   then observation ID, oldest first.
4. `format=csv` or `format=xlsx` selects the writer. Without `format`, the route
   displays the export form. Other formats and non-GET download requests are rejected.
5. A bounded preflight query rejects more than 50,000 matching observations with
   HTTP 400 and asks for narrower dates. The generation loop enforces the same
   bound in case a backdated observation appears after preflight.
6. The writer reads only required fields through `values_list().iterator()` in
   batches of 1,000. Related data is selected in the same SQL query, avoiding N+1
   queries and model-instance caches. A file is generated before the response
   starts; Django `FileResponse` streams that file and closes it afterward.

## Columns and precision

Both formats have English headers:

`Monitor ID`, `Monitor`, `Product`, `URL`, `Observed at`, `Timezone`, `Price`,
`Price exact`, `Currency`, `Availability`, `Source`, `Check ID`.

`Product` is the observed title. `Monitor` is its current user-assigned name.
`URL` is the check's URL snapshot, so edits to the monitor do not relabel old
observations with a new product URL. Availability and extraction source use their
stable English codes, such as `out_of_stock` and `json_ld`. Internal diagnostics,
HTML, email settings, and check errors are not included.

Prices are formatted directly from `Decimal`, without rounding or monetary
calculations through `float`. `Price exact` always contains the original decimal
string, including the database's decimal scale. Missing prices are blank, not zero.

CSV uses UTF-8 with a BOM, semicolon delimiters, quoted fields, and CRLF row
endings. Unicode, quotes, semicolons, and embedded newlines are supported. Both
price columns contain the exact decimal string. Excel's automatic CSV detection
depends on regional settings: if necessary, use **Data > From Text/CSV**, choose
UTF-8 and `;`, and import price columns as **Text** to avoid Excel's automatic
rounding and number conversion.

XLSX has a frozen header row, autofilter, fixed readable column widths, typed
local datetimes, and a separate timezone column. Its `Price` cells are numeric
for amounts with at most 15 significant digits that also round-trip unchanged
through XlsxWriter's numeric serialization. Longer amounts and values that would
gain a binary rounding digit are stored as text. `Price exact` is always a text
cell, so the original value survives regardless of Excel's numeric limitations.
Numeric display uses at least two decimal places and up to the model's ten
decimal places. Blank rows are not added to empty exports.

Dates are based on `Observation.observed_at`. CSV includes an ISO timestamp with
its UTC offset and microseconds. XLSX uses local Excel datetimes displayed to
seconds, plus the timezone name; Excel dates have lower precision than the
original timestamp. Django currently uses `Asia/Yekaterinburg`; storage remains UTC.

## Security and resource limits

- Every route requires authentication and scopes the database selection by owner.
  Client-supplied owner/user parameters cannot broaden this selection.
- Text starting with spreadsheet formula characters (`=`, `+`, `-`, `@`), even
  after whitespace or invisible prefixes, is prefixed with an apostrophe. Leading
  tabs/newlines are also protected. Invalid control characters are replaced with
  the Unicode replacement character. These deliberate text changes are visible
  in the export; normal Cyrillic and other Unicode text is preserved.
- XLSX text is written with explicit string cells. Automatic formula and hyperlink
  conversion is disabled. The file contains no macros, formulas, or external links.
- Responses are attachments with fixed ASCII filenames and private/no-store cache
  headers. Exported URLs may contain the user's own query parameters; downloaded
  files should be treated as private data.
- CSV uses a temporary binary file, not an in-memory complete response. XlsxWriter
  uses `constant_memory` and temporary worksheet files. Memory grows with the
  database batch and row width, rather than the number of exported rows.
- Generation is synchronous and occupies a web request until the file is ready.
  Temporary disk usage and database work still grow with result size. The 50,000-row
  cap bounds each request, not the total across simultaneous requests. For a public
  deployment, measure concurrency and disk capacity before increasing this cap.
- The export fixes an upper time boundary but is not a historical database snapshot
  of concurrent edits. A monitor rename during generation can affect its label;
  check URL snapshots and observations remain the source for measured data.

## Checks and troubleshooting

Build with the new dependency, then run checks on PostgreSQL:

```powershell
docker compose up --build -d
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python -m pytest tests/monitoring/test_exports.py tests/monitoring/test_analytics.py --ds=config.settings.test --reuse-db
docker compose exec web python -m pytest --ds=config.settings.test --reuse-db --cov=apps --cov-report=term-missing
```

Use controlled observations or the existing isolated integration fixture setup
described in the runbook. Do not add real stores just to verify a download.

For a reproducible browser demo, run the standard tests first to prepare
`test_pricewatch`, then create the guarded fixtures and start only the test web service:

```powershell
docker compose exec -e PRICEWATCH_INTEGRATION=1 -e POSTGRES_DB=test_pricewatch -e CELERY_BROKER_URL=redis://redis:6379/15 web python -m tests.integration.exports_demo
docker compose --profile integration up -d --no-deps integration-web
```

Open `http://127.0.0.1:8011/` (or `INTEGRATION_WEB_PORT`) and sign in as
`stage7_export_demo` with the test-only password `PriceWatch-demo-7!`. This utility
refuses the application database and an existing demo account; it does not fetch
URLs or queue tasks. It creates 28 observations in one monitor and one archived
USD observation, including Unicode, a formula-like title, a high-precision price,
and a missing price. Stop `integration-web` before running tests again. Never use
this test profile or password in a deployed environment.

To check manually:

1. Sign in, open a populated monitor, and choose a period/date range. Export both
   formats and compare their rows with the full filtered history, not just one page.
2. Check Cyrillic titles, prices with fractional digits, and a confirmed missing
   price. Confirm the URL matches the original check when a monitor was edited.
3. Open **Export history**, select all currencies, and verify each row has its own
   currency. Another user's observations must not appear.
4. Test an empty range and an invalid range. Empty ranges download headers; invalid
   ranges show errors. Files over the row cap are rejected without truncation.
5. Inspect `Price exact` for values exceeding Excel precision. When importing CSV,
   explicitly choose text columns if exact decimal values are required.

Common errors are an invalid period/date/currency or the row cap (HTTP 400), an
unauthenticated session (login redirect), and a foreign monitor (404). If XlsxWriter
cannot be imported, rebuild the image. Temporary-disk or database failures are
server errors: inspect `docker compose logs --tail=100 web`; existing observations
are not modified by export failures.

References: [XlsxWriter memory mode](https://xlsxwriter.readthedocs.io/working_with_memory.html)
and [Excel numeric precision](https://support.microsoft.com/en-us/excel/keeping-leading-zeros-and-large-numbers).
