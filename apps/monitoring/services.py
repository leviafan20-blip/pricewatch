"""The single URL -> validated product entry point; no persistence or background jobs."""

import logging

from django.conf import settings

from .scraping.errors import ScrapingError
from .scraping.extractors import extract_product
from .scraping.fetcher import FetchPolicy, PageLoader

logger = logging.getLogger(__name__)


def inspect_url(url, config=None):
    try:
        policy = FetchPolicy(allowed_hosts=tuple(settings.SCRAPING_ALLOWED_HOSTS))
        with PageLoader(policy) as loader:
            page = loader.fetch(url)
        return extract_product(page.html, config)
    except ScrapingError as error:
        # Full URLs, queries and HTML may contain secrets; log only a stable code.
        logger.warning("Product inspection failed: %s", error.code)
        raise
