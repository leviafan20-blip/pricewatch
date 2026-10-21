"""Inspect product extraction from a local UTF-8 HTML file without network access."""

import json
import re
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils.translation import gettext as _

from apps.monitoring.scraping.errors import ScrapingError
from apps.monitoring.scraping.extractors import extract_product
from apps.monitoring.scraping.types import ExtractionConfig

MAX_HTML_BYTES = 2 * 1024 * 1024


def _read_local_html(filename):
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", filename) or filename.startswith(
        ("\\\\", "//")
    ):
        raise CommandError(_("file_required: Provide a local HTML file, not a URL or network path."))
    try:
        path = Path(filename).resolve(strict=True)
        if str(path).startswith(("\\\\", "//")):
            raise CommandError(_("file_required: Network paths are not supported."))
        if not path.is_file():
            raise CommandError(_("file_required: Provide a regular local HTML file."))
        with path.open("rb") as html_file:
            raw = html_file.read(MAX_HTML_BYTES + 1)
    except (OSError, ValueError, RuntimeError) as exc:
        raise CommandError(_("file_unreadable: Could not read the local HTML file.")) from exc
    if len(raw) > MAX_HTML_BYTES:
        raise CommandError(_("file_too_large: The HTML file exceeds 2 MiB."))
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CommandError(_("file_encoding: The HTML file must use UTF-8 encoding.")) from exc


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported result type: {type(value).__name__}")


class Command(BaseCommand):
    help = _("Extract product data from a local UTF-8 HTML file without network requests.")
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("path", help=_("Path to a local UTF-8 HTML file, no larger than 2 MiB."))
        for field in ("title", "price", "currency", "availability"):
            parser.add_argument(f"--{field}-selector", help=_("CSS selector for %(field)s.") % {"field": field})
            parser.add_argument(
                f"--{field}-attribute", help=_("HTML attribute for %(field)s; defaults to element text.") % {"field": field}
            )
        parser.add_argument("--currency", dest="currency_hint", help=_("Explicit currency, for example RUB."))

    def handle(self, *args, **options):
        html = _read_local_html(options["path"])
        config = ExtractionConfig(
            title_selector=options["title_selector"],
            price_selector=options["price_selector"],
            currency_selector=options["currency_selector"],
            availability_selector=options["availability_selector"],
            currency_hint=options["currency_hint"],
            title_attribute=options["title_attribute"],
            price_attribute=options["price_attribute"],
            currency_attribute=options["currency_attribute"],
            availability_attribute=options["availability_attribute"],
        )
        try:
            product = extract_product(html, config)
        except ScrapingError as exc:
            raise CommandError(f"{exc.code}: {exc.message}") from exc
        self.stdout.write(
            json.dumps(asdict(product), ensure_ascii=False, indent=2, default=_json_default)
        )
