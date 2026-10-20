"""Scraping tests cannot accidentally contact an external site."""

import socket

import pytest
from django.conf import settings

from apps.monitoring.scraping.robots import clear_cache


@pytest.fixture(autouse=True)
def no_real_scraping_network(monkeypatch):
    original_resolve = socket.getaddrinfo
    database = settings.DATABASES["default"]

    def blocked(*args, **kwargs):
        raise AssertionError("External network is forbidden in scraping tests; inject DNS/transport fixtures")

    def resolve_database_only(host, port, *args, **kwargs):
        # Psycopg 3 also uses Python DNS when opening a connection in a new thread.
        # Only the configured test PostgreSQL endpoint is exempt from the DNS guard.
        if host == database.get("HOST") and str(port) == str(database.get("PORT")):
            return original_resolve(host, port, *args, **kwargs)
        return blocked(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve_database_only)
    monkeypatch.setattr(socket, "create_connection", blocked)
    clear_cache()
    yield
    clear_cache()
